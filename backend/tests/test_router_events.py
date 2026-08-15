"""``GET /api/atrium_ddns/events`` — the log search.

Four acceptance criteria on #46, and each has a plausible-looking
implementation that passes a weaker test. What this file is written to
*prove*:

**1. The filters are indexed queries, not client-side filtering of a
large fetch — measured by two instruments of different shape.**

``test_the_filters_reach_the_sql`` compiles the statement and reads the
``WHERE`` clause: an instrument on *our intent*, and it reds the moment
a filter moves into Python.
``test_the_optimiser_chooses_the_compound_indexes`` runs ``EXPLAIN``
against the live MySQL and reads ``key`` and ``type``: an instrument on
*the optimiser's decision*, which is a different question and can
disagree. The pair matters because a filter can be perfectly present in
the ``WHERE`` clause and still unservable by an index — a function
wrapped round the column, an ``OR`` spanning two of them — and the first
instrument cannot see that at all.
``test_a_non_sargable_predicate_is_visible_only_to_explain``
demonstrates exactly that disagreement, so the pair is shown working
rather than asserted to work.

**2. The two reaches are two mechanisms and stay apart.** #14 asserted
that ``atrium_ddns.events.read.all`` opens the audit log *and nothing
else*. Three tests hold the corners: an ordinary tenant with **no**
``atrium_ddns`` permission at all reads their own log (so the endpoint
was not gated on the cross-tenant grant); a holder of the grant and of
*nothing else* reads across tenants (so the surface was not gated on
``device.manage``); and the same grant leaves ``Domain`` and ``Device``
scoped, asserted through the scope's own predicate rather than through
this endpoint.

**3. A row about a deleted device is still readable — with the device
actually deleted.** ``test_a_deleted_device_leaves_a_readable_row``
issues a real ``DELETE FROM ddns_device``, so ``ON DELETE SET NULL``
fires for real. Writing the row with ``device_id=None`` in the first
place would assert about a shape the database never produced, and would
pass against a schema with no foreign key at all.

**4. Four ways of having no rows are four states.** ``rows: []`` with
``any_rows_in_scope=False`` (nothing ever logged), with ``True``
(filters matched nothing), with ``None`` (never asked, because there
were rows), and a ``403`` (refused). A surface that renders all four as
an empty panel tells a user a fact about their account that is not true.

Everything created here is namespaced by ``PYTEST_XDIST_WORKER``: ten
workers share one MySQL, and a test that hardcodes an email or a device
name produces collisions that read as flakiness.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.auth.principal import Principal
from app.auth.rbac import current_principal
from app.db import get_session_factory
from app.models.auth import User
from fastapi import FastAPI
from httpx import ASGITransport

from atrium_ddns import models as m
from atrium_ddns import router as events_router
from atrium_ddns import router_nic
from atrium_ddns import worker_jobs as wj
from atrium_ddns.providers import PROVIDER_STATUSES, known_services
from atrium_ddns.router import (
    BACKEND_TYPE_NONE,
    EVENT_TYPES,
    EVENTS_MAX_LIMIT,
    RESPONSE_CODES,
    UNATTRIBUTED_RESPONSE_CODES,
    EventFilters,
    build_events_query,
    decode_cursor,
    encode_cursor,
    router,
    unmatchable,
)
from atrium_ddns.scope import (
    CROSS_TENANT_PERMISSION,
    EVENTS_CROSS_TENANT_PERMISSION,
    DdnsScope,
)

W = os.environ.get("PYTEST_XDIST_WORKER", "serial")

#: The three ``.manage`` codes an ordinary tenant role holds. Named here
#: so the "reads the log holding none of them" test is visibly holding
#: *nothing*, rather than holding an empty set nobody looked at.
TENANT_PERMISSIONS = frozenset(
    {
        "atrium_ddns.domain.manage",
        "atrium_ddns.device.manage",
        "atrium_ddns.hostname.manage",
    }
)


def _now() -> datetime:
    return wj._now()


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #


def _password_hash() -> str:
    from fastapi_users.password import PasswordHelper

    return PasswordHelper().hash("unusable-" + "x" * 24)


#: Primary keys of every row :func:`_add_event` wrote, so teardown can
#: delete them by ``id``. See :func:`_purge`.
_WRITTEN_EVENT_IDS: list[int] = []


async def _purge(emails: list[str]) -> None:
    """Remove this module's rows, by index and by primary key.

    **Not** ``DELETE FROM ddns_event WHERE user_email IN (…)``, which is
    the idiom the sibling test modules use and which this file copied
    first. Two things are wrong with it here, and the first one bit:

    **It is a full table scan, and ten workers share one MySQL.**
    ``user_email`` carries no index (``SHOW INDEX FROM ddns_event``:
    ``id``, ``client_ip``, ``created_at``, ``(device_id, created_at)``,
    ``domain_id``, ``hostname_id``, ``(user_id, created_at)`` — and no
    ``user_email``). So the delete takes row locks across the whole
    table, while ``DELETE FROM users`` separately takes locks on the
    same rows through ``ddns_event.user_id``'s ``ON DELETE SET NULL``.
    Two statements touching one set of rows in two different orders,
    once per worker, is a deadlock — observed as
    ``(1213, 'Deadlock found when trying to get lock')`` on a fixture
    setup, intermittently, once this module started running beside
    ``test_router_tenant.py``'s identical purge.

    **It cannot see an unattributed row.**
    ``test_a_tenant_scoped_badauth_filter_is_named_not_served_a_zero``
    writes the shape ``router_nic`` actually produces for a failed
    credential: ``user_id`` **and** ``user_email`` both ``NULL``.
    ``user_email IN (…)`` never matches ``NULL``, so that row survived
    teardown and accumulated one per run, forever. The bug is invisible
    from the test — everything passes — and it is the same
    null-is-not-a-value confusion the surface under test exists to
    prevent, which is a good reason to write it down rather than
    quietly fix it.

    So: delete by ``user_id`` (indexed, and the scope's own column), and
    delete this module's own writes by primary key. Both are point
    lookups; neither scans.

    Events go before users, so that by the time ``DELETE FROM users``
    runs, ``ON DELETE SET NULL`` has nothing left to set. A cascade that
    touches no rows takes no row locks, and that cascade is one half of
    the lock-ordering conflict.

    What this does **not** fix, measured rather than assumed
    -------------------------------------------------------
    Ten workers, this box, full-suite sweeps:

    ==========================================  ==========================
    configuration                               runs with a 1213 deadlock
    ==========================================  ==========================
    milestone tip, this file genuinely absent   3 of 6
    tip + this file, ``user_email`` purge       6 of 8
    tip + this file, indexed purge (this one)   5 of 8
    tip + this file, indexed purge + GET_LOCK   5 of 8
    serial (``-n 0``), any configuration        0 — 604 passed
    ==========================================  ==========================

    **The deadlock predates this file**: row one. It is not introduced
    here, it is made more frequent here, and neither an indexed purge
    nor a named lock around *this* module's teardown moves it — because
    the statement that deadlocks is in ``test_router_tenant.py``'s
    purge, not in this one, and a lock only one participant takes
    serialises nothing.

    ``_purge`` is copy-pasted into **seven** test modules
    (``grep -c 'async def _purge' backend/tests/*.py``), every one of
    them running the unindexed ``DELETE FROM ddns_event WHERE
    user_email IN (…)`` plus a cascading ``DELETE FROM users``. The fix
    is one shared teardown — a ``conftest.py`` this package does not
    have — and that belongs to no issue in this milestone. Surfaced in
    #46's PR rather than designed around; the serial row is the
    instrument that says the *code* is fine and the *harness* contends.

    A first version of the diagnosis was wrong and is worth recording:
    the "is it mine?" experiment removed this file with
    ``docker compose cp … || rm``, the ``cp`` **succeeded**, and the file
    was never removed. It reported "not mine" on a run that still
    contained it. Actually deleting the file reversed the conclusion to
    the honest one above — the same could-not-fail shape this repository
    keeps cataloguing, in a diagnostic rather than in a probe.
    """
    factory = get_session_factory()
    async with factory() as s:
        if _WRITTEN_EVENT_IDS:
            await s.execute(
                sa.delete(m.DnsEvent).where(
                    m.DnsEvent.id.in_(list(_WRITTEN_EVENT_IDS))
                )
            )
            _WRITTEN_EVENT_IDS.clear()
        user_ids = list(
            (
                await s.execute(sa.select(User.id).where(User.email.in_(emails)))
            ).scalars()
        )
        if user_ids:
            await s.execute(
                sa.delete(m.DnsEvent).where(m.DnsEvent.user_id.in_(user_ids))
            )
        for email in emails:
            await s.execute(sa.text("DELETE FROM users WHERE email = :e"), {"e": email})
        await s.commit()


@pytest_asyncio.fixture
async def tenants() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a domain, a device and a hostname.

    Two rather than one because the scope is asserted behaviourally:
    tenant A asks for the log and tenant B's rows must not be in it. A
    single-tenant fixture cannot fail that assertion.
    """
    factory = get_session_factory()
    emails = [f"ddns-log-a-{W}@example.invalid", f"ddns-log-b-{W}@example.invalid"]
    await _purge(emails)

    built: dict[str, Any] = {"emails": emails}
    async with factory() as s:
        for tag, email in (("a", emails[0]), ("b", emails[1])):
            user = User(
                email=email,
                hashed_password=_password_hash(),
                is_active=True,
                is_verified=True,
                full_name=f"DDNS log probe {W}",
                preferred_language="en",
            )
            s.add(user)
            await s.flush()
            domain = m.Domain(user_id=user.id, name=f"{tag}-log-{W}.example.invalid")
            device = m.Device(
                user_id=user.id,
                username=f"ddns-log-{tag}-{W}",
                password_hash="$argon2id$v=19$m=65536,t=3,p=4$fake$fake",
                name=f"router-{tag}-{W}",
            )
            s.add_all([domain, device])
            await s.flush()
            hostname = m.Hostname(
                domain_id=domain.id,
                device_id=device.id,
                name=f"host-{tag}.{domain.name}",
            )
            s.add(hostname)
            await s.flush()
            built[tag] = {
                "user": user,
                "user_id": user.id,
                "email": email,
                "domain_id": domain.id,
                "domain_name": domain.name,
                "device_id": device.id,
                "device_name": device.name,
                "hostname_id": hostname.id,
                "hostname": hostname.name,
            }
        await s.commit()

    yield built

    await _purge(emails)


def _app(user: User, permissions: set[str]) -> FastAPI:
    """The host router with the principal overridden.

    ``current_principal`` rather than ``current_user``: ``require_perm``
    and ``atrium_ddns.scope.get_scope`` both resolve through it, so one
    override drives the gate *and* the tenancy — and the pair cannot
    drift apart in a test the way two separate fakes would.
    """
    application = FastAPI()
    application.include_router(router)

    async def _principal() -> Principal:
        return Principal(
            user=user,
            permissions=frozenset(permissions),
            auth_method="password",
            token_id=None,
            auth_session_id=None,
        )

    application.dependency_overrides[current_principal] = _principal
    return application


async def _events(
    user: User,
    permissions: set[str] | None = None,
    **params: Any,
) -> httpx.Response:
    perms = set(TENANT_PERMISSIONS) if permissions is None else permissions
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(user, perms)), base_url="http://log.test"
    ) as client:
        return await client.get("/api/atrium_ddns/events", params=params)


async def _add_event(**kwargs: Any) -> int:
    """Write one event and remember its id for teardown.

    The id is recorded rather than the row being found again later,
    because one test deliberately writes a row with no ``user_id`` and
    no ``user_email`` — the shape a failed credential produces — and
    there is no attribute on it to find it by. See :func:`_purge`.
    """
    factory = get_session_factory()
    async with factory() as s:
        event = m.DnsEvent(**kwargs)
        s.add(event)
        await s.flush()
        eid = event.id
        await s.commit()
    _WRITTEN_EVENT_IDS.append(eid)
    return eid


def _line(tenant: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """A fully denormalised event row, the way ``router_nic`` writes it."""
    row: dict[str, Any] = {
        "created_at": _now(),
        "user_id": tenant["user_id"],
        "user_email": tenant["email"],
        "device_id": tenant["device_id"],
        "device_name": tenant["device_name"],
        "domain_id": tenant["domain_id"],
        "domain_name": tenant["domain_name"],
        "hostname_id": tenant["hostname_id"],
        "hostname": tenant["hostname"],
        "event_type": router_nic.EVENT_UPDATE,
        "response_code": "good",
        "client_ip": "2001:0db8:0000:0000:0000:0000:0000:0001",
        "ip": "2001:0db8:0000:0000:0000:0000:0000:0001",
        "backend_type": "aws",
        "message": None,
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------- #
# 1. The filters are indexed queries — instrument A, the emitted SQL
# --------------------------------------------------------------------- #


def _sql(stmt: Any) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_the_filters_reach_the_sql() -> None:
    """Every filter lands in the ``WHERE`` clause, with a vacuity check.

    The vacuity check is the half that matters. Every assertion below is
    "this string is in the SQL", and an implementation that ignored the
    filters entirely would fail them — but an implementation that put
    *every* column name in the SQL for unrelated reasons would pass
    them. So the unfiltered statement is compiled first and asserted
    **not** to contain the same strings: the marker has to arrive with
    the filter and not with the model.
    """
    scope = DdnsScope(user_id=7, permissions=frozenset())

    bare = _sql(
        build_events_query(
            scope=scope, filters=EventFilters(), cursor=None, limit=10
        )
    )
    # The scope's own predicate is always there — that is the control.
    assert "ddns_event.user_id = 7" in bare

    cases: list[tuple[EventFilters, str]] = [
        (EventFilters(device_id=42), "ddns_event.device_id = 42"),
        (EventFilters(domain_id=43), "ddns_event.domain_id = 43"),
        (EventFilters(hostname_id=44), "ddns_event.hostname_id = 44"),
        (EventFilters(event_type="update"), "ddns_event.event_type = 'update'"),
        (EventFilters(response_code="nochg"), "ddns_event.response_code = 'nochg'"),
        (EventFilters(backend_type="aws"), "ddns_event.backend_type = 'aws'"),
        (EventFilters(backend_type=BACKEND_TYPE_NONE), "ddns_event.backend_type IS NULL"),
        (EventFilters(client_ip="192.0.2.1"), "ddns_event.client_ip = '192.0.2.1'"),
        (EventFilters(since="2026-08-01T00:00:00Z"), "ddns_event.created_at >="),
        (EventFilters(until="2026-08-02T00:00:00Z"), "ddns_event.created_at <="),
    ]
    for filters, marker in cases:
        sql = _sql(
            build_events_query(scope=scope, filters=filters, cursor=None, limit=10)
        )
        assert marker in sql, f"{filters} did not reach the SQL"
        assert marker not in bare, f"{marker!r} is in the unfiltered SQL too"

    # And the fetch is bounded. A filter that narrows the WHERE clause
    # but returns the whole retention window is the same defect with a
    # smaller blast radius.
    assert "LIMIT 10" in bare


def test_the_query_has_no_offset_and_orders_by_the_index_tail() -> None:
    """Keyset, not offset, and newest first.

    ``LIMIT 100 OFFSET 5000`` reads 5100 rows to return 100 — the
    "client-side filtering of a large fetch" defect moved one layer down
    where it is harder to see. Asserted as an absence *plus* the
    presence of the thing that replaces it, because an absence on its
    own also passes against a query that does not paginate at all.
    """
    scope = DdnsScope(user_id=7, permissions=frozenset())
    stmt = build_events_query(
        scope=scope,
        filters=EventFilters(),
        cursor=(datetime(2026, 8, 15, 12, 0, 0), 99),
        limit=10,
    )
    sql = _sql(stmt)
    assert "OFFSET" not in sql.upper()
    assert "ORDER BY ddns_event.created_at DESC, ddns_event.id DESC" in sql
    # The sargable shape: a plain range on the compound index's trailing
    # column, ANDed with a tie-break — not one OR spanning both columns.
    assert "ddns_event.created_at <= " in sql
    assert "ddns_event.id < 99" in sql


def test_the_cursor_round_trips_and_refuses_junk() -> None:
    at = datetime(2026, 8, 15, 12, 34, 56, 789012)
    raw = encode_cursor(at, 4242)
    assert raw == "2026-08-15T12:34:56.789012Z|4242"
    assert decode_cursor(raw) == (at, 4242)
    assert decode_cursor(None) is None
    assert decode_cursor("") is None

    from fastapi import HTTPException

    for junk in ("nonsense", "2026-08-15T12:00:00Z|", "|4242", "not-a-date|1"):
        with pytest.raises(HTTPException) as caught:
            decode_cursor(junk)
        assert caught.value.status_code == 400, junk


# --------------------------------------------------------------------- #
# 1b. Instrument B — the optimiser's own decision
# --------------------------------------------------------------------- #


async def _explain(stmt: Any) -> dict[str, Any]:
    """``EXPLAIN`` one statement against the live MySQL, as a dict.

    A different instrument from reading the compiled SQL, and
    deliberately so: this one is the optimiser's answer to "how would
    you run this", which the ORM cannot know and a compiled-SQL
    assertion cannot see.
    """
    factory = get_session_factory()
    async with factory() as s:
        compiled = stmt.compile(
            dialect=s.bind.dialect, compile_kwargs={"literal_binds": True}
        )
        rows = (await s.execute(sa.text(f"EXPLAIN {compiled}"))).mappings().all()
    assert rows, "EXPLAIN returned nothing"
    return dict(rows[0])


def _possible(plan: dict[str, Any]) -> set[str]:
    """``EXPLAIN.possible_keys`` as a set. ``NULL`` is the empty set."""
    raw = plan.get("possible_keys")
    return set(raw.split(",")) if raw else set()


# Why these tests assert on `possible_keys` and not on `key`
# ----------------------------------------------------------
# `key` is *which index the optimiser chose*, and that is a cost-model
# decision — a function of how many rows are in the table right now.
# Measured on this stack, `ddns_event` holding 14 rows:
#
#   WHERE created_at >= '2026-08-01' ORDER BY created_at DESC
#     -> type=ALL, key=NULL, possible_keys=ix_ddns_event_created_at
#
# MySQL lists the index as usable and then declines it, because at 14
# rows a full scan is genuinely cheaper. An assertion on `key` would
# therefore fail on an empty test database and pass in production, which
# is the worst direction for a guard to be wrong in — and the reverse is
# just as available: a `key` assertion that passes today flips the first
# time one tenant owns most of the table.
#
# `possible_keys` answers a different and stable question: *can this
# predicate use that index at all?* That is a property of the schema and
# the predicate, not of the row count, and it is exactly what the
# acceptance criterion means by "the filters are indexed queries". The
# same 14-row table answers:
#
#   WHERE DATE(created_at) = '2026-08-15'   -> possible_keys=NULL
#
# — the non-sargable form, distinguished from the sargable one at any
# data volume, including zero.


@pytest.mark.asyncio
async def test_every_filter_is_servable_by_an_index(
    tenants: dict[str, Any],
) -> None:
    """The optimiser agrees each filter *can* use an index, by name.

    Instrument B on the acceptance criterion. The two the criterion
    names by name — ``(user_id, created_at)`` and
    ``(device_id, created_at)`` — are asserted by name; the rest are
    asserted to reach *some* index, because which one is the optimiser's
    business and the claim is only that a filter is not a scan.
    """
    a = tenants["a"]
    await _add_event(**_line(a))

    scope = DdnsScope(user_id=a["user_id"], permissions=frozenset())

    def q(filters: EventFilters) -> Any:
        return build_events_query(
            scope=scope, filters=filters, cursor=None, limit=100
        )

    # The tenancy predicate alone — every request on this surface carries
    # it, so this is the floor.
    plan = await _explain(q(EventFilters()))
    assert "ix_ddns_event_user_created" in _possible(plan), plan

    plan = await _explain(q(EventFilters(device_id=a["device_id"])))
    assert "ix_ddns_event_device_created" in _possible(plan), plan

    for filters in (
        EventFilters(domain_id=a["domain_id"]),
        EventFilters(hostname_id=a["hostname_id"]),
        EventFilters(client_ip="2001:0db8:0000:0000:0000:0000:0000:0001"),
        EventFilters(since="2026-08-01T00:00:00Z"),
    ):
        plan = await _explain(q(filters))
        assert _possible(plan), (filters, plan)


@pytest.mark.asyncio
async def test_the_cross_tenant_view_is_still_an_indexed_query(
    tenants: dict[str, Any],
) -> None:
    """The case the tenancy predicate does *not* protect.

    For an ordinary tenant, ``ddns_event.user_id = :me`` is a leading
    equality on ``(user_id, created_at)``, so **no filter can degrade
    this query to a full scan** — the scope carries it. That is a
    comfortable property and it is also a blind spot: it means the
    tenant-scoped tests above cannot detect a filter that the index
    could not serve.

    For a caller holding ``atrium_ddns.events.read.all`` the scope
    predicate compiles to a literal ``true``, the protection is gone,
    and a non-sargable filter really is a scan of the whole retention
    window across every tenant. This is the reading that matters, and it
    is the one an implementation is least likely to have taken.
    """
    a = tenants["a"]
    await _add_event(**_line(a))
    support = DdnsScope(
        user_id=a["user_id"],
        permissions=frozenset({EVENTS_CROSS_TENANT_PERMISSION}),
    )

    # The scope really is unrestricted here — otherwise this test is the
    # tenant-scoped one again under a different name.
    assert support.reaches_all_tenants(m.DnsEvent)

    for filters, index in (
        (EventFilters(since="2026-08-01T00:00:00Z"), "ix_ddns_event_created_at"),
        (EventFilters(device_id=a["device_id"]), "ix_ddns_event_device_created"),
        (EventFilters(user_id=a["user_id"]), "ix_ddns_event_user_created"),
        (EventFilters(hostname_id=a["hostname_id"]), "ix_ddns_event_hostname_id"),
    ):
        plan = await _explain(
            build_events_query(
                scope=support, filters=filters, cursor=None, limit=100
            )
        )
        assert index in _possible(plan), (filters, plan)


@pytest.mark.asyncio
async def test_a_non_sargable_predicate_is_visible_only_to_explain(
    tenants: dict[str, Any],
) -> None:
    """Show the two instruments disagreeing, so the pair is not a ritual.

    ``DATE(created_at) = …`` is the natural way to write "on this day"
    and it returns exactly the right rows. Instrument A — the compiled
    SQL — sees a filter naming ``created_at`` and is satisfied.
    Instrument B sees ``possible_keys = NULL``: a function wrapped round
    an indexed column cannot use the index, at any row count.

    This is not a test of the shipped query. It is the demonstration
    that instrument B can red while instrument A is green, which is the
    only reason to keep both. If it ever stops showing a disagreement,
    the pair has collapsed into one instrument and the guard above is
    worth less than it reads.

    Run against the **cross-tenant** scope on purpose. Under the tenant
    predicate this demonstration does not work: ``user_id = :me`` is a
    leading equality that keeps ``ix_ddns_event_user_created`` in
    ``possible_keys`` however the rest of the ``WHERE`` clause is
    written, so both queries look indexed and the disagreement is
    invisible. Measured, rather than reasoned about — the first version
    of this test asserted on the tenant scope and passed for a reason
    that had nothing to do with what it claimed.
    """
    a = tenants["a"]
    await _add_event(**_line(a))
    support = DdnsScope(
        user_id=a["user_id"],
        permissions=frozenset({EVENTS_CROSS_TENANT_PERMISSION}),
    )

    good = build_events_query(
        scope=support,
        filters=EventFilters(since="2026-08-01T00:00:00Z"),
        cursor=None,
        limit=100,
    )
    bad = (
        support.select(m.DnsEvent)
        .where(sa.func.date(m.DnsEvent.created_at) == sa.literal("2026-08-15"))
        .order_by(m.DnsEvent.created_at.desc(), m.DnsEvent.id.desc())
        .limit(100)
    )

    # Instrument A cannot tell them apart: both name the column.
    assert "created_at" in _sql(good)
    assert "created_at" in _sql(bad)

    # Instrument B can, and the difference is total rather than a
    # matter of degree.
    good_plan = await _explain(good)
    bad_plan = await _explain(bad)
    assert "ix_ddns_event_created_at" in _possible(good_plan), good_plan
    assert _possible(bad_plan) == set(), bad_plan


# --------------------------------------------------------------------- #
# 2. Two reaches, two mechanisms
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_tenant_holding_no_ddns_permission_reads_their_own_log(
    tenants: dict[str, Any],
) -> None:
    """The endpoint is not gated on the cross-tenant grant.

    Driven with an **empty** permission set, which is the strongest form
    of the assertion: it fails against a handler carrying any
    ``require_perm`` at all, including one gated on a code every tenant
    role happens to hold today.
    """
    a, b = tenants["a"], tenants["b"]
    await _add_event(**_line(a))
    await _add_event(**_line(b))

    response = await _events(a["user"], permissions=set())
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["rows"]) == 1
    assert body["rows"][0]["user_email"] == a["email"]
    assert body["cross_tenant"] is False
    # Non-vacuous: tenant B's row exists and is invisible, rather than
    # the table being empty.
    assert all(row["user_email"] != b["email"] for row in body["rows"])


@pytest.mark.asyncio
async def test_events_read_all_alone_opens_the_cross_tenant_view(
    tenants: dict[str, Any],
) -> None:
    """The grant works while holding nothing else.

    A support role is ``{atrium_ddns.events.read.all}`` and nothing
    more. If this endpoint had been gated on ``device.manage`` — the
    board's gate, and the tempting one to reuse — that role would be
    refused outright, and #14's "different reaches" would have become
    one reach with a second name.
    """
    a, b = tenants["a"], tenants["b"]
    await _add_event(**_line(a))
    await _add_event(**_line(b))

    response = await _events(
        a["user"], permissions={EVENTS_CROSS_TENANT_PERMISSION}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cross_tenant"] is True
    seen = {row["user_email"] for row in body["rows"]}
    assert a["email"] in seen and b["email"] in seen


@pytest.mark.asyncio
async def test_events_read_all_opens_the_log_and_nothing_else() -> None:
    """The same grant leaves every other model scoped.

    Asserted through the scope's own predicate rather than through this
    endpoint, because the claim is about the *permission's reach* and
    not about one handler's behaviour — a handler that never queries
    ``Domain`` would pass a behavioural version of this test while the
    grant quietly widened it for everyone else.

    Derived over the whole registry rather than a list of two models, so
    a table added by a later issue is covered the moment it is
    classified.
    """
    from atrium_ddns.scope import TENANT_PATHS

    support = DdnsScope(
        user_id=99, permissions=frozenset({EVENTS_CROSS_TENANT_PERMISSION})
    )
    admin = DdnsScope(user_id=99, permissions=frozenset({CROSS_TENANT_PERMISSION}))

    widened = {
        model for model in TENANT_PATHS if support.reaches_all_tenants(model)
    }
    assert widened == {m.DnsEvent}, widened

    # The control: the *other* cross-tenant code widens everything, so
    # "reaches exactly one model" is a property of this grant and not an
    # artefact of how `reaches_all_tenants` is written.
    assert {model for model in TENANT_PATHS if admin.reaches_all_tenants(model)} == set(
        TENANT_PATHS
    )


@pytest.mark.asyncio
async def test_filtering_to_another_tenant_is_refused_not_narrowed(
    tenants: dict[str, Any],
) -> None:
    """403, naming the permission — not an empty list, not your own rows.

    Both wrong answers are *plausible* and both are worse than the
    refusal. Narrowing silently answers a question nobody asked, in a
    shape indistinguishable from the answer to the one they did ask; an
    empty list is a false negative carrying the authority of a
    measurement.
    """
    a, b = tenants["a"], tenants["b"]
    await _add_event(**_line(a))
    await _add_event(**_line(b))

    refused = await _events(a["user"], user_id=b["user_id"])
    assert refused.status_code == 403, refused.text
    assert EVENTS_CROSS_TENANT_PERMISSION in refused.json()["detail"]

    # Filtering to *yourself* is reach one and is allowed: it is a no-op
    # the scope already enforces, and refusing it would make a
    # pre-applied link from your own row fail for you.
    mine = await _events(a["user"], user_id=a["user_id"])
    assert mine.status_code == 200, mine.text
    assert len(mine.json()["rows"]) == 1

    # And with the grant, the same request that was refused succeeds and
    # returns the *other* tenant's rows — so the 403 above was about the
    # permission and not about the parameter being rejected outright.
    allowed = await _events(
        a["user"],
        permissions={EVENTS_CROSS_TENANT_PERMISSION},
        user_id=b["user_id"],
    )
    assert allowed.status_code == 200, allowed.text
    rows = allowed.json()["rows"]
    assert rows and all(row["user_email"] == b["email"] for row in rows)


# --------------------------------------------------------------------- #
# 3. A deleted device — with the device actually deleted
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_deleted_device_leaves_a_readable_row(
    tenants: dict[str, Any],
) -> None:
    """``ON DELETE SET NULL`` fires for real, and the name survives it.

    The device row is deleted with a real ``DELETE``, so the database
    performs the ``SET NULL`` rather than the test asserting about a row
    it wrote with ``device_id=None``. That distinction is the whole
    test: the null-from-the-start version passes identically against a
    schema with no foreign key, against a foreign key declared
    ``ON DELETE RESTRICT``, and against a writer that never captured the
    name — three defects it cannot see.

    Both halves are asserted. The name is still there (so the row reads)
    **and** the id is now null (so the UI knows there is nothing left to
    filter on, and can say *deleted* instead of drawing an inert link).
    """
    a = tenants["a"]
    device_name = a["device_name"]
    await _add_event(**_line(a))

    before = (await _events(a["user"])).json()["rows"][0]
    assert before["device_id"] == a["device_id"]
    assert before["device_name"] == device_name

    factory = get_session_factory()
    async with factory() as s:
        # The hostname points at the device too, and its FK is not
        # SET NULL; clear it so the delete is about the event's FK.
        await s.execute(
            sa.update(m.Hostname)
            .where(m.Hostname.device_id == a["device_id"])
            .values(device_id=None)
        )
        await s.execute(sa.delete(m.Device).where(m.Device.id == a["device_id"]))
        await s.commit()

        # The device is really gone, not merely dereferenced.
        remaining = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(m.Device)
                .where(m.Device.id == a["device_id"])
            )
        ).scalar_one()
    assert remaining == 0

    after = (await _events(a["user"])).json()["rows"][0]
    assert after["id"] == before["id"], "the log row itself was deleted"
    assert after["device_name"] == device_name, "the row went unreadable"
    assert after["device_id"] is None, "ON DELETE SET NULL did not fire"
    # The rest of the line is untouched: a deleted device must not take
    # the domain, the hostname or the address with it.
    assert after["hostname"] == a["hostname"]
    assert after["domain_name"] == a["domain_name"]
    assert after["client_ip"] == before["client_ip"]


# --------------------------------------------------------------------- #
# 4. Four ways of having no rows
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_empty_is_four_states_and_not_one(tenants: dict[str, Any]) -> None:
    """*Never logged*, *filtered out*, *not asked* and *refused*.

    Rendering these in one type is the single most common way the
    "``n/a`` is never ``0``" family arises, and on this surface it would
    tell a user with a working filter that they have no devices.
    """
    a, b = tenants["a"], tenants["b"]

    # (i) nothing has ever been logged for this tenant.
    nothing = (await _events(a["user"])).json()
    assert nothing["rows"] == []
    assert nothing["any_rows_in_scope"] is False

    await _add_event(**_line(a))

    # (ii) rows exist, but not these. The probe is asked and says so.
    filtered = (
        await _events(a["user"], event_type=router_nic.EVENT_DELETE)
    ).json()
    assert filtered["rows"] == []
    assert filtered["any_rows_in_scope"] is True
    assert filtered["filters"]["event_type"] == router_nic.EVENT_DELETE

    # (iii) rows came back, so the question was never asked. `None`, and
    # deliberately not `False` — a `False` here would read as "the scope
    # is empty" on a page that just returned rows.
    full = (await _events(a["user"])).json()
    assert len(full["rows"]) == 1
    assert full["any_rows_in_scope"] is None

    # (iv) refused. A status code, not an empty list.
    refused = await _events(a["user"], user_id=b["user_id"])
    assert refused.status_code == 403


@pytest.mark.asyncio
async def test_an_unmatchable_filter_value_is_named_rather_than_returning_a_bare_zero(
    tenants: dict[str, Any],
) -> None:
    """A typo must not read like a measurement.

    ``backend_type=rout53`` returns zero rows, and zero rows is exactly
    what "no traffic for that provider" looks like. The query still runs
    — V1M4 imports legacy history and a service name this build does not
    ship must stay findable — but the response *names* the value that
    cannot match, so the empty panel can say which of the two it is.
    """
    a = tenants["a"]
    await _add_event(**_line(a))

    typo = (await _events(a["user"], backend_type="rout53")).json()
    assert typo["rows"] == []
    assert [f["filter"] for f in typo["unmatchable_filters"]] == [
        "backend_type=rout53"
    ]
    # The reason travels with it. "No rows" has three causes on this
    # surface and only the reason separates them.
    assert "not a provider" in typo["unmatchable_filters"][0]["reason"]
    # The control: a real provider name is not named as unmatchable,
    # even when it happens to match nothing.
    real = (await _events(a["user"], backend_type=known_services()[0])).json()
    assert real["unmatchable_filters"] == []


def test_unmatchable_is_silent_on_every_offered_value() -> None:
    """Nothing the UI can select is ever reported as unmatchable.

    Derived over the shipped vocabulary rather than spot-checked, so an
    option added to either list is covered without this test changing.
    """
    from atrium_ddns.router import _vocabulary

    vocabulary = _vocabulary()

    def flags(filters: EventFilters) -> list[str]:
        return [
            f.filter for f in unmatchable(filters, vocabulary, cross_tenant=True)
        ]

    for value in vocabulary.event_types:
        assert flags(EventFilters(event_type=value)) == []
    for value in vocabulary.response_codes:
        # `cross_tenant=True`, because the unattributed-row warning is a
        # property of the *caller* and is asserted in its own test.
        assert flags(EventFilters(response_code=value)) == []
    for value in [*vocabulary.backend_types, vocabulary.backend_type_none]:
        assert flags(EventFilters(backend_type=value)) == []
    # Vacuity: the function is capable of returning something.
    assert flags(EventFilters(event_type="nope")) != []
    # And the tenant-scoped reading of the same offered value does flag,
    # so "silent on every offered value" above is about the reach and
    # not about the function being inert.
    assert [
        f.filter
        for f in unmatchable(
            EventFilters(response_code="badauth"), vocabulary, cross_tenant=False
        )
    ] == ["response_code=badauth"]


# --------------------------------------------------------------------- #
# The vocabulary is derived from the writers, not retyped
# --------------------------------------------------------------------- #


def test_the_event_type_vocabulary_is_every_event_type_with_a_writer() -> None:
    """Re-derived from ``router_nic``'s own constants.

    ``models.DnsEvent``'s comment lists five values; only three have a
    writer. Offering ``checkip`` or ``healthcheck`` would ship a select
    option that can never match a row — the artefact-with-no-writer
    defect wearing a dropdown's clothes, and indistinguishable to a user
    from "that has not happened lately".

    Derived rather than hardcoded so a fourth ``EVENT_*`` added to
    ``router_nic`` fails here rather than silently going unfilterable.
    """
    declared = {
        value
        for name, value in vars(router_nic).items()
        if name.startswith("EVENT_") and isinstance(value, str)
    }
    assert declared, "no EVENT_* constants found — the derivation is vacuous"
    assert set(EVENT_TYPES) == declared, (set(EVENT_TYPES), declared)
    assert "checkip" not in EVENT_TYPES
    assert "healthcheck" not in EVENT_TYPES


def test_the_response_code_vocabulary_covers_both_halves_of_the_wire() -> None:
    """Provider statuses plus the refusals ``router_nic`` decides itself."""
    declared = {
        value
        for name, value in vars(router_nic).items()
        if name.startswith("STATUS_") and isinstance(value, str)
    }
    assert declared, "no STATUS_* constants found — the derivation is vacuous"
    assert declared <= set(RESPONSE_CODES), declared - set(RESPONSE_CODES)
    assert PROVIDER_STATUSES <= set(RESPONSE_CODES)


def test_the_null_backend_sentinel_cannot_collide_with_a_provider() -> None:
    """Three states need three spellings, and the third must be safe."""
    assert BACKEND_TYPE_NONE not in known_services()
    assert known_services(), "the provider registry is empty — vacuous"


def test_unattributed_codes_are_derived_from_the_writer_not_listed() -> None:
    """Read off ``router_nic``'s AST: which ``record_event`` calls omit ``auth``.

    A hardcoded ``{'badauth'}`` is the identical defect one release
    later — a writer that stops passing ``auth`` on some other refusal
    would silently join the set, and the surface would keep serving that
    filter a false zero to every tenant.

    So the set is re-derived here from the writer's own call sites:
    every ``record_event(...)`` in ``router_nic`` that does **not** pass
    ``auth=`` writes a row with no tenant, and the ``response_code`` it
    passes is the code that becomes unattributable. The AST rather than
    a grep, so a call spanning lines or carrying a comment does not
    change the answer.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(router_nic))
    constants = {
        name: value
        for name, value in vars(router_nic).items()
        if isinstance(value, str)
    }

    derived: set[str] = set()
    seen_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "record_event":
            continue
        seen_calls += 1
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        if "auth" in kwargs:
            continue  # the row carries a device, and therefore a tenant
        code = kwargs.get("response_code")
        if isinstance(code, ast.Name) and code.id in constants:
            derived.add(constants[code.id])
        elif isinstance(code, ast.Constant) and isinstance(code.value, str):
            derived.add(code.value)

    # Vacuity, both ways: the walk found call sites at all, and it found
    # some that *do* pass `auth` — otherwise "these omit auth" is true of
    # everything and the derivation says nothing.
    assert seen_calls >= 3, seen_calls
    assert derived, "no record_event call omits auth= — the derivation is vacuous"
    assert derived != {c for c in constants.values()}, "the filter did nothing"

    assert set(UNATTRIBUTED_RESPONSE_CODES) == derived, (
        set(UNATTRIBUTED_RESPONSE_CODES),
        derived,
    )


@pytest.mark.asyncio
async def test_a_tenant_scoped_badauth_filter_is_named_not_served_a_zero(
    tenants: dict[str, Any],
) -> None:
    """The false negative that would have read as "my credentials are fine".

    ``badauth`` rows are written before any device is identified, so
    they carry no ``user_id`` and are invisible to every tenant — which
    is the correct tenancy answer and the wrong *log search* answer,
    because zero is also what a healthy account looks like. Found by
    running real ``/nic/update`` traffic and reading the result, not by
    reading the code.

    Asserted from both ends: the tenant is told, the cross-tenant reader
    is not (because for them the filter matches normally), and the row
    genuinely is invisible to the tenant rather than merely flagged.
    """
    a = tenants["a"]
    # The shape the writer produces: no user, no device, no names.
    await _add_event(
        created_at=_now(),
        user_id=None,
        user_email=None,
        device_id=None,
        device_name=None,
        domain_id=None,
        domain_name=None,
        hostname_id=None,
        hostname=None,
        event_type=router_nic.EVENT_AUTH,
        response_code="badauth",
        client_ip="203.0.113.10",
        ip=None,
        backend_type=None,
        message=None,
    )
    await _add_event(**_line(a))

    mine = (await _events(a["user"], response_code="badauth")).json()
    assert mine["rows"] == [], "an unattributed row reached a tenant's log"
    assert [f["filter"] for f in mine["unmatchable_filters"]] == [
        "response_code=badauth"
    ]
    reason = mine["unmatchable_filters"][0]["reason"]
    assert "no account" in reason
    # The sentence has to say what the zero is *not* evidence of. That
    # is the whole point of writing it.
    assert "not evidence" in reason

    # The control, and it is what stops this being a blanket warning:
    # for a cross-tenant reader the same filter matches, so it must not
    # be flagged.
    across = (
        await _events(
            a["user"],
            permissions={EVENTS_CROSS_TENANT_PERMISSION},
            response_code="badauth",
        )
    ).json()
    assert across["unmatchable_filters"] == []
    assert len(across["rows"]) >= 1

    # And an ordinary successful filter is not flagged either, or the
    # signal means nothing.
    ok = (await _events(a["user"], response_code="good")).json()
    assert ok["unmatchable_filters"] == []


# --------------------------------------------------------------------- #
# Paging, combination, and the limit ceiling
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_paging_walks_every_row_once(tenants: dict[str, Any]) -> None:
    """Keyset paging loses nothing and repeats nothing — including ties.

    Two rows share a ``created_at`` to the microsecond, which is what a
    router bursting several hostnames writes, and it is the case a
    cursor on the timestamp alone gets wrong: ``created_at < :c`` skips
    the tied row that did not fit on the page, and ``<=`` serves it
    forever.

    **The tie is placed so that it straddles a page boundary**, and that
    placement is the whole test. Measured, not assumed: an earlier
    version put the tied pair in the middle of page two, where both
    members fit on one page and a timestamp-only cursor loses nothing —
    the mutation that removes the ``id`` tie-break survived it, and was
    caught by an unrelated assertion on the compiled SQL. A test that
    names ties in its docstring and cannot see one is worth less than no
    test, because it is believed.

    With ``limit=2`` and ``ORDER BY created_at DESC, id DESC``, page one
    ends on the *higher-id* member of the tied pair, so the lower-id
    member is the first row page two must find.
    """
    a = tenants["a"]
    base = _now()
    tied = base - timedelta(minutes=1)
    stamps = [
        base,
        tied,
        tied,  # deliberate tie, straddling the limit=2 page boundary
        base - timedelta(minutes=2),
        base - timedelta(minutes=3),
    ]
    written = [await _add_event(**_line(a, created_at=at)) for at in stamps]

    seen: list[int] = []
    boundary_page: list[int] = []
    cursor: str | None = None
    for page_number in range(10):  # bounded: never an unbounded `while`
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (await _events(a["user"], **params)).json()
        ids = [row["id"] for row in page["rows"]]
        if page_number == 0:
            boundary_page = ids
        seen.extend(ids)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("paging did not terminate within 10 pages")

    # The placement actually happened: page one ends on one member of
    # the tied pair and not on both. Without this the test can silently
    # stop exercising the tie if the ordering ever changes.
    tied_ids = set(written[1:3])
    assert len(set(boundary_page) & tied_ids) == 1, (boundary_page, tied_ids)

    assert sorted(seen) == sorted(written), "paging lost or invented a row"
    assert len(seen) == len(set(seen)), "a row was served twice"


@pytest.mark.asyncio
async def test_filters_combine(tenants: dict[str, Any]) -> None:
    """Two filters together are an intersection, not the last one wins."""
    a = tenants["a"]
    await _add_event(**_line(a, event_type="update", response_code="good"))
    await _add_event(**_line(a, event_type="update", response_code="nochg"))
    await _add_event(**_line(a, event_type="auth", response_code="badauth"))

    both = (
        await _events(a["user"], event_type="update", response_code="nochg")
    ).json()
    assert len(both["rows"]) == 1
    assert both["rows"][0]["response_code"] == "nochg"

    # The control: each filter alone matches more, so the intersection
    # above is doing work rather than one filter being ignored.
    assert len((await _events(a["user"], event_type="update")).json()["rows"]) == 2
    assert len((await _events(a["user"], response_code="nochg")).json()["rows"]) == 1


@pytest.mark.asyncio
async def test_the_null_backend_filter_is_a_third_state(
    tenants: dict[str, Any],
) -> None:
    """*No filter*, *this provider*, *no provider was reached*.

    ``backend_type IS NULL`` is a meaning — "decided before any backend
    was contacted", which is every ``badauth``, ``abuse``, ``911``,
    ``notfqdn`` and ``nohost`` row. A query string that can only express
    two of the three cannot ask the third question at all.
    """
    a = tenants["a"]
    await _add_event(**_line(a, backend_type="aws"))
    await _add_event(
        **_line(a, backend_type=None, event_type="auth", response_code="badauth")
    )

    assert len((await _events(a["user"])).json()["rows"]) == 2
    assert len((await _events(a["user"], backend_type="aws")).json()["rows"]) == 1

    nulls = (await _events(a["user"], backend_type=BACKEND_TYPE_NONE)).json()
    assert len(nulls["rows"]) == 1
    assert nulls["rows"][0]["backend_type"] is None
    assert nulls["rows"][0]["response_code"] == "badauth"


@pytest.mark.asyncio
async def test_the_limit_ceiling_refuses_rather_than_clamps(
    tenants: dict[str, Any],
) -> None:
    """A limit above the ceiling is a 422, not a silent clamp.

    Clamping would report ``limit`` back as the ceiling while the caller
    believes it asked for more — and a caller paging on its own count
    would then stop early and call the log short.
    """
    a = tenants["a"]
    over = await _events(a["user"], limit=EVENTS_MAX_LIMIT + 1)
    assert over.status_code == 422, over.text
    at_ceiling = await _events(a["user"], limit=EVENTS_MAX_LIMIT)
    assert at_ceiling.status_code == 200
    assert at_ceiling.json()["limit"] == EVENTS_MAX_LIMIT


@pytest.mark.asyncio
async def test_the_retention_window_comes_from_the_config(
    tenants: dict[str, Any],
) -> None:
    """The UI's "the log holds the last N days" is read, never typed.

    Compared against ``load_config`` rather than against the literal
    30: an operator who changes ``event_retention_days`` must not be
    able to make that sentence wrong, and a test asserting ``== 30``
    would keep passing while the panel lied.
    """
    a = tenants["a"]
    factory = get_session_factory()
    async with factory() as s:
        expected = (await wj.load_config(s)).event_retention_days
    assert (await _events(a["user"])).json()["retention_days"] == expected


@pytest.mark.asyncio
async def test_a_malformed_instant_is_refused_not_dropped(
    tenants: dict[str, Any],
) -> None:
    """A range endpoint that cannot be parsed must not widen the query.

    Silently becoming ``None`` would return the whole retention window
    and echo the filter back as applied — a filter that reads as applied
    and is not, which is the "assertions on the report" family.
    """
    a = tenants["a"]
    await _add_event(**_line(a))
    bad = await _events(a["user"], since="last tuesday")
    assert bad.status_code == 400, bad.text
    assert "since" in bad.json()["detail"]


@pytest.mark.asyncio
async def test_the_time_range_narrows(tenants: dict[str, Any]) -> None:
    a = tenants["a"]
    now = _now()
    await _add_event(**_line(a, created_at=now))
    await _add_event(**_line(a, created_at=now - timedelta(days=10)))

    cut = events_router._iso(now - timedelta(days=1))
    recent = (await _events(a["user"], since=cut)).json()
    assert len(recent["rows"]) == 1
    older = (await _events(a["user"], until=cut)).json()
    assert len(older["rows"]) == 1
    assert recent["rows"][0]["id"] != older["rows"][0]["id"]
