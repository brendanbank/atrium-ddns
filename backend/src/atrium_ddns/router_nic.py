"""The DynDNS v2 wire surface: ``/nic/checkip``, ``/nic/update``, ``/nic/delete``.

The frozen 131-case table in ``tests/compat/protocol_cases.yaml`` is the
specification for this module, and `make test-compat TARGET=host
BASE_URL=…` is how it is checked. Nothing here may be "tidied" against
``docs/DYNDNS-PROTOCOL.md``: where the document and the legacy
implementation disagree the implementation wins, in nine places that
are each written down in plan §1 and carry a case here.

Five properties of this module are load-bearing.

**Every ``GET`` answers HTTP 200**, ``badauth`` included. A 401 breaks
every router in the field, because clients parse the body and not the
status. The only non-200s in this file are the two deliberate ``HEAD``
refusals below.

**``HEAD`` is refused on the two endpoints that write, by hand.**
Plan §1 decided 405 for ``HEAD /nic/update`` and ``HEAD /nic/delete``
and 200 for ``HEAD /nic/checkip``, and the framework will not produce
the refusal for us. Measured against ``atrium:0.28`` in
``tests/test_router_nic.py::test_the_head_refusal_does_not_fall_out_of
_the_framework``: bare FastAPI answers 405 for a ``HEAD`` on a
GET-only route, and *the same route behind atrium's SPA catch-all
answers 404* — a ``Mount`` matches every method and Starlette prefers a
full match to a partial one, so the route's own 405 never reaches the
wire. Hence the explicit ``methods=["HEAD"]`` routes: they are the only
thing that makes the two frozen cases assert anything.

(Plan §1 and the frozen table's ``known_gaps`` both record **200** for
that middle reading rather than 404. The difference is
``SPAStaticFiles``' fallback to ``index.html`` being guarded by
``scope["method"] == "GET"``, so a ``HEAD`` miss re-raises rather than
being served the shell. The decision and the code are unchanged — 404
is no more the frozen 405 than 200 was — but the recorded *reason*
suggests un-shadowing the mount would fix it, and it would not.)

**Every provider call is blocking and runs in a worker thread.** boto3,
httpx and dnspython are synchronous (see :mod:`atrium_ddns.providers`),
and so is password hashing (see :mod:`atrium_ddns.auth_device`). The
whole DNS phase of a request is one ``anyio.to_thread.run_sync`` hop —
one hop rather than one per backend, so a 25-hostname request does not
occupy 25 threads, and zero of it on the event loop.

**Provider credentials are unlocked once, up front.** ``await
unlock_user_secrets(session, owner_id)`` is a *database read*; doing it
inside the per-backend loop would be IO inside what is otherwise a
pure aggregation, and doing it lazily at ``.reveal()`` is impossible
because the descriptor is synchronous. It is resolved as part of the
same fetch that loads the hostname and domain rows. There is no
authenticated atrium user anywhere in this request — the owner is
``domain.user_id``, read off the row.

**Every query goes through :class:`~atrium_ddns.scope.DdnsScope`.** Not
one hand-written ``user_id`` filter. #17 measured that this is not
checkable from a query log — SQLAlchemy elides ``sa.true()`` once it is
ANDed with anything, so a composed statement carries no trace of a
cross-tenant scope — so it is checked behaviourally in
``backend/tests/test_router_nic.py`` instead.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import anyio
from app.db import get_session
from app.host_sdk.crypto import SecretShreddedError, unlock_user_secrets
from app.logging import log
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .auth_device import (
    DeviceAuth,
    authenticate_device,
    check_rate_limit,
    client_address,
    effective_rate_limit,
    normalise_address,
    parse_basic_auth,
)
from .models import DnsEvent, Domain, Hostname
from .providers import (
    DEFAULT_TTL,
    RTYPE_A,
    STATUS_DNSERR,
    STATUS_GOOD,
    STATUS_NOCHG,
    STATUS_UNKNOWN_SERVICE,
    BaseProvider,
    ProviderAccount,
    get_provider,
)
from .worker_jobs import load_config

router = APIRouter(tags=["nic"])

# --- the wire vocabulary --------------------------------------------- #
#: Not in ``providers.PROVIDER_STATUSES``: no adapter returns these.
#: They are decided by this module, before or instead of a provider.
STATUS_BADAUTH = "badauth"
STATUS_ABUSE = "abuse"
STATUS_NOHOST = "nohost"
STATUS_NOTFQDN = "notfqdn"
STATUS_911 = STATUS_UNKNOWN_SERVICE

#: ``ddns_event.event_type``. Matches the vocabulary
#: ``models.DnsEvent`` documents and ``worker_jobs.device_statuses``
#: filters on (``event_type == "update"``) — a third spelling here
#: would silently empty the status board.
EVENT_UPDATE = "update"
EVENT_DELETE = "delete"
EVENT_AUTH = "auth"

#: The `checkip` HTML wrapper, byte for byte.
#:
#: ``html.escape`` is deliberately **not** applied. The value
#: interpolated here is ``str(ipaddress.ip_address(...))`` or the empty
#: string, and every string that constructor can produce is drawn from
#: ``[0-9a-f.:%]`` — none of which ``html.escape`` touches. The legacy
#: service escapes it; the escape is structurally unreachable, so the
#: compat suite documents it rather than asserting it
#: (``tests/compat/README.md``, "Deliberately not asserted"), and
#: reproducing an unreachable call here would be cargo.
CHECKIP_HTML = (
    "<html><head><title>Current IP Check</title></head>"
    "<body>Current IP Address: {ip}</body></html>"
)


# --------------------------------------------------------------------- #
# Hostname syntax — the frozen helper, not a re-implementation
# --------------------------------------------------------------------- #


def split_hostnames(raw: str) -> list[str] | None:
    """``BaseProvider.isvalidhostname``, with its quirks intact.

    Called rather than re-implemented on purpose. Two documented
    divergences live *inside* it — a single label with no dot
    validates, and a label with a trailing newline validates because
    Python's ``$`` matches before a final newline — and both are
    preserved decisions with frozen cases. It returns the elements
    **unstripped**, which is what makes ``ok.example.com.`` and
    ``foo\\n`` miss the lookup and answer ``nohost`` rather than
    ``notfqdn``.

    Re-deriving any of that here would be a second copy of the
    behaviour, and the copy is the one that would get "fixed".
    """
    parts = BaseProvider.isvalidhostname(raw)
    return None if parts is False else list(parts)


# --------------------------------------------------------------------- #
# Plans — everything the DNS phase needs, with no session attached
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class BackendPlan:
    """One ``ddns_domain_backend`` row, flattened for the thread.

    ``credentials`` is the **revealed** plaintext (or ``None``). It is
    resolved on the event loop, before the thread starts, because
    revealing is a descriptor read that needs the owner's key already
    in ``session.info`` — and getting it there is a database read.
    """

    backend_id: int
    backend_type: str
    domains: tuple[str, ...]
    credentials: Mapping[str, Any] | None
    config: Mapping[str, Any]
    ttl: int

    @property
    def has_credentials(self) -> bool:
        """Whether the row carries anything at all.

        ``NULL`` ciphertext is a real state, not a missing one — the
        frozen fixture's ``credentials: absent`` backend contributes
        ``911`` and this is where that is decided. An empty mapping is
        treated the same: the legacy service's ``get_credentials()``
        returns ``{}`` for a backend with no config rows, and that is
        the condition ``update/backend-no-credentials-911`` was written
        against.
        """
        return bool(self.credentials)


@dataclass(frozen=True)
class HostnamePlan:
    """One requested hostname, resolved or not."""

    #: As the client spelled it, unstripped. This is what the response
    #: line is *about*; it is never echoed, but it is what the event row
    #: records.
    requested: str
    #: Lower-cased, for the lookup. The frozen table asserts the lookup
    #: is case-insensitive and that a trailing dot is *not* stripped.
    lookup: str
    hostname_id: int | None = None
    domain_id: int | None = None
    domain_name: str | None = None
    backends: tuple[BackendPlan, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.hostname_id is not None


@dataclass
class HostnameResult:
    """What one hostname answered, and why."""

    plan: HostnamePlan
    #: ``(backend_type, status)`` in backend order. Empty when the
    #: outcome was decided before any backend was contacted.
    attempts: list[tuple[str, str]] = field(default_factory=list)
    #: The aggregate — the token that reaches the wire.
    status: str = STATUS_911


# --------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------- #


def aggregate(statuses: Sequence[str]) -> str:
    """any good -> good; all nochg -> nochg; else the **first** other.

    "First that is neither ``good`` nor ``nochg``, in backend order" —
    not the last error and not the first error overall. The frozen
    fixture's ``firsterr.example.com`` is ordered ``nochg, 911,
    dnserr`` precisely so that an implementation returning either wrong
    answer fails; ``models.Domain.backends`` carries
    ``order_by="DomainBackend.id"`` for the same reason.
    """
    if not statuses:
        # Unreachable from the handlers: a hostname with zero backends
        # is answered before this is called. Kept as a refusal rather
        # than a default so a future caller finds out here instead of
        # seeing a plausible `911` it cannot explain.
        raise ValueError("aggregate() called with no backend statuses")
    if STATUS_GOOD in statuses:
        return STATUS_GOOD
    if all(status == STATUS_NOCHG for status in statuses):
        return STATUS_NOCHG
    for status in statuses:
        if status not in (STATUS_GOOD, STATUS_NOCHG):
            return status
    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------- #
# The DNS phase — one thread hop, no session, no event loop
# --------------------------------------------------------------------- #


def _run_backend(
    backend: BackendPlan, hostname: str, *, op: str, ip: str | None, rtype: str | None
) -> str:
    """One backend's contribution for one hostname. **Blocking.**

    Ordered exactly as plan §1's table is:

    1. no stored credentials -> ``911``
    2. service name unknown to the factory -> ``911``
    3. hostname outside the backend's zone -> ``nohost``
    4. whatever the adapter answered, defaulting to ``dnserr``

    A hostname *absent* from the adapter's returned map is read as
    ``dnserr`` rather than as success. ``providers.PROVIDER_STATUSES``
    is a closed vocabulary for exactly this reason: an adapter that
    forgets a key must degrade to an error, not to a silent pass.
    """
    if not backend.has_credentials:
        log.info(
            "ddns.backend.no_credentials",
            backend_id=backend.backend_id,
            backend_type=backend.backend_type,
        )
        return STATUS_911

    provider = get_provider(
        ProviderAccount(
            service=backend.backend_type,
            domains=backend.domains,
            credentials=backend.credentials or {},
            config=backend.config,
            id=str(backend.backend_id),
        )
    )
    if provider is None:
        # `get_provider` logs the unknown service and the known set.
        return STATUS_911

    grouped = provider.hostnameperzone([hostname])
    if not grouped:
        return STATUS_NOHOST

    if op == "update":
        result = provider.createrecords(
            ip, grouped, rtype=rtype or RTYPE_A, ttl=backend.ttl
        )
    else:
        result = provider.deleterecords(grouped, rtype=rtype)

    return result.get(hostname, STATUS_DNSERR)


def _run_dns_phase(
    plans: Sequence[HostnamePlan], *, op: str, ip: str | None, rtype: str | None
) -> list[HostnameResult]:
    """Every backend call for the whole request. **Blocking.**

    Called through ``anyio.to_thread.run_sync`` exactly once per
    request. One hop rather than one per backend: a 25-hostname request
    would otherwise take 25 threads out of anyio's pool, and the
    ordering guarantees the frozen table asserts are easier to reason
    about when the whole phase is sequential.

    Takes no session and holds no ORM object. Everything it needs was
    flattened into :class:`BackendPlan` on the event loop, including the
    revealed credentials — a lazy load in here would be a synchronous
    database call from a worker thread against an async engine.
    """
    results: list[HostnameResult] = []
    for plan in plans:
        result = HostnameResult(plan=plan)
        if not plan.resolved:
            result.status = STATUS_NOHOST
            results.append(result)
            continue
        if not plan.backends:
            # "hostname owned, but zero backends -> 911 {ip}". Decided
            # here rather than by `aggregate`, because there is nothing
            # to aggregate and an empty aggregate is a guess.
            result.status = STATUS_911
            results.append(result)
            continue
        for backend in plan.backends:
            status = _run_backend(
                backend, plan.lookup, op=op, ip=ip, rtype=rtype
            )
            result.attempts.append((backend.backend_type, status))
        result.status = aggregate([status for _, status in result.attempts])
        results.append(result)
    return results


# --------------------------------------------------------------------- #
# Loading — one fetch, one unlock
# --------------------------------------------------------------------- #


async def load_plans(
    session: AsyncSession, auth: DeviceAuth, requested: Sequence[str]
) -> list[HostnamePlan]:
    """Resolve every requested hostname, in **request order**.

    One query for the whole request, not one per hostname: 25 hostnames
    is a frozen case (divergence 3 — there is no cap) and 25 round
    trips per update is how a five-minute fleet becomes a database
    problem. Duplicates in the request produce duplicate *lines*
    (``update-duplicate-hostname-produces-two-lines``) but not
    duplicate queries.

    **Ownership is checked against the device, not the user.** Plan
    §3.3: after the migration a hostname belongs to the device that
    updates it, so a hostname owned by the same tenant but assigned to
    a different device answers ``nohost``. The scope is applied *as
    well*, and neither is redundant — the scope is the tenancy control
    (a bug that loses the device filter must still not cross tenants),
    the device filter is the ownership control.

    The unlock happens here, once per distinct owner, and only for
    owners that actually hold ciphertext. ``SecretShreddedError`` is
    caught rather than raised: a tenant whose key is gone cannot have
    their provider credentials read, which is precisely the
    "no usable credentials" condition that already answers ``911``. A
    500 here would answer nothing a DynDNS client can parse.
    """
    lookups = {name.lower() for name in requested}
    scope = auth.scope

    rows = (
        (
            await session.execute(
                scope.select(Hostname)
                .where(
                    Hostname.name.in_(lookups),
                    Hostname.device_id == auth.device.id,
                )
                .options(
                    selectinload(Hostname.domain).selectinload(Domain.backends)
                )
            )
        )
        .scalars()
        .all()
    )
    by_name = {row.name: row for row in rows}

    owners_with_ciphertext = {
        row.domain.user_id
        for row in rows
        for backend in row.domain.backends
        if backend.credentials_ct is not None
    }
    locked: set[int] = set()
    for owner_id in sorted(owners_with_ciphertext):
        try:
            await unlock_user_secrets(session, owner_id)
        except SecretShreddedError:
            # Their key is gone, so their ciphertext is unreadable and
            # is not coming back. Recorded as an owner whose backends
            # have no usable credentials.
            log.error("ddns.secrets.shredded_owner", user_id=owner_id)
            locked.add(owner_id)

    plans: list[HostnamePlan] = []
    for name in requested:
        row = by_name.get(name.lower())
        if row is None:
            plans.append(HostnamePlan(requested=name, lookup=name.lower()))
            continue
        plans.append(
            HostnamePlan(
                requested=name,
                lookup=row.name,
                hostname_id=row.id,
                domain_id=row.domain_id,
                domain_name=row.domain.name,
                backends=tuple(
                    _backend_plan(backend, row.domain, locked)
                    for backend in row.domain.backends
                ),
            )
        )
    return plans


def _backend_plan(backend: Any, domain: Domain, locked: set[int]) -> BackendPlan:
    """Flatten one backend row, revealing its credentials.

    ``backend.credentials`` is the ``UserSecret`` descriptor; reading it
    decrypts with the key :func:`load_plans` put in ``session.info``.
    A row whose ciphertext is ``NULL`` returns ``None`` without needing
    a key at all, which is why the unlock above is scoped to owners
    that hold ciphertext.
    """
    if backend.credentials_ct is None or backend.user_id in locked:
        credentials: Mapping[str, Any] | None = None
    else:
        revealed = backend.credentials
        credentials = None if revealed is None else revealed.reveal()

    config = dict(backend.config or {})
    ttl = config.get("ttl", DEFAULT_TTL)
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        log.warning(
            "ddns.backend.bad_ttl", backend_id=backend.id, ttl=repr(ttl)
        )
        ttl = DEFAULT_TTL

    return BackendPlan(
        backend_id=backend.id,
        backend_type=backend.backend_type,
        domains=(domain.name,),
        credentials=credentials,
        config=config,
        ttl=ttl,
    )


# --------------------------------------------------------------------- #
# The event log
# --------------------------------------------------------------------- #


def _now() -> datetime:
    """Naive UTC — the column is ``DATETIME(6)`` with no timezone."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record_event(
    session: AsyncSession,
    *,
    event_type: str,
    response_code: str | None,
    auth: DeviceAuth | None = None,
    plan: HostnamePlan | None = None,
    client_ip: str | None = None,
    ip: str | None = None,
    message: str | None = None,
) -> None:
    """Queue one ``ddns_event`` row. Never raises.

    Both halves of plan §3.4 in one call: the indexed foreign keys the
    filters use, and the denormalised names that keep an entry readable
    after the device it describes is deleted — which is precisely when
    the log is being read.

    Three model-behaviour decisions are applied here rather than
    inherited:

    * **one row per backend attempt**, carrying that backend's own
      status, not the aggregate the wire saw
      (``event-one-row-per-backend-attempt``,
      ``event-response-is-the-per-backend-status-not-the-aggregate``);
    * a refusal decided before any hostname is looked at — ``911``,
      ``notfqdn``, ``badauth``, ``abuse`` — **still writes a row**
      (``event-911-and-notfqdn-write-no-row``, disposition ``fix``;
      ``event-badauth-writes-no-row``, disposition ``change``). The
      legacy service wrote nothing for any of them, so a router sending
      a malformed hostname appeared in the log as complete silence,
      indistinguishable from a router that had stopped calling;
    * a rate-limited ``/nic/delete`` is logged as a **delete**
      (``event-a-rate-limited-delete-is-logged-as-dns_update``,
      disposition ``fix`` — the legacy branch passes the literal
      ``dns_update``, so "show me this device's refused deletes"
      silently returned nothing).

    **Known gap, stated rather than worked around:** ``ddns_event`` has
    no ``backend_type`` column, so
    ``event-backend-type-is-null-for-outcomes-decided-before-any-backend``
    is not expressible on this schema. It is *not* smuggled into
    ``message``: ``event-detail-is-populated-only-for-rate-limit-refusals``
    is a preserved case, and a free-text column that sometimes carries a
    backend name and sometimes a refusal reason is not a filter. Adding
    the column is a migration, and the host chain admits one author at a
    time.
    """
    session.add(
        DnsEvent(
            created_at=_now(),
            user_id=auth.user_id if auth else None,
            device_id=auth.device.id if auth else None,
            domain_id=plan.domain_id if plan else None,
            hostname_id=plan.hostname_id if plan else None,
            user_email=auth.user_email if auth else None,
            device_name=auth.device.name if auth else None,
            domain_name=plan.domain_name if plan else None,
            hostname=plan.requested[:255] if plan else None,
            event_type=event_type,
            response_code=response_code,
            client_ip=client_ip,
            ip=ip,
            message=message,
        )
    )


