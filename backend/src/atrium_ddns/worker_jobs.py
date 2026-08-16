"""Scheduled work: DNS health checks and the retention prune.

Since #75 the health check has a **second caller**: the manual trigger
``POST /api/atrium_ddns/health-checks/run``, which is
:func:`run_health_checks` with ``due_only=False`` and the requesting
tenant's scope. It is not a second implementation — see that function's
docstring for why the difference is one predicate and not a parallel
sweep — and :func:`clear_health_check_results` next to it is the other
half of the legacy pair. Everything below about the scheduler still
describes the only path that *schedules* anything.

Both jobs run in the **worker** process, off ``init_worker(host)`` in
:mod:`atrium_ddns.bootstrap`. Neither has a request, a session cookie
or an authenticated user, which is exactly the context in which a
hand-written ``WHERE user_id = …`` looks reasonable and is wrong — so
every statement in this module goes through
:class:`~atrium_ddns.scope.DdnsScope`, built once per run by
:func:`_sweep_scope` and carrying its reason in the SQL as a literal
``true``.

Four things here are deliberately not the shape the legacy code had,
and each one is a defect the port would otherwise have inherited.

**The scheduler is an asyncio loop, not a background thread.** The old
service ran ``dns.resolver.resolve()`` (blocking) on an APScheduler
``BackgroundScheduler`` thread. Atrium hands hosts an
``AsyncIOScheduler`` running on the worker's *only* event loop, shared
with the heartbeat, the ``scheduled_jobs`` drain and the email outbox.
A blocking resolve of N names at a 5 s timeout stalls all of them, and
the first visible symptom is ``/health`` reporting a dead worker
because ``worker_heartbeat`` stopped being upserted. So this module
uses ``dns.asyncresolver`` and bounds its own fan-out with a semaphore.

**Addresses are compared canonically, not as strings.** The legacy
check compared ``answers[0].address == expected_ip`` verbatim. For
``AAAA`` that is a coin flip: ``2001:db8::1`` and
``2001:0db8:0000::0001`` are the same address and different strings.
Plan §3.3.1 measured this estate at **305 of 448 events IPv6** — the
common path, not the exotic one — so the string comparison would have
reported mismatches for the majority of traffic. Both sides go through
:func:`ipaddress.ip_address` before they are compared.

**The answer is a set, not ``answers[0]``.** A name with two A records
matched only when the resolver happened to return the tracked one
first. Membership is the correct test; the address stored back is the
matching one when there is a match, and the first answer otherwise.

**The retention prune is a scheduled job, bounded and keyed.** The old
code pruned inside ``log_event`` — an unbounded
``DELETE … WHERE created_at < cutoff`` on the write path, so every DNS
update paid for it and a large history took a range lock while a
router waited. Here it is a job, it deletes in committed batches by
primary key, and it stops after ``prune_max_batches`` rather than
running until the table is empty. #14 recorded the matching hazard
next door: a ``DELETE`` that could not match a row still took a
scan-wide lock and deadlocked two xdist workers. **A deletion that
cannot delete anything still costs a lock** — which is why the batches
are keyed on ``id`` and why ``tests/test_worker_jobs.py`` seeds rows on
both sides of the cutoff and asserts the kept count as well as the
removed one.

``n/a`` is not ``0``
--------------------
The states this module renders apart, because collapsing them is how
the status board this job feeds ends up lying:

*Per hostname* — :class:`DnsCheckStatus`. ``NEVER_CHECKED`` (no check
has run), ``ERROR`` (the check could not be made) and ``MISSING`` (the
check was made and the record genuinely is not there) are three
different facts. They are persisted as three distinguishable column
states on ``ddns_hostname`` and read back by
:func:`stored_dns_status`, so the distinction survives the database
rather than living only in this process.

*Per device* — :class:`Liveness`. A device that has never called
(``last_seen_at IS NULL``), one whose last call failed, and one with
zero updates in the window are three states;
:meth:`DeviceStatus.render_updates` renders them as a dash, the word
``error`` and the digit ``0`` respectively — plan §3.4's own words.
``updates_in_window`` is ``None`` for a device that never called,
because "0 updates in the last 7 days" about a device that has never
been heard from at all is true and misleading.
"""
from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Sequence

import dns.asyncresolver
import dns.exception
import dns.resolver
import sqlalchemy as sa
from app.db import get_session_factory
from app.logging import log
from app.services.app_config import get_namespace, register_namespace
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Device, DnsEvent, Hostname, RateLimitEvent
from .scope import DdnsScope

# --------------------------------------------------------------------- #
# Configuration — one namespace, registered at bootstrap import time
# --------------------------------------------------------------------- #

#: KV key in atrium's ``app_settings``.
CONFIG_NAMESPACE = "atrium_ddns"


