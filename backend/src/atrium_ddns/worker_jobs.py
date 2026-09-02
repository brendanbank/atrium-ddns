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

import dns.asyncquery
import dns.asyncresolver
import dns.exception
import dns.message
import dns.name
import dns.rcode
import dns.rdatatype
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

    **Every field carries a ``description``, and that is load-bearing
    rather than documentation.** #73's settings form renders its help
    text, its bounds and its input type out of this model's JSON schema
    — so a field added here appears on the form with no frontend change,
    and a bound tightened here cannot drift from the bound the form
    offers. ``tests/test_settings_schema.py`` refuses a field with an
    empty description for the same reason: a number box labelled only
    ``Prune Max Batches`` is a box an operator has to guess at.
    """

    # --- retention ---------------------------------------------------- #
    event_retention_days: int = Field(
        default=30,
        ge=1,
        le=3650,
        description=(
            "How long a DNS-update log row is kept before the scheduled "
            "prune deletes it."
        ),
    )
    #: Deliberately *not* ``event_retention_days``. ``ddns_event`` and
    #: ``ddns_rate_limit_event`` answer different questions and
    #: models.py says so explicitly: hanging the limiter off the log's
    #: retention makes "how long do we keep logs" silently also mean
    #: "how far back does the limiter look".
    rate_limit_event_retention_hours: int = Field(
        default=24,
        ge=1,
        le=8760,
        description=(
            "How long the rate limiter's own rows are kept. Deliberately "
            "separate from the log's retention: these rows are the "
            "limiter's window, not history."
        ),
    )
    #: Rows per committed batch. The lock is held for one batch, not for
    #: the run.
    prune_batch_size: int = Field(
        default=1000,
        ge=1,
        le=50_000,
        description=(
            "Rows deleted per committed batch. The lock is held for one "
            "batch, not for the whole run."
        ),
    )
    #: Hard ceiling on batches per tick. A first prune against a large
    #: history stops and says there is more to do rather than holding
    #: the worker for an unbounded time.
    prune_max_batches: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description=(
            "Ceiling on batches per prune tick. A first prune against a "
            "large history stops and reports more to do rather than "
            "holding the worker open."
        ),
    )

    # --- health checks ------------------------------------------------ #
    health_check_enabled: bool = Field(
        default=True,
        description=(
            "Whether the scheduled health check resolves hostnames at "
            "all. Off means the board's 'answered' station stops being "
            "updated; it does not mean the names stop being published."
        ),
    )
    #: How stale a hostname's ``dns_checked_at`` may get before it is
    #: re-checked. **Not** an APScheduler interval: the job ticks on a
    #: fixed short period and selects what is due, so changing this
    #: takes effect on the next tick with no reschedule dance (the
    #: legacy ``reschedule_health_checker`` exists only because the
    #: interval was baked into the trigger at startup).
    health_check_interval_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
        description=(
            "How stale a hostname's last check may get before it is "
            "re-checked. Not a scheduler interval: the job ticks often "
            "and selects what is due, so a change takes effect on the "
            "next tick with no restart."
        ),
    )
    #: Hostnames per tick. Bounds both the DNS fan-out and the write.
    health_check_batch_size: int = Field(
        default=200,
        ge=1,
        le=10_000,
        description=(
            "Hostnames resolved per tick. Bounds both the DNS fan-out "
            "and the write."
        ),
    )
    health_check_timeout_seconds: float = Field(
        default=5.0,
        ge=0.1,
        le=60.0,
        description=(
            "Per-query DNS timeout. A fractional value is allowed and is "
            "the reason this field is not an integer."
        ),
    )
    health_check_concurrency: int = Field(
        default=8,
        ge=1,
        le=64,
        description=(
            "How many DNS queries run at once inside one tick."
        ),
    )
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
    #:
    #: #75 added this field and #73 added the settings form; they were
    #: written against the same base and met here. The ``description``
    #: is #73's requirement of every field on this model — the form
    #: renders it as the help text under the input — and the sentence is
    #: #75's own, condensed. Without it this field would still appear on
    #: the Health checks page, because the page's field list is derived
    #: from this model rather than typed out, but it would appear as a
    #: bare number box, and ``test_settings_schema`` refuses that.
    health_check_manual_cooldown_seconds: int = Field(
        default=60,
        ge=0,
        le=86_400,
        description=(
            "Debounce on the manual 'Run health check' button, per "
            "actor. Nothing on the scheduled path reads it. 0 disables "
            "the debounce, which is a deliberate choice rather than the "
            "default: the button fans out to real nameservers."
        ),
    )

    # --- status board ------------------------------------------------- #
    #: The denominator behind "zero updates in the window". Printed
    #: beside every count this module reports, because a ratio whose
    #: divisor is not named is not a measurement.
    device_idle_window_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description=(
            "The board's denominator — the window 'updates / N d' is "
            "counted over, and the window a device is called idle for "
            "producing nothing in."
        ),
    )

    # --- rate limiting (enforced by #16; the default lives here) ------- #
    #: Installation-wide default; ``ddns_device.rate_limit_per_minute``
    #: overrides it per device and ``NULL`` there means *inherit this*,
    #: which is not the same as ``0``. models.py assigns the namespace
    #: to this issue and the enforcement to #16.
    rate_limit_per_minute: int = Field(
        default=30,
        ge=0,
        le=10_000,
        description=(
            "Updates a device may make per minute unless it carries its "
            "own limit. A device's own value overrides this; 0 here "
            "mutes every device that inherits."
        ),
    )


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

    Why the population is split four ways rather than three (#107)
    -------------------------------------------------------------
    It used to be three numbers — considered, never-written, checked —
    and the gap between them had no name. A tick that reported
    ``considered=5 never_written=1 checked=2`` was *arithmetically
    silent*: the two missing names could have been legitimately
    not-due, could have been past the batch ceiling, or could have
    disappeared between the two statements that read them, and the
    summary rendered all three identically and returned success. #107
    is that exact reading, observed once and never reproduced, and the
    reason it was worth fixing rather than retrying is that in
    production the symptom is **hostnames silently skipped by a
    health-check sweep**.

    So every hostname in ``hostnames_considered`` now lands in exactly
    one named bucket:

    ==========================  ==============================================
    ``hostnames_never_written`` no ``last_ip`` — nothing to compare against
    ``hostnames_not_due``       has a ``last_ip``, checked recently enough
    ``hostnames_checked``       resolved on this tick
    ``hostnames_deferred``      due, but past ``batch_size``
    ``hostnames_moved``         due when counted, absent when fetched
    ==========================  ==============================================

    and :meth:`assert_consistent` refuses any run whose buckets do not
    add up. The gap is still allowed to be non-zero — batching and
    staleness legitimately leave rows unchecked — but it can no longer
    be *unattributed*.
    """

    enabled: bool = True
    hostnames_considered: int = 0
    hostnames_never_written: int = 0
    #: Carry a ``last_ip`` but were checked more recently than
    #: ``health_check_interval_minutes`` ago, so this tick skipped them
    #: on purpose. Always ``0`` on a forced run, where the staleness
    #: clause is a literal ``true``.
    hostnames_not_due: int = 0
    #: Carry a ``last_ip`` and passed the staleness clause — the work
    #: this tick *should* do, counted before the batch ceiling is
    #: applied. Read in the same statement as the three counts above,
    #: so it is one snapshot of the population by construction.
    hostnames_due: int = 0
    hostnames_checked: int = 0
    #: Due, but beyond ``batch_size`` — real work this tick did not do,
    #: and the number ``truncated=True`` used to assert without
    #: quantifying.
    hostnames_deferred: int = 0
    #: Counted as due by the aggregate statement and **not returned** by
    #: the row fetch that followed it. Under the engine's READ
    #: COMMITTED isolation those are two snapshots unless something
    #: makes them one, so this is the residue of a concurrent writer —
    #: normally ``0``, negative if the population *grew*. It is a
    #: measurement, not an error code: a manual run stamping
    #: ``dns_checked_at`` a millisecond earlier is benign, and what is
    #: not acceptable is it being invisible. See :func:`run_health_checks`.
    hostnames_moved: int = 0
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
        # #107. Two invariants, and they fail for different reasons.
        #
        # The first says the four population counts *partition* the
        # population. They come out of one SQL statement built from one
        # `has_last_ip` expression and one `staleness` expression, so it
        # holds by construction — until three-valued logic, an edited
        # predicate or a `NOT` that does not distribute breaks it, which
        # are the ways this class of SQL actually goes wrong.
        by_population = (
            self.hostnames_never_written + self.hostnames_not_due + self.hostnames_due
        )
        if by_population != self.hostnames_considered:
            raise AssertionError(
                f"health check population does not partition: "
                f"never_written={self.hostnames_never_written} "
                f"not_due={self.hostnames_not_due} due={self.hostnames_due} "
                f"sum to {by_population}, but considered="
                f"{self.hostnames_considered}"
            )
        # The second says every hostname the run *counted* as due was
        # then accounted for — resolved, deferred past the batch
        # ceiling, or explicitly recorded as having moved between the
        # count and the fetch. This is the one that would have fired on
        # #107's recorded run had `hostnames_moved` been the mechanism:
        # `checked=2` against `due=4` with nothing named for the other
        # two is exactly the state that reported success.
        by_due = self.hostnames_checked + self.hostnames_deferred + self.hostnames_moved
        if by_due != self.hostnames_due:
            raise AssertionError(
                f"health check due accounting does not balance: "
                f"checked={self.hostnames_checked} "
                f"deferred={self.hostnames_deferred} "
                f"moved={self.hostnames_moved} sum to {by_due}, but due="
                f"{self.hostnames_due}"
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
            "hostnames_not_due": self.hostnames_not_due,
            "hostnames_due": self.hostnames_due,
            "hostnames_checked": self.hostnames_checked,
            "hostnames_deferred": self.hostnames_deferred,
            "hostnames_moved": self.hostnames_moved,
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


class AuthoritativeLookupError(Exception):
    """The delegation walk did not reach a server that owns the name.

    Raised rather than falling back to a cache. :func:`_resolve_one` turns
    any exception into an error string and :func:`classify_record` turns that
    into ``ERROR`` — *could not measure*, which is what this is. A fallback
    would produce an ``address`` instead, and a cached address that disagrees
    with what we published is indistinguishable from a zone that really has
    drifted. `providers.base.check_hostnameon_server` does fall back to a
    public resolver, and is right to: a stale answer there means "changed",
    so it writes, and a spurious write is idempotent. Here the same stale
    answer means "your zone is wrong", shown to a tenant.
    """


#: Where the delegation walk starts. Not used to answer the question — only
#: to ask the root/parent side who is authoritative.
_WALK_START_TIMEOUT = 5.0


async def authoritative_nameserver(
    fqdn: str,
    *,
    timeout: float,
    system: Any = None,
    query: Any = None,
) -> str:
    """The address of a nameserver that owns ``fqdn``.

    The async twin of ``providers.base.BaseProvider.get_authoritative_
    nameserver``, walking the same delegation chain the same way — label
    by label from the public suffix down, following NS targets, stopping
    where the parent stops delegating.

    Two arguments exist for the tests, which state that nothing in the
    health-check suite opens a socket: ``query`` stands in for
    ``dns.asyncquery.udp`` and ``system`` for the resolver used to turn an
    NS *name* into an address.
    """
    resolver = system if system is not None else dns.asyncresolver.Resolver()
    send = query if query is not None else dns.asyncquery.udp

    if not resolver.nameservers:
        raise AuthoritativeLookupError(
            "no system resolver to start the delegation walk from"
        )
    nameserver = resolver.nameservers[0]

    name = dns.name.from_text(fqdn)
    depth = 2
    last = False
    while not last:
        split = name.split(depth)
        last = split[0].to_unicode() == "@"
        sub = split[1]

        message = dns.message.make_query(sub, dns.rdatatype.NS)
        try:
            response = await send(message, str(nameserver), timeout=timeout)
        except (dns.exception.Timeout, OSError) as exc:
            raise AuthoritativeLookupError(
                f"NS query for {sub} failed: {exc}"
            ) from exc

        rcode = response.rcode()
        if rcode != dns.rcode.NOERROR:
            if rcode == dns.rcode.NXDOMAIN and depth > 2:
                # No delegation at this level, so the server found at the
                # parent owns the whole remaining name.
                return str(nameserver)
            raise AuthoritativeLookupError(
                f"NS query for {sub} answered {dns.rcode.to_text(rcode)}"
            )

        if not response.authority and not response.answer:
            raise AuthoritativeLookupError(f"NS query for {sub} carried no records")
        rrset = response.authority[0] if response.authority else response.answer[0]
        record = rrset[0]
        if record.rdtype != dns.rdatatype.SOA:
            authority = record.target
            try:
                answer = await resolver.resolve(authority)
                nameserver = answer.rrset[0].to_text()
            except (
                dns.exception.Timeout,
                dns.resolver.NoNameservers,
                dns.resolver.NXDOMAIN,
                dns.resolver.NoAnswer,
                OSError,
            ) as exc:
                raise AuthoritativeLookupError(
                    f"the NS target {authority} has no address: {exc}"
                ) from exc

        depth += 1

    return str(nameserver)


def make_resolver(timeout: float) -> Resolve:
    """The real resolver: the **authoritative** server, asked directly.

    This used to be a bare ``dns.asyncresolver.Resolver()``, which reads
    ``/etc/resolv.conf`` — in a container, Docker's embedded DNS forwarding
    to a recursive cache. That answers "what does the world currently see",
    and the board's column claims something else: *Current IP in zone*.

    Inside one TTL the two disagree, and the disagreement is not theoretical.
    A publish at 20:07:05 and another at 20:07:17 were re-checked at 20:07:45
    — 40 and 28 seconds later, against a 60-second default TTL — and the
    cache returned the pre-update address for both: ``mismatch: 2``,
    ``transitions: 2``, on two publishes that had succeeded. Clearing the
    stale reading on a good publish made the rows due immediately, which put
    the re-check *inside* the cache window rather than after it.

    ``dns.asyncresolver``, never the blocking one: ``dns.resolver.resolve``
    on this scheduler blocks the worker's only event loop — see the module
    docstring. dnspython reaches the image as a hard dependency of atrium's
    ``email-validator``, a transitive path, so ``tests/test_worker_jobs.py``
    asserts the import rather than trusting it to stay there.
    """
    # Per-sweep, and deliberately not wider: a delegation that changes
    # between sweeps must be followed, and a process-lifetime cache would
    # pin the answer to whatever was true when the worker started. Within
    # one sweep it saves a walk per record — A and AAAA for one name is two
    # lookups of the same delegation.
    seen: dict[str, str] = {}

    async def _resolve(name: str, rdtype: str) -> list[str]:
        key = name.lower().rstrip(".")
        nameserver = seen.get(key)
        if nameserver is None:
            nameserver = await authoritative_nameserver(name, timeout=timeout)
            seen[key] = nameserver

        resolver = dns.asyncresolver.Resolver(configure=False)
        resolver.nameservers = [nameserver]
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

    One snapshot, and what happens when it is two (#107)
    ----------------------------------------------------
    #107 recorded a tick that reported ``hostnames_considered=5``,
    ``hostnames_never_written=1`` and ``hostnames_checked=2`` — a
    population query and a ``due`` query that could not both be right —
    and asked whether the two are guaranteed to read one snapshot. They
    were **not**, and not by accident: ``app/db.py`` creates the engine
    with ``isolation_level="READ COMMITTED"`` so the worker's
    ``SELECT … FOR UPDATE SKIP LOCKED`` on ``scheduled_jobs`` takes a
    record lock instead of gap locks (atrium #152). Under READ
    COMMITTED every statement gets a fresh read view, so two statements
    in one transaction are two snapshots however the ``async with``
    block is written. The isolation level is three modules away from
    here and set for a reason that has nothing to do with this job,
    which is exactly why it was worth writing down at the call site.

    Two things follow, and both are implemented below rather than
    asserted in prose:

    * the **counts** — population, never-written, due, not-due — come
      out of a **single** ``SELECT``, which is one snapshot under any
      isolation level, instead of being two statements that happened to
      share a session;
    * the **row fetch** is necessarily a second statement, so it is
      *reconciled* against the count rather than trusted. The
      difference is published as ``hostnames_moved`` and logged at
      warning, and :meth:`HealthCheckSummary.assert_consistent` refuses
      a run whose buckets do not add up.

    What can write ``dns_checked_at`` between the two statements, since
    the answer is small and worth stating: this function (the worker
    container's own 60 s tick sweeps **cross-tenant**, so it reaches
    rows a tenant-scoped caller is looking at) and
    :func:`clear_health_check_results`, which writes it back to
    ``NULL``. Nothing else in this package touches the column.
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

        # The two predicates, built once and used by both statements
        # below. Spelled as values rather than repeated inline because a
        # population count and a row fetch that disagree about what
        # "due" means is the same defect as reading two snapshots, and
        # the copy-paste version is how it happens.
        #
        # Neither expression is ever NULL: `IS NULL` / `IS NOT NULL` and
        # `COALESCE` all return a value for a NULL column, so
        # `sa.not_()` over them partitions cleanly rather than
        # swallowing rows into the three-valued hole.
        #
        # `COALESCE(…, '') != ''` rather than `IS NOT NULL`, because
        # `''` is **not NULL in SQL and falsy in Python** (#107). A row
        # whose `last_ip_v4` is the empty string passed the old
        # `IS NOT NULL` filter, was loaded into the batch, and was then
        # dropped by `_check`'s `if not expected: continue` — occupying
        # a batch slot it could never use and contributing nothing to
        # any count. That is the same defect
        # `test_the_never_written_slice_is_excluded_by_the_query_not_just_the_loop`
        # documents for NULL, spelled differently; the two sides now
        # agree on what "has something to compare against" means, so
        # such a row is counted as `never_written`, which is what it is.
        has_last_ip = sa.or_(
            sa.func.coalesce(Hostname.last_ip_v4, "") != "",
            sa.func.coalesce(Hostname.last_ip_v6, "") != "",
        )
        # The staleness clause, or a literal `true` when the caller
        # forced the run. One value, so there is one place the ordering
        # and the batch ceiling are applied.
        staleness = (
            sa.or_(
                Hostname.dns_checked_at.is_(None),
                Hostname.dns_checked_at < stale_before,
            )
            if due_only
            else sa.true()
        )

        # The population, split into the three slices that partition it.
        # Counted, not dropped: "0 problems" over an unstated population
        # is the shape this whole file is written against, and #107 is
        # what an *unstated slice* costs — a tick that checked 2 of 4 due
        # names logged `hostnames_checked=2` and returned success,
        # because the two it skipped had nowhere to be counted.
        #
        # **One statement, therefore one snapshot** — this is the part
        # #107 asked to be made explicit rather than left incidental.
        # `app/db.py` creates the engine with
        # `isolation_level="READ COMMITTED"` (atrium #152: it drops the
        # gap locks that deadlocked the `scheduled_jobs` claim against
        # concurrent API inserts), so two statements in one transaction
        # here are two snapshots, not one. Rather than raise this
        # block's isolation and re-import that deadlock, the three
        # counts and the due count are read by a single `SELECT`, which
        # is atomic under every isolation level MySQL offers.
        totals = (
            await session.execute(
                scope.select(
                    Hostname,
                    sa.func.count().label("total"),
                    sa.func.sum(
                        sa.case((sa.not_(has_last_ip), 1), else_=0)
                    ).label("never_written"),
                    sa.func.sum(
                        sa.case((sa.and_(has_last_ip, staleness), 1), else_=0)
                    ).label("due"),
                    sa.func.sum(
                        sa.case(
                            (sa.and_(has_last_ip, sa.not_(staleness)), 1), else_=0
                        )
                    ).label("not_due"),
                ).select_from(Hostname)
            )
        ).one()
        summary.hostnames_considered = int(totals.total or 0)
        summary.hostnames_never_written = int(totals.never_written or 0)
        summary.hostnames_due = int(totals.due or 0)
        summary.hostnames_not_due = int(totals.not_due or 0)

        due_stmt = (
            scope.select(Hostname)
            .where(has_last_ip, staleness)
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

        # The count and the fetch are two statements, so they are two
        # snapshots. Reconcile them instead of trusting them: `expected`
        # is what the fetch should have returned given the count and the
        # ceiling, `deferred` is the honest remainder above the ceiling,
        # and `moved` is whatever the two readings disagree by. Negative
        # `moved` means the population *grew* between them.
        expected = min(summary.hostnames_due, config.health_check_batch_size)
        summary.hostnames_deferred = summary.hostnames_due - expected
        summary.hostnames_moved = expected - len(due)
        if summary.hostnames_moved:
            # Not an error — a concurrent manual run or another sweep
            # stamping `dns_checked_at` between the two statements is
            # benign and expected on a busy installation. What is not
            # acceptable is it being invisible, which is the whole of
            # #107.
            log.warning(
                "atrium_ddns.health_check.population_moved",
                counted_due=summary.hostnames_due,
                fetched=len(due),
                expected=expected,
                batch_size=config.health_check_batch_size,
            )

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
    "AuthoritativeLookupError",
    "authoritative_nameserver",
    "make_resolver",
    "register_jobs",
    "reset_counters",
    "run_health_checks",
    "run_retention_prune",
    "stored_dns_status",
    "worst",
]