async def _commit(session: AsyncSession, *, endpoint: str) -> None:
    """Commit, and never let the commit decide the response.

    ``event-a-failed-event-write-never-fails-the-request``: a full disk
    on the log store must not stop DNS updates. By the time this runs
    the provider has already been written to, so the body is a
    statement about DNS and refusing to send it would be the lie. The
    failure is logged at ``critical`` with the exception text intact —
    reducing it to ``type(exc).__name__`` turns a one-line diagnosis
    into three deploys.

    The cost is stated rather than hidden: a failed commit also loses
    the ``last_ip_*`` write-back and the rate-limit row, so a client
    hammering a broken database is not limited. That is the same trade
    the legacy service made and it is the right one — the alternative
    is refusing updates because the log is full.
    """
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — the whole point
        log.critical(
            "ddns.commit_failed",
            endpoint=endpoint,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        try:
            await session.rollback()
        except Exception as rollback_exc:  # noqa: BLE001  # pragma: no cover
            log.critical("ddns.rollback_failed", error=str(rollback_exc))


# --------------------------------------------------------------------- #
# Shared request preamble
# --------------------------------------------------------------------- #


@dataclass
class Admission:
    """The outcome of authenticate-then-rate-limit."""

    auth: DeviceAuth | None
    refusal: str | None


async def _admit(
    request: Request,
    session: AsyncSession,
    *,
    event_type: str,
    client_ip: str | None,
) -> Admission:
    """Authenticate the device, then charge it a rate-limit slot.

    Order is frozen: ``update-badauth-precedes-911`` and
    ``update-abuse-precedes-911`` say auth and the limiter both run
    before the ``hostname`` parameter is even looked at, and auth is
    first because the limiter is per-device and there is no device
    until the credential verifies.
    """
    credentials = parse_basic_auth(request.headers.get("authorization"))
    auth = await authenticate_device(session, credentials)
    if auth is None:
        record_event(
            session,
            event_type=EVENT_AUTH,
            response_code=STATUS_BADAUTH,
            client_ip=client_ip,
            message=None,
        )
        return Admission(auth=None, refusal=STATUS_BADAUTH)

    _touch_device(auth, request, client_ip)

    config = await load_config(session)
    admitted = await check_rate_limit(
        session, auth, default_per_minute=config.rate_limit_per_minute
    )
    if not admitted:
        limit = effective_rate_limit(auth.device, config.rate_limit_per_minute)
        record_event(
            session,
            event_type=event_type,
            response_code=STATUS_ABUSE,
            auth=auth,
            client_ip=client_ip,
            # The one row kind that carries a message — see
            # `event-detail-is-populated-only-for-rate-limit-refusals`.
            message=f"rate limit exceeded ({limit}/minute)",
        )
        return Admission(auth=None, refusal=STATUS_ABUSE)

    return Admission(auth=auth, refusal=None)


def _touch_device(
    auth: DeviceAuth, request: Request, client_ip: str | None
) -> None:
    """Record that this device called in, whatever the answer turns out to be.

    ``last_seen_at`` answers "which of my devices stopped calling",
    which is a different question from "which of my devices last
    succeeded" — so it moves on a rate-limited call too. The three
    states plan §3.4 insists on (never seen / last call failed / idle)
    are reconstructed by ``worker_jobs.device_statuses`` from this
    column *and* the event log; writing ``last_seen_at`` on success
    only would collapse two of them.

    The addresses stored are the ones the device **called from**, not
    the ones it asked to publish. ``client_ip`` and ``myip`` are
    different facts and the difference is the interesting part of a
    NAT'd update.
    """
    device = auth.device
    device.last_seen_at = _now()
    user_agent = request.headers.get("user-agent")
    # Absent and empty are the same fact here — "the client sent no
    # identification" — and both render as NULL rather than as "".
    # Divergence 2: the header is never *inspected*, which is a
    # different statement from never being recorded.
    device.last_user_agent = user_agent[:255] if user_agent else None
    if client_ip is None:
        return
    if ":" in client_ip:
        device.last_ip_v6 = client_ip
    else:
        device.last_ip_v4 = client_ip


def _record_hostname_events(
    session: AsyncSession,
    results: Iterable[HostnameResult],
    *,
    event_type: str,
    auth: DeviceAuth,
    client_ip: str | None,
    ip: str | None,
) -> None:
    for result in results:
        if not result.attempts:
            record_event(
                session,
                event_type=event_type,
                response_code=result.status,
                auth=auth,
                plan=result.plan,
                client_ip=client_ip,
                ip=ip,
            )
            continue
        for _backend_type, status in result.attempts:
            record_event(
                session,
                event_type=event_type,
                response_code=status,
                auth=auth,
                plan=result.plan,
                client_ip=client_ip,
                ip=ip,
            )


# --------------------------------------------------------------------- #
# GET /nic/checkip
# --------------------------------------------------------------------- #


@router.api_route(
    "/nic/checkip",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def checkip(request: Request) -> Response:
    """Echo the client's address. Unauthenticated and unrate-limited.

    ``HEAD`` is declared here and **only** here. The handler is
    read-only, so ``HEAD`` is safe in the RFC 9110 sense and a
    monitoring probe is exactly what it is for — plan §1, "``HEAD``:
    refused where the handler is not safe". FastAPI's ``APIRoute`` does
    not add ``HEAD`` to a ``GET`` route, so this is a deliberate
    declaration and not a default; the body is suppressed by the ASGI
    server, which is what makes ``Content-Length`` the full wrapper
    length with an empty body.

    Credentials are neither consulted nor rejected
    (``checkip-ignores-credentials-entirely``), and no event row is
    written: the caller is unattributable, a liveness probe polls this
    endpoint far more often than a router updates, and a log of
    unattributed polls would bury the updates the log exists to make
    searchable. Stated because it is a decision, not an omission.
    """
    address = client_address(request) or ""
    if request.query_params.get("format") == "plain":
        # Exact string comparison, so `PLAIN` and `json` both fall back
        # to HTML — `checkip-format-plain-is-exact-match-only`.
        return PlainTextResponse(address)
    return HTMLResponse(CHECKIP_HTML.format(ip=address))


# --------------------------------------------------------------------- #
# GET /nic/update
# --------------------------------------------------------------------- #


@router.get("/nic/update", include_in_schema=False)
async def update(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    client_ip = client_address(request)
    admission = await _admit(
        request, session, event_type=EVENT_UPDATE, client_ip=client_ip
    )
    if admission.refusal is not None:
        await _commit(session, endpoint="update")
        return PlainTextResponse(admission.refusal)
    auth = admission.auth
    assert auth is not None  # narrowed by `refusal is None`

    params = request.query_params
    hostname_param = params.get("hostname")
    myip_param = params.get("myip")

    # Order is frozen and each step has a case that only that order
    # satisfies:
    #   `update-911-empty-hostname`  — `not hostnames` fires before the
    #       regex, so `hostname=` is 911 and a trailing comma is notfqdn
    #   `update-911-myip-checked-before-hostname-syntax` — myip is
    #       validated before the hostname regex, so a request wrong in
    #       both ways answers 911
    if not hostname_param:
        return await _refuse(
            session, STATUS_911, auth, EVENT_UPDATE, client_ip, None
        )

    # `myip` defaults to the client address; an empty `myip=` takes the
    # same path as an omitted one.
    raw_ip = myip_param or client_ip
    if not raw_ip:
        return await _refuse(
            session, STATUS_911, auth, EVENT_UPDATE, client_ip, None
        )
    ip = normalise_address(raw_ip)
    if ip is None:
        return await _refuse(
            session, STATUS_911, auth, EVENT_UPDATE, client_ip, None
        )

    requested = split_hostnames(hostname_param)
    if requested is None:
        return await _refuse(
            session, STATUS_NOTFQDN, auth, EVENT_UPDATE, client_ip, ip
        )

    # The record type. Invisible on the wire on both endpoints — #29
    # mutated `getiptype` to answer `A` for every address and ran the
    # whole 131-case table at 121 executed, 0 failed — so the only
    # assertions that can catch a mis-dispatch here live in
    # `backend/tests/test_router_nic.py` and `test_providers.py`.
    rtype = BaseProvider.getiptype(ip)
    if rtype is False:  # pragma: no cover — `ip` already parsed
        return await _refuse(
            session, STATUS_911, auth, EVENT_UPDATE, client_ip, ip
        )

    plans = await load_plans(session, auth, requested)
    results = await anyio.to_thread.run_sync(
        functools.partial(_run_dns_phase, plans, op="update", ip=ip, rtype=rtype)
    )

    await _persist_updates(session, auth, results, ip=ip, rtype=rtype)
    _record_hostname_events(
        session,
        results,
        event_type=EVENT_UPDATE,
        auth=auth,
        client_ip=client_ip,
        ip=ip,
    )
    await _commit(session, endpoint="update")

    # One line per requested hostname, newline-joined, in request order,
    # with no trailing newline. `"\n".join(...)`, asserted byte for byte.
    return PlainTextResponse(
        "\n".join(f"{result.status} {ip}" for result in results)
    )


async def _persist_updates(
    session: AsyncSession,
    auth: DeviceAuth,
    results: Sequence[HostnameResult],
    *,
    ip: str,
    rtype: str,
) -> None:
    """Write back ``last_ip_*`` and ``last_updated_at`` — on ``good`` only.

    A ``nochg`` leaves all three untouched, which the frozen table
    asserts (``update-nochg-single-backend``) and which
    ``tracked-ip-moves-only-on-good-so-last-updated-at-is-not-a-liveness-signal``
    explains the consequence of: this column is a record of the last
    *change*, not of the last successful call.

    **One column, chosen by the record type**, and the mirror is
    asserted in both directions: a v6 update must leave ``last_ip_v4``
    alone and a v4 update must leave ``last_ip_v6`` alone. The v2 table
    asserted only the first, so a host writing *both* columns on every
    update passed it; #29 added
    ``update-ipv4-persists-last-ip-v4-not-v6`` for the other direction,
    and neither is visible on the wire.
    """
    changed = [
        result
        for result in results
        if result.status == STATUS_GOOD and result.plan.hostname_id is not None
    ]
    if not changed:
        return
    scope = auth.scope
    moment = _now()
    for result in changed:
        row = await scope.get(session, Hostname, result.plan.hostname_id)
        if row is None:  # pragma: no cover — loaded under the same scope
            log.error(
                "ddns.persist.row_vanished", hostname_id=result.plan.hostname_id
            )
            continue
        if rtype == RTYPE_A:
            row.last_ip_v4 = ip
        else:
            row.last_ip_v6 = ip
        row.last_updated_at = moment


async def _refuse(
    session: AsyncSession,
    status: str,
    auth: DeviceAuth,
    event_type: str,
    client_ip: str | None,
    ip: str | None,
) -> Response:
    """A whole-request refusal: one line, one event row, one commit."""
    record_event(
        session,
        event_type=event_type,
        response_code=status,
        auth=auth,
        client_ip=client_ip,
        ip=ip,
    )
    await _commit(session, endpoint=event_type)
    return PlainTextResponse(status)


@router.api_route("/nic/update", methods=["HEAD"], include_in_schema=False)
async def update_head() -> Response:
    """``HEAD /nic/update`` -> 405. Hand-written, and it has to be.

    Flask adds ``HEAD`` to every ``GET`` route and serves it by running
    the handler and discarding the body, so on the legacy service a
    ``HEAD`` is a full DNS write with nothing on the wire to say so —
    measured: ``Content-Length: 18``, empty body, ``last_ip_v4``
    moving. The realistic sender is an uptime monitor configured with
    the router's credentials, and a ``HEAD`` with no ``myip`` repoints
    the hostname at the prober's own address.

    Declaring the ``GET`` route alone does **not** produce this 405 in
    this stack — measured, four readings, in
    ``test_the_head_refusal_does_not_fall_out_of_the_framework``. A
    ``Mount`` matches every method and Starlette prefers a full match
    over a partial one, so the route's own refusal never reaches the
    wire and the catch-all answers 404 instead. This route is what
    produces the 405.
    """
    raise HTTPException(
        status_code=405, detail="Method Not Allowed", headers={"Allow": "GET"}
    )


# --------------------------------------------------------------------- #
# GET /nic/delete
# --------------------------------------------------------------------- #


@router.get("/nic/delete", include_in_schema=False)
async def delete(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    client_ip = client_address(request)
    admission = await _admit(
        request, session, event_type=EVENT_DELETE, client_ip=client_ip
    )
    if admission.refusal is not None:
        await _commit(session, endpoint="delete")
        return PlainTextResponse(admission.refusal)
    auth = admission.auth
    assert auth is not None

    params = request.query_params
    hostname_param = params.get("hostname")
    myip_param = params.get("myip")

    if not hostname_param:
        return await _refuse(
            session, STATUS_911, auth, EVENT_DELETE, client_ip, None
        )

    # **The one real asymmetry with update.** Delete does not
    # substitute the client address for a missing `myip`: `myip` here
    # selects a record family and nothing else, and its absence means
    # "both families" rather than "no address". So the identical
    # request that answers 911 on update succeeds here —
    # `update-911-no-myip-and-no-client-ip` and
    # `delete-myip-does-not-default-to-client-ip` are that pair.
    ip: str | None = None
    rtype: str | None = None
    if myip_param:
        ip = normalise_address(myip_param)
        if ip is None:
            return await _refuse(
                session, STATUS_911, auth, EVENT_DELETE, client_ip, None
            )
        family = BaseProvider.getiptype(ip)
        rtype = None if family is False else family

    requested = split_hostnames(hostname_param)
    if requested is None:
        return await _refuse(
            session, STATUS_NOTFQDN, auth, EVENT_DELETE, client_ip, ip
        )

    plans = await load_plans(session, auth, requested)
    results = await anyio.to_thread.run_sync(
        functools.partial(_run_dns_phase, plans, op="delete", ip=None, rtype=rtype)
    )

    # Delete persists nothing: `last_ip_v4` / `last_ip_v6` /
    # `last_updated_at` are untouched, which the frozen
    # `delete-good-single-backend` records in its `effects:` block and
    # which no runner asserts — `backend/tests/test_router_nic.py` does.
    _record_hostname_events(
        session,
        results,
        event_type=EVENT_DELETE,
        auth=auth,
        client_ip=client_ip,
        # The normalised address, not the raw parameter —
        # `event-ip-address-on-delete-is-the-raw-myip-parameter`,
        # disposition `fix`. The legacy handler logged the query
        # parameter verbatim, so a log grouped by address split one
        # address across every spelling a client happened to use, on
        # the one endpoint where the normalisation is invisible from
        # outside (divergence 1: delete replies carry no IP suffix).
        ip=ip,
    )
    await _commit(session, endpoint="delete")

    # No IP suffix — divergence 1. `good`, `nochg`, `nohost`, `911`,
    # `dnserr`, bare.
    return PlainTextResponse("\n".join(result.status for result in results))


@router.api_route("/nic/delete", methods=["HEAD"], include_in_schema=False)
async def delete_head() -> Response:
    """``HEAD /nic/delete`` -> 405. See :func:`update_head`.

    The decision applies to both writing endpoints, not just update: a
    ``HEAD`` that deletes DNS records is the same defect as one that
    rewrites them, and leaving delete out would be the seam between the
    two.
    """
    raise HTTPException(
        status_code=405, detail="Method Not Allowed", headers={"Allow": "GET"}
    )


__all__ = [
    "CHECKIP_HTML",
    "EVENT_AUTH",
    "EVENT_DELETE",
    "EVENT_UPDATE",
    "STATUS_911",
    "STATUS_ABUSE",
    "STATUS_BADAUTH",
    "STATUS_NOHOST",
    "STATUS_NOTFQDN",
    "BackendPlan",
    "HostnamePlan",
    "HostnameResult",
    "aggregate",
    "load_plans",
    "record_event",
    "router",
    "split_hostnames",
]