class DdnsConfig(BaseModel):
    """Operator-tunable settings, read **at run time, never at import**.

    A value captured into a module constant at import is a value that
    needs a worker restart to change, and the restart is the thing
    nobody does. Every job re-reads this on every tick; ``get_namespace``
    returns ``DdnsConfig()`` when no row exists, so the defaults below
    are live with nothing seeded.

    Defaults are the legacy service's, measured rather than invented:
    ``health_check_interval_minutes`` 15 and ``health_check_enabled``
    true are ``health_check_config``'s column defaults;
    ``rate_limit_per_minute`` 30 is ``dyndns.py``'s global
    ``RateLimitConfig(requests_per_minute=30)``. The one deliberate
    change is retention: 24 hours becomes 30 days, per plan §3.4 — the
    old window was sized for a dashboard and makes "which of my devices
    stopped updating, and when" unanswerable.
    """

    # --- retention ---------------------------------------------------- #
    event_retention_days: int = Field(default=30, ge=1, le=3650)
    #: Deliberately *not* ``event_retention_days``. ``ddns_event`` and
    #: ``ddns_rate_limit_event`` answer different questions and
    #: models.py says so explicitly: hanging the limiter off the log's
    #: retention makes "how long do we keep logs" silently also mean
    #: "how far back does the limiter look".
    rate_limit_event_retention_hours: int = Field(default=24, ge=1, le=8760)
    #: Rows per committed batch. The lock is held for one batch, not for
    #: the run.
    prune_batch_size: int = Field(default=1000, ge=1, le=50_000)
    #: Hard ceiling on batches per tick. A first prune against a large
    #: history stops and says there is more to do rather than holding
    #: the worker for an unbounded time.
    prune_max_batches: int = Field(default=100, ge=1, le=10_000)

    # --- health checks ------------------------------------------------ #
    health_check_enabled: bool = True
    #: How stale a hostname's ``dns_checked_at`` may get before it is
    #: re-checked. **Not** an APScheduler interval: the job ticks on a
    #: fixed short period and selects what is due, so changing this
    #: takes effect on the next tick with no reschedule dance (the
    #: legacy ``reschedule_health_checker`` exists only because the
    #: interval was baked into the trigger at startup).
    health_check_interval_minutes: int = Field(default=15, ge=1, le=1440)
    #: Hostnames per tick. Bounds both the DNS fan-out and the write.
    health_check_batch_size: int = Field(default=200, ge=1, le=10_000)
    health_check_timeout_seconds: float = Field(default=5.0, ge=0.1, le=60.0)
    health_check_concurrency: int = Field(default=8, ge=1, le=64)
    #: Debounce on the **manual** trigger
    #: (``POST /api/atrium_ddns/health-checks/run``), per actor. Nothing
    #: on the scheduled path reads it: the scheduler's own cadence is
    #: ``HEALTH_CHECK_TICK_SECONDS`` and its due-ness filter is
    #: ``health_check_interval_minutes`` above.
    #:
    #: The button fans out to real nameservers — up to
    #: ``health_check_batch_size`` names × two record types at
    #: ``health_check_concurrency`` — so it is the one host surface where
    #: a held-down mouse button is an outbound query storm someone else
    #: pays for. ``0`` disables the debounce and is a deliberate,
    #: spellable choice rather than the default.
    health_check_manual_cooldown_seconds: int = Field(default=60, ge=0, le=86_400)

    # --- status board ------------------------------------------------- #
    #: The denominator behind "zero updates in the window". Printed
    #: beside every count this module reports, because a ratio whose
    #: divisor is not named is not a measurement.
    device_idle_window_days: int = Field(default=7, ge=1, le=365)

    # --- rate limiting (enforced by #16; the default lives here) ------- #
    #: Installation-wide default; ``ddns_device.rate_limit_per_minute``
    #: overrides it per device and ``NULL`` there means *inherit this*,
    #: which is not the same as ``0``. models.py assigns the namespace
    #: to this issue and the enforcement to #16.
    rate_limit_per_minute: int = Field(default=30, ge=0, le=10_000)


# Module top level, in the module that *reads* the namespace.
#
# The issue said to put this in ``bootstrap.py`` — top level there
# rather than inside ``init_app``, because atrium imports ``bootstrap``
# from the api process and the worker process but only the api calls
# ``init_app``, so a namespace registered there is absent from the
# worker's ``NAMESPACES`` and every ``get_namespace`` on a scheduler
# tick raises ``KeyError``.
#
# That is right as far as it goes, and it is one import away from the
# same defect: ``NAMESPACES`` is populated only if somebody imported
# ``bootstrap``, and nothing about ``atrium_ddns.worker_jobs`` requires
# that. The pytest session found it immediately — it imports
# ``worker_jobs`` and ``scope`` directly, never ``bootstrap``, and
# ``put_namespace`` raised ``KeyError: 'atrium_ddns'`` on the first
# fixture. The deployed processes would have been fine, which is what
# makes it the bad kind of bug.
#
# Registering here makes the failure structurally impossible: the
# module that reads the key is the module that registers it, so
# importing the reader is sufficient. ``bootstrap`` imports this module
# at *its* top level, so atrium's own import path still runs it in both
# processes. Both properties are asserted in
# ``tests/test_worker_jobs.py``.
register_namespace(CONFIG_NAMESPACE, DdnsConfig, public=False)


async def load_config(session: AsyncSession) -> DdnsConfig:
    """Read the namespace, or the defaults when nothing is seeded."""
    value = await get_namespace(session, CONFIG_NAMESPACE)
    # ``get_namespace`` validates against whatever model was registered.
    # Re-validating here means a mis-registration (someone registering a
    # different model under this key) fails loudly at the call site
    # rather than handing back an object with none of these fields.
    return DdnsConfig.model_validate(value.model_dump())


# --------------------------------------------------------------------- #
# Three states, twice
# --------------------------------------------------------------------- #


class DnsCheckStatus(str, Enum):
    """What the last health check found for one hostname.

    ``MISSING`` and ``ERROR`` are the pair that must not merge.
    ``MISSING`` is a *measurement*: a nameserver answered and there is
    no such record. ``ERROR`` is a *refusal to measure*: the query
    timed out, or no nameserver would answer. Rendering both as "no
    address" tells an operator their DNS is broken when their resolver
    is, and vice versa.
    """

    NEVER_CHECKED = "never_checked"
    OK = "ok"
    MISMATCH = "mismatch"
    MISSING = "missing"
    ERROR = "error"


class Liveness(str, Enum):
    """What a device's history says about it. Plan §3.4's three states.

    ``NEVER_SEEN`` is ``last_seen_at IS NULL`` — no call has ever
    arrived. ``LAST_CALL_FAILED`` is the most recent event on record
    carrying a non-success response code. ``IDLE`` is a device that has
    called, whose last call succeeded, and which has zero *successful
    updates* inside the window. ``ACTIVE`` is the rest.
    """

    NEVER_SEEN = "never_seen"
    LAST_CALL_FAILED = "last_call_failed"
    IDLE = "idle"
    ACTIVE = "active"


