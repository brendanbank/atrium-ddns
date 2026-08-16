"""The host's JSON surface.

- ``GET /api/atrium_ddns/state`` — read-only, auth required (demo).
- ``POST /api/atrium_ddns/bump`` — gated by ``atrium_ddns.write`` (demo).
- ``GET /api/atrium_ddns/board`` — the device board and the resolution
  strips, gated by ``atrium_ddns.device.manage`` and scoped by
  :class:`~atrium_ddns.scope.DdnsScope`.
- ``POST /api/atrium_ddns/health-checks/run`` and ``…/clear`` — the
  board's two on-demand operator actions (#75, ui-parity §3.3 G3), same
  permission and same scope. ``run`` **is** the scheduled job, called
  with ``due_only=False``; it is debounced per actor because it fans out
  to real nameservers.
- ``GET /api/atrium_ddns/events`` — the log search. Authenticated, and
  scoped: a tenant sees their own rows and ``atrium_ddns.events.read.all``
  widens that and nothing else. Every filter is an indexed query.
- ``GET|POST /api/atrium_ddns/hostnames``,
  ``PATCH|DELETE /api/atrium_ddns/hostnames/{id}`` — the hostname
  lifecycle, gated by ``atrium_ddns.hostname.manage`` and scoped. The
  writer the board and the wire protocol were both missing; see the
  Hostnames section for why its two validators are *imported* rather
  than written.
- ``PATCH /api/atrium_ddns/domains/{id}`` — the zone rename (#75,
  ui-parity §3.3 G4). Refuses a rename that would leave a hostname
  outside its zone rather than rewriting the names; see
  :func:`rename_domain` for the argument, which is one the model had
  already made about :class:`HostnameAssignIn`.

Atrium mounts every JSON route under ``/api/...`` so the SPA owns
un-prefixed URL space (atrium issue #89); host routes follow the same
contract. The auth dependencies and the audit/notify helpers (imported
from ``app.*``) are the surface a host calls atrium through.

Why the board is one endpoint and not four
------------------------------------------
The board's whole job is to rank a tenant's devices by liveness and
hang the resolution strips underneath them. Every one of those verdicts
— the five ``DnsCheckStatus`` states, the four ``Liveness`` states, the
two joint verdicts per strip, the collapse denominator — is **computed
here and shipped as a value**, never as the ingredients for the browser
to recombine.

That is not tidiness. ``docs/ops/ui-design.md`` §4.2 states the rule
outright: *"Never compute the five states in the frontend. They arrive
from the API as the ``DnsCheckStatus`` string. Two implementations of a
five-state rule is how five states become three."* Splitting this into
per-resource endpoints would put the join — and therefore the
arithmetic — back in the browser, which is the failure mode the rule
exists to prevent.

The five states themselves come from
:func:`~atrium_ddns.worker_jobs.stored_dns_status`, i.e. from the
function #17 wrote and the same one the health-check job re-derives
against. This module adds no sixth reading of the columns.
"""
from __future__ import annotations

import secrets as secrets_module
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_dirty
from sqlalchemy.sql import Select

from app.auth.rbac import require_perm
from app.auth.users import current_user
from app.db import get_session
from app.host_sdk.crypto import apply_secret_update, unlock_user_secrets
from app.models.auth import User

# atrium's own audit table, read (never written) directly by the
# health-check debounce. `record_audit` is the writer; this is the same
# rows read back, and it is the only place host code reads an atrium
# model. See `_last_manual_run` for why the debounce lives here rather
# than in a table of its own.
from app.models.ops import AuditLog
from app.services.audit import record as record_audit

from .auth_device import effective_rate_limit, hash_password
from .models import (
    AtriumDdnsState,
    Device,
    DnsEvent,
    Domain,
    DomainBackend,
    Hostname,
)
from .providers import PROVIDER_STATUSES, known_services, provider_class
from .settings_schema import (
    APP_CONFIG_MANAGE_PERMISSION,
    SettingsSchemaOut,
    settings_schema,
)

# The zone-containment rule, imported rather than re-derived. This is the
# same function object ``BaseProvider.domaininhostname`` calls, which is
# how ``POST /hostnames`` and ``/nic/update`` are guaranteed to agree
# about what "inside its zone" means. See its docstring.
from .providers.base import zone_contains
from .router_nic import (
    EVENT_AUTH,
    EVENT_DELETE,
    EVENT_UPDATE,
    STATUS_911,
    STATUS_ABUSE,
    STATUS_BADAUTH,
    STATUS_NOHOST,
    STATUS_NOTFQDN,
    # The syntax rule, likewise: `/nic/update` calls this exact function
    # to decide `notfqdn`, and so does `POST /hostnames`.
    split_hostnames,
)
from .scope import EVENTS_CROSS_TENANT_PERMISSION, DdnsScope, get_scope
from .worker_jobs import (
    SUCCESS_RESPONSE_CODES,
    DnsCheckStatus,
    Liveness,
    clear_health_check_results,
    device_statuses,
    load_config,
    # The scheduled job's own function object. `POST /health-checks/run`
    # calls *this*, with `due_only=False` — see #75's section below for
    # why a second sweep in this module was not an option.
    run_health_checks,
    stored_dns_status,
)

# One canonicaliser for the whole host, not two. The health-check job
# compares ``2001:db8::1`` against ``2001:0db8:0000::0001`` through this
# function; a board that compared the strings would report a divergence
# the job does not see, on the majority of this estate's traffic
# (ui-design.md M1: v6 is 68–97% of update events). Imported rather than
# re-implemented, and ``tests/test_router_board.py`` asserts the two
# names still resolve to one object.
from .worker_jobs import _canonical as canonical_address

router = APIRouter(prefix="/api/atrium_ddns", tags=["atrium_ddns"])


class StateOut(BaseModel):
    message: str
    counter: int


async def _load_state(session: AsyncSession) -> AtriumDdnsState:
    state = (
        await session.execute(
            select(AtriumDdnsState).where(AtriumDdnsState.id == 1)
        )
    ).scalar_one_or_none()
    if state is None:
        raise RuntimeError(
            "atrium_ddns_state row id=1 missing — run the host alembic upgrade",
        )
    return state


