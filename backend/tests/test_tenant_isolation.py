"""Cross-tenant isolation, measured twice by two differently-shaped instruments.

The boundary this file guards is a *query* boundary, and it is not the
same boundary the per-user encryption guards. ``UserSecret`` stops a
ciphertext being decrypted under the wrong owner; it says nothing about
a query returning the wrong tenant's row, and every non-secret column
on that row — zone name, hostname, last IP, user agent, the whole event
history — is plaintext. Plan §3.1.1, last paragraph. So:

==============================================  ==================================================
instrument A — the scope API, against MySQL     instrument B — the SQL that actually reached MySQL
==============================================  ==================================================
tenant B fetches tenant A's row: zero rows      the statement carries ``<owner>.user_id = %s``
counted with ``COUNT()``, not just listed       captured from ``before_cursor_execute``
==============================================  ==================================================

**Neither one is redundant.** A scope that issues an unfiltered
``SELECT`` and filters the result in Python satisfies A completely and
fails B — and it is not a hypothetical, it is the shape anyone reaches
for when a predicate is awkward to express.
``test_a_scope_that_filters_in_python_passes_a_and_fails_b`` builds
exactly that scope, asserts both halves, and shows the leak it lets
through the moment a ``LIMIT`` is involved. If instrument B is ever
deleted, that test fails.

The registry checks are the part written for issues that do not exist
yet. #15, #16 and #17 all add tables. Every one of them will fail this
file until the new model is either given a tenancy path or written down
as installation-wide with a reason — and until the isolation fixture
builds a row for it, so the new model gets its own zero-rows assertion
rather than inheriting somebody else's.

Requires a live database, so it runs inside the api container via
``make test-backend``. Everything it creates is namespaced by
``PYTEST_XDIST_WORKER``: zone names, hostnames and device usernames are
globally unique by construction, so ten workers sharing one MySQL
collide on every one of them.
"""
from __future__ import annotations

import ast
import contextlib
import inspect
import os
import pathlib
import re
import textwrap
from typing import Any, Iterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.auth.rbac import current_principal
from app.db import get_engine, get_session_factory
from app.models.auth import User
from conftest import fixture_writes, purge_tenants, unusable_password_hash
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from atrium_ddns import models as m
from atrium_ddns import scope as sc
from atrium_ddns.scope import (
    CROSS_TENANT_PERMISSION,
    EVENTS_CROSS_TENANT_PERMISSION,
    TENANT_PATHS,
    UNSCOPED,
    DdnsScope,
    NotTenantScoped,
    UnregisteredModel,
)

pytestmark = pytest.mark.asyncio


def _worker_id() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER", "serial")


W = _worker_id()

#: Model -> the key under which the fixture stores that tenant's row id.
#: Deliberately a second, hand-written list rather than a loop over
#: ``TENANT_PATHS``: it is what forces a model added by a later issue to
#: get a *row* built for it, not just a registry entry. The two are
#: compared by ``test_every_tenant_model_has_a_row_in_the_fixture``.
ISOLATION_FIXTURE: dict[type[Any], str] = {
    m.Domain: "domain_id",
    m.DomainBackend: "backend_id",
    m.Device: "device_id",
    m.Hostname: "hostname_id",
    m.HostnameBackend: "hostname_backend_id",
    m.DnsEvent: "event_id",
    m.RateLimitEvent: "rate_limit_event_id",
}


@pytest_asyncio.fixture
async def tenants():
    """Two tenants, one row of every host model each, plus an orphan event.

    Creates its own users rather than adopting existing ones — the
    teardown hard-deletes them, and on a developer's own stack the rows
    a ``LIMIT 2`` returns are real accounts.
    """
    emails = [f"ddns-scope-a-{W}@example.invalid", f"ddns-scope-b-{W}@example.invalid"]
    orphan_email = f"ddns-scope-orphan-{W}@example.invalid"

    # The orphan row has `user_id IS NULL` by construction — that is the
    # state under test — so nothing indexed can name it and a leftover
    # from a killed run has to be swept by its denormalised email. See
    # `conftest.purge_tenants`.
    await purge_tenants(
        emails,
        unattributed_emails=[orphan_email],
        owner="test_tenant_isolation.tenants",
    )

    hashed = unusable_password_hash()
    ids: list[int] = []
    async with fixture_writes("test_tenant_isolation.tenants/users") as s:
        for email in emails:
            u = User(
                email=email,
                hashed_password=hashed,
                is_active=True,
                is_verified=True,
                full_name=f"DDNS scope probe {W}",
                preferred_language="en",
            )
            s.add(u)
            await s.flush()
            ids.append(u.id)

    built: dict[str, dict[str, Any]] = {}
    async with fixture_writes("test_tenant_isolation.tenants/world") as s:
        for tag, uid, email in (("a", ids[0], emails[0]), ("b", ids[1], emails[1])):
            domain = m.Domain(user_id=uid, name=f"{tag}-scope-{W}.example.invalid")
            s.add(domain)
            await s.flush()
            backend = m.DomainBackend(
                domain_id=domain.id, user_id=uid, backend_type="stub"
            )
            device = m.Device(
                user_id=uid,
                username=f"ddns-scope-{tag}-{W}",
                password_hash="$argon2id$v=19$m=65536,t=3,p=4$fake$fake",
                name=f"router-{tag}",
            )
            s.add_all([backend, device])
            await s.flush()
            hostname = m.Hostname(
                domain_id=domain.id,
                device_id=device.id,
                name=f"home.{tag}-scope-{W}.example.invalid",
            )
            s.add(hostname)
            await s.flush()
            event = m.DnsEvent(
                user_id=uid,
                device_id=device.id,
                domain_id=domain.id,
                hostname_id=hostname.id,
                user_email=email,
                device_name=device.name,
                domain_name=domain.name,
                hostname=hostname.name,
                event_type="update",
                response_code="good",
                ip="192.0.2.1",
            )
            rate_limit_event = m.RateLimitEvent(device_id=device.id)
            # #74's selection row. Two hops from its owner — this row has
            # no `user_id`, and neither does the hostname it hangs off;
            # the tenant is the hostname's *domain's* `user_id`. It is in
            # the fixture so the parameterised cases below cover it like
            # every other model rather than special-casing it.
            selection = m.HostnameBackend(
                hostname_id=hostname.id, backend_id=backend.id
            )
            s.add_all([event, rate_limit_event, selection])
            await s.flush()
            built[tag] = {
                "user_id": uid,
                "email": email,
                "domain_id": domain.id,
                "backend_id": backend.id,
                "device_id": device.id,
                "hostname_id": hostname.id,
                "hostname_backend_id": selection.id,
                "event_id": event.id,
                "rate_limit_event_id": rate_limit_event.id,
            }

        # An event owned by nobody — the state `ON DELETE SET NULL`
        # leaves behind when a tenant is deleted. It must be invisible to
        # every tenant, and a predicate widened to "mine or unowned"
        # would hand the deleted tenant's history to everyone.
        orphan = m.DnsEvent(
            user_id=None,
            user_email=orphan_email,
            event_type="update",
            response_code="good",
        )
        s.add(orphan)
        await s.flush()
        built["orphan_event_id"] = orphan.id

    yield built

    # By primary key on the way out: this process wrote the row, so it
    # knows its id and does not need the scan the setup half used.
    await purge_tenants(
        emails,
        event_ids=[built["orphan_event_id"]],
        owner="test_tenant_isolation.tenants",
    )