#: Wire responses that mean the call worked. Everything else
#: (``nohost``, ``badauth``, ``notfqdn``, ``abuse``, ``dnserr``, ``911``)
#: is a failure. Spelled as the success set rather than the failure set
#: on purpose: a response code added by a later issue defaults to
#: "failure", which is the direction that shows up on a status board
#: instead of the direction that hides.
SUCCESS_RESPONSE_CODES: frozenset[str] = frozenset({"good", "nochg"})


@dataclass(frozen=True)
class DeviceStatus:
    """One device's line on the status board.

    ``updates_in_window`` is ``int | None`` and the ``None`` is
    load-bearing — see :meth:`render_updates`.
    """

    device_id: int
    user_id: int
    device_name: str
    liveness: Liveness
    last_seen_at: datetime | None
    #: ``None`` means *no event on record*, which is not *success*.
    last_response_code: str | None
    #: ``None`` for a device that has never called: there is no window
    #: measurement to report. ``0`` is a measured zero.
    updates_in_window: int | None
    #: The denominator, carried beside the numerator so a caller cannot
    #: render the count without being able to render what it is out of.
    window_days: int

    def render_updates(self) -> str:
        """A dash, an error, or a number — plan §3.4, literally.

        Three states, three strings, and the only one that is ``"0"`` is
        the measured zero. A renderer that formats ``updates_in_window``
        directly gets ``None`` printed as ``None`` or, worse, coerced to
        ``0``; this method is the one place that decision is made.
        """
        if self.liveness is Liveness.NEVER_SEEN:
            return "—"
        if self.liveness is Liveness.LAST_CALL_FAILED:
            return "error"
        return str(self.updates_in_window)


@dataclass(frozen=True)
class RecordCheck:
    """The result of resolving one hostname for one record type."""

    hostname_id: int
    name: str
    rdtype: str
    expected: str
    answered: tuple[str, ...]
    status: DnsCheckStatus
    detail: str | None = None


@dataclass
class HealthCheckSummary:
    """What one health-check tick did, with every denominator named.

    ``hostnames_considered`` is the population;
    ``hostnames_never_written`` is the slice excluded because there is
    nothing to compare against (the ``n/a`` slice — it is *counted*,
    not silently dropped); ``hostnames_checked`` is what was actually
    resolved. The three record-status counters sum to
    ``records_checked``. :meth:`assert_consistent` is what stops this
    block becoming an accounting that reads like a pass.
    """

    enabled: bool = True
    hostnames_considered: int = 0
    hostnames_never_written: int = 0
    hostnames_checked: int = 0
    #: ``True`` when the staleness filter was skipped — i.e. this run
    #: re-checked everything in reach rather than what was due. Carried
    #: on the summary rather than left to the caller to remember,
    #: because "0 checked" means two different things depending on it:
    #: on the scheduled path it means *nothing was stale*, and on the
    #: manual path it means *there is nothing to check at all*.
    forced: bool = False
    records_checked: int = 0
    ok: int = 0
    mismatch: int = 0
    missing: int = 0
    error: int = 0
    transitions: int = 0
    devices: int = 0
    devices_never_seen: int = 0
    devices_last_call_failed: int = 0
    devices_idle: int = 0
    devices_active: int = 0
    window_days: int = 0
    batch_size: int = 0
    #: True when the batch ceiling was reached, i.e. there is more due
    #: work than this tick did. A summary that cannot say so reads
    #: identical whether it swept everything or 1%.
    truncated: bool = False

    def assert_consistent(self) -> None:
        if self.ok + self.mismatch + self.missing + self.error != self.records_checked:
            raise AssertionError(
                f"health check accounting does not balance: "
                f"ok={self.ok} mismatch={self.mismatch} missing={self.missing} "
                f"error={self.error} != records_checked={self.records_checked}"
            )
        by_liveness = (
            self.devices_never_seen
            + self.devices_last_call_failed
            + self.devices_idle
            + self.devices_active
        )
        if by_liveness != self.devices:
            raise AssertionError(
                f"device accounting does not balance: {by_liveness} != {self.devices}"
            )

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "hostnames_considered": self.hostnames_considered,
            "hostnames_never_written": self.hostnames_never_written,
            "hostnames_checked": self.hostnames_checked,
            "records_checked": self.records_checked,
            "ok": self.ok,
            "mismatch": self.mismatch,
            "missing": self.missing,
            "error": self.error,
            "transitions": self.transitions,
            "devices": self.devices,
            "devices_never_seen": self.devices_never_seen,
            "devices_last_call_failed": self.devices_last_call_failed,
            "devices_idle": self.devices_idle,
            "devices_active": self.devices_active,
            "window_days": self.window_days,
            "truncated": self.truncated,
            "forced": self.forced,
        }


@dataclass
class PruneSummary:
    """What one prune tick removed, and what it left.

    ``kept`` is not decoration. A prune reporting only ``deleted`` reads
    identically whether it removed the right rows or every row, and the
    cheapest way to delete a lot is to get the predicate wrong.
    """

    table: str
    cutoff: datetime
    deleted: int = 0
    kept: int = 0
    batches: int = 0
    truncated: bool = False


# --------------------------------------------------------------------- #
# The scope every statement in this module goes through
# --------------------------------------------------------------------- #

#: Why these jobs are unrestricted, in one sentence, at the site that
#: builds the scope. ``DdnsScope.cross_tenant`` refuses an empty one.
SWEEP_REASON = (
    "worker sweep (#17): the health check and the retention prune have no "
    "request, no principal and no tenant — they operate on every tenant's "
    "rows by design, and this reason is what distinguishes that from a "
    "query that lost its user id"
)


def _sweep_scope() -> DdnsScope:
    """The one scope these jobs use. Built per run, never per query."""
    return DdnsScope.cross_tenant(reason=SWEEP_REASON)


def _now() -> datetime:
    """Naive UTC, matching the ``DATETIME(6)`` columns and atrium's own
    ``datetime.now(UTC).replace(tzinfo=None)`` convention in
    ``app/worker.py``."""
    return datetime.now(UTC).replace(tzinfo=None)


# --------------------------------------------------------------------- #
# DNS resolution
# --------------------------------------------------------------------- #

