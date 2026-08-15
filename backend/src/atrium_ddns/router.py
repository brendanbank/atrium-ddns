"""The host's JSON surface.

- ``GET /api/atrium_ddns/state`` — read-only, auth required (demo).
- ``POST /api/atrium_ddns/bump`` — gated by ``atrium_ddns.write`` (demo).
- ``GET /api/atrium_ddns/board`` — the device board and the resolution
  strips, gated by ``atrium_ddns.device.manage`` and scoped by
  :class:`~atrium_ddns.scope.DdnsScope`.

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

from datetime import UTC, datetime
from enum import Enum
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import require_perm
from app.auth.users import current_user
from app.db import get_session
from app.models.auth import User
from app.services.audit import record as record_audit

from .models import AtriumDdnsState, Device, DnsEvent, Domain, Hostname
from .scope import DdnsScope, get_scope
from .worker_jobs import (
    SUCCESS_RESPONSE_CODES,
    DnsCheckStatus,
    Liveness,
    device_statuses,
    load_config,
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
