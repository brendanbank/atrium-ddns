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
import zlib
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
from conftest import fixture_writes, purge_tenants, unusable_password_hash
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
    PARTIALLY_ATTRIBUTED_RESPONSE_CODES,
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

#: The address every unattributable row this module writes comes from.
#:
#: Per worker, and that is not tidiness. The tally in
#: ``router._unattributable_tally`` counts rows with ``user_id IS NULL``
#: and is **deliberately unscoped** — there is no tenant those rows
#: belong to — so it is the one figure on this surface that ten parallel
#: workers sharing one MySQL can move under each other. Every
#: ``badauth`` row ``test_router_nic.py`` writes with no credentials
#: lands in the same population.
#:
#: ``client_ip`` is a column an ownerless row *does* carry, so the tally
#: honours it, and filtering on this address turns an installation-wide
#: count into a per-worker one. Asserting a bare count instead would be
#: flaky in exactly the way the template calls "diagnosed by sweeping,
#: not by re-running".
UNATTRIBUTED_IP = f"2001:db8:64::{zlib.crc32(W.encode()) & 0xFFFF:x}"

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


#: Primary keys of every row :func:`_add_event` wrote, so teardown can
#: delete them by ``id``. Handed to ``conftest.purge_tenants``, which
#: cannot find them any other way: one of them is written with
#: ``user_id`` and ``user_email`` both NULL — the shape ``router_nic``
#: produces for a failed credential — and ``IN (…)`` never matches NULL,
#: so before #46 that row survived teardown and accumulated one per run
#: forever. Everything passed throughout, which is why it needed
#: measuring rather than reading.
_WRITTEN_EVENT_IDS: list[int] = []


async def _purge_this_module(emails: list[str]) -> None:
    """This module's rows: the shared teardown plus its own event ids.

    #46 wrote a by-primary-key purge here and measured that it did
    **not** move the deadlock rate (5 of 8 with it, 6 of 8 with the
    unindexed form, 3 of 6 with this file removed entirely). That
    measurement was right and #65 explains it: the deadlock is between
    fixture ``INSERT``s on two unique indexes, and no delete strategy
    reaches it. See ``conftest.py``.
    """
    written = list(_WRITTEN_EVENT_IDS)
    _WRITTEN_EVENT_IDS.clear()
    await purge_tenants(
        emails, event_ids=written, owner="test_router_events.tenants"
    )


@pytest_asyncio.fixture
async def tenants() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a domain, a device and a hostname.

    Two rather than one because the scope is asserted behaviourally:
    tenant A asks for the log and tenant B's rows must not be in it. A
    single-tenant fixture cannot fail that assertion.
    """
    emails = [f"ddns-log-a-{W}@example.invalid", f"ddns-log-b-{W}@example.invalid"]
    await _purge_this_module(emails)

    hashed = unusable_password_hash()
    built: dict[str, Any] = {"emails": emails}
    async with fixture_writes("test_router_events.tenants") as s:
        for tag, email in (("a", emails[0]), ("b", emails[1])):
            user = User(
                email=email,
                hashed_password=hashed,
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

    yield built

    await _purge_this_module(emails)


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
    there is no attribute on it to find it by. See
    :func:`_purge_this_module`.
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


@pytest.mark.functional  # migrated MySQL schema / dialect-specific
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


@pytest.mark.functional  # migrated MySQL schema / dialect-specific
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


@pytest.mark.functional  # migrated MySQL schema / dialect-specific
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

    ``badauth`` is now inside this sweep rather than exempted from it.
    Before #64 it was the one offered value a tenant *was* flagged on,
    because no tenant-scoped query could match it; the writer now
    attributes those rows, so flagging the filter would be a false
    statement about a query that works.
    """
    from atrium_ddns.router import _vocabulary

    vocabulary = _vocabulary()

    def flags(filters: EventFilters) -> list[str]:
        return [f.filter for f in unmatchable(filters, vocabulary)]

    for value in vocabulary.event_types:
        assert flags(EventFilters(event_type=value)) == []
    for value in vocabulary.response_codes:
        assert flags(EventFilters(response_code=value)) == []
    for value in [*vocabulary.backend_types, vocabulary.backend_type_none]:
        assert flags(EventFilters(backend_type=value)) == []
    # Vacuity, three ways: the function is capable of returning
    # something on each of the three filters it inspects. Without this
    # the sweep above passes on a function that returns [] always.
    assert flags(EventFilters(event_type="nope")) != []
    assert flags(EventFilters(response_code="nope")) != []
    assert flags(EventFilters(backend_type="nope")) != []
    # And specifically: the value that used to be flagged is not.
    for code in PARTIALLY_ATTRIBUTED_RESPONSE_CODES:
        assert flags(EventFilters(response_code=code)) == [], code


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