#: ``(name, rdtype) -> addresses``. Raises to signal *could not
#: measure*; returns ``[]`` to signal *measured, nothing there*. The
#: two are not interchangeable and the whole ``MISSING``/``ERROR`` split
#: rests on the difference. Injected so the tests never open a socket.
Resolve = Callable[[str, str], Awaitable[list[str]]]


def make_resolver(timeout: float) -> Resolve:
    """The real resolver: ``dns.asyncresolver``, never the blocking one.

    ``dns.resolver.resolve`` on this scheduler blocks the worker's only
    event loop — see the module docstring. dnspython reaches the image
    as a hard dependency of atrium's ``email-validator``
    (``pip show dnspython`` → ``Required-by: email-validator``); that is
    a transitive path, so ``tests/test_worker_jobs.py`` asserts the
    import rather than trusting it to stay there.
    """

    async def _resolve(name: str, rdtype: str) -> list[str]:
        resolver = dns.asyncresolver.Resolver()
        resolver.timeout = timeout
        resolver.lifetime = timeout
        answers = await resolver.resolve(name, rdtype)
        return [str(rdata.address) for rdata in answers]

    return _resolve


def _canonical(address: str) -> str | None:
    """``ipaddress`` normal form, or ``None`` if it is not an address.

    The comparison that matters is between addresses, not between the
    strings two different resolvers chose to spell them with.
    """
    try:
        return str(ipaddress.ip_address(address.strip()))
    except ValueError:
        return None


def classify_record(
    expected: str, answered: Sequence[str] | None, error: str | None
) -> tuple[DnsCheckStatus, str | None, str | None]:
    """``(status, stored_address, detail)`` for one resolved record.

    ``answered is None`` together with a non-``None`` ``error`` is the
    *could not measure* case; ``answered == []`` with no error is the
    *measured and absent* case. Keeping them as two distinct inputs
    rather than one nullable list is what stops the two collapsing at
    the call site.
    """
    if error is not None:
        return DnsCheckStatus.ERROR, None, error[:255]
    if answered is None:
        # Defensive: no answer and no error is not a state this function
        # can interpret, and guessing either way falsifies the board.
        return (
            DnsCheckStatus.ERROR,
            None,
            "resolver returned neither an answer nor an error",
        )
    canonical = [c for c in (_canonical(a) for a in answered) if c is not None]
    if not canonical:
        return DnsCheckStatus.MISSING, None, None
    want = _canonical(expected)
    if want is not None and want in canonical:
        return DnsCheckStatus.OK, want, None
    stored = canonical[0]
    return (
        DnsCheckStatus.MISMATCH,
        stored,
        f"expected {expected}, answered {', '.join(canonical)}"[:255],
    )


def stored_dns_status(
    *,
    last_ip: str | None,
    dns_ip: str | None,
    dns_checked_at: datetime | None,
    dns_check_error: str | None,
) -> DnsCheckStatus:
    """Read the three states back **off the persisted columns**.

    This is the second instrument. :func:`classify_record` is what the
    job decided; this is what the database can still say afterwards, and
    they are written by different code paths. If the columns cannot
    distinguish ``ERROR`` from ``MISSING`` then the job's own
    classification is a fact that exists only inside one process, and
    the status board — which reads the columns — is the thing that
    lies.

    The mapping, and it is the one documented on ``Hostname``:

    ============================  =========================
    ``dns_checked_at IS NULL``    ``NEVER_CHECKED``  (n/a)
    ``dns_check_error NOT NULL``  ``ERROR``
    checked, no error, no ip      ``MISSING``  (a real zero)
    checked, ip == last_ip        ``OK``
    checked, ip != last_ip        ``MISMATCH``
    ============================  =========================
    """
    if dns_checked_at is None:
        return DnsCheckStatus.NEVER_CHECKED
    if dns_check_error is not None:
        return DnsCheckStatus.ERROR
    if dns_ip is None:
        return DnsCheckStatus.MISSING
    want = _canonical(last_ip) if last_ip else None
    got = _canonical(dns_ip)
    if want is not None and got is not None and want == got:
        return DnsCheckStatus.OK
    return DnsCheckStatus.MISMATCH


# --------------------------------------------------------------------- #
# The health check
# --------------------------------------------------------------------- #

#: Precedence when a hostname carries both an A and an AAAA result. A
#: failed measurement outranks a bad measurement outranks a good one —
#: so a hostname whose AAAA resolves fine and whose A times out is not
#: reported as healthy.
_STATUS_RANK: dict[DnsCheckStatus, int] = {
    DnsCheckStatus.OK: 0,
    DnsCheckStatus.MISSING: 1,
    DnsCheckStatus.MISMATCH: 2,
    DnsCheckStatus.ERROR: 3,
    DnsCheckStatus.NEVER_CHECKED: -1,
}


def worst(statuses: Iterable[DnsCheckStatus]) -> DnsCheckStatus:
    ordered = sorted(statuses, key=lambda s: _STATUS_RANK[s], reverse=True)
    return ordered[0] if ordered else DnsCheckStatus.NEVER_CHECKED


async def _resolve_one(
    resolve: Resolve, name: str, rdtype: str
) -> tuple[list[str] | None, str | None]:
    """``(addresses, error)`` — exactly one of the two is ``None``.

    ``NXDOMAIN`` and ``NoAnswer`` are answers, not failures: the zone
    was reached and there is no such record. Everything else is a
    failure to measure.
    """
    try:
        return await resolve(name, rdtype), None
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return [], None
    except dns.resolver.NoNameservers:
        return None, "no nameservers would answer"
    except dns.exception.Timeout:
        return None, "resolver timed out"
    except Exception as exc:  # noqa: BLE001 — the detail is the point
        return None, f"{type(exc).__name__}: {exc}"