@router.get("/state", response_model=StateOut)
async def get_state(
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StateOut:
    state = await _load_state(session)
    return StateOut(message=state.message, counter=state.counter)


@router.post("/bump", response_model=StateOut)
async def bump(
    user: User = Depends(require_perm("atrium_ddns.write")),
    session: AsyncSession = Depends(get_session),
) -> StateOut:
    state = await _load_state(session)
    before = state.counter
    state.counter += 1
    await record_audit(
        session,
        actor_user_id=user.id,
        entity="atrium_ddns_state",
        entity_id=state.id,
        action="bump",
        diff={"counter": {"before": before, "after": state.counter}},
    )
    await session.commit()
    return StateOut(message=state.message, counter=state.counter)


# --------------------------------------------------------------------- #
# The device board and the resolution strip
# --------------------------------------------------------------------- #

#: Read gate on the board. Held by the ``user`` role (and therefore by
#: every ordinary tenant), by ``admin``, and auto-granted to
#: ``super_admin``; seeded by the ``0002`` migration from
#: :data:`atrium_ddns.models.PERMISSIONS`. A caller *without* it gets
#: 403 rather than an empty board — an empty board and a refused one
#: are different facts and rendering both as "you have no devices" is
#: the same collapse this whole surface exists to avoid.
BOARD_READ_PERMISSION = "atrium_ddns.device.manage"

#: What "which family is this strip about" is called on the wire. The
#: DNS record type rather than ``v4``/``v6``, because it is what the
#: badge renders and what a reader would type into ``dig``.
Family = Literal["A", "AAAA"]

FAMILIES: tuple[Family, ...] = ("A", "AAAA")


class JointVerdict(str, Enum):
    """One of the two joints on a strip's rail — ui-design.md §3.2.

    Five renderings, and the three that are *not* ``agreed``/``diverged``
    are the point. ``NOT_APPLICABLE`` means *no comparison was made and
    none should have been*, which is not the same as a comparison that
    came back equal (``AGREED``) and not the same as one we could not
    make (``NOT_MEASURED_*``). The design draws them as no segment, a
    solid hairline, and a dotted/dashed hairline respectively.
    """

    AGREED = "agreed"
    DIVERGED = "diverged"
    NOT_MEASURED_NEVER = "not_measured_never"
    NOT_MEASURED_FAILED = "not_measured_failed"
    NOT_APPLICABLE = "not_applicable"


class LowerJointReason(str, Enum):
    """*Why* the lower joint was or was not evaluated — ui-design.md §3.3.

    ``Device.last_ip_*`` is the address the device **called from**, not
    the address it asked us to publish (``router_nic.py`` says so in as
    many words). For a device behind IPv4 NAT sending an explicit
    ``myip=`` those are permanently and correctly different, so a strip
    that compared them unconditionally would show a divergence on every
    render, forever — an indicator that is always on cannot indicate.

    So the comparison is conditional on the most recent successful
    update event for this hostname and family having
    ``client_ip == ip``, and when it is not made the reason travels with
    the strip. A reader learns *why* nothing is being compared instead
    of assuming a bug.
    """

    #: ``event.client_ip == event.ip`` — the device publishes the
    #: address it called from, so the joint is a real verdict.
    EVALUATED = "evaluated"
    #: ``Hostname.device_id IS NULL``. A configuration state, not a
    #: fault, and it must not be marked (§4.1).
    NO_DEVICE = "no_device"
    #: No ``good``/``nochg`` update event for this hostname and family,
    #: so there is nothing that says which of the two shapes this device
    #: is. Refusing beats guessing.
    NO_UPDATE_ON_RECORD = "no_update_on_record"
    #: ``event.client_ip != event.ip`` — the device declares its address
    #: explicitly. The two columns differ permanently and correctly.
    DECLARED_MYIP = "declared_myip"
    #: One of the two addresses is null: nothing published for this
    #: family yet, or the device has no call-from address in it.
    NOT_COMPARABLE = "not_comparable"


def _iso(value: datetime | None) -> str | None:
    """Absolute UTC ISO-8601 with a ``Z``, or ``None`` — ui-design.md §4.4.

    The columns are naive UTC (``DATETIME(6)``, atrium's convention), so
    a bare ``isoformat()`` produces a string with no offset that a
    browser parses as *local* time. The board's relative ages are
    rendered from these, and an hour of silent skew is exactly the sort
    of thing that reads as a real signal.

    ``None`` stays ``None``. It never becomes an epoch: ``now - 0`` is
    fifty-six years, and a freshness rule fed that alarms for a full
    cadence after every deploy (§4.2, third prohibition).
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


class AnsweredStation(BaseModel):
    """``; answered`` — what the authoritative nameserver said.

    ``status`` is the whole point of this model. Three of the five
    states carry a null ``address`` and they are three different facts:
    ``NEVER_CHECKED`` (we have not looked), ``ERROR`` (we looked and
    could not measure) and ``MISSING`` (we looked and the name does not
    resolve — an outage). The frontend renders the string; it does not
    re-derive it.
    """

    address: str | None
    status: DnsCheckStatus
    checked_at: str | None
    #: ``Hostname.dns_check_error``, verbatim, when there is one. A
    #: resolver's own words are a diagnostic and are wanted in full.
    error: str | None


class PublishedStation(BaseModel):
    """``; published`` — what we last successfully wrote."""

    address: str | None
    updated_at: str | None


class CalledFromStation(BaseModel):
    """``; called from`` — where the device's last call originated."""

    address: str | None
    seen_at: str | None
    reason: LowerJointReason
    #: The address the device *asked* us to publish on its most recent
    #: successful update, when that differs from where it called from.
    #: Carried so the UI can say what was declared rather than leaving
    #: the reader to infer it from an absent comparison.
    declared_address: str | None = None


class StripOut(BaseModel):
    """One resolution strip: three stations on a rail, two joints.

    The counts at the bottom are the collapse rule's denominator
    (§3.4). ``; agrees`` on its own would be a ratio with the divisor
    hidden, and the divisor here *moves* — it is 2 for a device
    publishing its own address and 1 for one declaring ``myip``.
    """

    family: Family
    answered: AnsweredStation
    published: PublishedStation
    called_from: CalledFromStation
    #: ``dns_ip_*`` vs ``last_ip_*`` — *does the zone carry what we
    #: wrote?*
    upper_joint: JointVerdict
    #: ``last_ip_*`` vs ``Device.last_ip_*`` — *has the device moved
    #: without the name following?*
    lower_joint: JointVerdict
    joints_agreed: int
    #: The divisor: joints that produced a verdict either way.
    joints_compared: int
    joints_not_applicable: int
    joints_unmeasured: int
    #: True only when every joint that produced a verdict said
    #: ``agreed`` **and** at least one did. A strip whose joints are all
    #: ``not_applicable`` stays expanded: ``0 of 0 agree`` is vacuous,
    #: and collapsing it would make the collapsed shape mean two things.
    collapsible: bool


class HostnameOut(BaseModel):
    """One name, and up to two strips — one per address family.

    ``strips`` is empty for a hostname nothing has ever been published
    for. That is a real state (#17's ``hostnames_never_written`` slice)
    and the UI says so rather than drawing two empty rails.
    """

    id: int
    name: str
    domain_name: str
    device_id: int | None
    strips: list[StripOut]


class DeviceOut(BaseModel):
    """One device's line on the board, with its names nested under it."""

    id: int
    name: str
    liveness: Liveness
    #: ``!`` in the gutter. **Only** ``NEVER_SEEN`` and
    #: ``LAST_CALL_FAILED`` — ui-design.md M3 measured half the fleet
    #: producing zero events in a 24-hour window, so marking ``IDLE``
    #: would paint half the board and destroy the marker. Decided here
    #: rather than in the browser so it cannot be decided twice.
    marked: bool
    last_seen_at: str | None
    last_response_code: str | None
    #: ``None`` for a device that has never called. There is no window
    #: measurement to report, and ``0`` would be a lie with a number on
    #: it.
    updates_in_window: int | None
    #: :meth:`~atrium_ddns.worker_jobs.DeviceStatus.render_updates` —
    #: ``—``, ``error`` or the count. Three strings for three facts,
    #: chosen by the one function that owns that decision.
    updates_display: str
    #: The denominator, beside the numerator, so a caller cannot render
    #: the count without being able to render what it is out of.
    window_days: int
    #: ``NULL`` means *inherit*. Carried beside the resolved value below
    #: rather than instead of it: "30/min" and "30/min because nobody
    #: set one" are different facts and the board is allowed to say
    #: which. #73's AC 4 — the stored value is displayed wherever a
    #: device is shown, not only accepted at creation.
    rate_limit_per_minute: int | None
    #: :func:`~atrium_ddns.auth_device.effective_rate_limit`, the
    #: limiter's own function. See ``DeviceSummaryOut``.
    effective_rate_limit_per_minute: int
    hostnames: list[HostnameOut]


class BoardOut(BaseModel):
    """Everything the board renders, in the order it renders it."""

    generated_at: str
    #: ``DdnsConfig.device_idle_window_days``.
    window_days: int
    #: ``DdnsConfig.health_check_interval_minutes`` — read from the API
    #: so the empty state's sentence ("the health check runs every 15
    #: minutes") cannot go stale when an operator changes it (§4.5).
    health_check_interval_minutes: int
    #: Ordered ``never_seen`` → ``last_call_failed`` → ``idle`` →
    #: ``active``, oldest ``last_seen_at`` first inside a bucket. The
    #: ordering **is** the opinion (§3.6): a device that has gone quiet
    #: is at the top of the page without anyone asking for it. The
    #: array order is the board order; the client does not re-sort.
    devices: list[DeviceOut] = Field(default_factory=list)
    #: Hostnames with ``device_id IS NULL``. Listed rather than dropped
    #: — a name nobody can update is exactly the kind of row this board
    #: exists to surface, and hiding it because it has no parent would
    #: make the board's silence mean two things.
    unassigned_hostnames: list[HostnameOut] = Field(default_factory=list)


#: ``Liveness`` -> board position. Not ``_STATUS_RANK`` and not any
#: other severity order: this is *whose problem it is first*, which is
#: the question the board answers.
_LIVENESS_ORDER: dict[Liveness, int] = {
    Liveness.NEVER_SEEN: 0,
    Liveness.LAST_CALL_FAILED: 1,
    Liveness.IDLE: 2,
    Liveness.ACTIVE: 3,
}

#: The two liveness states that earn the ``!`` gutter marker.
MARKED_LIVENESS: frozenset[Liveness] = frozenset(
    {Liveness.NEVER_SEEN, Liveness.LAST_CALL_FAILED}
)

#: ``DnsCheckStatus`` -> the upper joint's verdict. Deliberately **not**
#: ``worker_jobs._STATUS_RANK``: that ranks ``ERROR`` above ``MISMATCH``
#: because an unmeasured half must not hide behind a known-bad *when
#: aggregating*. Visual weight runs the other way — ``MISSING`` and
#: ``MISMATCH`` are the tenant's problem and are loud; ``ERROR`` is our
#: resolver's problem and is quiet. An implementation that reused the
#: rank to pick a rendering would paint every resolver hiccup as an
#: outage (ui-design.md §4.3).
_UPPER_JOINT: dict[DnsCheckStatus, JointVerdict] = {
    DnsCheckStatus.NEVER_CHECKED: JointVerdict.NOT_MEASURED_NEVER,
    DnsCheckStatus.ERROR: JointVerdict.NOT_MEASURED_FAILED,
    DnsCheckStatus.MISSING: JointVerdict.DIVERGED,
    DnsCheckStatus.OK: JointVerdict.AGREED,
    DnsCheckStatus.MISMATCH: JointVerdict.DIVERGED,
}


def build_strip(
    *,
    family: Family,
    last_ip: str | None,
    last_updated_at: datetime | None,
    dns_ip: str | None,
    dns_checked_at: datetime | None,
    dns_check_error: str | None,
    has_device: bool,
    device_ip: str | None,
    device_seen_at: datetime | None,
    event_client_ip: str | None,
    event_ip: str | None,
) -> StripOut:
    """One strip, from columns that already exist. Pure — no IO.

    Pure on purpose: every verdict on this surface is decided here, so a
    test can drive all five ``; answered`` states and all five lower-joint
    reasons without a database, and the endpoint below becomes a query
    plus a loop.
    """
    status = stored_dns_status(
        last_ip=last_ip,
        dns_ip=dns_ip,
        dns_checked_at=dns_checked_at,
        dns_check_error=dns_check_error,
    )
    upper = _UPPER_JOINT[status]

    reason, lower = _lower_joint(
        last_ip=last_ip,
        has_device=has_device,
        device_ip=device_ip,
        event_client_ip=event_client_ip,
        event_ip=event_ip,
    )

    verdicts = (upper, lower)
    agreed = sum(1 for v in verdicts if v is JointVerdict.AGREED)
    compared = sum(
        1
        for v in verdicts
        if v in (JointVerdict.AGREED, JointVerdict.DIVERGED)
    )
    not_applicable = sum(
        1 for v in verdicts if v is JointVerdict.NOT_APPLICABLE
    )
    unmeasured = sum(
        1
        for v in verdicts
        if v
        in (JointVerdict.NOT_MEASURED_NEVER, JointVerdict.NOT_MEASURED_FAILED)
    )

    return StripOut(
        family=family,
        answered=AnsweredStation(
            address=dns_ip,
            status=status,
            checked_at=_iso(dns_checked_at),
            error=dns_check_error,
        ),
        published=PublishedStation(
            address=last_ip, updated_at=_iso(last_updated_at)
        ),
        called_from=CalledFromStation(
            address=device_ip,
            seen_at=_iso(device_seen_at),
            reason=reason,
            declared_address=(
                event_ip if reason is LowerJointReason.DECLARED_MYIP else None
            ),
        ),
        upper_joint=upper,
        lower_joint=lower,
        joints_agreed=agreed,
        joints_compared=compared,
        joints_not_applicable=not_applicable,
        joints_unmeasured=unmeasured,
        collapsible=agreed >= 1 and agreed == compared,
    )


def _lower_joint(
    *,
    last_ip: str | None,
    has_device: bool,
    device_ip: str | None,
    event_client_ip: str | None,
    event_ip: str | None,
) -> tuple[LowerJointReason, JointVerdict]:
    """``(reason, verdict)`` for the lower joint — ui-design.md §3.3.

    The order of the guards is the argument. *No device* comes first
    because it is a fact about configuration; *no event* next because
    without one we cannot tell which shape this device is; *declared
    myip* next because that is the case the naive implementation gets
    wrong; and only then is a comparison made at all.
    """
    if not has_device:
        return LowerJointReason.NO_DEVICE, JointVerdict.NOT_APPLICABLE
    if event_client_ip is None or event_ip is None:
        return LowerJointReason.NO_UPDATE_ON_RECORD, JointVerdict.NOT_APPLICABLE
    called = canonical_address(event_client_ip)
    declared = canonical_address(event_ip)
    if called is None or declared is None or called != declared:
        return LowerJointReason.DECLARED_MYIP, JointVerdict.NOT_APPLICABLE
    if last_ip is None or device_ip is None:
        return LowerJointReason.NOT_COMPARABLE, JointVerdict.NOT_APPLICABLE
    published = canonical_address(last_ip)
    from_addr = canonical_address(device_ip)
    if published is not None and from_addr is not None and published == from_addr:
        return LowerJointReason.EVALUATED, JointVerdict.AGREED
    return LowerJointReason.EVALUATED, JointVerdict.DIVERGED


def _strips_for(
    hostname: Hostname,
    device: Device | None,
    events: dict[Family, tuple[str | None, str | None]],
) -> list[StripOut]:
    """Up to two strips for one hostname — ui-design.md §3.4.

    **A correction to §3.4's wording, argued rather than assumed.** The
    design says a family is rendered when it has "at least one non-null
    value across the three stations". Read literally that includes
    ``Device.last_ip_*``, which is a property of the *device* and not of
    the name — so a v6-only hostname sitting on a router that also holds
    an IPv4 address would grow a spurious ``A`` strip whose published
    and answered stations are both empty, forever. That is the opposite
    of §3.4's own stated intent ("a v6-only hostname shows one strip and
    reserves no space for the other").

    So the test is the two *hostname* stations: a family is rendered
    when this name has ever been published in it or has ever been
    answered in it. The call-from station rides along on whichever
    families that produces.
    """
    out: list[StripOut] = []
    for family in FAMILIES:
        suffix = "v4" if family == "A" else "v6"
        last_ip = getattr(hostname, f"last_ip_{suffix}")
        dns_ip = getattr(hostname, f"dns_ip_{suffix}")
        if last_ip is None and dns_ip is None:
            continue
        event_client_ip, event_ip = events.get(family, (None, None))
        out.append(
            build_strip(
                family=family,
                last_ip=last_ip,
                last_updated_at=hostname.last_updated_at,
                dns_ip=dns_ip,
                dns_checked_at=hostname.dns_checked_at,
                dns_check_error=hostname.dns_check_error,
                has_device=device is not None,
                device_ip=(
                    getattr(device, f"last_ip_{suffix}") if device else None
                ),
                device_seen_at=device.last_seen_at if device else None,
                event_client_ip=event_client_ip,
                event_ip=event_ip,
            )
        )
    return out


def _family_of(address: str | None) -> Family | None:
    """``A``/``AAAA`` for a stored address, or ``None`` if it is not one.

    Derived with :mod:`ipaddress` rather than by looking for a colon.
    The SQL below partitions on the colon because that is what an index
    can do cheaply; this is the reading that decides, so a row the
    partition mis-buckets lands nowhere rather than in the wrong family.
    """
    canonical = canonical_address(address) if address else None
    if canonical is None:
        return None
    return "AAAA" if ":" in canonical else "A"


async def _latest_successful_updates(
    session: AsyncSession, scope: DdnsScope, hostname_ids: list[int]
) -> dict[int, dict[Family, tuple[str | None, str | None]]]:
    """``hostname_id -> family -> (client_ip, ip)`` for the newest success.

    "Newest" is ``MAX(id)``, not ``MAX(created_at)``: ``ddns_event``
    carries microsecond timestamps precisely because a router bursting
    several hostnames writes several rows inside one second, and the
    primary key is the only total order the table has.

    Grouped in SQL and joined back rather than fetched-and-reduced, so
    the work is bounded by the number of ``(hostname, family)`` pairs
    the tenant owns instead of by the retention window.
    """
    if not hostname_ids:
        return {}

    # The partition key. IPv6 in string form always carries a colon and
    # IPv4 never does, so this is exact — but it is only used to *group*;
    # `_family_of` is what decides which bucket a row is finally read
    # into. See its docstring.
    family_key = sa.case((DnsEvent.ip.like("%:%"), "AAAA"), else_="A")

    newest = (
        scope.select(
            DnsEvent,
            DnsEvent.hostname_id.label("hostname_id"),
            family_key.label("family_key"),
            sa.func.max(DnsEvent.id).label("id"),
        )
        .where(
            DnsEvent.hostname_id.in_(hostname_ids),
            DnsEvent.event_type == "update",
            DnsEvent.response_code.in_(sorted(SUCCESS_RESPONSE_CODES)),
            DnsEvent.ip.is_not(None),
        )
        .group_by(DnsEvent.hostname_id, family_key)
        .subquery()
    )
    rows = (
        await session.execute(
            scope.select(
                DnsEvent,
                DnsEvent.hostname_id,
                DnsEvent.client_ip,
                DnsEvent.ip,
            ).join(newest, DnsEvent.id == newest.c.id)
        )
    ).all()

    out: dict[int, dict[Family, tuple[str | None, str | None]]] = {}
    for row in rows:
        family = _family_of(row.ip)
        if family is None or row.hostname_id is None:
            continue
        out.setdefault(int(row.hostname_id), {})[family] = (row.client_ip, row.ip)
    return out


@router.get("/board", response_model=BoardOut)
async def get_board(
    _user: User = Depends(require_perm(BOARD_READ_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> BoardOut:
    """The whole board, ordered, with every verdict already decided.

    Four reads, none of them per-row: the devices, the hostnames with
    their zone names, the newest successful update per
    ``(hostname, family)``, and #17's own
    :func:`~atrium_ddns.worker_jobs.device_statuses`. Everything after
    that is a loop over dictionaries.

    Every statement goes through :class:`~atrium_ddns.scope.DdnsScope`.
    A caller holding ``atrium_ddns.admin`` sees every tenant's rows;
    everyone else sees their own; a caller with no tenant identity sees
    nothing, because the scope's third state is a literal ``false``
    rather than an absent ``WHERE``.
    """
    config = await load_config(session)

    devices = list((await session.execute(scope.select(Device))).scalars().all())
    by_device_id = {device.id: device for device in devices}

    # `scope.select(Hostname, …)` — the first argument names the model
    # whose tenancy predicate is applied, the rest are the entities
    # selected. Both are `Hostname` here because the row wants the ORM
    # object *and* the zone name, and the zone name is the one thing on
    # this surface that lives on another table.
    hostnames: list[tuple[Hostname, str]] = [
        (row[0], row[1])
        for row in (
            await session.execute(
                scope.select(Hostname, Hostname, Domain.name)
                .join(Domain, Domain.id == Hostname.domain_id)
                .order_by(Hostname.name)
            )
        ).all()
    ]

    events = await _latest_successful_updates(
        session, scope, [hostname.id for hostname, _ in hostnames]
    )

    statuses = await device_statuses(
        session, scope=scope, window_days=config.device_idle_window_days
    )
    by_status = {status.device_id: status for status in statuses}

    nested: dict[int, list[HostnameOut]] = {device.id: [] for device in devices}
    unassigned: list[HostnameOut] = []
    for hostname, domain_name in hostnames:
        device = (
            by_device_id.get(hostname.device_id)
            if hostname.device_id is not None
            else None
        )
        rendered = HostnameOut(
            id=hostname.id,
            name=hostname.name,
            domain_name=domain_name,
            device_id=hostname.device_id,
            strips=_strips_for(hostname, device, events.get(hostname.id, {})),
        )
        if device is None:
            unassigned.append(rendered)
        else:
            nested[device.id].append(rendered)

    out: list[DeviceOut] = []
    for device in devices:
        status = by_status.get(device.id)
        if status is None:
            # `device_statuses` is built from the same scoped select, so
            # this cannot happen — and if it ever does, dropping the
            # device silently is the wrong direction. Refuse loudly.
            raise RuntimeError(
                f"device {device.id} is visible to the scope but has no "
                f"DeviceStatus; the two reads disagree about the population"
            )
        out.append(
            DeviceOut(
                id=device.id,
                name=device.name,
                liveness=status.liveness,
                marked=status.liveness in MARKED_LIVENESS,
                last_seen_at=_iso(status.last_seen_at),
                last_response_code=status.last_response_code,
                updates_in_window=status.updates_in_window,
                updates_display=status.render_updates(),
                window_days=status.window_days,
                rate_limit_per_minute=device.rate_limit_per_minute,
                effective_rate_limit_per_minute=effective_rate_limit(
                    device, config.rate_limit_per_minute
                ),
                hostnames=nested[device.id],
            )
        )
    out.sort(
        key=lambda d: (
            _LIVENESS_ORDER[d.liveness],
            # Oldest first inside a bucket. `never_seen` has no
            # timestamp at all and every member of that bucket sorts
            # equal, so the tie-break is the name — stable, and not the
            # insertion order of a dict.
            d.last_seen_at or "",
            d.name,
        )
    )

    # `datetime.now(UTC)` is aware and `_iso` expects the naive-UTC shape
    # every column on this host uses, so it is stripped on the way in
    # rather than producing a string with a different tail from every
    # other timestamp in the payload.
    generated_at = _iso(datetime.now(UTC).replace(tzinfo=None))
    assert generated_at is not None  # `_iso` only returns None for None

    return BoardOut(
        generated_at=generated_at,
        window_days=config.device_idle_window_days,
        health_check_interval_minutes=config.health_check_interval_minutes,
        devices=out,
        unassigned_hostnames=unassigned,
    )


# ===================================================================== #
# On-demand health-check actions — the legacy `/admin/health-checks/*`
#
# ui-parity §3.3 G3, closed by #75. Two routes, and the reason they are
# worth having is a wait rather than a capability: the check is
# scheduled, so an operator who has just fixed a provider outage watches
# a stale board for up to `health_check_interval_minutes` (default 15)
# with no way to say "look again". The board is the surface this
# milestone is built around, and it was the one surface with no refresh.
#
# **Neither of these is a second implementation, and that is the design
# constraint rather than a tidiness preference.** `run` is
# `worker_jobs.run_health_checks` — the scheduled job's own function
# object — called with `due_only=False` and the caller's scope, so the
# batch ceiling, the concurrency semaphore, the timeout, the canonical
# address comparison, the five-state classification and the accounting
# assertion are reached by exactly one path. A hand-rolled "check these
# names now" in this module would be a sixth reading of the columns and
# the first thing it would lose is the ERROR/MISSING distinction.
#
# **The third disposition next door is not extended to these.**
# `POST /admin/events/clear` was struck by operator decision (ui-parity
# §3.4) and named that route and only that route. `clear` here resets
# *health-check results* — four columns on `ddns_hostname` — and touches
# no `ddns_event` row. Clearing a result and clearing a log are different
# operations on different tables; the legacy service had them as two
# routes for that reason, and inventing a disposition by analogy is the
# one thing the parity table is not allowed to do.
# ===================================================================== #


class HealthCheckRunOut(BaseModel):
    """What one manual run did, with every denominator named.

    Mirrors :class:`~atrium_ddns.worker_jobs.HealthCheckSummary` rather
    than summarising it: an operator who pressed the button is owed the
    same accounting the scheduled tick logs, including the slices that
    were **not** checked. ``hostnames_considered`` is the population,
    ``hostnames_never_written`` is the slice with nothing to compare
    against, ``hostnames_checked`` is what was resolved, and the four
    record counters sum to ``records_checked`` — asserted server-side by
    ``HealthCheckSummary.assert_consistent`` before this is built.
    """

    #: ``False`` when ``health_check_enabled`` is off. The run then
    #: resolves nothing and every count below is zero — which is a
    #: refusal, not a clean sweep, and the flag is what tells them apart.
    enabled: bool
    #: Always ``True`` on this route: a manual run is by definition not
    #: the scheduled one. Carried so a client rendering the summary does
    #: not have to know which endpoint produced it.
    forced: bool
    hostnames_considered: int
    hostnames_never_written: int
    hostnames_checked: int
    records_checked: int
    ok: int
    mismatch: int
    missing: int
    error: int
    #: Names whose aggregate verdict *changed* on this run. The number an
    #: operator who just fixed something is actually looking for.
    transitions: int
    #: ``True`` when there was more in reach than ``batch_size`` allowed.
    #: A summary that cannot say so reads identically whether it swept
    #: everything or one percent.
    truncated: bool
    batch_size: int


class HealthCheckClearOut(BaseModel):
    """What one clear reset, over the population it could have reached.

    ``cleared`` on its own reads identically whether it reset the right
    rows or every row in the installation. ``in_scope`` is read back
    after the write and printed beside it, so a zero is a measurement.
    """

    cleared: int
    in_scope: int


def _cooldown_remaining(
    last_run: datetime | None, cooldown_seconds: int, now: datetime
) -> int:
    """Whole seconds left on the debounce, or ``0``.

    Separate from the handler and pure, so the arithmetic is testable
    without a database and without a clock. ``last_run is None`` — never
    run — is the state that must not collapse into "ran a long time
    ago"; both permit the run, and only one of them is a measurement.
    """
    if cooldown_seconds <= 0 or last_run is None:
        return 0
    elapsed = (now - last_run).total_seconds()
    remaining = cooldown_seconds - elapsed
    # `ceil`, not `round`: reporting 0 while still refusing would send a
    # client straight back into another 429.
    return max(0, -int(-remaining // 1))


async def _last_manual_run(session: AsyncSession, actor_user_id: int) -> datetime | None:
    """When this actor last triggered a manual run, or ``None``.

    Read out of **atrium's ``audit_log``**, which is where the trigger
    already writes: the manual run is an operator action and gets an
    audit row whether or not anything debounces it, so the debounce
    reads a row that exists rather than needing a table of its own.
    Three properties that a module-level dict would not have, and
    ``models.RateLimitEvent``'s docstring is the local precedent for
    caring: api and worker are separate containers and api may be
    several processes, so an in-memory window is per-process, resets on
    every deploy, and lets a caller multiply its allowance by the number
    of workers.

    It is also deliberately not a new table. This chain admits one
    revision author at a time and a debounce is not worth the slot;
    ``audit_log`` indexes ``entity`` and ``actor_user_id``, and the
    query is bounded by ``created_at`` on top of those.

    Per **actor**, not per tenant and not installation-wide. An admin
    holding ``atrium_ddns.admin`` reaches every tenant's names, so a
    per-tenant key would let one admin fan out once per tenant per
    cooldown; and an installation-wide key would let one operator's
    press block every other tenant's own board.
    """
    return (
        await session.execute(
            select(sa.func.max(AuditLog.created_at)).where(
                AuditLog.entity == AUDIT_ENTITY_HEALTH_CHECK,
                AuditLog.action == AUDIT_ACTION_RUN,
                AuditLog.actor_user_id == actor_user_id,
            )
        )
    ).scalar_one_or_none()


#: The ``audit_log.entity`` these two routes write. Not ``ddns_hostname``
#: — the action is about the check, not about one row — and not
#: ``ddns_domain``, which would put operator actions in a zone's history.
AUDIT_ENTITY_HEALTH_CHECK = "ddns_health_check"
AUDIT_ACTION_RUN = "run"
AUDIT_ACTION_CLEAR = "clear"

# `audit_log.entity_id` is NOT NULL and these two actions name no row.
# The actor's own id is written instead of a magic `0`: it is true, it is
# the thing the debounce keys on, and a sentinel in a
# foreign-key-shaped column is how a later reader joins it to the wrong
# table.

#: Spelled through ``fastapi.status`` rather than as a literal, the same
#: way :data:`UNPROCESSABLE`'s neighbours are.
TOO_MANY_REQUESTS = status.HTTP_429_TOO_MANY_REQUESTS


@router.post("/health-checks/run", response_model=HealthCheckRunOut)
async def run_health_checks_now(
    user: User = Depends(require_perm(BOARD_READ_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> HealthCheckRunOut:
    """Re-check every name this caller can see, now. Debounced per actor.

    Reach is the **scope's**, not a parameter: a tenant re-checks their
    own names and a holder of ``atrium_ddns.admin`` re-checks every
    tenant's, because that is what the scope already means everywhere
    else on this surface. Gated on ``atrium_ddns.device.manage`` — the
    board's own permission — rather than on ``atrium_ddns.admin``, for
    the reason #14 recorded about the log: *reading your own* and
    *reading everyone's* are different reaches, and collapsing them into
    one permission takes the surface away from the people it was built
    for. A caller with no tenant identity gets the scope's third state,
    a literal ``false``, and checks nothing.

    **Bounded by the scheduled path's own settings.** ``batch_size``,
    ``concurrency`` and ``timeout`` come from the same ``atrium_ddns``
    namespace the worker reads on every tick; nothing here is a second
    knob. The honest consequence, stated rather than discovered: this is
    a synchronous request that resolves up to ``health_check_batch_size``
    names × two record types at ``health_check_concurrency``, so on a
    large estate whose nameservers are all timing out it is a long
    request, and ``truncated`` in the response says when the ceiling was
    the thing that stopped it.

    The debounce, and why the claim is committed first
    --------------------------------------------------
    The audit row is written **and committed before the fan-out starts**,
    not after it. A debounce that records the run on the way out is not a
    debounce: two requests that arrive together both read "nothing
    recent", both start, and the limit is enforced only against a caller
    slow enough to wait. Claim first, then work.

    A consequence worth naming: a run that then fails still consumed its
    cooldown. That is the correct direction for a control whose purpose
    is to bound outbound DNS traffic — the queries were sent either way.
    """
    config = await load_config(session)
    now = datetime.now(UTC).replace(tzinfo=None)

    remaining = _cooldown_remaining(
        await _last_manual_run(session, user.id),
        config.health_check_manual_cooldown_seconds,
        now,
    )
    if remaining:
        raise HTTPException(
            status_code=TOO_MANY_REQUESTS,
            detail=(
                f"a manual health check was run less than "
                f"{config.health_check_manual_cooldown_seconds}s ago; "
                f"{remaining}s remaining. This button resolves every name in "
                f"reach against real nameservers, so it is debounced per "
                f"operator. The scheduled check keeps running regardless."
            ),
            headers={"Retry-After": str(remaining)},
        )

    await record_audit(
        session,
        actor_user_id=user.id,
        entity=AUDIT_ENTITY_HEALTH_CHECK,
        entity_id=user.id,
        action=AUDIT_ACTION_RUN,
        diff={"cross_tenant": scope.reaches_all_tenants(Hostname)},
    )
    await session.commit()

    summary = await run_health_checks(scope=scope, due_only=False)
    return HealthCheckRunOut(
        enabled=summary.enabled,
        forced=summary.forced,
        hostnames_considered=summary.hostnames_considered,
        hostnames_never_written=summary.hostnames_never_written,
        hostnames_checked=summary.hostnames_checked,
        records_checked=summary.records_checked,
        ok=summary.ok,
        mismatch=summary.mismatch,
        missing=summary.missing,
        error=summary.error,
        transitions=summary.transitions,
        truncated=summary.truncated,
        batch_size=summary.batch_size,
    )


@router.post("/health-checks/clear", response_model=HealthCheckClearOut)
async def clear_health_checks_now(
    user: User = Depends(require_perm(BOARD_READ_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> HealthCheckClearOut:
    """Reset every health-check result in reach to ``never_checked``.

    The legacy ``POST /admin/health-checks/clear``. What it is *for*: a
    board carrying stale ``error`` and ``mismatch`` verdicts from an
    outage that is over reads as a broken estate until the next
    scheduled sweep replaces each one. Clearing says *we do not know
    yet*, which is a true statement and a different one from *it is
    wrong*.

    **Not rate-limited, and the asymmetry is the point.** ``run`` fans
    out to other people's nameservers; this is one scoped ``UPDATE``
    against our own database. Debouncing it would be a control with no
    cost to bound.

    It writes ``NULL`` into all four columns — see
    :func:`~atrium_ddns.worker_jobs.clear_health_check_results` for why
    ``NEVER_CHECKED`` and not ``MISSING`` is the honest reset — and it
    deletes nothing. No hostname, no published address, and in
    particular no ``ddns_event`` row: the log has its own retention and
    the route that cleared *it* is the one the operator struck.
    """
    summary = await clear_health_check_results(scope=scope)
    await record_audit(
        session,
        actor_user_id=user.id,
        entity=AUDIT_ENTITY_HEALTH_CHECK,
        entity_id=user.id,
        action=AUDIT_ACTION_CLEAR,
        diff={
            "cleared": summary.cleared,
            "in_scope": summary.in_scope,
            "cross_tenant": scope.reaches_all_tenants(Hostname),
        },
    )
    await session.commit()
    return HealthCheckClearOut(cleared=summary.cleared, in_scope=summary.in_scope)


# ===================================================================== #
# The tenant CRUD surface — domains, provider credentials, devices
#
# Everything below is #45. Three resources, one rule each that is not
# obvious from the code and each of which has a plausible-looking
# implementation that leaks:
#
# **A credential is never returned.** Not masked, not truncated, not
# "first four characters". ``providers/base.py`` already says why in one
# line — *"a prefix of an API token is still a disclosure and it is the
# shape people reach for first"* — and the response models below carry
# ``credentials_set``, a boolean, and nothing else. The listing endpoints
# do not decrypt at all: ``credentials_set`` is
# ``credentials_ct IS NOT NULL``, read off the column, so there is no
# code path from a GET to a plaintext. ``tests/test_router_tenant.py``
# proves that structurally (a sweep over the router's own route table)
# and behaviourally (the listing still answers 200 for a tenant whose
# encryption key has been *shredded* — an endpoint that decrypted would
# raise).
#
# **A device secret exists in cleartext at exactly two moments.** It is
# hashed, not encrypted (plan §3.2), so create and rotate are the only
# times it can be shown and there is no "show it to me again". A device
# whose row was migrated from the old service carries a **bcrypt** hash
# whose plaintext we never had, so it can never display one at all —
# :func:`credential_origin` is how the interface learns that, and the
# frontend says so rather than rendering an empty field that reads as a
# bug.
#
# **Credential updates use the blank-preserves rule**, through
# ``app.host_sdk.crypto.apply_secret_update`` and never by assigning:
# ``null`` clears, ``""`` preserves, anything else replaces. The failure
# mode of getting it wrong is the one worth restating — editing an
# unrelated field on the same form silently blanks a working provider
# credential, and it surfaces much later as a DNS failure with no
# obvious cause.
# ===================================================================== #

#: Manage your own zones and their provider bindings. Held by ``user``
#: and by ``admin``; seeded by the ``0002`` migration from
#: :data:`atrium_ddns.models.PERMISSIONS`.
DOMAIN_MANAGE_PERMISSION = "atrium_ddns.domain.manage"

#: Manage your own devices. Deliberately *the same string* as
#: :data:`BOARD_READ_PERMISSION` rather than a second constant with the
#: same value: the board renders devices and this surface edits them,
#: and a reader who can see a device must be able to find the page that
#: created it.
DEVICE_MANAGE_PERMISSION = BOARD_READ_PERMISSION

#: The wire value that means *preserve the stored secret*. Named because
#: it is the one value on this surface whose meaning is not what it
#: looks like: an empty string here is not "empty", it is "unchanged".
#: The frontend must never send it as the result of a user clearing a
#: text box — ``frontend/src/api/credentials.ts`` is the other half of
#: that contract and asserts it.
PRESERVE = ""

#: How long a freshly issued device secret is, in bytes of entropy.
#: ``token_urlsafe(32)`` renders 43 characters, comfortably under
#: bcrypt's 72-**byte** ceiling (``auth_device._BCRYPT_MAX_BYTES``) so a
#: secret issued today still verifies if a future row is ever stored
#: under bcrypt rather than argon2id.
SECRET_ENTROPY_BYTES = 32

#: Bytes of entropy in a generated device username. See
#: :func:`_generate_username` for why the client does not get to pick.
USERNAME_ENTROPY_BYTES = 6

#: 422, spelled as the number.
#:
#: Starlette renamed ``HTTP_422_UNPROCESSABLE_ENTITY`` to
#: ``HTTP_422_UNPROCESSABLE_CONTENT`` and deprecated the old name; this
#: image emits a ``StarletteDeprecationWarning`` for every use of it and
#: a future one will remove it, while an older Starlette does not have
#: the new name at all. The status code is the stable thing — it is what
#: the wire carries and what the tests assert on — so it is written
#: here once rather than being a dependency-version question at eight
#: call sites.
UNPROCESSABLE = 422


class CredentialOrigin(str, Enum):
    """Where a device's stored hash came from — and therefore whether a
    plaintext ever existed on our side.

    This is the *only* thing a read tells a caller about a device
    secret, and it exists because the alternative is worse. A migrated
    device's secret was hashed by the old service with bcrypt; we have
    never held its plaintext and never will. An interface that rendered
    an empty "secret" field for it would be saying *something is missing
    here*, when the truth is *nothing was ever here and rotating is the
    only way to get one*.
    """

    #: Hashed by us, by the hasher this build issues with. The plaintext
    #: existed once, at create or at rotate, and was shown once.
    ISSUED = "issued"
    #: Recognised by one of the verifying hashers, but not the issuing
    #: one — i.e. bcrypt, i.e. a row migrated from the old service.
    #: There is no plaintext to show and there never was.
    MIGRATED = "migrated"
    #: No hasher recognises the stored value. A truncated column, or a
    #: row written by something else. It cannot authenticate either —
    #: ``auth_device._verify_sync`` answers ``badauth`` for it — so the
    #: interface says so rather than implying the device works.
    UNRECOGNISED = "unrecognised"


def credential_origin(password_hash: str | None) -> CredentialOrigin:
    """Classify a stored hash, by asking the hashers that verify it.

    Derived rather than pattern-matched. The obvious implementation is
    ``hash.startswith("$argon2")``, and it is the identical defect the
    moment ``auth_device._PASSWORD_HASH`` gains a hasher or reorders —
    the tuple's *first* element is what
    :meth:`~pwdlib.PasswordHash.verify_and_update` re-hashes into, so
    "issued by us" is by definition "identified by the current hasher"
    and nothing else. Reordering that tuple therefore moves this
    classification with it, which is the property a prefix test cannot
    have.

    Reading ``auth_device``'s module-level tuple directly (rather than
    building a second ``PasswordHash``) is deliberate for the same
    reason: two instances configured separately are two sources of truth
    about which hasher issues.
    """
    from .auth_device import _PASSWORD_HASH

    if not password_hash:
        return CredentialOrigin.UNRECOGNISED
    if _PASSWORD_HASH.current_hasher.identify(password_hash):
        return CredentialOrigin.ISSUED
    for hasher in _PASSWORD_HASH.hashers:
        if hasher.identify(password_hash):
            return CredentialOrigin.MIGRATED
    return CredentialOrigin.UNRECOGNISED


# --------------------------------------------------------------------- #
# The provider catalogue — what a credential form is allowed to ask for
# --------------------------------------------------------------------- #


class ProviderOut(BaseModel):
    """One DNS provider this build ships, and its field list.

    Shipped to the browser so the credential form is **derived from the
    provider classes** rather than from a field list retyped in
    TypeScript. A provider deleted from ``providers._PROVIDERS`` takes
    its form with it; one whose ``REQUIRED_CREDENTIALS`` gains a key
    grows the field the next time the page loads. A hardcoded field list
    in the frontend is the same defect one release later.
    """

    #: The canonical ``backend_type`` a new row should store. Aliases
    #: (``aws`` for route53) are resolvable but are not offered — a new
    #: row minted under an alias would be a second spelling of one
    #: provider inside a ``UNIQUE(domain_id, backend_type)`` constraint.
    service: str
    #: ``BaseProvider.REQUIRED_CREDENTIALS`` — every key that must be
    #: present and non-empty before the adapter will talk to the
    #: provider at all. These go in the **encrypted** column.
    credential_keys: list[str]


class ProviderCatalogueOut(BaseModel):
    providers: list[ProviderOut]


def _credential_keys(backend_type: str) -> tuple[str, ...]:
    """``REQUIRED_CREDENTIALS`` for a stored ``backend_type``, or ``()``.

    ``()`` for a service nobody claims. That is a real state — the
    compat table's ``unknownsvc`` row is exactly it — and it means
    "no opinion", not "no secrets": the guards below that consume this
    are therefore written so an empty tuple *relaxes* nothing that
    matters. See :func:`_reject_secrets_in_config`.
    """
    cls = provider_class(backend_type)
    return () if cls is None else tuple(cls.REQUIRED_CREDENTIALS)


def _all_secret_keys() -> frozenset[str]:
    """Every key any shipped provider treats as a secret.

    The union across providers, so a value that is secret *somewhere*
    cannot be written into the plaintext ``config`` column *anywhere*.

    Computed at call time rather than snapshotted at import:
    ``providers.register`` is a supported seam (#16's scripted stubs use
    it), and a constant taken at import would miss anything registered
    afterwards — a guard that silently stops covering the thing it was
    written for.
    """
    return frozenset(
        key for service in known_services() for key in _credential_keys(service)
    )


def _reject_secrets_in_config(config: dict[str, Any] | None) -> None:
    """Refuse a plaintext ``config`` that carries a credential key.

    ``ddns_domain_backend.config`` is a plain JSON column with no
    encryption on it, and ``nsupdate`` already logs
    ``provider.secret_in_plaintext_config`` when it finds
    ``nsupdate_secret`` there — i.e. the shape is known to happen and
    the current handling is to notice it *after* it has been stored.
    This refuses it at the door instead.

    Checked against the union across every provider rather than against
    this row's own provider: a form that posts ``aws_secret_access_key``
    into an ``nsupdate`` row's config has still written an AWS key into
    a plaintext column, and "wrong provider" is not a reason to keep it.
    """
    if not config:
        return
    offending = sorted(set(config) & _all_secret_keys())
    if offending:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            # Names the keys, never the values. A refusal that echoed
            # the value would put the credential in the response body,
            # in the browser's network tab, and in any log that keeps
            # response bodies — which is the disclosure this guard
            # exists to prevent, committed by the guard itself.
            detail=(
                f"config carries credential key(s) {offending}; `config` is a "
                f"plaintext column. Send these under `credentials`, which is "
                f"encrypted per-user."
            ),
        )


def _require_complete_credentials(
    backend_type: str, credentials: dict[str, str]
) -> None:
    """Every ``REQUIRED_CREDENTIALS`` key present, when replacing.

    Enforced only on a *replacement*, never on a preserve: the whole
    point of blank-preserves is that a form editing ``config`` does not
    have to resend the secret, so demanding a complete set on every
    PATCH would defeat it.

    The rule is the provider's own — ``BaseProvider.has_credentials``
    returns False unless every key is present and truthy, and a backend
    that fails it answers ``dnserr`` per hostname. Storing a partial set
    is therefore storing a row that cannot work, which is worth a 422
    now rather than a DNS failure later.
    """
    required = set(_credential_keys(backend_type))
    if not required:
        return
    missing = sorted(required - set(credentials))
    if missing:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=(
                f"{backend_type} needs {sorted(required)}; missing {missing}. "
                f"A partial credential set stores a backend that answers "
                f"dnserr on every update."
            ),
        )


@router.get("/providers", response_model=ProviderCatalogueOut)
async def list_providers(
    _user: User = Depends(require_perm(DOMAIN_MANAGE_PERMISSION)),
) -> ProviderCatalogueOut:
    """The services a new backend row may name, and their field lists.

    No database and no tenancy: this is a property of the build, not of
    a tenant. It is still gated, because the set of providers an
    installation ships is not something an unauthenticated caller needs.
    """
    return ProviderCatalogueOut(
        providers=[
            ProviderOut(
                service=service, credential_keys=list(_credential_keys(service))
            )
            for service in known_services()
        ]
    )


# --------------------------------------------------------------------- #
# Wire models
# --------------------------------------------------------------------- #

#: The three states an inbound secret may be in, *after* validation.
#: Spelled as a type so the *absence* of the field and the value ``""``
#: are the same thing — which is the shape ``apply_secret_update``'s
#: docstring names.
CredentialsIn = dict[str, str] | Literal[""] | None


class _CredentialsField(BaseModel):
    """Mixin carrying the one field with the blank-preserves rule.

    **The field is typed ``Any``, and that is a security decision rather
    than laziness.** It was found by writing the obvious version first
    (``dict[str, str] | Literal[""] | None`` with a
    ``@field_validator``) and reading what the refusal actually
    returned:

        POST … {"credentials": {"aws_access_key_id": "AKIA…",
                                "aws_secret_access_key": ""}}
        422 {"detail":[{…,"input":{"aws_access_key_id":"AKIA…",
                                   "aws_secret_access_key":""}}]}

    FastAPI's ``RequestValidationError`` handler serialises
    ``exc.errors()``, and every entry carries ``input`` — **the value
    that failed**. So any Pydantic-level rejection of this field puts
    the submitted credential in the response body, in the browser's
    network tab, and in anything that logs response bodies. A guard
    committing the disclosure it exists to prevent, which is the exact
    shape ``docs/ops/overnight-template.md`` catalogues under
    *assertions on the report*.

    Typing the field ``Any`` means **no request-validation path can
    reject it**, so there is no Pydantic error carrying it. Every
    content rule moves into :func:`_coerce_credentials`, which raises an
    ``HTTPException`` whose detail names **keys** and never values.
    ``tests/test_router_tenant.py`` asserts that across every malformed
    shape, including the three that used to echo.
    """

    #: ``null`` clears · ``""`` (and absent) preserves · an object
    #: replaces. Defaulting to :data:`PRESERVE` is what makes "absent"
    #: mean "preserve" without the handler having to inspect
    #: ``model_fields_set``. Validated by :func:`_coerce_credentials`,
    #: never by Pydantic — see the class docstring.
    credentials: Any = PRESERVE


def _coerce_credentials(value: Any) -> CredentialsIn:
    """Validate an inbound ``credentials`` value without ever echoing it.

    Four refusals, and every message names keys or types only:

    - a value that is neither an object, ``""`` nor ``null`` — the wire
      has exactly three spellings and inventing a fourth is a client
      bug;
    - ``{}``, which is ambiguous between *clear* and *preserve* when the
      wire already has an unambiguous spelling for each. Refusing beats
      picking one;
    - a non-string value inside the object;
    - a blank string inside the object — a text box the user cleared.
      Stored, it replaces a working credential with a blank one: the
      exact failure blank-preserves exists to prevent, arriving one
      level below the field that guards it.
    """
    if value is None or value == PRESERVE:
        return None if value is None else PRESERVE
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=(
                f"credentials must be an object, \"\" (preserve) or null "
                f"(clear); got {type(value).__name__}"
            ),
        )
    if not value:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=(
                "credentials: {} is ambiguous. Send null to clear the stored "
                'credential, or "" (or omit the field) to preserve it.'
            ),
        )
    non_string = sorted(k for k, v in value.items() if not isinstance(v, str))
    if non_string:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=f"credentials: non-string value for {non_string}",
        )
    blank = sorted(key for key, item in value.items() if not item.strip())
    if blank:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=(
                f"credentials: blank value for {blank}. A cleared text box is "
                f'not a credential — omit the field or send "" to preserve the '
                f"stored one, or send null to clear it."
            ),
        )
    return dict(value)


class DomainBackendOut(BaseModel):
    """One provider binding, with **no credential material on it**.

    ``credentials_set`` is the only thing this says about the secret,
    and it is read off ``credentials_ct IS NOT NULL`` without
    decrypting. Not a mask, not a length, not a prefix: a boolean, which
    is the largest amount of information about a stored credential that
    is safe to publish and is also all a form needs in order to say
    *"leave blank to keep the stored one"*.
    """

    id: int
    domain_id: int
    backend_type: str
    #: Non-secret provider settings — hosted-zone id, TTL, nameserver.
    #: :func:`_reject_secrets_in_config` is what keeps that true.
    config: dict[str, Any]
    #: Ciphertext present. **Never** the ciphertext, and never anything
    #: derived from the plaintext.
    credentials_set: bool
    #: Whether ``backend_type`` resolves to an adapter in this build. A
    #: migrated row may name a service this build does not ship, and the
    #: interface should say *"this build has no adapter for it"* rather
    #: than rendering it as though it worked. The wire half is ``911``.
    known_service: bool
    #: This provider's credential keys, so a form can render the right
    #: fields for an existing row without a second request. Empty for an
    #: unknown service, which is the same "no opinion" state.
    credential_keys: list[str]


class DomainOut(BaseModel):
    id: int
    name: str
    created_at: str
    backends: list[DomainBackendOut]
    #: The count, not the names. The board is where hostnames are read;
    #: this surface is about the zone and its provider bindings, and a
    #: nested list here would be a second rendering of the board's data
    #: that could disagree with it.
    hostname_count: int


class DomainCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=253)


class DomainRenameIn(BaseModel):
    """The body of ``PATCH /domains/{id}`` — the legacy rename (#75).

    One field, and the operation is *rename the zone*. Ownership is not
    spellable here for the reason :func:`create_domain` gives about its
    own body: a ``user_id`` in a payload is a way to write into another
    tenant's account through an endpoint whose read side is correctly
    scoped.

    **The rename does not rewrite hostnames, and the refusal is the
    feature.** The two options were reject-or-rewrite and this model
    picks reject; :func:`rename_domain` argues it, and
    ``tests/test_router_hostnames.py`` asserts it in both directions.

    ``PUT`` stays ``405`` deliberately. Every mutation on this surface
    that changes part of a row is a ``PATCH``
    (``/backends/{id}``, ``/hostnames/{id}``); a ``PUT`` alias would be a
    second spelling of one operation, and the first thing a second
    spelling grows is a divergent validation path.
    """

    name: str = Field(min_length=1, max_length=253)


class DomainBackendCreateIn(_CredentialsField):
    backend_type: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)


class DomainBackendUpdateIn(_CredentialsField):
    """``config`` absent means *leave it alone*; ``credentials`` absent
    means *preserve*.

    Two different spellings of "unchanged" for two fields, and that is
    deliberate rather than untidy: ``config`` is plaintext and can be
    read back, so ``None`` can safely mean "not supplied". A secret
    cannot be read back, so its "not supplied" needs a value that is not
    ``None`` — because ``None`` is the only unambiguous way to say
    *clear it*.
    """

    config: dict[str, Any] | None = None


class DeviceSummaryOut(BaseModel):
    """A device, as a *read* can describe it.

    There is no secret on this model and there is no field that could
    hold one. That is asserted structurally in
    ``tests/test_router_tenant.py`` by sweeping the router's own route
    table, so an endpoint added later that returns the wrong model fails
    the sweep rather than a review.
    """

    id: int
    name: str
    #: The HTTP Basic username the router sends. Not a secret — it is
    #: half of a credential pair and is useless without the other half,
    #: and the owner has to be able to read it back to configure a
    #: replacement router.
    username: str
    created_at: str
    last_seen_at: str | None
    #: ``NULL`` means *inherit the namespace default*, which is not the
    #: same as ``0`` (may never call). Two states, carried as two.
    rate_limit_per_minute: int | None
    #: What the limiter will actually allow this device, with ``NULL``
    #: already resolved against the namespace default.
    #:
    #: Computed by :func:`~atrium_ddns.auth_device.effective_rate_limit`
    #: — *the same function* ``/nic/update`` calls on the request path,
    #: not a second reading of the two columns. A browser that resolved
    #: ``NULL`` itself would need the namespace default, which a plain
    #: tenant cannot read (it is behind ``app_setting.manage``), so it
    #: would either show nothing or invent one. Shipping the resolved
    #: value means the number on the screen is the number the limiter
    #: enforces, by construction.
    effective_rate_limit_per_minute: int
    #: :class:`CredentialOrigin`. The one thing a read says about the
    #: secret, and the reason a migrated device can be described
    #: honestly instead of rendered as an empty field.
    credential_origin: CredentialOrigin
    hostname_count: int


class DeviceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    rate_limit_per_minute: int | None = Field(default=None, ge=0)


class DeviceUpdateIn(BaseModel):
    """The per-device rate limit, and deliberately nothing else.

    ``rate_limit_per_minute`` is **required**, and that is not an
    oversight. ``None`` is a *value* on this field — it means *inherit
    the installation default* — so a body that omits the key and a body
    that sends ``null`` would be indistinguishable under a default of
    ``None``, and one of the two readings silently un-mutes a device an
    operator muted on purpose. Required means the omission is a 422 the
    caller can read instead of a mutation nobody asked for.

    There is no ``name`` here and no secret. Renaming is a separate
    change (the create path has a uniqueness conflict to handle and this
    route does not), and the secret is the point of the route: #73 exists
    because tightening an abusive device's limit meant delete-and-
    recreate, which rotates the credential and breaks the device until
    its owner reconfigures it. This model has no field that could carry
    one and ``tests/test_router_tenant.py`` compares the stored hash
    across the call.
    """

    #: ``0`` mutes the device; ``null`` returns it to the installation
    #: default. ``ge=0`` and no upper bound of its own — the namespace's
    #: ``le=10_000`` bounds the default, and a per-device override that
    #: could not exceed it would be a rule nobody wrote down.
    rate_limit_per_minute: int | None = Field(ge=0)


class DeviceSecretOut(BaseModel):
    """A device **and its secret** — the only model on this surface that
    carries one, returned by exactly two routes.

    The secret is here once. It is hashed on the way into the database
    (argon2id) and there is no endpoint, no admin screen and no support
    procedure that can produce it again; rotating issues a *new* one and
    invalidates this. ``tests/test_router_tenant.py`` proves the second
    read does not carry it, and proves it non-vacuously by checking that
    the new secret verifies against the stored hash and the old one does
    not — otherwise "rotate returns a random string" would pass.
    """

    device: DeviceSummaryOut
    #: Cleartext, once. Never logged, never audited, never re-derivable.
    secret: str


# --------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------- #


def _normalise_zone(name: str) -> str:
    """Lower-cased, no trailing dot — the shape ``models.Domain`` stores.

    Done here rather than left to the caller because the UNIQUE index on
    ``ddns_domain.name`` is the only thing stopping ``Example.com`` and
    ``example.com`` becoming two zones, and an index cannot normalise.
    """
    return name.strip().rstrip(".").lower()


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _not_found(what: str) -> HTTPException:
    """404 for both *no such row* and *not yours*.

    :meth:`DdnsScope.get` already collapses the two, on purpose:
    distinguishing them tells a caller which ids exist. This helper
    exists so every handler spells the refusal the same way.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"no such {what}"
    )


async def _hostname_counts(
    session: AsyncSession, scope: DdnsScope
) -> tuple[dict[int, int], dict[int, int]]:
    """``(by_domain_id, by_device_id)`` — one scoped query, not N.

    Both counts come off the same scoped read of ``ddns_hostname``, so
    a hostname the scope hides is missing from both totals rather than
    from one of them. Counting them separately is how a domain's count
    and a device's count end up describing different populations.
    """
    rows = (
        await session.execute(
            scope.select(Hostname, Hostname.domain_id, Hostname.device_id)
        )
    ).all()
    by_domain: dict[int, int] = {}
    by_device: dict[int, int] = {}
    for domain_id, device_id in rows:
        by_domain[domain_id] = by_domain.get(domain_id, 0) + 1
        if device_id is not None:
            by_device[device_id] = by_device.get(device_id, 0) + 1
    return by_domain, by_device


def _render_backend(backend: DomainBackend) -> DomainBackendOut:
    """A backend row for the wire. **Touches no plaintext.**

    ``credentials_ct is not None`` and not ``backend.credentials is not
    None``: the second reads the ``UserSecret`` descriptor, which
    decrypts, which needs an unlocked key, which would make this
    function raise for a locked tenant *and* would put the plaintext one
    attribute access away from a response model. The column is the
    right instrument for the question "is one stored".
    """
    return DomainBackendOut(
        id=backend.id,
        domain_id=backend.domain_id,
        backend_type=backend.backend_type,
        config=dict(backend.config or {}),
        credentials_set=backend.credentials_ct is not None,
        known_service=provider_class(backend.backend_type) is not None,
        credential_keys=list(_credential_keys(backend.backend_type)),
    )


def _render_device(
    device: Device, hostname_count: int, *, default_per_minute: int
) -> DeviceSummaryOut:
    """One device, with its limit resolved by the limiter's own function.

    ``default_per_minute`` is ``DdnsConfig.rate_limit_per_minute``, read
    from the namespace by the caller. It is a required keyword rather
    than an optional one: a default here would let a call site that
    forgot to read the config render an *effective* limit that is not
    the effective limit, and it would look right.
    """
    return DeviceSummaryOut(
        id=device.id,
        name=device.name,
        username=device.username,
        created_at=_iso(device.created_at) or "",
        last_seen_at=_iso(device.last_seen_at),
        rate_limit_per_minute=device.rate_limit_per_minute,
        effective_rate_limit_per_minute=effective_rate_limit(
            device, default_per_minute
        ),
        credential_origin=credential_origin(device.password_hash),
        hostname_count=hostname_count,
    )


# --------------------------------------------------------------------- #
# Domains
# --------------------------------------------------------------------- #


@router.get("/domains", response_model=list[DomainOut])
async def list_domains(
    _user: User = Depends(require_perm(DOMAIN_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> list[DomainOut]:
    """A tenant's zones, their provider bindings, and their name counts.

    Three scoped reads and no per-row query. Nothing here decrypts:
    see :func:`_render_backend`.
    """
    domains = list(
        (await session.execute(scope.select(Domain).order_by(Domain.name)))
        .scalars()
        .all()
    )
    backends = list(
        (
            await session.execute(
                scope.select(DomainBackend).order_by(DomainBackend.id)
            )
        )
        .scalars()
        .all()
    )
    by_domain_hostnames, _ = await _hostname_counts(session, scope)

    grouped: dict[int, list[DomainBackendOut]] = {}
    for backend in backends:
        grouped.setdefault(backend.domain_id, []).append(_render_backend(backend))

    return [
        DomainOut(
            id=domain.id,
            name=domain.name,
            created_at=_iso(domain.created_at) or "",
            backends=grouped.get(domain.id, []),
            hostname_count=by_domain_hostnames.get(domain.id, 0),
        )
        for domain in domains
    ]


@router.post("/domains", response_model=DomainOut, status_code=201)
async def create_domain(
    body: DomainCreateIn,
    user: User = Depends(require_perm(DOMAIN_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> DomainOut:
    """Claim a zone for the calling tenant.

    **The owner is the caller, never a parameter.** A ``user_id`` in the
    body would be a way to write rows into another tenant's account
    through an endpoint whose *read* side is correctly scoped, which is
    the asymmetry a scope cannot see.

    ``ddns_domain.name`` is globally unique — DNS is global, and two
    tenants claiming one zone is a conflict the database refuses rather
    than a state the UI has to explain. The 409 below is therefore
    reachable for a name *another* tenant owns, and it deliberately says
    nothing about who. "Already claimed" and "already claimed by you"
    are the same sentence here on purpose.
    """
    if scope.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this caller has no tenant identity and cannot own a zone",
        )
    name = _normalise_zone(body.name)
    if not name:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail="a zone name cannot be empty once normalised",
        )

    domain = Domain(user_id=scope.user_id, name=name)
    session.add(domain)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _conflict(f"the zone {name} is already claimed") from exc

    # `created_at` is a server default, so the INSERT does not bring it
    # back and reading it would be a lazy load — which in an async
    # session is a `MissingGreenlet`, not a slow query. Awaited here,
    # where it is an ordinary read, rather than at attribute access.
    await session.refresh(domain)

    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_domain",
        entity_id=domain.id,
        action="create",
        diff={"name": name},
    )
    await session.commit()
    return DomainOut(
        id=domain.id,
        name=domain.name,
        created_at=_iso(domain.created_at) or "",
        backends=[],
        hostname_count=0,
    )


#: How many orphaned names a refusal names before it stops listing them.
#: The count is always exact; the list is a sample, and the message says
#: which is which. A refusal that printed 4,000 names would be unreadable
#: and one that printed none would be unactionable.
RENAME_ORPHAN_SAMPLE = 5


@router.patch("/domains/{domain_id}", response_model=DomainOut)
async def rename_domain(
    domain_id: int,
    body: DomainRenameIn,
    user: User = Depends(require_perm(DOMAIN_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> DomainOut:
    """Rename a zone. **Refuses rather than orphaning a name.**

    The legacy ``GET,POST /admin/domains/<id>`` (ui-parity §3.3 G4).
    Delete-and-recreate was never an equivalent — ``DELETE`` cascades and
    takes the hostnames and the provider bindings with it — so until this
    existed, correcting a typo in a zone name cost the tenant every name
    under it and every stored credential.

    Reject or rewrite: this rejects, and here is the argument
    ---------------------------------------------------------
    A rename is not a string swap. Every ``ddns_hostname`` under the zone
    has to satisfy :func:`zone_contains` afterwards or it becomes a row
    that ``POST /hostnames`` would refuse to create and ``/nic/update``
    answers ``nohost`` for — creatable once, updatable never. There were
    two ways to keep that true, and only one of them is available here:

    - **Rewrite the names transactionally**, replacing the old zone
      suffix on each. This is *already decided against*, one model along:
      :class:`HostnameAssignIn` says in as many words that a hostname's
      name is not editable, because *"a hostname is the DNS name — it is
      what /nic/update looks the row up by and what a provider has
      already published a record under. Renaming the row would leave the
      old record in the zone with nothing pointing at it and no way to
      reach it again."* A rename that rewrote names would do exactly that
      to every name at once, silently, as a side effect of correcting a
      spelling — and it would do it to records already published at a
      provider this endpoint is not going to call.
    - **Refuse the rename that would orphan a name.** The tenant keeps
      the choice the model already gives them: delete the affected names
      deliberately, seeing what they are giving up, and recreate them.

    So this endpoint refuses, with ``409`` and the count of names that
    would be orphaned. A rename to a zone that still contains every
    existing name — narrowing ``example.invalid`` to
    ``sub.example.invalid`` when every name is already under ``sub`` — is
    allowed and rewrites nothing, because there is nothing to rewrite.
    A zone with no names is renameable to anything free.

    The containment test is :func:`zone_contains`, the **same function
    object** ``/nic/update`` and ``POST /hostnames`` reach, with all
    three of its preserved legacy quirks. A second copy here would agree
    on the day it was written and diverge silently afterwards, and the
    direction it diverges in decides whether a tenant ends up with names
    the wire cannot update.

    ``409`` twice, for two different conflicts
    ------------------------------------------
    A name another tenant already claims and a rename that would orphan
    names are both ``409`` — both are refusals about rows that exist
    rather than about a malformed body — and the details say which. The
    first deliberately says nothing about *who* holds the name, for the
    reason :func:`create_domain` records.
    """
    domain = await scope.get(session, Domain, domain_id)
    if domain is None:
        raise _not_found("domain")

    name = _normalise_zone(body.name)
    if not name:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail="a zone name cannot be empty once normalised",
        )

    previous = domain.name
    if name == previous:
        # Not an error: a form that submits an unchanged field is a
        # normal thing for a form to do. It writes nothing and audits
        # nothing, so an unchanged submit cannot manufacture history.
        return await _render_domain(session, scope, domain)

    # The names under this zone, read through the scope. Not
    # `domain.hostnames`: a relationship load here is a lazy load in an
    # async session, and the collection would not carry the scope.
    names = list(
        (
            await session.execute(
                scope.select(Hostname, Hostname.name)
                .where(Hostname.domain_id == domain.id)
                .order_by(Hostname.name)
            )
        )
        .scalars()
        .all()
    )
    orphans = [existing for existing in names if not zone_contains(name, existing)]
    if orphans:
        sample = ", ".join(repr(o) for o in orphans[:RENAME_ORPHAN_SAMPLE])
        more = len(orphans) - min(len(orphans), RENAME_ORPHAN_SAMPLE)
        raise _conflict(
            f"renaming {previous!r} to {name!r} would leave "
            f"{len(orphans)} of {len(names)} hostname"
            f"{'' if len(orphans) == 1 else 's'} outside the zone: {sample}"
            + (f" and {more} more" if more else "")
            + ". /nic/update answers nohost for a hostname outside its "
            "domain's zone, so those rows would exist and never be "
            "updatable again. The rename is refused rather than rewriting "
            "them: a hostname is the DNS name a provider has already "
            "published a record under. Delete the names you no longer "
            "want, then rename."
        )

    domain.name = name
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _conflict(f"the zone {name} is already claimed") from exc

    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_domain",
        entity_id=domain.id,
        action="rename",
        # Both sides. An audit row carrying only the new value cannot
        # answer "what was it before", which is the only question anyone
        # reads a rename audit for.
        diff={"name": {"from": previous, "to": name}, "hostnames": len(names)},
    )
    await session.commit()
    return await _render_domain(session, scope, domain)


async def _render_domain(
    session: AsyncSession, scope: DdnsScope, domain: Domain
) -> DomainOut:
    """One zone in :class:`DomainOut`'s shape, backends and count included.

    Shares :func:`_render_backend` and :func:`_hostname_counts` with
    :func:`list_domains` rather than assembling a second, thinner
    rendering — a single-row shape that drifts from the list's is how a
    page ends up showing different facts before and after an edit.
    """
    backends = list(
        (
            await session.execute(
                scope.select(DomainBackend)
                .where(DomainBackend.domain_id == domain.id)
                .order_by(DomainBackend.id)
            )
        )
        .scalars()
        .all()
    )
    by_domain, _ = await _hostname_counts(session, scope)
    return DomainOut(
        id=domain.id,
        name=domain.name,
        created_at=_iso(domain.created_at) or "",
        backends=[_render_backend(backend) for backend in backends],
        hostname_count=by_domain.get(domain.id, 0),
    )


@router.delete("/domains/{domain_id}", status_code=204)
async def delete_domain(
    domain_id: int,
    user: User = Depends(require_perm(DOMAIN_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Destroy a zone, its provider bindings and its hostnames.

    ``scope.get`` and not ``session.get``: the latter consults the
    identity map, takes no ``WHERE`` clause and would happily delete
    another tenant's zone.

    The cascade is the ORM's (``cascade="all, delete-orphan"``), so the
    encrypted credential rows go with it. The tenant's *key* is not
    shredded — that belongs to account deletion, not to dropping one
    zone, and shredding here would destroy every other zone's
    credentials as a side effect.
    """
    domain = await scope.get(session, Domain, domain_id)
    if domain is None:
        raise _not_found("domain")
    name = domain.name
    await session.delete(domain)
    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_domain",
        entity_id=domain_id,
        action="delete",
        diff={"name": name},
    )
    await session.commit()


# --------------------------------------------------------------------- #
# Provider credentials
# --------------------------------------------------------------------- #


async def _apply_credentials(
    session: AsyncSession,
    backend: DomainBackend,
    incoming: CredentialsIn,
) -> bool:
    """The blank-preserves rule, applied through atrium's own helper.

    ``apply_secret_update(obj, field, incoming)`` and **not** an
    assignment. The three directions are its three branches — ``None``
    clears, ``""`` preserves, anything else replaces — and it returns
    whether the attribute was touched.

    The unlock is conditional on there being something to encrypt.
    Unwrapping a per-user key is a database read; doing it on a preserve
    would be IO for a no-op, and doing it on a clear would mint a key
    for a tenant who has just told us they store nothing.
    ``create=True`` is correct on the replace path and only there: this
    may be the tenant's first encrypted value, and refusing to mint
    their key would make "add a credential" fail for every new account.

    Why ``flag_dirty`` — an upstream defect, measured
    -------------------------------------------------
    ``UserSecret.__set__`` writes the plaintext into a private slot in
    ``instance.__dict__`` and returns; the mapped column is untouched
    until the ``before_flush`` hook (``_encrypt_pending_user_secrets``)
    runs. But that hook iterates ``session.new`` and ``session.dirty``,
    and a write to ``instance.__dict__`` **does not make the row
    dirty**. So setting a credential on a persistent row that is not
    otherwise modified is a *silent no-op*: the commit succeeds, the
    response is 200, and the stored ciphertext is the old one.

    Measured against atrium 0.28 in this image, three cases, one
    throwaway table::

        A. set on a NEW object            -> STORED
        B. set on a CLEAN persistent row  -> 'set-on-new'   (discarded)
        C. set alongside a column change  -> stored

    Case B is precisely "the user changed only the credential", which is
    the most common reason to open a credential form at all. It is also
    the mirror image of the blank-preserves hazard: instead of blanking
    a credential nobody meant to touch, it fails to replace one somebody
    did, and the router keeps authenticating with the old provider key
    until someone notices. Nothing in the response can tell you.

    ``flag_dirty`` is the fix, and it is the primitive SQLAlchemy
    documents for exactly this: *"mark an instance as dirty without any
    specific attribute mentioned … to allow the object to travel through
    the flush process for interception by events such as
    before_flush"*. It emits no SQL of its own — the hook then sets
    ``credentials_ct`` for real, which is what makes the UPDATE happen.

    ``flag_modified(backend, "credentials_ct")`` was the first attempt
    and it is **wrong**: on a freshly inserted row the column was never
    assigned, so it is absent from the instance state and SQLAlchemy
    raises *"Can't flag attribute 'credentials_ct' modified; it's not
    present in the object state"*. Loading it first would be a lazy read
    on an async session, i.e. a ``MissingGreenlet``.

    Applied only when ``apply_secret_update`` reports a touch, so a
    preserve still writes nothing at all — which is the property
    ``test_blank_preserves_in_all_three_directions`` checks by
    comparing raw ciphertext bytes.
    """
    if isinstance(incoming, dict):
        await unlock_user_secrets(session, backend.user_id, create=True)
    touched = apply_secret_update(backend, "credentials", incoming)
    if touched:
        flag_dirty(backend)
    return touched


def _credential_audit(incoming: CredentialsIn, touched: bool) -> str:
    """What the audit row says happened to the secret. Never the secret.

    Three words for three outcomes, and ``preserved`` is the one worth
    having: an audit trail that recorded nothing for an untouched
    credential is indistinguishable from one written by a build that
    forgot to preserve it.
    """
    if incoming is None:
        return "cleared"
    if not touched:
        return "preserved"
    return "replaced"


@router.post(
    "/domains/{domain_id}/backends",
    response_model=DomainBackendOut,
    status_code=201,
)
async def create_backend(
    domain_id: int,
    body: DomainBackendCreateIn,
    user: User = Depends(require_perm(DOMAIN_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> DomainBackendOut:
    """Bind a provider to a zone, optionally with its credentials.

    **The row's ``user_id`` is the domain's, never the caller's**, and
    that is the single most consequential line in this handler. A caller
    holding ``atrium_ddns.admin`` reaches every tenant's zones, so
    taking the owner from the scope would encrypt the credential under
    the *administrator's* key: ``/nic/update`` unlocks the device
    owner's key and would fail to decrypt it, and
    :class:`~atrium_ddns.scope.DdnsScope` would hide the row from the
    tenant who owns the zone it hangs under. Two silent failures from
    one plausible line.

    ``backend_type`` is refused unless it names an adapter this build
    ships. The *column* stays a free string — migrated rows may carry a
    service nobody claims, and ``models.py`` says so — but a row minted
    through this endpoint could only ever answer ``911``, so refusing is
    the kinder answer.
    """
    domain = await scope.get(session, Domain, domain_id)
    if domain is None:
        raise _not_found("domain")

    if provider_class(body.backend_type) is None:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=(
                f"no adapter for {body.backend_type!r}; this build ships "
                f"{list(known_services())}"
            ),
        )
    _reject_secrets_in_config(body.config)
    credentials = _coerce_credentials(body.credentials)
    if isinstance(credentials, dict):
        _require_complete_credentials(body.backend_type, credentials)

    backend = DomainBackend(
        domain_id=domain.id,
        # From the row above, not from `scope.user_id`. See the docstring.
        user_id=domain.user_id,
        backend_type=body.backend_type,
        config=dict(body.config),
    )
    session.add(backend)

    # Flushed *before* the credential is applied, and deliberately so.
    # `unlock_user_secrets` is a database read that may itself flush, so
    # applying first would let the INSERT happen inside the unlock —
    # where this `except` cannot see the unique-constraint violation and
    # the 409 becomes a 500.
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _conflict(
            f"{domain.name} already has a {body.backend_type} backend"
        ) from exc

    touched = await _apply_credentials(session, backend, credentials)
    await session.flush()

    # `created_at` is a server default, so the INSERT does not bring it
    # back and reading it would be a lazy load — a `MissingGreenlet` in
    # an async session, not a slow query.
    await session.refresh(backend)

    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_domain_backend",
        entity_id=backend.id,
        action="create",
        # Key names, never values, and never the plaintext. The audit
        # helper redacts a `MaskedSecret` to `***` for exactly this
        # reason, but a raw `str` would sail straight through it into a
        # durable table — so the value never reaches the call.
        diff={
            "backend_type": body.backend_type,
            "config_keys": sorted(body.config),
            "credentials": _credential_audit(credentials, touched),
        },
    )
    await session.commit()
    return _render_backend(backend)


@router.patch("/backends/{backend_id}", response_model=DomainBackendOut)
async def update_backend(
    backend_id: int,
    body: DomainBackendUpdateIn,
    user: User = Depends(require_perm(DOMAIN_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> DomainBackendOut:
    """Edit a binding's settings, its credential, or one without the other.

    This is the endpoint the blank-preserves rule exists for. A form
    that changes a TTL posts ``{"config": {...}}`` and no
    ``credentials`` key; the default :data:`PRESERVE` makes that a
    no-op on the secret, and :func:`_apply_credentials` returns
    ``False`` so the ciphertext is not even re-encrypted. Re-encrypting
    the same plaintext would change the stored bytes — a new nonce every
    time — which is both a needless write and a misleading audit diff,
    and it would make "did this request touch the credential"
    unanswerable from the database.
    """
    backend = await scope.get(session, DomainBackend, backend_id)
    if backend is None:
        raise _not_found("backend")

    if body.config is not None:
        _reject_secrets_in_config(body.config)
    credentials = _coerce_credentials(body.credentials)
    if isinstance(credentials, dict):
        _require_complete_credentials(backend.backend_type, credentials)

    before_config = dict(backend.config or {})
    if body.config is not None:
        backend.config = dict(body.config)

    touched = await _apply_credentials(session, backend, credentials)

    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_domain_backend",
        entity_id=backend.id,
        action="update",
        diff={
            "config_keys": {
                "before": sorted(before_config),
                "after": sorted(backend.config or {}),
            },
            "credentials": _credential_audit(credentials, touched),
        },
    )
    await session.commit()
    await session.refresh(backend)
    return _render_backend(backend)


@router.delete("/backends/{backend_id}", status_code=204)
async def delete_backend(
    backend_id: int,
    user: User = Depends(require_perm(DOMAIN_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> None:
    backend = await scope.get(session, DomainBackend, backend_id)
    if backend is None:
        raise _not_found("backend")
    backend_type = backend.backend_type
    domain_id = backend.domain_id
    await session.delete(backend)
    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_domain_backend",
        entity_id=backend_id,
        action="delete",
        diff={"backend_type": backend_type, "domain_id": domain_id},
    )
    await session.commit()


# --------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------- #


def _generate_secret() -> str:
    return secrets_module.token_urlsafe(SECRET_ENTROPY_BYTES)


def _generate_username() -> str:
    """A device's HTTP Basic username, minted here rather than chosen.

    ``ddns_device.username`` is **globally unique** — it has to be, the
    lookup happens before there is any tenant to scope by
    (``auth_device`` says so). A client-chosen username on a globally
    unique column is an enumeration oracle: post a candidate, read the
    409, learn that some other tenant's router uses it. No scope can
    close that, because the global uniqueness *is* the point.

    So the API generates it. The ergonomic cost is nil — the username
    and the secret are handed over in the same response, at the one
    moment the user is configuring the router anyway — and the migration
    path (V1M4) writes legacy usernames directly to the table rather
    than through this endpoint.
    """
    return f"ddns-{secrets_module.token_hex(USERNAME_ENTROPY_BYTES)}"


@router.get("/devices", response_model=list[DeviceSummaryOut])
async def list_devices(
    _user: User = Depends(require_perm(DEVICE_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> list[DeviceSummaryOut]:
    """A tenant's devices. No secret, and no field that could hold one."""
    config = await load_config(session)
    devices = list(
        (await session.execute(scope.select(Device).order_by(Device.name)))
        .scalars()
        .all()
    )
    _, by_device = await _hostname_counts(session, scope)
    return [
        _render_device(
            device,
            by_device.get(device.id, 0),
            default_per_minute=config.rate_limit_per_minute,
        )
        for device in devices
    ]


@router.post("/devices", response_model=DeviceSecretOut, status_code=201)
async def create_device(
    body: DeviceCreateIn,
    user: User = Depends(require_perm(DEVICE_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> DeviceSecretOut:
    """Issue a device, and show its secret — once.

    The plaintext exists in this function and in this response and
    nowhere else. It is argon2id-hashed before the row is flushed, it is
    not logged, it is not audited, and there is no endpoint that can
    produce it again. The interface is responsible for saying so at the
    moment it is shown; the API is responsible for making that true.
    """
    if scope.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this caller has no tenant identity and cannot own a device",
        )

    config = await load_config(session)
    secret = _generate_secret()
    device = Device(
        user_id=scope.user_id,
        username=_generate_username(),
        password_hash=await hash_password(secret),
        name=body.name.strip(),
        rate_limit_per_minute=body.rate_limit_per_minute,
    )
    session.add(device)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _conflict(
            f"you already have a device called {body.name.strip()!r}"
        ) from exc

    await session.refresh(device)

    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_device",
        entity_id=device.id,
        action="create",
        # `credential` records the *kind*, never the value. The
        # plaintext is not passed to this call in any form.
        diff={
            "name": device.name,
            "username": device.username,
            "credential": credential_origin(device.password_hash).value,
        },
    )
    await session.commit()
    return DeviceSecretOut(
        device=_render_device(
            device, 0, default_per_minute=config.rate_limit_per_minute
        ),
        secret=secret,
    )


@router.post("/devices/{device_id}/rotate", response_model=DeviceSecretOut)
async def rotate_device_secret(
    device_id: int,
    user: User = Depends(require_perm(DEVICE_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> DeviceSecretOut:
    """Issue a new secret for an existing device, and show it once.

    The only route by which a *migrated* device ever acquires a secret
    anyone knows — its bcrypt hash came from the old service and its
    plaintext was never ours. Rotating one also upgrades it to argon2id,
    so :func:`credential_origin` moves from ``migrated`` to ``issued``
    and the interface stops saying "we cannot show you this".

    The old secret stops working the moment this commits. That is the
    whole point and it is also the sharp edge: a router still configured
    with it starts answering ``badauth``, and the interface has to say
    so *before* the button is pressed rather than after.
    """
    device = await scope.get(session, Device, device_id)
    if device is None:
        raise _not_found("device")

    config = await load_config(session)
    before = credential_origin(device.password_hash)
    secret = _generate_secret()
    device.password_hash = await hash_password(secret)

    _, by_device = await _hostname_counts(session, scope)
    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_device",
        entity_id=device.id,
        action="rotate_secret",
        diff={
            "credential": {
                "before": before.value,
                "after": credential_origin(device.password_hash).value,
            }
        },
    )
    await session.commit()
    return DeviceSecretOut(
        device=_render_device(
            device,
            by_device.get(device.id, 0),
            default_per_minute=config.rate_limit_per_minute,
        ),
        secret=secret,
    )


@router.patch("/devices/{device_id}", response_model=DeviceSummaryOut)
async def update_device(
    device_id: int,
    body: DeviceUpdateIn,
    user: User = Depends(require_perm(DEVICE_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> DeviceSummaryOut:
    """Change a device's rate limit **without touching its credential**.

    The route #73 opened this issue for. Before it, the only way to
    tighten one device's limit was delete-and-recreate — which mints a
    new username and a new secret, so the operator's only route to
    slowing an abusive device was to break it until its owner
    reconfigured the router. That is backwards: the abuse control and
    the credential are different decisions and only one of them was
    reachable.

    **Nothing in this function reads or writes ``password_hash``**, and
    that is asserted rather than asserted-about: ``test_router_tenant``
    captures the stored hash before and after and compares the bytes,
    *and* drives ``/nic/update`` with the original secret afterwards, so
    a rewrite that happened to produce an equal-length hash still fails.
    One of those two alone would be weaker — a byte comparison cannot
    tell a hash that still verifies from one that does not, and a
    successful login cannot see a hash quietly upgraded in place.

    Scoped like every other device route: ``scope.get`` answers ``None``
    for another tenant's device, which becomes the same 404 a
    nonexistent id gets. A 403 here would confirm the row exists.
    """
    device = await scope.get(session, Device, device_id)
    if device is None:
        raise _not_found("device")

    config = await load_config(session)
    before = device.rate_limit_per_minute
    device.rate_limit_per_minute = body.rate_limit_per_minute

    _, by_device = await _hostname_counts(session, scope)
    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_device",
        entity_id=device.id,
        action="update",
        # Both readings, because `null` and `0` are different states and
        # an audit row saying "rate_limit_per_minute changed" would not
        # let a reader tell *muted* from *returned to the default*.
        diff={
            "rate_limit_per_minute": {
                "before": before,
                "after": device.rate_limit_per_minute,
            }
        },
    )
    await session.commit()
    return _render_device(
        device,
        by_device.get(device.id, 0),
        default_per_minute=config.rate_limit_per_minute,
    )


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(
    device_id: int,
    user: User = Depends(require_perm(DEVICE_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a device. Its hostnames survive, orphaned.

    ``ddns_hostname.device_id`` is ``ON DELETE SET NULL`` by design —
    deleting a router must not destroy the names it was maintaining, and
    the board lists an unassigned hostname rather than dropping it.
    """
    device = await scope.get(session, Device, device_id)
    if device is None:
        raise _not_found("device")
    name = device.name
    username = device.username
    await session.delete(device)
    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_device",
        entity_id=device_id,
        action="delete",
        diff={"name": name, "username": username},
    )
    await session.commit()


# --------------------------------------------------------------------- #
# Hostnames
# --------------------------------------------------------------------- #
#
# The object #69 found nothing could create. `Hostname` had a table, a
# tenancy path, a board that renders it and a wire protocol that updates
# it — and the only construction of one in the whole package was a test
# fixture seeder refused under `ENVIRONMENT=prod`. So the resolution
# strip, which is this milestone's signature element, could not be made
# to render by a tenant starting from an empty account.
#
# **The one rule this section exists to hold: a row created here is a row
# `/nic/update` can find and write.** There are exactly two things that
# can make a hostname permanently un-updatable, and both are decided
# before the row is written, by the same two functions the wire calls:
#
# 1. **Syntax** — `split_hostnames`, i.e. `BaseProvider.isvalidhostname`.
#    The wire answers `notfqdn` when it refuses.
# 2. **Zone containment** — `zone_contains`, which is what
#    `BaseProvider.domaininhostname` calls for each configured zone. The
#    wire answers `nohost` when it refuses.
#
# Neither is re-implemented here. A second validator would agree on the
# day it was written and diverge the first time either copy was edited,
# and the divergence is silent in both directions: a name this endpoint
# accepts and the wire rejects is un-updatable forever and reads to the
# owner as a broken router; a name this endpoint rejects and the wire
# would have accepted reads as the interface lying. Nothing raises.
#
# That includes the **preserved legacy divergences**, which are preserved
# here too because they are the same code:
#
# - `"foo\n"` passes the label regex (Python's `$` matches before a final
#   newline) — and is then refused by containment, exactly as the wire
#   refuses it with `nohost`.
# - a dotless label passes the regex — and is refused by containment
#   unless the zone is itself dotless, again matching the wire.
# - `notexample.com` **is** inside zone `example.com`, because the
#   containment test has no label-boundary check. This endpoint accepts
#   it, because the wire accepts it. Refusing it here would be the
#   asymmetry this section is built to prevent, not a fix.
#
# The model deliberately allows an out-of-zone row to *exist*
# (`models.Hostname` says so: the compat fixture seeds
# `outofzone.example.com` so the frozen table can assert `nohost`). The
# database holding one and this endpoint minting one are different
# questions, and only the second is answered "no".

#: Manage your own hostnames. Seeded by ``0002_ddns_core`` and — until
#: this section existed — referenced by no endpoint at all, which is
#: worse than a permission that does not exist because it reads as
#: coverage. Held by ``user`` and by ``admin`` in the same migration, so
#: every ordinary tenant already has it and no migration is needed here.
HOSTNAME_MANAGE_PERMISSION = "atrium_ddns.hostname.manage"


class HostnameRowOut(BaseModel):
    """A hostname as the CRUD surface describes it.

    Deliberately **not** :class:`HostnameOut`, which is the board's
    model and carries the resolution strips. Two surfaces, two shapes:
    this one is about the row's identity and its assignment, the board's
    is about what DNS currently says. Merging them would put the strip
    arithmetic on an editing screen and the editing fields on the board.
    """

    id: int
    name: str
    domain_id: int
    domain_name: str
    #: ``None`` is a real and supported state — a name registered before
    #: any device was assigned to it, or one whose device was deleted
    #: (``ON DELETE SET NULL``). It is not "unknown" and it is not an
    #: error; ``/nic/update`` simply has nothing authenticating as its
    #: owner yet.
    device_id: int | None
    #: ``None`` whenever ``device_id`` is, and also when the referenced
    #: device is outside this caller's scope — see :func:`list_hostnames`
    #: for why that is resolved through a scoped read rather than a join.
    device_name: str | None
    created_at: str
    #: What we last successfully published, and when. All three stay
    #: ``NULL`` until a `good` aggregate lands — a `nochg` leaves them
    #: untouched — so ``None`` here means *nothing has ever been
    #: published for this name*, not *the address is zero*.
    last_ip_v4: str | None
    last_ip_v6: str | None
    last_updated_at: str | None


class HostnameCreateIn(BaseModel):
    #: 253 is the FQDN limit the column is sized for. The *validator*
    #: applies the legacy service's own (over-generous) 255 against the
    #: whole comma element; this bound is the narrower of the two and is
    #: about what the column can store, not about what is a legal name.
    name: str = Field(min_length=1, max_length=253)
    domain_id: int
    #: Optional, and ``None`` is not "unset" — it is *register the name
    #: now, assign it later*, which the model exists to allow.
    device_id: int | None = None


class HostnameAssignIn(BaseModel):
    """The body of ``PATCH /hostnames/{id}``.

    ``device_id`` has **no default**, so it is required and ``null`` is
    an explicit value rather than an omission. That is the whole reason
    this model has one field: the operation is *set the device*, and
    unassigning is the same operation with ``null``. A model where
    ``null`` and *absent* were both spellable would need a sentinel to
    tell them apart (see :class:`DomainBackendUpdateIn`, which does need
    one), and there is nothing here to preserve.

    The name is deliberately **not** editable. A hostname *is* the DNS
    name — it is what ``/nic/update`` looks the row up by and what a
    provider has already published a record under. Renaming the row
    would leave the old record in the zone with nothing pointing at it
    and no way to reach it again; delete-and-create is the honest
    spelling of that operation, and it is one the owner can see the
    consequences of.
    """

    device_id: int | None


def _validated_hostname(raw: str, zone: str) -> str:
    """The name to store, or the refusal — through the wire's own rules.

    Returns the string to write to ``ddns_hostname.name``. Raises 422
    naming the wire status the same input would have produced, so a
    reader who has seen `notfqdn` or `nohost` in the log recognises the
    refusal rather than meeting a second vocabulary.

    **Exactly one transformation happens before the shared validators,
    and it is the one the wire itself applies.** ``router_nic`` looks the
    row up as ``requested.lower()`` against the stored column, so an
    upper-cased row is unreachable; lower-casing here is not a
    convenience, it is what makes the row findable at all. It is also
    verdict-preserving *by construction* rather than by luck: ``_LABEL``
    carries ``re.IGNORECASE`` and :func:`zone_contains` lower-cases both
    sides, so no case-only edit can move either answer.

    **Nothing else is normalised, and ``.strip()`` in particular is
    not.** It is tempting — a pasted trailing space becomes a confusing
    422 without it — and it is the one change that would break the
    property this whole section exists for. Stripping makes the endpoint
    accept a string the wire refuses (``"foo.example.com\\n"`` is
    ``notfqdn`` on the wire and would have become a stored
    ``foo.example.com`` here), so the two would no longer be answering
    the same question about the same bytes. Trimming is a *form*
    concern: the frontend trims before it submits, where the user can
    see what is being sent. The API answers about what it was given.
    """
    candidate = raw.lower()

    parts = split_hostnames(candidate)
    if parts is None:
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=(
                f"{candidate!r} is not a valid hostname. /nic/update answers "
                f"notfqdn for this name, so the row could be created and never "
                f"updated. Labels are 1–63 characters of letters, digits and "
                f"hyphens, and may not start or end with a hyphen."
            ),
        )
    if len(parts) != 1:
        # A comma is how the *wire* spells "several hostnames in one
        # request"; it is split before any lookup happens, so a stored
        # row containing one could never be found. Refusing it here
        # refuses only names the wire could not reach anyway.
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=(
                "a hostname cannot contain a comma — /nic/update reads a comma "
                "as a separator between several hostnames, so a stored name "
                "containing one can never be looked up. Create them one at a "
                "time."
            ),
        )

    name = parts[0]
    if not zone_contains(zone, name):
        raise HTTPException(
            status_code=UNPROCESSABLE,
            detail=(
                f"{name!r} is not inside the zone {zone!r}. /nic/update answers "
                f"nohost for a hostname outside its domain's zone, so the row "
                f"could be created and never updated."
            ),
        )
    return name


def _render_hostname(
    hostname: Hostname, domain_name: str, device_name: str | None
) -> HostnameRowOut:
    return HostnameRowOut(
        id=hostname.id,
        name=hostname.name,
        domain_id=hostname.domain_id,
        domain_name=domain_name,
        device_id=hostname.device_id,
        device_name=device_name,
        created_at=_iso(hostname.created_at) or "",
        last_ip_v4=hostname.last_ip_v4,
        last_ip_v6=hostname.last_ip_v6,
        last_updated_at=_iso(hostname.last_updated_at),
    )


@router.get("/hostnames", response_model=list[HostnameRowOut])
async def list_hostnames(
    _user: User = Depends(require_perm(HOSTNAME_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> list[HostnameRowOut]:
    """A tenant's hostnames, with their zone and their device.

    Two scoped reads and no per-row query. The device name comes from a
    separate ``scope.select(Device)`` rather than from an outer join,
    which is not a style preference: a join would render whatever name
    sits on the referenced row, and ``Hostname``'s tenancy predicate
    reaches through ``domain_id`` only. A row pointing at a device
    outside the caller's scope — which this endpoint cannot create, but
    a direct write could — renders ``device_name: null`` instead of
    another tenant's device name.
    """
    rows: list[tuple[Hostname, str]] = [
        (row[0], row[1])
        for row in (
            await session.execute(
                scope.select(Hostname, Hostname, Domain.name)
                .join(Domain, Domain.id == Hostname.domain_id)
                .order_by(Hostname.name)
            )
        ).all()
    ]
    device_names = {
        device.id: device.name
        for device in (await session.execute(scope.select(Device))).scalars().all()
    }
    return [
        _render_hostname(
            hostname,
            domain_name,
            device_names.get(hostname.device_id)
            if hostname.device_id is not None
            else None,
        )
        for hostname, domain_name in rows
    ]


@router.post("/hostnames", response_model=HostnameRowOut, status_code=201)
async def create_hostname(
    body: HostnameCreateIn,
    user: User = Depends(require_perm(HOSTNAME_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> HostnameRowOut:
    """Register a name under one of the caller's zones.

    **The owner is never a parameter.** It is not even a column — a
    hostname's owner is ``domain.user_id``, so ownership follows from
    which domain the caller was able to reach, and the caller can only
    reach their own. ``scope.get`` answering ``None`` for another
    tenant's ``domain_id`` is what makes a cross-tenant create a **404**
    rather than a 403: distinguishing *no such zone* from *not your
    zone* would tell a caller which ids exist.

    ``device_id`` is optional and ``null`` is a supported value, not a
    missing one — the model allows a name to be registered before
    anything is assigned to it.

    ``ddns_hostname.name`` is globally unique, and it has to be:
    ``/nic/update?hostname=…`` looks it up with no tenant context, so
    two tenants owning one name would make the lookup ambiguous at the
    exact moment there is nobody to ask. The 409 below is therefore
    reachable for a name another tenant holds, and it says nothing about
    who holds it — same rule, and same sentence, as
    :func:`create_domain`.
    """
    domain = await scope.get(session, Domain, body.domain_id)
    if domain is None:
        raise _not_found("domain")

    device: Device | None = None
    if body.device_id is not None:
        device = await scope.get(session, Device, body.device_id)
        if device is None:
            raise _not_found("device")

    name = _validated_hostname(body.name, domain.name)

    hostname = Hostname(
        domain_id=domain.id,
        device_id=device.id if device is not None else None,
        name=name,
    )
    session.add(hostname)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The UNIQUE index, surfaced as a 409 with a sentence someone can
        # act on. Without this the same collision is a 500 and an
        # unhandled `IntegrityError` in the log, which tells the owner
        # nothing and tells the operator that the service is broken.
        await session.rollback()
        raise _conflict(f"the hostname {name} is already registered") from exc

    # Server-default `created_at` does not come back from the INSERT, and
    # reading it lazily in an async session is a `MissingGreenlet` rather
    # than a slow query. Awaited here, where it is an ordinary read.
    await session.refresh(hostname)

    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_hostname",
        entity_id=hostname.id,
        action="create",
        diff={
            "name": name,
            "domain": domain.name,
            "device_id": hostname.device_id,
        },
    )
    await session.commit()
    return _render_hostname(
        hostname, domain.name, device.name if device is not None else None
    )


@router.patch("/hostnames/{hostname_id}", response_model=HostnameRowOut)
async def assign_hostname(
    hostname_id: int,
    body: HostnameAssignIn,
    user: User = Depends(require_perm(HOSTNAME_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> HostnameRowOut:
    """Assign, reassign, or unassign a hostname's device.

    Three operations, one endpoint, because they are one operation with
    three arguments: ``null`` -> unassigned, an id -> that device,
    a *different* id -> moved. The model was built for this — ``one
    device per hostname``, ``device_id`` nullable, ``ON DELETE SET
    NULL`` — and delete-and-recreate would be a poor substitute because
    it destroys the published-address history the board renders.

    Both lookups are scoped, so a hostname belonging to another tenant
    and a device belonging to another tenant are each a 404. The device
    check matters as much as the hostname one: without it this endpoint
    would be a way to point your own hostname at somebody else's device,
    through a handler whose *read* side is correctly scoped.
    """
    hostname = await scope.get(session, Hostname, hostname_id)
    if hostname is None:
        raise _not_found("hostname")

    device: Device | None = None
    if body.device_id is not None:
        device = await scope.get(session, Device, body.device_id)
        if device is None:
            raise _not_found("device")

    domain = await scope.get(session, Domain, hostname.domain_id)
    if domain is None:  # pragma: no cover — the scope reached the hostname
        raise _not_found("domain")

    before = hostname.device_id
    hostname.device_id = device.id if device is not None else None

    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_hostname",
        entity_id=hostname.id,
        action="assign_device",
        # Both ends recorded, and `None` stays `None` rather than
        # becoming 0 — "was unassigned" and "was device 0" are different
        # sentences and only one of them is true.
        diff={"device_id": {"before": before, "after": hostname.device_id}},
    )
    await session.commit()
    return _render_hostname(
        hostname, domain.name, device.name if device is not None else None
    )


@router.delete("/hostnames/{hostname_id}", status_code=204)
async def delete_hostname(
    hostname_id: int,
    user: User = Depends(require_perm(HOSTNAME_MANAGE_PERMISSION)),
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Forget a name.

    **This deletes the row, not the DNS record.** Whatever the provider
    last published stays published until something removes it —
    ``/nic/delete`` is the operation that withdraws a record, and it
    needs the row in order to find the backend. Deleting here first
    leaves an orphaned record in the zone, which is a real consequence
    and one the interface has to state before the button is pressed
    rather than after.

    Left as a documented consequence rather than a cascade: withdrawing
    records is a network operation against a third party, it can fail
    partway, and doing it inside a DELETE handler would make "forget
    this name" depend on a provider being reachable.
    """
    hostname = await scope.get(session, Hostname, hostname_id)
    if hostname is None:
        raise _not_found("hostname")
    name = hostname.name
    await session.delete(hostname)
    await record_audit(
        session,
        actor_user_id=user.id,
        entity="ddns_hostname",
        entity_id=hostname_id,
        action="delete",
        diff={"name": name},
    )
    await session.commit()


# --------------------------------------------------------------------- #
# Log search
# --------------------------------------------------------------------- #
#
# The old UI had a scrolling event log with a 24-hour horizon. The
# question people actually ask is *"which of my devices stopped
# updating, and when"* — unanswerable by scrolling, and unanswerable at
# 24 hours. Retention is now ~30 days (#17), so this is a **search**
# surface: filters combinable, every one of them an indexed query.
#
# Three properties the rest of this section exists to hold:
#
# 1. **Every filter narrows the SQL.** Not one of them is applied in
#    Python after a wide fetch, and not one of them is applied in the
#    browser. ``(user_id, created_at)`` and ``(device_id, created_at)``
#    exist for exactly this, and ``test_router_events.py`` reads the
#    optimiser's own ``EXPLAIN`` rather than trusting the ORM's intent.
#
# 2. **Two reaches, two mechanisms.** A tenant reading their own rows
#    needs nothing but a session; ``atrium_ddns.events.read.all`` opens
#    the cross-tenant view and *nothing else*. #14 asserted those are
#    different reaches, and a single ``require_perm`` on this endpoint
#    would collapse them — either locking every ordinary tenant out of
#    their own history, or turning a read-the-log grant into the only
#    way to reach the surface at all. See :func:`get_events`.
#
# 3. **The denormalised name columns are what renders.** A row about a
#    deleted device stays readable, which is precisely when the log is
#    being read. The id travels beside the name so the UI can tell "you
#    can filter on this" from "this device is gone", and those are two
#    different sentences rather than one blank.


#: What a ``backend_type`` filter says when it means ``IS NULL``.
#:
#: ``NULL`` in that column is a *meaning*, not a missing value:
#: ``models.DnsEvent`` records it as "decided before any backend was
#: contacted" — badauth, abuse, 911, notfqdn, nohost, and a hostname
#: whose domain has zero backends. So *no filter*, *this provider* and
#: *no provider was reached* are three states, and a query string that
#: can only express two of them cannot ask the third question.
#:
#: Shipped to the client in :class:`EventVocabulary` rather than typed
#: into the frontend, so there is one spelling of the sentinel and not
#: two. A provider can never collide with it: ``known_services()`` are
#: real service names and this is not a legal one.
BACKEND_TYPE_NONE = "__none__"

#: ``ddns_event.event_type`` values that have a **writer**.
#:
#: Three, and the count is the point. ``models.DnsEvent``'s own comment
#: lists ``'update' | 'delete' | 'checkip' | 'auth' | 'healthcheck' |
#: …``, but ``checkip`` and ``healthcheck`` are written by nothing in
#: this codebase — a grep for ``event_type=`` reaches
#: :mod:`atrium_ddns.router_nic` and nowhere else, and it passes exactly
#: these three constants. Offering the other two as filter options would
#: ship a dropdown entry that can never match a row: the
#: artefact-with-no-writer defect wearing a select box's clothes, and
#: indistinguishable to a user from "that has not happened lately".
#:
#: Imported from the writer rather than retyped, and
#: ``test_router_events.py`` re-derives the set from every ``EVENT_*``
#: constant in ``router_nic`` so a fourth one added there fails here
#: rather than silently going unfilterable.
EVENT_TYPES: tuple[str, ...] = tuple(sorted({EVENT_UPDATE, EVENT_DELETE, EVENT_AUTH}))

#: Every ``response_code`` the wire can carry, from the two modules that
#: own the halves: ``providers.PROVIDER_STATUSES`` is what an adapter
#: returns, and the five below are decided by ``router_nic`` before or
#: instead of a provider. Derived from both rather than restated, so a
#: sixth refusal added to either half appears here.
RESPONSE_CODES: tuple[str, ...] = tuple(
    sorted(
        PROVIDER_STATUSES
        | {STATUS_BADAUTH, STATUS_ABUSE, STATUS_NOHOST, STATUS_NOTFQDN, STATUS_911}
    )
)

#: Response codes on rows that carry **no tenant at all**, and are
#: therefore invisible to every tenant-scoped query.
#:
#: Found by running real traffic through ``/nic/update`` and reading the
#: result, not by reading the code. ``_admit`` writes the ``badauth``
#: row *before* a device is resolved — it has to, because there is no
#: device — so ``user_id``, ``device_id`` and every denormalised name on
#: that row are ``NULL``. ``scope.py`` then documents the consequence in
#: as many words: ``user_id == uid`` is never true for ``NULL``, so an
#: orphan row "is invisible to every tenant", and widening that "looks
#: like a kindness" and hands every tenant the deleted tenant's history.
#:
#: Both halves of that are right. The consequence for **this** surface
#: is not: ``response_code=badauth`` is offered in the filter vocabulary,
#: and for an ordinary tenant it returns zero rows *however many times
#: their router failed to authenticate*. Zero rows is exactly what "my
#: credentials are fine" looks like, so the single most common support
#: question — *why is my router not updating* — gets a confidently wrong
#: answer from the surface built to answer it.
#:
#: Naming it is what this constant is for. The query still runs; the
#: response carries an :class:`UnmatchableFilter` saying the rows exist
#: and are not attributable, which is a different sentence from "there
#: were none". Fixing it properly means attributing the row to the
#: username that was tried, and that is a decision about confirming
#: which usernames exist — ``authenticate_device`` deliberately does not
#: distinguish "no such user" from "wrong password". That belongs to
#: #16's writer and to the milestone owner, not to a reader.
UNATTRIBUTED_RESPONSE_CODES: frozenset[str] = frozenset({STATUS_BADAUTH})

#: Page size. The default is a screenful and a half; the ceiling stops
#: a hand-written query string turning a keyset scan into a full read of
#: the retention window.
EVENTS_DEFAULT_LIMIT = 100
EVENTS_MAX_LIMIT = 500


class EventRow(BaseModel):
    """One line of the log, as it renders.

    **The names are the display and the ids are the controls.** Both
    halves of every pair are carried because they answer different
    questions:

    - ``device_name`` is captured at write time and survives the device
      being deleted. It is what the row *says*.
    - ``device_id`` is ``ON DELETE SET NULL``, so it is ``None`` exactly
      when the device is gone. It is what the row can be *filtered by*.

    A UI given only the id renders a wall of blanks for a deleted
    device, which is the state the log is most often opened to
    investigate. A UI given only the name cannot offer "show me
    everything this device did". Given both, it renders the name and
    offers the filter only when there is something to filter on — and
    the difference between the two is visible to the reader rather than
    inferred from an inert link.
    """

    id: int
    created_at: str

    user_id: int | None
    user_email: str | None
    device_id: int | None
    device_name: str | None
    domain_id: int | None
    domain_name: str | None
    hostname_id: int | None
    hostname: str | None

    event_type: str
    response_code: str | None
    client_ip: str | None
    #: The address the request was *about* (``myip``, normalised). Not
    #: the same fact as ``client_ip``, and the difference is the
    #: interesting part of a NAT'd update (``router_nic.py:670``).
    ip: str | None
    backend_type: str | None
    message: str | None


class EventVocabulary(BaseModel):
    """The filter values this installation can actually produce.

    Shipped with every page so the frontend cannot invent an option.
    Every list is derived — from the writer's own constants, from the
    provider registry — so a provider deleted from ``_PROVIDERS`` takes
    its filter option with it.
    """

    event_types: list[str]
    response_codes: list[str]
    backend_types: list[str]
    #: The sentinel for ``backend_type IS NULL``. Transported rather
    #: than duplicated: two spellings of a sentinel is a filter that
    #: silently stops matching.
    backend_type_none: str
    #: ``worker_jobs.SUCCESS_RESPONSE_CODES``, verbatim.
    #:
    #: Carried because the log's one accented rendering keys off it —
    #: ``ui-design.md`` §1.2 Rule 2: ``--ddns-diverge`` appears nowhere
    #: except on a measured disagreement, and on this surface a
    #: non-success response code *is* the measured disagreement. A
    #: frontend holding its own ``['good', 'nochg']`` would be a second
    #: implementation of a classification the health-check job already
    #: owns, and the two would part company the first time a code was
    #: reclassified — silently, because both renderings look correct.
    success_response_codes: list[str]


class EventFilters(BaseModel):
    """The filters that were **applied**, echoed back.

    Not the ones that were asked for — the ones the query ran with. An
    empty result is only interpretable beside the filters that produced
    it, and a UI that renders its own filter state next to a server's
    rows is two sources of truth for one question.
    """

    user_id: int | None = None
    device_id: int | None = None
    domain_id: int | None = None
    hostname_id: int | None = None
    event_type: str | None = None
    response_code: str | None = None
    backend_type: str | None = None
    client_ip: str | None = None
    since: str | None = None
    until: str | None = None


class UnmatchableFilter(BaseModel):
    """A filter that ran and structurally cannot have matched.

    Both fields are needed. ``filter`` is the reader's own input, so
    they can see which of several it was; ``reason`` is why, because
    "no rows" has at least three causes on this surface and only one of
    them means *nothing happened*.
    """

    #: ``key=value``, the reader's own input.
    filter: str
    #: One sentence, in the interface's voice.
    reason: str


class EventPage(BaseModel):
    """One page of the log, and everything needed to read a zero."""

    rows: list[EventRow]
    #: Keyset cursor for the next page, or ``None`` when this is the
    #: last one. Derived by fetching ``limit + 1`` rows and dropping the
    #: extra — so "is there more" is answered without a ``COUNT(*)``
    #: over the retention window.
    next_cursor: str | None
    limit: int
    filters: EventFilters
    vocabulary: EventVocabulary
    #: ``DdnsConfig.event_retention_days``, read from the API so the
    #: UI's "the log holds the last N days" sentence cannot go stale
    #: when an operator changes it.
    retention_days: int
    #: Whether this caller is reading across tenants, and therefore
    #: whether the user column means anything. The UI shows it only when
    #: this is true — rendering an always-identical column is noise, and
    #: deciding it client-side from a permission list is a second
    #: implementation of a rule the server already applied.
    cross_tenant: bool
    #: **Three states, deliberately.** ``None`` means *not measured* —
    #: the page had rows, so the question was never asked. ``True``
    #: means the scope holds rows but none match these filters.
    #: ``False`` means the scope holds no rows at all.
    #:
    #: Those are three different sentences on an empty panel — "narrow
    #: your filters", against "nothing has ever been logged; add a
    #: device" — and collapsing them into a boolean makes a working
    #: filter look like an empty account.
    any_rows_in_scope: bool | None
    #: Filters that cannot match a row **for this caller**, each with
    #: the reason. A typo'd ``backend_type`` otherwise returns zero rows
    #: and reads exactly like "no traffic for that provider"; a
    #: tenant-scoped ``badauth`` filter returns zero rows and reads
    #: exactly like "my credentials are fine". Both are false negatives
    #: carrying the authority of a measurement, and they are two
    #: different sentences. Empty for the normal case.
    unmatchable_filters: list[UnmatchableFilter] = Field(default_factory=list)


def _parse_instant(raw: str | None, *, field: str) -> datetime | None:
    """An ISO-8601 string to the naive-UTC shape every column here uses.

    Refuses rather than guessing. A range endpoint that silently became
    ``None`` on a parse failure would widen the query to the whole
    retention window and report the result as if the range had been
    applied — a filter that reads as applied and is not.
    """
    if raw is None or raw == "":
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} is not an ISO-8601 instant: {exc}",
        ) from exc
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def encode_cursor(created_at: datetime, row_id: int) -> str:
    """``(created_at, id)`` as one opaque-but-legible string.

    Legible on purpose: an opaque base64 blob costs a decode step every
    time someone reads a request log, and there is nothing secret in a
    timestamp the caller was just shown. ``id`` is in it because
    ``created_at`` is not unique — a router bursting several hostnames
    writes several rows inside one microsecond, and a cursor on the
    timestamp alone either loses those rows or repeats them forever.
    """
    return f"{_iso(created_at)}|{row_id}"


def decode_cursor(raw: str | None) -> tuple[datetime, int] | None:
    """The inverse, refusing anything it cannot read."""
    if raw is None or raw == "":
        return None
    head, _, tail = raw.rpartition("|")
    if not head or not tail.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cursor is not a '<iso8601>|<id>' pair",
        )
    parsed = _parse_instant(head, field="cursor")
    assert parsed is not None  # `head` is non-empty
    return parsed, int(tail)


def build_events_query(
    *,
    scope: DdnsScope,
    filters: EventFilters,
    cursor: tuple[datetime, int] | None,
    limit: int,
) -> Select:
    """The whole search, as one statement. Pure — no IO.

    Pure so the SQL can be compiled and read in a test without a
    database, which is one of the two instruments on the acceptance
    criterion *"filters are indexed queries, not client-side filtering
    of a large fetch"*. The other is an ``EXPLAIN`` against a live
    MySQL: this one reads our intent, that one reads the optimiser's
    decision, and only the pair can catch a filter that is present in
    the ``WHERE`` clause and unservable by an index.

    Ordering and the cursor
    -----------------------
    ``ORDER BY created_at DESC, id DESC`` — newest first, which is the
    only order a log is read in. The keyset predicate is written as::

        created_at <= :c AND (created_at < :c OR id < :i)

    rather than the equivalent single ``OR``, and the shape is the
    point. The first conjunct is a plain range on the compound index's
    trailing column, so ``(user_id, created_at)`` still serves the scan;
    the second is a tie-break applied to the handful of rows sharing a
    microsecond. Written as one ``OR`` across both columns it is not
    sargable, the optimiser falls back to a scan, the rows are identical
    — and ``EXPLAIN`` is the only thing that would tell you.

    ``OFFSET`` is deliberately absent. ``LIMIT 100 OFFSET 5000`` reads
    5100 rows to return 100, which is the "client-side filtering of a
    large fetch" defect moved one layer down where it is harder to see.
    """
    stmt = scope.select(DnsEvent)

    # Every one of these is a column with an index in front of it:
    # `(user_id, created_at)`, `(device_id, created_at)`,
    # `ix_ddns_event_domain_id`, `ix_ddns_event_hostname_id`,
    # `ix_ddns_event_client_ip`.
    if filters.user_id is not None:
        stmt = stmt.where(DnsEvent.user_id == filters.user_id)
    if filters.device_id is not None:
        stmt = stmt.where(DnsEvent.device_id == filters.device_id)
    if filters.domain_id is not None:
        stmt = stmt.where(DnsEvent.domain_id == filters.domain_id)
    if filters.hostname_id is not None:
        stmt = stmt.where(DnsEvent.hostname_id == filters.hostname_id)
    if filters.client_ip is not None:
        stmt = stmt.where(DnsEvent.client_ip == filters.client_ip)
    if filters.event_type is not None:
        stmt = stmt.where(DnsEvent.event_type == filters.event_type)
    if filters.response_code is not None:
        stmt = stmt.where(DnsEvent.response_code == filters.response_code)
    if filters.backend_type is not None:
        # The third state. `IS NULL` is a question about a meaning, not
        # a fallback for an absent parameter.
        if filters.backend_type == BACKEND_TYPE_NONE:
            stmt = stmt.where(DnsEvent.backend_type.is_(None))
        else:
            stmt = stmt.where(DnsEvent.backend_type == filters.backend_type)

    since = _parse_instant(filters.since, field="since")
    until = _parse_instant(filters.until, field="until")
    if since is not None:
        stmt = stmt.where(DnsEvent.created_at >= since)
    if until is not None:
        stmt = stmt.where(DnsEvent.created_at <= until)

    if cursor is not None:
        at, row_id = cursor
        stmt = stmt.where(
            DnsEvent.created_at <= at,
            sa.or_(DnsEvent.created_at < at, DnsEvent.id < row_id),
        )

    return stmt.order_by(DnsEvent.created_at.desc(), DnsEvent.id.desc()).limit(limit)


def _row_out(event: DnsEvent) -> EventRow:
    """One ORM row to one wire row. No verdicts, no derivation."""
    return EventRow(
        id=event.id,
        created_at=_iso(event.created_at) or "",
        user_id=event.user_id,
        user_email=event.user_email,
        device_id=event.device_id,
        device_name=event.device_name,
        domain_id=event.domain_id,
        domain_name=event.domain_name,
        hostname_id=event.hostname_id,
        hostname=event.hostname,
        event_type=event.event_type,
        response_code=event.response_code,
        client_ip=event.client_ip,
        ip=event.ip,
        backend_type=event.backend_type,
        message=event.message,
    )


def _vocabulary() -> EventVocabulary:
    return EventVocabulary(
        event_types=list(EVENT_TYPES),
        response_codes=list(RESPONSE_CODES),
        backend_types=list(known_services()),
        backend_type_none=BACKEND_TYPE_NONE,
        success_response_codes=sorted(SUCCESS_RESPONSE_CODES),
    )


def unmatchable(
    filters: EventFilters,
    vocabulary: EventVocabulary,
    *,
    cross_tenant: bool,
) -> list[UnmatchableFilter]:
    """Filters that ran and structurally cannot have matched.

    A zero result is a measurement only when the question was askable.
    Two ways it is not, and they are different sentences:

    **The value does not exist here.** ``backend_type=rout53`` returns
    zero rows and reads exactly like *no traffic for that provider* — a
    false negative wearing a number, the defect family this repository
    catalogues as "the probe that could not fail", pointed at a filter
    instead of a metric. It is **named** rather than refused: V1M4
    imports the legacy service's history, and a row carrying a service
    name this build does not ship must stay findable.

    **The rows exist and are not yours.** ``response_code=badauth`` in a
    tenant-scoped query returns zero rows *however many times that
    tenant's router failed to authenticate*, because the writer has no
    device to attribute the row to and ``user_id IS NULL`` is invisible
    to every tenant. See :data:`UNATTRIBUTED_RESPONSE_CODES`. This is
    the one that matters, because zero is also what a healthy account
    looks like.

    ``cross_tenant`` is a keyword and required: the second reason is a
    property of *this caller*, not of the installation, and a signature
    that let it default would answer the wrong question by omission.
    """
    out: list[UnmatchableFilter] = []
    if (
        filters.event_type is not None
        and filters.event_type not in vocabulary.event_types
    ):
        out.append(
            UnmatchableFilter(
                filter=f"event_type={filters.event_type}",
                reason=(
                    "no writer in this installation produces that event type, "
                    "so no row can carry it"
                ),
            )
        )
    if filters.response_code is not None:
        if filters.response_code not in vocabulary.response_codes:
            out.append(
                UnmatchableFilter(
                    filter=f"response_code={filters.response_code}",
                    reason="that is not a response code this service answers with",
                )
            )
        elif (
            not cross_tenant
            and filters.response_code in UNATTRIBUTED_RESPONSE_CODES
        ):
            out.append(
                UnmatchableFilter(
                    filter=f"response_code={filters.response_code}",
                    reason=(
                        "these lines are written before any device is "
                        "identified, so they belong to no account and cannot "
                        "appear in your log. A zero here is not evidence that "
                        "your credentials are working."
                    ),
                )
            )
    if (
        filters.backend_type is not None
        and filters.backend_type != vocabulary.backend_type_none
        and filters.backend_type not in vocabulary.backend_types
    ):
        out.append(
            UnmatchableFilter(
                filter=f"backend_type={filters.backend_type}",
                reason=(
                    "that is not a provider this installation knows about, "
                    "so no row can name it"
                ),
            )
        )
    return out


@router.get("/events", response_model=EventPage)
async def get_events(
    scope: DdnsScope = Depends(get_scope),
    session: AsyncSession = Depends(get_session),
    user_id: int | None = Query(default=None),
    device_id: int | None = Query(default=None),
    domain_id: int | None = Query(default=None),
    hostname_id: int | None = Query(default=None),
    event_type: str | None = Query(default=None),
    response_code: str | None = Query(default=None),
    backend_type: str | None = Query(default=None),
    client_ip: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=EVENTS_DEFAULT_LIMIT, ge=1, le=EVENTS_MAX_LIMIT),
) -> EventPage:
    """The log, filtered, newest first.

    Two reaches, two mechanisms, and keeping them apart is the whole
    authorisation story
    ------------------------------------------------------------------
    **Reach one — your own rows.** Any authenticated caller. There is no
    ``require_perm`` on this endpoint, and that is deliberate rather
    than an omission. ``get_scope`` resolves through
    ``current_principal``, which resolves through ``current_user``, so
    an anonymous request is already 401 and atrium's TOTP gate is
    already enforced; and ``DdnsScope.for_principal`` always carries the
    caller's ``user_id``, so the predicate this statement runs with is
    ``ddns_event.user_id = :me``. A tenant reading their own update
    history needs no grant to do it — it is their data, and gating the
    endpoint on ``atrium_ddns.events.read.all`` would lock every
    ordinary tenant out of the surface built for them.

    **Reach two — everybody's rows.** ``atrium_ddns.events.read.all``,
    and it opens the audit log **and nothing else**: ``scope.py``
    records it against ``DnsEvent``'s ``TenantPath`` alone, so the same
    grant that widens this query leaves ``Domain``, ``Device``,
    ``Hostname`` and ``DomainBackend`` scoped exactly as before. That is
    #14's assertion, and it is what makes a support role possible — read
    anyone's update history without being able to touch anyone's zones.

    A single ``require_perm(EVENTS_CROSS_TENANT_PERMISSION)`` on this
    handler would collapse the two into one check and be wrong in both
    directions at once: it locks ordinary tenants out of their own log,
    and it turns a read-the-log grant into the only way to reach the
    surface at all.

    The one place the two meet is ``?user_id=``. Filtering to *someone
    else* is reach two; filtering to yourself is reach one, and it is
    allowed because it is a no-op the scope already enforces. Asking for
    another tenant without the grant is **403 naming the permission** —
    not a silent narrowing to your own rows (which answers a question
    nobody asked, in a shape indistinguishable from the answer to the
    one they did ask) and not an empty list (which is a false negative
    with the authority of a measurement).
    """
    config = await load_config(session)
    cross_tenant = scope.reaches_all_tenants(DnsEvent)

    if user_id is not None and not cross_tenant and user_id != scope.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"reading another tenant's log needs the "
                f"{EVENTS_CROSS_TENANT_PERMISSION} permission. This is a "
                f"refusal, not an empty log."
            ),
        )

    filters = EventFilters(
        user_id=user_id,
        device_id=device_id,
        domain_id=domain_id,
        hostname_id=hostname_id,
        event_type=event_type,
        response_code=response_code,
        backend_type=backend_type,
        client_ip=client_ip,
        since=since,
        until=until,
    )

    # `limit + 1`: the extra row is how "is there another page" is
    # answered. A `COUNT(*)` over the same predicate would read the
    # whole matching set to produce a number nothing on this surface
    # renders.
    stmt = build_events_query(
        scope=scope,
        filters=filters,
        cursor=decode_cursor(cursor),
        limit=limit + 1,
    )
    fetched = list((await session.execute(stmt)).scalars().all())
    has_more = len(fetched) > limit
    events = fetched[:limit]

    next_cursor = (
        encode_cursor(events[-1].created_at, events[-1].id)
        if has_more and events
        else None
    )

    # Asked only when the page came back empty, and *only* about the
    # scope — no filters. This is what separates "your filters matched
    # nothing" from "nothing has ever been logged for you", which are
    # two different empty panels with two different next actions. It is
    # one indexed row read (`LIMIT 1` on the tenancy predicate), and it
    # stays `None` — not `False` — on the path where it was never asked.
    any_rows_in_scope: bool | None = None
    if not events:
        probe = scope.select(DnsEvent, DnsEvent.id).limit(1)
        any_rows_in_scope = (await session.execute(probe)).first() is not None

    vocabulary = _vocabulary()
    return EventPage(
        rows=[_row_out(event) for event in events],
        next_cursor=next_cursor,
        limit=limit,
        filters=filters,
        vocabulary=vocabulary,
        retention_days=config.event_retention_days,
        cross_tenant=cross_tenant,
        any_rows_in_scope=any_rows_in_scope,
        unmatchable_filters=unmatchable(
            filters, vocabulary, cross_tenant=cross_tenant
        ),
    )


# --------------------------------------------------------------------- #
# Operator configuration — the schema the settings form renders from
# --------------------------------------------------------------------- #
#
# #73. The eleven `atrium_ddns.*` settings have been served by atrium's
# `GET /api/admin/app-config` since #17 and no screen could reach them:
# the shell's four config sections each pass a *string literal* to the
# namespace-parameterised mutation hook, and nothing in it derives a
# namespace from what the admin API returns.
#
# The host's settings pages read and write through atrium's own admin
# endpoints — `GET /api/admin/app-config` for the values,
# `PUT /api/admin/app-config/atrium_ddns` for the write. This route adds
# the one thing atrium cannot serve: the *shape*. Atrium's PUT takes a
# bare `dict[str, Any]`, so the namespace's types and bounds appear
# nowhere in the OpenAPI document, and a form that hardcoded them would
# be a second copy of the model with no test able to see it drift.
#
# It reads `DdnsConfig.model_json_schema()` — the same schema pydantic
# validates the PUT against.


@router.get("/config/schema", response_model=SettingsSchemaOut)
async def get_settings_schema(
    _actor: User = Depends(require_perm(APP_CONFIG_MANAGE_PERMISSION)),
) -> SettingsSchemaOut:
    """Every field of the ``atrium_ddns`` namespace, grouped for a form.

    Gated on **atrium's** ``app_setting.manage`` rather than on an
    `atrium_ddns.*` permission, and the reason is worth stating: the
    values live in atrium's ``app_settings`` table and are written
    through atrium's own endpoint, which is gated on exactly that. A
    host-specific permission here would let a holder open a form whose
    save button answers 403 — a surface that reads as broken rather than
    as refused.

    No session and no scope: this is the *description* of a global
    namespace, identical for every caller, and it touches no tenant row.
    The values themselves are a different request, to a different
    endpoint, which applies its own gate.
    """
    return settings_schema()