def _record_event_call_sites() -> tuple[dict[str, set[str]], int]:
    """Walk ``router_nic`` for every ``record_event`` call, by actor kwarg.

    Returns ``({actor_kwarg_or_'none': {response_code, ...}}, n_calls)``.
    The AST rather than a grep, so a call spanning lines or carrying a
    comment does not change the answer, and the *keyword name* rather
    than a type, because the source is all an AST can see.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(router_nic))
    constants = {
        name: value
        for name, value in vars(router_nic).items()
        if isinstance(value, str)
    }

    by_actor: dict[str, set[str]] = {
        "auth": set(),
        "failed_auth": set(),
        "none": set(),
    }
    seen = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "record_event":
            continue
        seen += 1
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        actor = (
            "auth"
            if "auth" in kwargs
            else "failed_auth"
            if "failed_auth" in kwargs
            else "none"
        )
        code = kwargs.get("response_code")
        if isinstance(code, ast.Name) and code.id in constants:
            by_actor[actor].add(constants[code.id])
        elif isinstance(code, ast.Constant) and isinstance(code.value, str):
            by_actor[actor].add(code.value)
        else:
            # A computed response code — the call site carries an actor
            # but no literal to attribute. Recorded rather than dropped,
            # so a set that quietly shrinks is visible.
            by_actor[actor].add("<computed>")
    return by_actor, seen


def test_partially_attributed_codes_are_derived_from_the_writer_not_listed() -> None:
    """Read off ``router_nic``'s AST: which ``record_event`` calls pass
    ``failed_auth=``.

    A hardcoded ``{'badauth'}`` is the identical defect one release
    later. The set is re-derived from the writer's own call sites: a
    call passing ``failed_auth=`` writes a row whose ``user_id`` is
    populated *only when the submitted username resolved*, so the code
    it carries is attributable sometimes and not always — which is
    exactly what the log surface has to say out loud.
    """
    by_actor, seen = _record_event_call_sites()

    # Vacuity, both ways: the walk found call sites at all, and it found
    # some in *each* category — otherwise "these pass failed_auth" is
    # true of everything or of nothing and the derivation says nothing.
    assert seen >= 3, seen
    assert by_actor["auth"], "no record_event call passes auth= — derivation vacuous"
    assert by_actor["failed_auth"], (
        "no record_event call passes failed_auth= — the derivation is vacuous, "
        "and #64's attribution has been reverted or renamed"
    )
    assert by_actor["auth"] != by_actor["failed_auth"], "the filter did nothing"

    assert set(PARTIALLY_ATTRIBUTED_RESPONSE_CODES) == by_actor["failed_auth"], (
        set(PARTIALLY_ATTRIBUTED_RESPONSE_CODES),
        by_actor["failed_auth"],
    )


def test_no_event_writer_omits_an_actor_entirely() -> None:
    """The guard that replaced ``UNATTRIBUTED_RESPONSE_CODES``.

    That constant named the codes whose rows could carry no tenant *at
    all*, and it existed because ``_admit`` wrote ``badauth`` with no
    actor. #64 emptied it, and an empty constant guarding nothing is
    worse than no constant: the branch that read it becomes unreachable
    and no mutation can show it biting.

    So the property is asserted directly instead. Every ``record_event``
    call passes an actor — ``auth=`` for a verified device,
    ``failed_auth=`` for a refusal that may or may not resolve one. A
    writer added later that passes neither reintroduces a response code
    no tenant can ever see, and it fails **here**, on the commit that
    adds it, rather than by serving every tenant a silent zero.
    """
    by_actor, seen = _record_event_call_sites()
    assert seen >= 3, seen
    assert by_actor["none"] == set(), (
        "these response codes are written with no actor at all, so their "
        f"rows carry user_id NULL and no tenant can ever see them: "
        f"{sorted(by_actor['none'])}. Pass auth= (a verified device) or "
        "failed_auth= (a refusal, attributed when the username resolved). "
        "A tenant filtering the log for one of these gets zero, and zero is "
        "also what a healthy account looks like."
    )


async def _add_unattributed_badauth(client_ip: str = UNATTRIBUTED_IP) -> None:
    """The shape ``_admit`` writes when the username resolved to nothing."""
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
        client_ip=client_ip,
        ip=None,
        backend_type=None,
        message=None,
    )


@pytest.mark.asyncio
async def test_a_tenant_sees_their_own_badauth_and_the_rest_is_counted(
    tenants: dict[str, Any],
) -> None:
    """#64, and #109's exit-criterion clause 2, from the reading end.

    Three populations, deliberately distinguishable:

    * a ``badauth`` row attributed to tenant **a** — their router,
      failing. It must appear in their log, which it could not before.
    * a ``badauth`` row attributed to tenant **b** — someone else's
      router. It must not.
    * a ``badauth`` row attributed to nobody — a username no device
      holds. Invisible to both, and *counted* rather than ignored.

    The counted third is what the acceptance criterion is about: a
    tenant's zero must not read the same as a tenant's zero with lines
    they cannot see.
    """
    a, b = tenants["a"], tenants["b"]

    await _add_event(
        **_line(a, event_type=router_nic.EVENT_AUTH, response_code="badauth")
    )
    await _add_event(
        **_line(b, event_type=router_nic.EVENT_AUTH, response_code="badauth")
    )
    await _add_unattributed_badauth()
    await _add_unattributed_badauth()

    mine = (await _events(a["user"], response_code="badauth")).json()

    # Clause 2's first half: the tenant sees their own failure. Before
    # #64 this list was empty for every tenant, always.
    assert len(mine["rows"]) == 1, mine["rows"]
    assert mine["rows"][0]["user_id"] == a["user_id"]

    # The filter matches, so it must not be described as unmatchable.
    assert mine["unmatchable_filters"] == []

    # Clause 2's second half: the residue is a measurement with its
    # population named, not an absence. Narrowed to this worker's own
    # address — see `UNATTRIBUTED_IP` — because the tally is unscoped by
    # construction and ten workers share one database.
    scoped = (
        await _events(
            a["user"], response_code="badauth", client_ip=UNATTRIBUTED_IP
        )
    ).json()
    tally = scoped["unattributable"]
    assert tally is not None, "a badauth filter was served no tally at all"
    assert tally["response_code"] == "badauth"
    assert tally["rows"] == 2, tally
    assert tally["since"] is not None
    assert tally["ignored_filters"] == []

    # The unnarrowed tally is a superset, never a smaller number. That
    # is the vacuity guard on the narrowing above: if `client_ip` were
    # silently ignored the two would be equal by accident, and this
    # inequality still holds — so the *previous* assertion is what
    # proves it was honoured, and this one proves the population is real.
    assert mine["unattributable"]["rows"] >= 2, mine["unattributable"]

    # The other tenant's view is the control: their own row, not a's.
    theirs = (await _events(b["user"], response_code="badauth")).json()
    assert {r["user_id"] for r in theirs["rows"]} == {b["user_id"]}

    # The admin path the acceptance asks for: the grant that already
    # exists shows every row, attributed or not. Narrowed to this
    # worker's rows for the same reason.
    across = (
        await _events(
            a["user"],
            permissions={EVENTS_CROSS_TENANT_PERMISSION},
            response_code="badauth",
            client_ip=UNATTRIBUTED_IP,
        )
    ).json()
    assert len(across["rows"]) == 2, across["rows"]
    assert all(r["user_id"] is None for r in across["rows"])

    # And an ordinary successful filter is not flagged either, or the
    # signal means nothing.
    ok = (await _events(a["user"], response_code="good")).json()
    assert ok["unmatchable_filters"] == []


@pytest.mark.asyncio
async def test_the_tally_is_not_asked_unless_the_filter_needs_it(
    tenants: dict[str, Any],
) -> None:
    """``None`` is *not asked* and ``0`` is *asked, and none* — two states.

    The probe question, answered explicitly: with no unattributable rows
    in the store the tally prints ``rows=0`` and is still an object,
    which is a different string from the ``null`` an unrelated filter
    gets. Were those the same value the field would be decoration.
    """
    a = tenants["a"]
    await _add_event(**_line(a))

    unrelated = (await _events(a["user"], response_code="good")).json()
    assert unrelated["unattributable"] is None

    # Narrowed to an address nothing wrote from, so "asked, and there
    # were none" is a fact about this query rather than a bet on what
    # nine other workers are doing.
    none_at_all = (
        await _events(
            a["user"], response_code="badauth", client_ip=UNATTRIBUTED_IP
        )
    ).json()
    assert none_at_all["unattributable"] is not None
    assert none_at_all["unattributable"]["rows"] == 0

    # No filter at all: still not asked. The tally answers a question
    # about one code, and a page-wide count would be a different figure
    # wearing the same name.
    unfiltered = (await _events(a["user"])).json()
    assert unfiltered["unattributable"] is None


@pytest.mark.asyncio
async def test_the_tally_names_the_filters_it_could_not_honour(
    tenants: dict[str, Any],
) -> None:
    """A filter an ownerless row cannot carry would zero it by construction.

    ``device_id`` is NULL on every unattributable row — that NULL is
    what makes it unattributable. Narrowing the count by one would
    return ``0`` for every installation forever: a probe that cannot
    fail. The filter is dropped and **named**, so the reader can see the
    population the count is really over.

    ``client_ip`` is the control: an ownerless row does carry it, so it
    is honoured, not named, and the count moves when it is applied.
    """
    a = tenants["a"]
    other = f"{UNATTRIBUTED_IP}:1"
    await _add_unattributed_badauth(UNATTRIBUTED_IP)
    await _add_unattributed_badauth(UNATTRIBUTED_IP)
    await _add_unattributed_badauth(other)

    dropped = (
        await _events(
            a["user"],
            response_code="badauth",
            device_id=99,
            client_ip=UNATTRIBUTED_IP,
        )
    ).json()
    # `device_id` was dropped and named; `client_ip` was honoured. Both
    # halves in one reading: 2 and not 3 proves the honoured one bit, and
    # 2 rather than 0 proves the dropped one did not.
    assert dropped["unattributable"]["ignored_filters"] == ["device_id=99"]
    assert dropped["unattributable"]["rows"] == 2, dropped["unattributable"]

    honoured = (
        await _events(a["user"], response_code="badauth", client_ip=other)
    ).json()
    assert honoured["unattributable"]["ignored_filters"] == []
    assert honoured["unattributable"]["rows"] == 1, "client_ip was not honoured"


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
    ddns_config: Any,
) -> None:
    """The UI's "the log holds the last N days" is read, never typed.

    An operator who changes ``event_retention_days`` must not be able to
    make that sentence wrong, and a test asserting ``== 30`` would keep
    passing while the panel lied.

    Two things this used to get wrong, both fixed by owning the row
    rather than reading it twice (#117).

    **It was flaky.** It read ``load_config`` and then the endpoint, and
    compared the two. ``test_worker_jobs.py`` writes
    ``event_retention_days=90`` into the same un-namespaced row and
    ``--dist=loadfile`` puts that file on another worker, so a write
    landing between the two reads produced ``assert 30 == 90`` — one
    gate run in two, measured by #108's agent. ``ddns_config`` holds a
    cross-worker lock for the length of this test, so there is no
    "between".

    **It was also vacuous.** With nothing seeded, ``load_config``
    returns the model default and so does the endpoint — 30 == 30 — and
    a handler that had hardcoded ``retention_days=30`` would have passed
    every time except during the race that broke it. So the window is
    pinned to a value that is *not* the default, and the vacuity guard
    below says so rather than leaving it to be re-derived.
    """
    a = tenants["a"]

    pinned = 47
    assert pinned != wj.DdnsConfig().event_retention_days, (
        "the pinned window has become the model default; this test would then "
        "pass against a handler that ignores the config entirely"
    )
    await ddns_config(wj.DdnsConfig(health_check_enabled=False, event_retention_days=pinned))

    factory = get_session_factory()
    async with factory() as s:
        assert (await wj.load_config(s)).event_retention_days == pinned
    assert (await _events(a["user"])).json()["retention_days"] == pinned


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