async def run_health_checks(
    *,
    resolve: Resolve | None = None,
    scope: DdnsScope | None = None,
    session_factory: Any = None,
    config: DdnsConfig | None = None,
    due_only: bool = True,
) -> HealthCheckSummary:
    """Resolve due hostnames, compare against what we last wrote, persist.

    Returns the summary rather than only logging it, so a test can read
    the same object the log line is rendered from — one source, two
    readers, instead of a log line asserted by a regex.

    Every argument defaults to ``None`` and is resolved from the process
    on the scheduler path: :data:`JOBS` binds the zero-argument form and
    ``test_the_scheduled_jobs_take_no_arguments`` holds it there, so
    ``config=`` cannot become a way for production to skip the
    namespace. The tests pass it because the deployed worker container
    is sweeping the same database they are asserting against, and a job
    firing mid-assertion is a race, not a test.

    ``due_only`` and the manual trigger
    -----------------------------------
    ``POST /api/atrium_ddns/health-checks/run`` (#75) is **this
    function**, called with ``due_only=False`` and the caller's own
    :class:`~atrium_ddns.scope.DdnsScope`. It is not a second
    implementation, and that is the whole reason the parameter is a flag
    on one predicate rather than a parallel sweep in the router: the
    batch ceiling, the concurrency semaphore, the timeout, the
    canonical-address comparison, the five-state classification, the
    transition log line and the accounting assertion are all reached by
    exactly one path.

    ``due_only=False`` drops the ``dns_checked_at`` staleness clause and
    nothing else. Keeping it would make the button *"a probe that could
    not fail"* in the inverse direction — an operator who has just fixed
    a provider outage presses it, every name was checked four minutes
    ago and is therefore not due, the run reports ``0 checked`` and the
    board does not move. That is indistinguishable from a working button
    against a healthy estate.

    What it does **not** drop is the ``last_ip`` filter: a name we have
    never published has nothing to compare an answer against, and it is
    counted in ``hostnames_never_written`` rather than resolved. Nor
    does it drop ``health_check_batch_size``; a forced run over a large
    estate returns ``truncated=True`` and says how much it did.
    """
    factory = session_factory or get_session_factory()
    scope = scope or _sweep_scope()

    async with factory() as session:
        config = config or await load_config(session)
        summary = HealthCheckSummary(
            enabled=config.health_check_enabled,
            window_days=config.device_idle_window_days,
            batch_size=config.health_check_batch_size,
            forced=not due_only,
        )
        if not config.health_check_enabled:
            log.info("atrium_ddns.health_check.disabled")
            return summary

        now = _now()
        stale_before = now - timedelta(minutes=config.health_check_interval_minutes)

        # The population, and the slice of it we cannot check. Counted,
        # not dropped: "0 problems" over an unstated population is the
        # shape this whole file is written against.
        totals = (
            await session.execute(
                scope.select(
                    Hostname,
                    sa.func.count().label("total"),
                    sa.func.sum(
                        sa.case(
                            (
                                sa.and_(
                                    Hostname.last_ip_v4.is_(None),
                                    Hostname.last_ip_v6.is_(None),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("never_written"),
                ).select_from(Hostname)
            )
        ).one()
        summary.hostnames_considered = int(totals.total or 0)
        summary.hostnames_never_written = int(totals.never_written or 0)

        # The staleness clause, or a literal `true` when the caller
        # forced the run. Spelled as a value rather than as two branches
        # building two statements: one statement, one place the ordering
        # and the batch ceiling are applied.
        staleness = (
            sa.or_(
                Hostname.dns_checked_at.is_(None),
                Hostname.dns_checked_at < stale_before,
            )
            if due_only
            else sa.true()
        )

        due_stmt = (
            scope.select(Hostname)
            .where(
                sa.or_(
                    Hostname.last_ip_v4.is_not(None),
                    Hostname.last_ip_v6.is_not(None),
                ),
                staleness,
            )
            # NULLs first: a hostname that has never been checked is more
            # interesting than one checked an hour ago, and without an
            # explicit order a batch-limited sweep can starve them
            # forever behind rows it keeps re-checking.
            .order_by(Hostname.dns_checked_at.is_(None).desc(), Hostname.dns_checked_at)
            .limit(config.health_check_batch_size + 1)
        )
        due = list((await session.execute(due_stmt)).scalars().all())
        summary.truncated = len(due) > config.health_check_batch_size
        due = due[: config.health_check_batch_size]

    if not due:
        summary.assert_consistent()
        log.info("atrium_ddns.health_check.nothing_due", **summary.as_log_fields())
        return summary

    resolve = resolve or make_resolver(config.health_check_timeout_seconds)
    gate = asyncio.Semaphore(config.health_check_concurrency)

    async def _check(hostname: Hostname) -> tuple[Hostname, list[RecordCheck]]:
        results: list[RecordCheck] = []
        for expected, rdtype in (
            (hostname.last_ip_v4, "A"),
            (hostname.last_ip_v6, "AAAA"),
        ):
            if not expected:
                continue
            async with gate:
                answered, error = await _resolve_one(resolve, hostname.name, rdtype)
            status, stored, detail = classify_record(expected, answered, error)
            results.append(
                RecordCheck(
                    hostname_id=hostname.id,
                    name=hostname.name,
                    rdtype=rdtype,
                    expected=expected,
                    answered=tuple(answered or ()),
                    status=status,
                    detail=detail,
                )
            )
            # `stored` rides along on the RecordCheck's status; the
            # address actually written back is recomputed below so the
            # write and the classification cannot drift apart.
            _ = stored
        return hostname, results

    checked = await asyncio.gather(*(_check(h) for h in due))

    now = _now()
    async with factory() as session:
        for hostname, results in checked:
            if not results:
                continue
            summary.hostnames_checked += 1
            # Re-derived per address family and aggregated the same way
            # the new result is. Reading `last_ip_v4 or last_ip_v6`
            # against `dns_ip_v4 or dns_ip_v6` compares a v4 expectation
            # to a v6 answer on any hostname carrying both, and reports a
            # transition on every tick.
            previous = worst(
                stored_dns_status(
                    last_ip=last,
                    dns_ip=seen,
                    dns_checked_at=hostname.dns_checked_at,
                    dns_check_error=hostname.dns_check_error,
                )
                for last, seen in (
                    (hostname.last_ip_v4, hostname.dns_ip_v4),
                    (hostname.last_ip_v6, hostname.dns_ip_v6),
                )
                if last
            )

            values: dict[str, Any] = {
                "dns_checked_at": now,
                "dns_ip_v4": None,
                "dns_ip_v6": None,
                "dns_check_error": None,
            }
            errors: list[str] = []
            for record in results:
                summary.records_checked += 1
                # Explicit, not `setattr(summary, status.value, …)`: a
                # status added to the enum and not to the summary would
                # silently grow a new attribute on the dataclass and
                # vanish from `assert_consistent`'s arithmetic.
                if record.status is DnsCheckStatus.OK:
                    summary.ok += 1
                elif record.status is DnsCheckStatus.MISMATCH:
                    summary.mismatch += 1
                elif record.status is DnsCheckStatus.MISSING:
                    summary.missing += 1
                elif record.status is DnsCheckStatus.ERROR:
                    summary.error += 1
                else:
                    raise AssertionError(
                        f"classify_record returned {record.status!r}, which the "
                        f"summary cannot count — add it to both or to neither"
                    )
                column = "dns_ip_v4" if record.rdtype == "A" else "dns_ip_v6"
                if record.status is DnsCheckStatus.ERROR:
                    # An ERROR leaves the address column NULL *and* fills
                    # dns_check_error. Filling only the former is the
                    # collapse: it reads back as MISSING, i.e. as a
                    # measurement that never happened.
                    errors.append(f"{record.rdtype}: {record.detail}")
                elif record.status is DnsCheckStatus.MISSING:
                    values[column] = None
                else:
                    canonical = [
                        c for c in (_canonical(a) for a in record.answered) if c
                    ]
                    want = _canonical(record.expected)
                    values[column] = (
                        want
                        if want is not None and want in canonical
                        else (canonical[0] if canonical else None)
                    )
                    if record.status is DnsCheckStatus.MISMATCH and record.detail:
                        errors.append(f"{record.rdtype}: {record.detail}")

            aggregate = worst(r.status for r in results)
            if aggregate is DnsCheckStatus.ERROR:
                values["dns_check_error"] = "; ".join(errors)[:255]

            await session.execute(
                scope.apply(
                    sa.update(Hostname).where(Hostname.id == hostname.id).values(**values),
                    Hostname,
                )
            )

            if aggregate is not previous:
                summary.transitions += 1
                log.info(
                    "atrium_ddns.health_check.transition",
                    hostname_id=hostname.id,
                    was=previous.value,
                    now=aggregate.value,
                    detail="; ".join(errors)[:255] or None,
                )
        await session.commit()

    async with factory() as session:
        statuses = await device_statuses(
            session, scope=scope, window_days=config.device_idle_window_days
        )
    summary.devices = len(statuses)
    for status in statuses:
        if status.liveness is Liveness.NEVER_SEEN:
            summary.devices_never_seen += 1
        elif status.liveness is Liveness.LAST_CALL_FAILED:
            summary.devices_last_call_failed += 1
        elif status.liveness is Liveness.IDLE:
            summary.devices_idle += 1
        else:
            summary.devices_active += 1

    summary.assert_consistent()
    log.info("atrium_ddns.health_check.done", **summary.as_log_fields())
    return summary


@dataclass(frozen=True)
class ClearSummary:
    """What one ``clear`` did, with its denominator.

    ``cleared`` alone reads identically whether it reset the right rows
    or every row in the installation, and a clear is a destructive
    operation whose whole failure mode is reaching further than the
    caller can see. ``in_scope`` is the population the caller's scope
    could have touched, read back after the write.
    """

    cleared: int
    in_scope: int


async def clear_health_check_results(
    *,
    scope: DdnsScope,
    session_factory: Any = None,
) -> ClearSummary:
    """Reset every health-check column in ``scope`` to ``NEVER_CHECKED``.

    The legacy ``POST /admin/health-checks/clear``. It writes the four
    columns :func:`stored_dns_status` reads and nothing else — no
    hostname is deleted, no ``last_ip`` is touched, and the ``ddns_event``
    history is untouched. **Clearing a result is not clearing a log**,
    and the two were separate routes in the legacy service for that
    reason; ``POST /admin/events/clear`` is the one the operator struck
    (ui-parity §3.4) and this is not it.

    The state it writes is ``NEVER_CHECKED``, which is the honest one:
    ``dns_checked_at IS NULL``. Writing ``MISSING`` instead — clearing
    the addresses and leaving the timestamp — would assert that a check
    was made and found nothing, which is exactly the ``n/a``-rendered-
    as-a-measurement defect the five states exist to prevent.

    ``scope`` is required and has no default. Every other entry point in
    this module falls back to :func:`_sweep_scope`; a destructive write
    that defaults to *every tenant* is the wrong direction to be
    convenient in.

    Why the ``UPDATE`` carries a "has a result" predicate
    ----------------------------------------------------
    It could be omitted — writing ``NULL`` over ``NULL`` changes
    nothing — and the first version did omit it. Then ``cleared`` was
    **1 on a second clear of the same row**, because this driver reports
    ``rowcount`` as rows *matched* rather than rows *changed*
    (``CLIENT_FOUND_ROWS``), so the number said "one result discarded"
    about a row that had none. Found by asserting the second call's
    count rather than the first's; a test that only pressed the button
    once agrees with either implementation.

    With the predicate, ``cleared`` is the count of hostnames that
    actually carried a health-check result, which is what the word
    means and what an operator is owed beside ``in_scope``. It also does
    strictly less work.
    """
    factory = session_factory or get_session_factory()
    async with factory() as session:
        result = await session.execute(
            scope.apply(
                sa.update(Hostname)
                .where(
                    sa.or_(
                        Hostname.dns_checked_at.is_not(None),
                        Hostname.dns_ip_v4.is_not(None),
                        Hostname.dns_ip_v6.is_not(None),
                        Hostname.dns_check_error.is_not(None),
                    )
                )
                .values(
                    dns_checked_at=None,
                    dns_ip_v4=None,
                    dns_ip_v6=None,
                    dns_check_error=None,
                ),
                Hostname,
            )
        )
        cleared = int(result.rowcount or 0)
        await session.commit()

    async with factory() as session:
        in_scope = int(
            (
                await session.execute(
                    scope.select(Hostname, sa.func.count()).select_from(Hostname)
                )
            ).scalar_one()
        )

    log.info(
        "atrium_ddns.health_check.cleared", cleared=cleared, in_scope=in_scope
    )
    return ClearSummary(cleared=cleared, in_scope=in_scope)


# --------------------------------------------------------------------- #
# Device liveness — the three states, per device
# --------------------------------------------------------------------- #


async def device_statuses(
    session: AsyncSession,
    *,
    scope: DdnsScope | None = None,
    window_days: int = 7,
) -> list[DeviceStatus]:
    """One :class:`DeviceStatus` per device visible to ``scope``.

    Two populations, named together because they are different: the
    **last response code** is read from the device's most recent event
    *of all time* (a device whose last call failed three weeks ago has
    still last failed), while **updates_in_window** counts successful
    ``update`` events inside the last ``window_days``. Computing both
    over one population is how "quiet" and "broken" become the same
    colour on a dashboard.
    """
    scope = scope or _sweep_scope()
    since = _now() - timedelta(days=window_days)

    devices = list((await session.execute(scope.select(Device))).scalars().all())
    if not devices:
        return []

    # Most recent event per device, of all time.
    newest = (
        scope.select(DnsEvent, DnsEvent.device_id, sa.func.max(DnsEvent.id).label("id"))
        .where(DnsEvent.device_id.is_not(None))
        .group_by(DnsEvent.device_id)
        .subquery()
    )
    last_rows = (
        await session.execute(
            scope.select(DnsEvent, DnsEvent.device_id, DnsEvent.response_code).join(
                newest, DnsEvent.id == newest.c.id
            )
        )
    ).all()
    last_code: dict[int, str | None] = {
        int(row.device_id): row.response_code for row in last_rows
    }

    # Successful updates inside the window.
    window_rows = (
        await session.execute(
            scope.select(DnsEvent, DnsEvent.device_id, sa.func.count().label("n"))
            .where(
                DnsEvent.device_id.is_not(None),
                DnsEvent.created_at >= since,
                DnsEvent.event_type == "update",
                DnsEvent.response_code.in_(sorted(SUCCESS_RESPONSE_CODES)),
            )
            .group_by(DnsEvent.device_id)
        )
    ).all()
    window_count: dict[int, int] = {
        int(row.device_id): int(row.n) for row in window_rows
    }

    out: list[DeviceStatus] = []
    for device in devices:
        code = last_code.get(device.id)
        count = window_count.get(device.id, 0)
        if device.last_seen_at is None:
            liveness = Liveness.NEVER_SEEN
            updates: int | None = None
        elif code is not None and code not in SUCCESS_RESPONSE_CODES:
            liveness = Liveness.LAST_CALL_FAILED
            updates = count
        elif count == 0:
            liveness = Liveness.IDLE
            updates = 0
        else:
            liveness = Liveness.ACTIVE
            updates = count
        out.append(
            DeviceStatus(
                device_id=device.id,
                user_id=device.user_id,
                device_name=device.name,
                liveness=liveness,
                last_seen_at=device.last_seen_at,
                last_response_code=code,
                updates_in_window=updates,
                window_days=window_days,
            )
        )
    return out


# --------------------------------------------------------------------- #
# The retention prune
# --------------------------------------------------------------------- #


async def _prune_table(
    factory: Any,
    scope: DdnsScope,
    model: type[Any],
    cutoff: datetime,
    *,
    batch_size: int,
    max_batches: int,
) -> PruneSummary:
    """Delete rows older than ``cutoff``, in committed batches, by id.

    Two statements per batch, deliberately:

    1. ``SELECT id … WHERE created_at < :cutoff ORDER BY created_at
       LIMIT :n`` — a bounded range scan on the ``created_at`` index.
    2. ``DELETE … WHERE id IN (…)`` — record locks on exactly those
       primary keys, held for the length of one batch.

    A single ``DELETE … WHERE created_at < :cutoff`` is one statement
    and looks tidier; it also takes locks proportional to the *history*
    rather than to the batch, which is precisely what the legacy
    write-path prune did to every DNS update. #14 measured the cost of
    the failure mode next door — a delete that matched nothing still
    took a scan-wide lock and deadlocked two workers.

    Both statements go through the scope, so an unrestricted sweep is a
    literal ``true`` in the SQL and a tenant-restricted one would be a
    real predicate. Neither is an absent ``WHERE``.
    """
    summary = PruneSummary(table=model.__tablename__, cutoff=cutoff)
    for _ in range(max_batches):
        async with factory() as session:
            ids = list(
                (
                    await session.execute(
                        scope.select(model, model.id)
                        .where(model.created_at < cutoff)
                        .order_by(model.created_at)
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                break
            result = await session.execute(
                scope.apply(sa.delete(model).where(model.id.in_(ids)), model)
            )
            await session.commit()
        summary.deleted += int(result.rowcount or 0)
        summary.batches += 1
        if len(ids) < batch_size:
            break
    else:
        # Ran out of batches with work still to do. Say so rather than
        # returning a summary indistinguishable from a clean sweep.
        summary.truncated = True

    async with factory() as session:
        summary.kept = int(
            (
                await session.execute(
                    scope.select(model, sa.func.count()).select_from(model)
                )
            ).scalar_one()
        )
        remaining = int(
            (
                await session.execute(
                    scope.select(model, sa.func.count())
                    .select_from(model)
                    .where(model.created_at < cutoff)
                )
            ).scalar_one()
        )
    if remaining and not summary.truncated:
        summary.truncated = True
    return summary


async def run_retention_prune(
    *,
    scope: DdnsScope | None = None,
    session_factory: Any = None,
    config: DdnsConfig | None = None,
) -> list[PruneSummary]:
    """Prune ``ddns_event`` and ``ddns_rate_limit_event``.

    Two tables, two independently configured windows — see
    :class:`DdnsConfig`. Idempotent: a second run against an unchanged
    clock deletes nothing, because the first one already removed
    everything on the far side of the same cutoff.

    ``config=`` is the test seam described on :func:`run_health_checks`.
    """
    factory = session_factory or get_session_factory()
    scope = scope or _sweep_scope()

    async with factory() as session:
        config = config or await load_config(session)

    now = _now()
    plan = (
        (DnsEvent, now - timedelta(days=config.event_retention_days)),
        (
            RateLimitEvent,
            now - timedelta(hours=config.rate_limit_event_retention_hours),
        ),
    )

    summaries: list[PruneSummary] = []
    for model, cutoff in plan:
        summary = await _prune_table(
            factory,
            scope,
            model,
            cutoff,
            batch_size=config.prune_batch_size,
            max_batches=config.prune_max_batches,
        )
        summaries.append(summary)
        log.info(
            "atrium_ddns.retention_prune.table",
            table=summary.table,
            cutoff=summary.cutoff.isoformat(),
            deleted=summary.deleted,
            kept=summary.kept,
            batches=summary.batches,
            truncated=summary.truncated,
        )
    return summaries


# --------------------------------------------------------------------- #
# Registration — and the guard that keeps a thrower off the loop
# --------------------------------------------------------------------- #

HEALTH_CHECK_JOB_ID = "atrium_ddns-health-check"
RETENTION_PRUNE_JOB_ID = "atrium_ddns-retention-prune"

#: The health check *ticks* every minute and decides for itself what is
#: due, using ``health_check_interval_minutes`` read from the namespace
#: at run time. The trigger period is therefore a constant and the
#: operator-facing knob is not, which is the way round the acceptance
#: criterion asks for: a value captured into a trigger at registration
#: cannot be changed without restarting the worker.
HEALTH_CHECK_TICK_SECONDS = 60
RETENTION_PRUNE_TICK_SECONDS = 3600

#: Per-job counters, read by ``tests/test_worker_jobs.py``. A run count
#: beside a failure count is the denominator: ``0 failures`` over ``0``
#: runs is silence, and it is what a job that never fired also prints.
JOB_RUNS: dict[str, int] = {}
JOB_FAILURES: dict[str, int] = {}


def reset_counters() -> None:
    JOB_RUNS.clear()
    JOB_FAILURES.clear()


def guarded(job_id: str, body: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[None]]:
    """Wrap a job body so a raise is logged and counted, never raised on.

    APScheduler's executor already catches a job exception, so the
    scheduler survives either way — measured, not assumed, in
    ``test_a_raising_job_does_not_stop_the_scheduler``. What the guard
    adds is that the failure is *ours*: a structured
    ``atrium_ddns.job.failed`` line naming the job, with the traceback,
    and a counter a test can read — instead of APScheduler's generic
    "Job raised an exception" on a logger nothing in this estate reads.

    ``CancelledError`` is re-raised. Swallowing it makes worker
    shutdown hang, and a guard that turns a cancellation into a logged
    failure is a guard that broke the thing it was protecting.
    """

    async def _run() -> None:
        JOB_RUNS[job_id] = JOB_RUNS.get(job_id, 0) + 1
        try:
            await body()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — that is the job
            JOB_FAILURES[job_id] = JOB_FAILURES.get(job_id, 0) + 1
            log.error(
                "atrium_ddns.job.failed",
                job_id=job_id,
                error=f"{type(exc).__name__}: {exc}",
                runs=JOB_RUNS[job_id],
                failures=JOB_FAILURES[job_id],
                exc_info=True,
            )

    _run.__name__ = f"guarded_{job_id.replace('-', '_')}"
    return _run


#: ``job id -> (body, trigger seconds)``. The scheduler registration
#: below is a loop over this table rather than two hand-written
#: ``add_job`` calls, so a job removed from the table takes its
#: registration — and the test that asserts it is scheduled — with it.
JOBS: dict[str, tuple[Callable[[], Awaitable[Any]], int]] = {
    HEALTH_CHECK_JOB_ID: (run_health_checks, HEALTH_CHECK_TICK_SECONDS),
    RETENTION_PRUNE_JOB_ID: (run_retention_prune, RETENTION_PRUNE_TICK_SECONDS),
}


def register_jobs(scheduler: Any) -> list[str]:
    """Add every job in :data:`JOBS` to ``scheduler``. Returns the ids.

    Does **no** IO. ``init_worker`` is called by ``app/worker.py``
    outside any try/except, so anything that can raise here — a config
    read, a database round-trip — takes the whole worker down at
    startup, before the scheduler is started and therefore before
    anything can retry.
    """
    for job_id, (body, seconds) in JOBS.items():
        scheduler.add_job(
            guarded(job_id, body),
            "interval",
            seconds=seconds,
            id=job_id,
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
    return list(JOBS)


__all__ = [
    "CONFIG_NAMESPACE",
    "HEALTH_CHECK_JOB_ID",
    "HEALTH_CHECK_TICK_SECONDS",
    "JOBS",
    "JOB_FAILURES",
    "JOB_RUNS",
    "RETENTION_PRUNE_JOB_ID",
    "RETENTION_PRUNE_TICK_SECONDS",
    "SUCCESS_RESPONSE_CODES",
    "SWEEP_REASON",
    "ClearSummary",
    "DdnsConfig",
    "DeviceStatus",
    "DnsCheckStatus",
    "HealthCheckSummary",
    "Liveness",
    "PruneSummary",
    "RecordCheck",
    "Resolve",
    "classify_record",
    "clear_health_check_results",
    "device_statuses",
    "guarded",
    "load_config",
    "make_resolver",
    "register_jobs",
    "reset_counters",
    "run_health_checks",
    "run_retention_prune",
    "stored_dns_status",
    "worst",
]