# --------------------------------------------------------------------- #
# Instrument B's plumbing: the SQL that actually reached the server
# --------------------------------------------------------------------- #


@contextlib.contextmanager
def capture_sql() -> Iterator[list[tuple[str, Any]]]:
    """Record every statement the driver sends while the block runs.

    ``before_cursor_execute`` is downstream of compilation, of the ORM
    and of anything a scope might do in Python, so what lands here is
    what MySQL was asked. That is the whole point of it being a
    different instrument from ``Select.compile()``: a compile reads a
    statement object somebody built, and the question is whether *that
    statement* is the one that ran.
    """
    engine = get_engine().sync_engine
    seen: list[tuple[str, Any]] = []

    def _on(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        seen.append((statement, parameters))

    sa.event.listen(engine, "before_cursor_execute", _on)
    try:
        yield seen
    finally:
        sa.event.remove(engine, "before_cursor_execute", _on)


def _statement_for(seen: list[tuple[str, Any]], table: str) -> tuple[str, Any]:
    """The one captured statement naming ``table``.

    Refuses when there are none and when there are several rather than
    picking one: "the query I meant" resolved by position is how a
    passing assertion ends up being made about a different query.
    """
    hits = [(stmt, params) for stmt, params in seen if table in stmt]
    assert hits, f"no statement naming {table} was captured (captured {len(seen)})"
    assert len(hits) == 1, f"expected exactly one statement naming {table}, got {len(hits)}"
    return hits[0]


def _where_clause(statement: str) -> str:
    """Everything after the statement's first ``WHERE``, or ``""``.

    Needed because ``SELECT ddns_domain.id, ddns_domain.user_id, …``
    contains the string ``ddns_domain.user_id`` whether or not anything
    is filtering on it. An assertion against the whole statement would
    pass for a completely unscoped query — the probe that cannot fail,
    in its most ordinary disguise.

    Matched on a word boundary rather than ``" WHERE "``: SQLAlchemy
    puts a newline before the keyword, and the first cut of this helper
    looked for a leading space and therefore reported *every* statement
    as carrying no ``WHERE`` clause. It failed loudly, which is the only
    reason that is a footnote and not a defect.
    """
    match = re.search(r"\bWHERE\b", statement, re.IGNORECASE)
    return "" if match is None else statement[match.end() :].strip()


def _const_where(clause: Any) -> str:
    """How a constant predicate renders on MySQL, asked of SQLAlchemy.

    ``sa.true()`` is *not* ``true`` inside a ``WHERE`` on this dialect —
    MySQL has no native boolean as far as SQLAlchemy is concerned, so it
    compiles to ``true = 1``. Deriving the expected text instead of
    typing it keeps the assertion honest across a SQLAlchemy upgrade.
    """
    return _where_clause(_compiled(sa.select(sa.literal_column("1")).where(clause)))


def _flat_params(parameters: Any) -> list[Any]:
    if isinstance(parameters, dict):
        return list(parameters.values())
    if isinstance(parameters, (list, tuple)):
        out: list[Any] = []
        for p in parameters:
            out.extend(_flat_params(p) if isinstance(p, (list, tuple, dict)) else [p])
        return out
    return [parameters]


def _compiled(stmt: Any) -> str:
    return str(
        stmt.compile(
            dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


TENANT_MODELS = sorted(TENANT_PATHS, key=lambda model: model.__name__)
MODEL_IDS = [model.__name__ for model in TENANT_MODELS]


# --------------------------------------------------------------------- #
# 1. Instrument A — zero rows, through the scope API, against MySQL
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("model", TENANT_MODELS, ids=MODEL_IDS)
async def test_tenant_b_gets_zero_rows_for_tenant_as_row(tenants, model):
    """One case per model: B asks for A's row three ways and gets nothing.

    The vacuity guard comes first. "Zero rows" is only a measurement if
    the same query returns one row for the tenant who owns it — a scope
    broken to return nothing for anybody would otherwise pass every
    assertion below.
    """
    key = ISOLATION_FIXTURE[model]
    a_pk = tenants["a"][key]
    scope_a = DdnsScope.for_user_id(tenants["a"]["user_id"])
    scope_b = DdnsScope.for_user_id(tenants["b"]["user_id"])

    async with get_session_factory()() as s:
        assert await scope_a.get(s, model, a_pk) is not None, (
            f"the owner cannot see their own {model.__name__} — the zero-row "
            f"assertions below would be vacuous"
        )

        assert await scope_b.get(s, model, a_pk) is None

        rows = (
            await s.execute(scope_b.select(model).where(model.id == a_pk))
        ).scalars().all()
        assert rows == []

        # An aggregate, because a scope that filters in Python can fix a
        # row list and cannot fix a COUNT.
        n = (
            await s.execute(
                scope_b.select(model, sa.func.count(model.id)).where(model.id == a_pk)
            )
        ).scalar_one()
        assert n == 0


async def test_session_get_returns_the_row_that_scope_get_refuses(tenants):
    """Why :meth:`DdnsScope.get` exists at all.

    ``session.get`` consults the identity map and issues a lookup that
    takes no ``WHERE`` clause, so it cannot be scoped. This asserts the
    disagreement rather than describing it, so anyone who "simplifies"
    the router back to ``session.get`` has a failing test naming the
    row that crossed the boundary.
    """
    a_domain = tenants["a"]["domain_id"]
    scope_b = DdnsScope.for_user_id(tenants["b"]["user_id"])

    async with get_session_factory()() as s:
        unscoped = await s.get(m.Domain, a_domain)
        assert unscoped is not None and unscoped.id == a_domain

    async with get_session_factory()() as s:
        assert await scope_b.get(s, m.Domain, a_domain) is None


async def test_an_event_owned_by_nobody_is_invisible_to_every_tenant(tenants):
    """``ddns_event.user_id`` is ``ON DELETE SET NULL``.

    A deleted tenant's history survives with a NULL owner. ``= uid`` is
    NULL-safe in the direction that matters. Widening the predicate to
    ``or_(user_id == uid, user_id.is_(None))`` — which reads like a
    kindness towards orphaned rows — publishes it to everybody.
    """
    orphan = tenants["orphan_event_id"]
    async with get_session_factory()() as s:
        for tag in ("a", "b"):
            scope = DdnsScope.for_user_id(tenants[tag]["user_id"])
            assert await scope.get(s, m.DnsEvent, orphan) is None

        admin = DdnsScope(
            user_id=tenants["a"]["user_id"],
            permissions=frozenset({CROSS_TENANT_PERMISSION}),
        )
        assert await admin.get(s, m.DnsEvent, orphan) is not None, (
            "the orphan row does not exist — the assertions above are vacuous"
        )


async def test_a_scope_with_no_tenant_matches_nothing(tenants):
    """Fail-closed. A lost user id must return zero rows, not every row."""
    empty = DdnsScope()
    async with get_session_factory()() as s:
        for model, key in ISOLATION_FIXTURE.items():
            assert await empty.get(s, model, tenants["a"][key]) is None
    # …and it is a *stated* false, not an absent WHERE clause.
    assert _where_clause(_compiled(empty.select(m.Domain))) == _const_where(sa.false())


async def test_the_admin_permission_lifts_the_restriction_on_every_model(tenants):
    admin = DdnsScope(
        user_id=tenants["b"]["user_id"],
        permissions=frozenset({CROSS_TENANT_PERMISSION}),
    )
    async with get_session_factory()() as s:
        for model, key in ISOLATION_FIXTURE.items():
            assert await admin.get(s, model, tenants["a"][key]) is not None


async def test_events_read_all_opens_the_log_and_nothing_else(tenants):
    """The narrow grant is narrow.

    A support role reading anyone's update history must not thereby be
    able to read anyone's zones. Asserted in both directions, because a
    per-model bypass collapsed into a global one passes the positive
    half alone.
    """
    support = DdnsScope(
        user_id=tenants["b"]["user_id"],
        permissions=frozenset({EVENTS_CROSS_TENANT_PERMISSION}),
    )
    async with get_session_factory()() as s:
        assert await support.get(s, m.DnsEvent, tenants["a"]["event_id"]) is not None
        for model in (m.Domain, m.Device, m.Hostname, m.DomainBackend):
            key = ISOLATION_FIXTURE[model]
            assert await support.get(s, model, tenants["a"][key]) is None


async def test_a_tenant_without_a_grant_never_reaches_across(tenants):
    """The bypass is opt-in, and an ordinary tenant does not hold it.

    ``for_user_id`` — the ``/nic/update`` shape — carries no permissions
    at all, so the hot path cannot acquire one by accident.
    """
    scope = DdnsScope.for_user_id(tenants["b"]["user_id"])
    assert scope.permissions == frozenset()
    for model in TENANT_MODELS:
        assert scope.reaches_all_tenants(model) is False


# --------------------------------------------------------------------- #
# 2. Instrument B — the SQL that actually reached the server
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("model", TENANT_MODELS, ids=MODEL_IDS)
async def test_the_emitted_sql_carries_the_tenant_predicate(model):
    """The statement MySQL was asked, read off the driver.

    Expectations are **derived** from the model's declared
    :class:`~atrium_ddns.scope.TenantPath` rather than written out per
    model. A hardcoded fragment list is the same defect one refactor
    later, and it would keep passing against a predicate that names the
    wrong table.

    Needs no fixture rows: this reads the query, not the result.
    """
    path = TENANT_PATHS[model]
    uid = 4_242_424
    scope = DdnsScope.for_user_id(uid)
    table = model.__tablename__

    async with get_session_factory()() as s:
        with capture_sql() as seen:
            await s.execute(scope.select(model))
        statement, parameters = _statement_for(seen, table)

        with capture_sql() as unscoped_seen:
            await s.execute(sa.select(model))
        unscoped_statement, _ = _statement_for(unscoped_seen, table)

    where = _where_clause(statement)
    owner_ref = f"{path.owner_table}.user_id"

    assert where, f"the scoped statement carries no WHERE clause at all:\n{statement}"
    assert owner_ref in where, (
        f"the emitted SQL does not restrict on {owner_ref}:\n{statement}"
    )
    assert uid in _flat_params(parameters), (
        f"{uid} was not bound into the statement: {parameters!r}"
    )

    if path.through is None:
        assert owner_ref.startswith(f"{table}."), (
            "a model owning its own user_id must be filtered on its own column"
        )
    else:
        assert "EXISTS" in where.upper(), (
            f"{model.__name__} reaches its owner through {path.through} and the "
            f"predicate should be a correlated EXISTS:\n{statement}"
        )
        assert f"{table}.{path.through}" in where

    # Vacuity guard: the fragment has to be contributed by the scope. An
    # unscoped SELECT of the same model names `<table>.user_id` in its
    # *column list* for four of these six models, so an assertion made
    # against the whole statement would pass with no scope at all.
    assert owner_ref not in _where_clause(unscoped_statement)


@pytest.mark.parametrize("model", TENANT_MODELS, ids=MODEL_IDS)
async def test_the_predicate_survives_update_and_delete(model):
    """A scoped read with unscoped writes behind it is not a boundary."""
    path = TENANT_PATHS[model]
    scope = DdnsScope.for_user_id(4_242_424)
    owner_ref = f"{path.owner_table}.user_id"

    for stmt in (
        scope.apply(sa.update(model).values(created_at=sa.func.now()), model),
        scope.apply(sa.delete(model), model),
    ):
        where = _where_clause(_compiled(stmt))
        assert owner_ref in where, f"{owner_ref} missing from:\n{_compiled(stmt)}"


async def test_a_scope_that_filters_in_python_passes_a_and_fails_b(tenants):
    """The reason both instruments exist, executed rather than asserted in prose.

    ``_PythonFilteringScope`` issues an unfiltered ``SELECT`` and drops
    the other tenant's rows afterwards. Instrument A cannot tell it
    apart from the real thing. Instrument B can — and the third block
    below shows what the difference costs: as soon as the database is
    asked to ``LIMIT``, it limits *before* the Python filter runs and
    hands tenant B tenant A's row.
    """
    a_domain = tenants["a"]["domain_id"]
    b_domain = tenants["b"]["domain_id"]
    fake = _PythonFilteringScope(tenants["b"]["user_id"])

    async with get_session_factory()() as s:
        # A — satisfied. Tenant B gets zero rows for tenant A's domain.
        assert await fake.get(s, m.Domain, a_domain) is None
        assert await fake.get(s, m.Domain, b_domain) is not None

        # B — not satisfied. Nothing in the emitted SQL is about tenancy.
        with capture_sql() as seen:
            await s.execute(fake.select(m.Domain))
        statement, _ = _statement_for(seen, m.Domain.__tablename__)
        assert "ddns_domain.user_id" not in _where_clause(statement)

        # …and here is the leak that the SQL test is really about.
        leaked = (
            await s.execute(
                fake.select(m.Domain)
                .where(m.Domain.id.in_([a_domain, b_domain]))
                .order_by(m.Domain.id)
                .limit(1)
            )
        ).scalars().all()
        assert [row.id for row in leaked] == [a_domain]

        real = DdnsScope.for_user_id(tenants["b"]["user_id"])
        kept = (
            await s.execute(
                real.select(m.Domain)
                .where(m.Domain.id.in_([a_domain, b_domain]))
                .order_by(m.Domain.id)
                .limit(1)
            )
        ).scalars().all()
        assert [row.id for row in kept] == [b_domain]


class _PythonFilteringScope:
    """A scope that is a no-op in SQL. Deliberately wrong; never imported.

    Kept next to the test that uses it rather than in a fixtures module
    so nobody can mistake it for something to build on.
    """

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id

    def select(self, model: type[Any], *entities: Any) -> Any:
        return sa.select(*(entities or (model,)))

    async def get(self, session: Any, model: type[Any], pk: Any) -> Any | None:
        row = (
            await session.execute(sa.select(model).where(model.id == pk))
        ).scalars().first()
        if row is None:
            return None
        return row if getattr(row, "user_id", None) == self.user_id else None


# --------------------------------------------------------------------- #
# 3. The registry — written for the models that do not exist yet
# --------------------------------------------------------------------- #


class _OtherBase(DeclarativeBase):
    """Not ``HostBase``. A throwaway class on the host's own metadata
    would be picked up by ``test_host_models.py``'s autogenerate
    comparison, which runs in the same process."""


class Unclassified(_OtherBase):
    """Stands in for the table #15/#16/#17 will add."""

    __tablename__ = "test_scope_unclassified"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column()


def _unclassified(models: set[type[Any]]) -> set[type[Any]]:
    """Models with no tenancy path and no written exemption."""
    return {model for model in models if model not in TENANT_PATHS and model not in UNSCOPED}


async def test_every_tenant_model_has_a_row_in_the_isolation_fixture():
    """Registering a model is half the job; it needs a row to be tested on.

    Without this, a model added to ``TENANT_PATHS`` by #15/#16/#17
    satisfies the completeness check and never gets a zero-rows
    assertion — a tenancy path nobody has watched work. The two lists
    are written separately on purpose so this comparison has something
    to compare.
    """
    assert set(ISOLATION_FIXTURE) == set(TENANT_PATHS), {
        "registered but not exercised": sorted(
            cls.__name__ for cls in set(TENANT_PATHS) - set(ISOLATION_FIXTURE)
        ),
        "exercised but not registered": sorted(
            cls.__name__ for cls in set(ISOLATION_FIXTURE) - set(TENANT_PATHS)
        ),
    }


async def test_every_host_model_is_classified():
    missing = _unclassified(sc.host_models())
    assert missing == set(), (
        "these models are mapped on HostBase and have no tenancy decision "
        f"recorded: {sorted(cls.__name__ for cls in missing)}. Add a TenantPath "
        "to atrium_ddns.scope.TENANT_PATHS, or an UNSCOPED entry with a "
        "written reason."
    )


async def test_the_classification_check_fails_when_a_model_is_added():
    """The guard, shown biting.

    A completeness check that has only ever been run against a complete
    registry is a check nobody has seen fail. This runs it against the
    registry plus one unclassified model and asserts it names it.
    """
    assert _unclassified(sc.host_models() | {Unclassified}) == {Unclassified}


async def test_the_registries_have_no_stale_entries():
    """A model deleted from ``models.py`` takes its entry with it."""
    known = set(TENANT_PATHS) | set(UNSCOPED)
    stale = known - sc.host_models()
    assert stale == set(), sorted(cls.__name__ for cls in stale)


async def test_the_two_registries_are_disjoint():
    both = set(TENANT_PATHS) & set(UNSCOPED)
    assert both == set(), sorted(cls.__name__ for cls in both)


async def test_every_unscoped_entry_carries_a_written_reason():
    assert UNSCOPED, "vacuous unless something is exempt"
    for model, reason in UNSCOPED.items():
        assert reason and reason.strip(), model.__name__
        assert len(reason.split()) >= 8, (
            f"{model.__name__}'s exemption reads as a placeholder: {reason!r}"
        )


async def test_a_model_carrying_user_id_cannot_be_declared_unscoped():
    """The lazy way to silence the completeness check, closed off.

    Anything with a ``user_id`` column is tenant data by construction,
    so an ``UNSCOPED`` entry for it is wrong however good the prose is.
    """
    for model in UNSCOPED:
        columns = set(sa.inspect(model).columns.keys())
        assert "user_id" not in columns, (
            f"{model.__name__} carries user_id and is exempted from scoping"
        )


async def test_classify_refuses_a_model_it_has_never_seen():
    with pytest.raises(UnregisteredModel) as exc:
        sc.classify(Unclassified)
    assert "Unclassified" in str(exc.value)
    assert "TENANT_PATHS" in str(exc.value)

    scope = DdnsScope.for_user_id(1)
    with pytest.raises(UnregisteredModel):
        scope.select(Unclassified)
    with pytest.raises(UnregisteredModel):
        scope.predicate(Unclassified)


async def test_the_installation_wide_table_refuses_with_its_recorded_reason():
    with pytest.raises(NotTenantScoped) as exc:
        DdnsScope.for_user_id(1).select(m.AtriumDdnsState)
    assert UNSCOPED[m.AtriumDdnsState][:30] in str(exc.value)


async def test_the_cross_tenant_permission_codes_are_the_seeded_ones():
    """Spelled once. The migration seeds ``models.PERMISSIONS``; a code
    here that is not in that tuple is a bypass nobody can be granted —
    and, read the other way, a typo that silently never fires."""
    for code in (CROSS_TENANT_PERMISSION, EVENTS_CROSS_TENANT_PERMISSION):
        assert code in m.PERMISSIONS, code


async def test_an_unrestricted_scope_has_to_say_why():
    with pytest.raises(ValueError):
        DdnsScope.cross_tenant(reason="")
    with pytest.raises(ValueError):
        DdnsScope.cross_tenant(reason="   ")

    swept = DdnsScope.cross_tenant(reason="retention prune sweeps every tenant (#17)")
    assert swept.reaches_all_tenants(m.DnsEvent) is True
    # …and it is visible as a literal `true` in the SQL rather than as
    # an absent WHERE clause, so an unscoped query and a deliberately
    # unrestricted one are distinguishable in a slow-query log.
    assert _where_clause(_compiled(swept.select(m.DnsEvent))) == _const_where(sa.true())


async def test_the_dependency_builds_a_scope_from_the_principal():
    """``get_scope`` reads the *effective* permission set.

    ``Principal.permissions`` is already the PAT-scope ∩ user-permission
    intersection, so a token that does not carry ``atrium_ddns.admin``
    cannot reach across tenants even when its owner could. Asserted on
    the construction path, which is where that property is inherited.
    """
    from app.auth.principal import Principal

    user = User(
        id=99_001,
        email=f"scope-dep-{W}@example.invalid",
        hashed_password="x",
        is_active=True,
        is_verified=True,
    )
    narrowed = Principal(
        user=user,
        permissions=frozenset({"atrium_ddns.domain.manage"}),
        auth_method="pat",
    )
    scope = await sc.get_scope(principal=narrowed)
    assert scope.user_id == 99_001
    assert scope.reaches_all_tenants(m.Domain) is False

    full = Principal(
        user=user,
        permissions=frozenset({CROSS_TENANT_PERMISSION}),
        auth_method="password",
    )
    assert (await sc.get_scope(principal=full)).reaches_all_tenants(m.Domain) is True


async def test_get_scope_is_mountable_as_a_fastapi_dependency():
    """Nothing calls ``get_scope`` yet — #16's router is the first caller.

    That makes it an artefact with no writer for one issue's worth of
    time, and the way such a thing fails is by not being wireable at
    all: a dependency whose own dependencies FastAPI cannot resolve
    blows up at import of the router, not here. So resolve it the way
    FastAPI does and assert the chain it built.
    """
    from fastapi.dependencies.utils import get_dependant

    dependant = get_dependant(path="/probe", call=sc.get_scope)
    sub_calls = {dep.call for dep in dependant.dependencies}
    assert current_principal in sub_calls, (
        "get_scope no longer resolves through current_principal, so PAT "
        "requests would carry un-intersected permissions"
    )


# --------------------------------------------------------------------- #
# Instrument C — the read-path census
#
# V1M2's exit criterion says "every read path returns nothing for a
# second tenant's rows". Instruments A and B above prove that of every
# *model*: six models, six zero-row assertions, six emitted predicates.
# That is not the same population. A read path is a **call site**, and a
# call site that never reaches ``DdnsScope`` at all is invisible to both
# — it is not a wrong predicate, it is an absent one, and neither a
# per-model assertion nor a query log has anything to notice.
#
# ``test_no_hand_written_tenancy_filter_survives_in_the_worker`` in
# test_worker_jobs.py is the closest existing guard and it looks for a
# different shape: it catches ``Hostname.user_id == uid`` written by
# hand. A bare ``sa.select(Hostname)`` carries no ``user_id`` comparison
# at all, so it passes that guard, returns every tenant's rows, and is
# exactly what someone adding an admin listing endpoint writes first.
#
# So the population is derived here — from the package directory and
# from ``DdnsScope``'s own method list, not from a list anyone maintains
# — and every member is classified. The default for a call site nobody
# classified is failure, not silence.
# --------------------------------------------------------------------- #


def _host_package_modules() -> list[pathlib.Path]:
    """Every module in the host package, read off the directory.

    Derived rather than listed, so a module added by a later issue is
    swept the moment it exists — the same mechanism as
    ``_package_modules`` in test_worker_jobs.py.
    """
    root = pathlib.Path(inspect.getsourcefile(sc)).parent
    return sorted(root.rglob("*.py"))


def _scope_query_methods() -> frozenset[str]:
    """``DdnsScope``'s public methods that take a model.

    Read off the class, so renaming ``select`` does not silently
    reclassify every call site as unscoped — the rename moves the name
    in both places at once, or this census goes red.
    """
    names = set()
    for name, fn in inspect.getmembers(DdnsScope, inspect.isfunction):
        if name.startswith("_"):
            continue
        if "model" in inspect.signature(fn).parameters:
            names.add(name)
    return frozenset(names)


#: The SQLAlchemy statement constructors a read path can be spelled
#: with, resolved against the installed ``sqlalchemy`` rather than left
#: as bare strings — a name that stops existing upstream then shows up
#: as a shrinking census instead of a clause that matches nothing.
_SA_CONSTRUCTORS = frozenset(
    name
    for name in ("select", "update", "delete", "insert")
    if callable(getattr(sa, name, None))
)

#: ``session.get(Model, pk)`` is a read path and **cannot** be scoped —
#: the identity-map lookup takes no ``WHERE``. It is in the census on
#: purpose, so writing one is a failure rather than an omission;
#: :meth:`DdnsScope.get` is the replacement and the reason that method
#: exists at all.
_SESSION_READERS = frozenset({"get"})

#: ``module:function`` -> why this call site does not go through the
#: scope. Every entry is a claim in writing; the tests below refuse an
#: empty one and refuse a stale one.
READ_PATHS_NOT_SCOPED: dict[str, str] = {
    "auth_device.py:authenticate_device": (
        "This is the query that ESTABLISHES the tenant. A device presents "
        "HTTP Basic credentials and there is no atrium session, no cookie "
        "and no principal anywhere in the request; the lookup is by the "
        "globally-unique ddns_device.username, and its result is what "
        "DdnsScope.for_user_id() is then built from. Scoping it would "
        "require the answer it is being asked for. It reads one device row "
        "by unique key and returns no other tenant data."
    ),
    "seed_compat_fixture.py:read_back": (
        "Development-only fixture seeder for the frozen compat table. It is "
        "refused unless ATRIUM_DDNS_COMPAT_STUB=1, refused outright when "
        "ENVIRONMENT=prod, and mounted on no route. Its whole purpose is to "
        "read back every fixture tenant's rows as the second instrument on "
        "what it has just written."
    ),
    "import_legacy.py:assert_target_is_empty": (
        "The one-shot cutover importer's refuse-to-run-twice check, and it "
        "has to be cross-tenant to be correct. ddns_device.username, "
        "ddns_domain.name and ddns_hostname.name are all GLOBALLY unique "
        "(DNS is global, and the device username is read before there is a "
        "tenant to scope by). Scoped to the importing owner it would miss a "
        "collision held by another tenant, report the database empty, and "
        "then fail on the UNIQUE constraint halfway through a transaction "
        "that is supposed to be all-or-nothing. It selects only the three "
        "name columns — never a row, never any other tenant's data — and "
        "reports counts with the names withheld. It is a CLI, mounted on no "
        "route, and it refuses to run at all once those rows exist."
    ),
    "seed_compat_fixture.py:verify_rehash": (
        "Same module and same two gates. It reports the SHAPE of every "
        "fixture device's stored password hash across all three fixture "
        "tenants, which is a cross-tenant read by design and never runs on "
        "a real installation."
    ),
}

#: One census row: ``(module:function, lineno, callee, verdict, models)``.
_CensusRow = tuple[str, int, str, str, tuple[str, ...]]


def _read_path_census(modules: list[tuple[str, str]]) -> list[_CensusRow]:
    """Every query call site naming a tenant model, classified.

    ``modules`` is ``(filename, source)`` rather than a directory, so
    the classifier can be run over synthetic source too — which is the
    only way to watch it fail.

    ``verdict`` is one of ``scoped`` (the call is
    ``scope.<method>(...)``), ``nested`` (it is lexically inside one —
    ``scope.apply(sa.update(X), X)`` is one read path spelled with two
    constructors), ``exempt`` (named in ``READ_PATHS_NOT_SCOPED``), or
    ``UNSCOPED``.
    """
    tenant_names = {model.__name__ for model in TENANT_PATHS}
    scope_methods = _scope_query_methods()
    callees = _SA_CONSTRUCTORS | scope_methods | _SESSION_READERS

    out: list[_CensusRow] = []
    for filename, source in modules:
        tree = ast.parse(source)
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def enclosing_function(node: ast.AST) -> str:
            cur = parents.get(node)
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return cur.name
                cur = parents.get(cur)
            return "<module>"

        def is_scope_call(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in scope_methods
                and ast.unparse(node.func.value) == "scope"
            )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                name, receiver = func.id, ""
            elif isinstance(func, ast.Attribute):
                name, receiver = func.attr, ast.unparse(func.value)
            else:
                continue
            if name not in callees:
                continue
            # `get` is a bare, extremely common method name, and it is
            # spelled twice in this codebase: `session.get` (a read
            # path that cannot be scoped) and `scope.get` (the
            # replacement for it). Both are read paths and both must
            # stay in; anything else called `get` is not.
            #
            # The first cut of this line narrowed to the session's
            # receiver alone and silently dropped
            # `router_nic.py:_persist_updates`, so the census counted
            # 13 sites where there are 14 and still reported a clean
            # sweep. `test_every_scope_entry_point_appears_in_the_census`
            # is the guard that caught it.
            if name in _SESSION_READERS and receiver not in {"session", "s", "scope"}:
                continue

            models = tuple(
                sorted(
                    {
                        child.id if isinstance(child, ast.Name) else child.attr
                        for child in ast.walk(node)
                        if (isinstance(child, ast.Name) and child.id in tenant_names)
                        or (
                            isinstance(child, ast.Attribute)
                            and child.attr in tenant_names
                        )
                    }
                )
            )
            if not models:
                continue

            key = f"{filename}:{enclosing_function(node)}"
            if is_scope_call(node):
                verdict = "scoped"
            else:
                verdict = "exempt" if key in READ_PATHS_NOT_SCOPED else "UNSCOPED"
                cur = parents.get(node)
                while cur is not None:
                    if is_scope_call(cur):
                        verdict = "nested"
                        break
                    cur = parents.get(cur)
            out.append((key, node.lineno, f"{receiver}.{name}", verdict, models))
    return sorted(out)


def _package_census() -> list[_CensusRow]:
    return _read_path_census(
        [(path.name, path.read_text()) for path in _host_package_modules()]
    )


async def test_every_read_path_in_the_host_package_is_scoped_or_excused():
    """The criterion's word "every", given a population it can count.

    Anything not routed through ``DdnsScope`` has to be named in
    ``READ_PATHS_NOT_SCOPED`` with a reason. A new call site nobody
    classified fails here on the day it is written, which is the only
    moment at which its author knows why it is shaped that way.
    """
    census = _package_census()
    assert census, (
        "the read-path census found no query call sites at all in the host "
        "package. That is not a clean bill of health, it is a broken "
        "instrument — router_nic.py and worker_jobs.py both query tenant "
        "tables. Check _SA_CONSTRUCTORS and _scope_query_methods()."
    )

    unscoped = [row for row in census if row[3] == "UNSCOPED"]
    assert unscoped == [], (
        "these query call sites name a tenant model and reach neither "
        "DdnsScope nor READ_PATHS_NOT_SCOPED:\n"
        + "\n".join(
            f"  {key} line {lineno}: {callee}{list(models)}"
            for key, lineno, callee, _, models in unscoped
        )
        + "\n\nA bare sa.select(Model) returns every tenant's rows and "
        "carries no user_id comparison for a filter-shaped guard to catch."
    )

    assert [row for row in census if row[3] in {"scoped", "nested"}], (
        "no call site reaches the scope at all — the census is inverted"
    )


async def test_every_scope_entry_point_appears_in_the_census():
    """A census that cannot see one spelling under-counts silently.

    ``DdnsScope`` has four entry points and the host uses all four. If
    the classifier stops recognising one, every call site written that
    way vanishes from the population — and the criterion's "every read
    path" is then measured against a denominator that quietly shrank,
    which is the whole defect family this file is written against.

    The comparison is against the **call graph**, not the source text.
    ``scope.predicate(DnsEvent)`` appears in ``scope.py``'s own module
    docstring as an example, and a substring search reports that as a
    call site — the identical defect
    ``test_the_blocking_resolver_is_not_used_on_the_event_loop`` in
    test_worker_jobs.py was written for. Measured: a text search says
    ``predicate`` is used and the AST says it is not, and the AST is
    right.

    This caught a real one: ``get`` was being narrowed to
    ``session.get``, ``scope.get`` was dropped with it, and the census
    counted 13 sites where there are 14 while still reporting a clean
    sweep.
    """
    tenant_names = {model.__name__ for model in TENANT_PATHS}
    called: set[str] = set()
    for path in _host_package_modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if ast.unparse(func.value) != "scope":
                continue
            if func.attr not in _scope_query_methods():
                continue
            names = {
                child.id if isinstance(child, ast.Name) else child.attr
                for child in ast.walk(node)
                if isinstance(child, (ast.Name, ast.Attribute))
            }
            if names & tenant_names:
                called.add(func.attr)

    assert called, "no scope call on a tenant model anywhere — instrument broken"
    seen = {row[2].split(".", 1)[1] for row in _package_census()}
    missing = called - seen
    assert missing == set(), (
        f"these DdnsScope entry points are called on a tenant model in the "
        f"package and appear in no census row: {sorted(missing)}. The "
        "classifier has lost a spelling, so the population is short by every "
        "call site written that way."
    )


async def test_the_read_path_census_fails_on_a_bare_select():
    """The guard, shown biting, against the two shapes it is written for.

    Run over synthetic source rather than by mutating the package, which
    cannot be done under a running test — but naming the real models, so
    a rename in ``models.py`` breaks this too.
    """
    leak = textwrap.dedent(
        """
        import sqlalchemy as sa
        from .models import Domain

        async def list_everything(session):
            return await session.execute(sa.select(Domain))

        async def fetch_one(session, pk):
            return await session.get(Domain, pk)
        """
    )
    verdicts = {
        (row[0], row[2]): row[3] for row in _read_path_census([("leak.py", leak)])
    }
    assert verdicts == {
        ("leak.py:list_everything", "sa.select"): "UNSCOPED",
        ("leak.py:fetch_one", "session.get"): "UNSCOPED",
    }, verdicts

    # Both scoped spellings, and `scope.get` specifically: it shares a
    # bare method name with `session.get`, and the narrowing that keeps
    # arbitrary `.get()` calls out of the census dropped it once.
    ok = textwrap.dedent(
        """
        from .models import Domain

        async def list_mine(session, scope):
            return await session.execute(scope.select(Domain))

        async def fetch_mine(session, scope, pk):
            return await scope.get(session, Domain, pk)
        """
    )
    assert sorted(
        (row[0], row[2], row[3]) for row in _read_path_census([("ok.py", ok)])
    ) == [
        ("ok.py:fetch_mine", "scope.get", "scoped"),
        ("ok.py:list_mine", "scope.select", "scoped"),
    ]


async def test_a_statement_composed_inside_a_scope_call_counts_as_scoped():
    """``scope.apply(sa.update(Hostname), Hostname)`` is one read path.

    The inner constructor is genuinely unscoped on its own and the
    census must not report it: worker_jobs.py writes exactly this shape
    for the health-check writeback, and a classifier that flagged it
    would be switched off within a week.
    """
    composed = textwrap.dedent(
        """
        import sqlalchemy as sa
        from .models import Hostname

        async def writeback(session, scope):
            return await session.execute(
                scope.apply(sa.update(Hostname).values(dns_ip_v4=None), Hostname)
            )
        """
    )
    assert sorted(row[3] for row in _read_path_census([("c.py", composed)])) == [
        "nested",
        "scoped",
    ]


async def test_every_excused_read_path_still_exists_and_says_why():
    """A stale exemption is an exemption for code that is gone.

    Both directions: every entry must still name a live call site, and
    every entry must carry a reason long enough to be one.
    """
    excused = {row[0] for row in _package_census() if row[3] == "exempt"}
    stale = set(READ_PATHS_NOT_SCOPED) - excused
    assert stale == set(), (
        "READ_PATHS_NOT_SCOPED excuses call sites that no longer exist: "
        f"{sorted(stale)}. The entry goes when the code does."
    )
    for key, reason in READ_PATHS_NOT_SCOPED.items():
        assert reason and reason.strip(), key
        assert len(reason.split()) >= 20, (
            f"{key}'s exemption reads as a placeholder: {reason!r}"
        )


async def test_the_census_and_the_model_registry_see_the_same_models():
    """Instrument C's population is derived from instrument A's.

    ``_read_path_census`` recognises the names in ``TENANT_PATHS``. A
    name it cannot recognise would silently drop call sites, so the two
    are compared rather than assumed aligned.

    The reverse direction is the interesting one and it is **not** a
    failure. Measured on this package, two of the six registered models
    — ``Domain`` and ``DomainBackend`` — appear in no query call site at
    all: they are loaded through relationships off ``Hostname``, so the
    predicate that protects them is ``Hostname``'s. That is a real
    property of the read paths and it is asserted here as reachability
    over the mappers' own relationships rather than as a list, so a
    model that becomes genuinely unreachable stops being excused.
    """
    census = _package_census()
    queried = {name for row in census for name in row[4]}
    registered = {model.__name__ for model in TENANT_PATHS}
    assert queried <= registered, sorted(queried - registered)

    # Transitive closure over relationships, read off the mappers.
    by_name = {model.__name__: model for model in TENANT_PATHS}
    reachable = set(queried)
    changed = True
    while changed:
        changed = False
        for name in sorted(reachable):
            model = by_name.get(name)
            if model is None:
                continue
            for rel in sa.inspect(model).relationships:
                target = rel.mapper.class_.__name__
                if target in registered and target not in reachable:
                    reachable.add(target)
                    changed = True

    assert registered <= reachable, (
        "these tenant models are neither queried directly nor reachable "
        "through a relationship from one that is, so no read path in this "
        f"package covers them: {sorted(registered - reachable)}"
    )
