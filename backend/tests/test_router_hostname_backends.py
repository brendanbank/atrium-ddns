"""Per-hostname backends, TTL and manual update (#74) — the schema change.

The last two legacy routes of the hostname group, and the only two that
could not be closed by writing a page: under `0003` a hostname publishes
to *every* backend bound to its zone, its TTL lives on the binding, and
nothing anywhere can be asked to publish now.

Five things this file is written to make fail, in the order they matter.

**1. An empty selection must not come to mean "publish nowhere".**
This is the whole risk of the change and it is a silent one: a fleet of
routers that receives `911` retries, logs, and goes on looking exactly
like a fleet that is working, while every record in every migrated zone
goes stale. §0 asserts the inherit reading against a row shaped exactly
as a pre-`0004` one — inserted naming only the columns that existed at
`0003` — and it asserts, separately, that the reconstruction is
*faithful*: that `ttl` is nullable with no default in the live schema,
and that revision `0004` contains no statement that could have written a
row. Both halves are needed. A behavioural assertion against a row this
file made up proves nothing about the migration unless the row is the
shape the migration leaves behind.

The independent second reading is not in this file at all: the frozen
124-case wire table runs against twelve hostnames seeded by
`scripts/seed_compat_fixture.py`, a script that has never heard of
`ddns_hostname_backend` and writes no row into it. Three of those cases
aggregate across two or three backends. "Empty means nowhere" takes them
from `nochg`/`good`/`dnserr` to `911`, so 124/124 after this change is a
statement about rows created by something that does not know the feature
exists.

**2. One resolver, not two that agree.** `/nic/update` and the editing
surface must never disagree about which backends a name publishes to.
They resolve it through one function object, asserted by identity —
V1M3's clearest lesson, applied to a join table instead of to a
validator.

**3. The three-level TTL fallback stays three levels.** Override, then
binding, then `DEFAULT_TTL`, with NULL meaning *inherit* at each step
and never meaning 60. The value that reaches the provider is read off
the stub's own call record, not off the row we wrote.

**4. The manual update is the wire's own publish path.** Same plan
build, same provider loop, same aggregate, same persistence rule. And it
is charged to the device's existing rate-limit budget, because a button
that spends a metered third-party quota on an unmetered path is a new
abuse surface however carefully the rest is written.

**5. Cross-tenant is 404, and an admin is not an exception to the
scope.** The admin pair works by the predicate widening, not by a branch
in the handler — demonstrated against a zone owned by a different
tenant.

Everything created here is namespaced by `PYTEST_XDIST_WORKER`.
"""
from __future__ import annotations

import ast
import base64
import contextlib
import os
import pathlib
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.auth.principal import Principal
from app.auth.rbac import current_principal
from app.db import get_session_factory
from app.host_sdk.crypto import unlock_user_secrets
from app.models.auth import User
from conftest import fixture_writes, purge_tenants, unusable_password_hash
from fastapi import FastAPI
from httpx import ASGITransport

from atrium_ddns import compat_stub
from atrium_ddns import models as m
from atrium_ddns import router as router_module
from atrium_ddns import router_nic
from atrium_ddns.providers import DEFAULT_TTL
from atrium_ddns.router import (
    BOARD_READ_PERMISSION,
    DEVICE_MANAGE_PERMISSION,
    DOMAIN_MANAGE_PERMISSION,
    HOSTNAME_MANAGE_PERMISSION,
    router,
)
from atrium_ddns.scope import CROSS_TENANT_PERMISSION

pytestmark = pytest.mark.asyncio

W = os.environ.get("PYTEST_XDIST_WORKER", "serial")

HOSTNAMES = "/api/atrium_ddns/hostnames"

ALL_PERMS = {
    DOMAIN_MANAGE_PERMISSION,
    DEVICE_MANAGE_PERMISSION,
    HOSTNAME_MANAGE_PERMISSION,
    BOARD_READ_PERMISSION,
}

#: The address these tests publish. RFC 5737 documentation space.
PUBLISHED = "203.0.113.74"

#: The revision under test, so §0's structural reading names a file
#: rather than a guess about where migrations live.
REVISION_0004 = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0004_hostname_backends_and_ttl.py"
)


# ===================================================================== #
# Fixtures
# ===================================================================== #


@pytest_asyncio.fixture
async def world() -> AsyncIterator[dict[str, Any]]:
    """Two tenants. A has a zone with **three** backends and a device.

    Three rather than one because every interesting property of this
    feature needs a subset to exist: "explicit selection wins" cannot be
    told from "inherit" with one backend, and neither can "the order is
    the domain's".

    The three are scripted `nochg`, `good`, `dnserr` — three different
    answers, so the aggregate is a measurement rather than a tautology
    (`good` wins over the `nochg`; drop the `good` from the selection
    and the answer becomes `dnserr`, which is the first status that is
    neither).

    B exists to be invisible.
    """
    compat_stub.register_stub_providers(force=True)
    tags = ("a", "b")
    emails = [f"ddns-hnb-{tag}-{W}@example.invalid" for tag in tags]
    await purge_tenants(emails, owner="test_router_hostname_backends.world")

    hashed = unusable_password_hash()
    built: dict[str, Any] = {"emails": emails}
    async with fixture_writes("test_router_hostname_backends.world") as s:
        for tag, email in zip(tags, emails):
            user = User(
                email=email,
                hashed_password=hashed,
                is_active=True,
                is_verified=True,
                full_name=f"DDNS publishing probe {W}",
                preferred_language="en",
            )
            s.add(user)
            await s.flush()
            await unlock_user_secrets(s, user.id, create=True)

            domain = m.Domain(user_id=user.id, name=f"{tag}-pub-{W}.example.invalid")
            s.add(domain)
            await s.flush()

            backend_ids: list[int] = []
            for slot, result in zip(compat_stub.SLOTS, ("nochg", "good", "dnserr")):
                backend = m.DomainBackend(
                    domain_id=domain.id,
                    user_id=user.id,
                    backend_type=slot,
                    config=compat_stub.scripted_config(result),
                )
                backend.credentials = {"stub_token": "not-a-secret"}
                s.add(backend)
                await s.flush()
                backend_ids.append(backend.id)

            device = m.Device(
                user_id=user.id,
                username=f"ddns-{tag}-{W}-pub",
                password_hash=hashed,
                name=f"router-{tag}-{W}-pub",
            )
            s.add(device)
            await s.flush()

            hostname = m.Hostname(
                domain_id=domain.id,
                device_id=device.id,
                name=f"home.{domain.name}",
            )
            s.add(hostname)
            await s.flush()

            built[tag] = {
                "user": user,
                "user_id": user.id,
                "email": email,
                "domain_id": domain.id,
                "domain_name": domain.name,
                "backend_ids": backend_ids,
                "device_id": device.id,
                "device_name": device.name,
                "device_username": device.username,
                "hostname_id": hostname.id,
                "hostname_name": hostname.name,
            }

    compat_stub.reset_calls()
    yield built
    await purge_tenants(emails, owner="test_router_hostname_backends.world")


def _app(user: User, permissions: set[str]) -> FastAPI:
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


def _client(user: User, permissions: set[str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(
            app=_app(user, ALL_PERMS if permissions is None else permissions)
        ),
        base_url="http://publishing.test",
    )


def _publishing_url(hostname_id: int) -> str:
    return f"{HOSTNAMES}/{hostname_id}/backends"


#: The device secret every `_wire` client authenticates with. Set on the
#: row the first time `_wire` is called for a tenant, because the
#: fixture stores an unusable placeholder hash (argon2 is ~22 ms and
#: most tests here never touch the wire).
_WIRE_SECRET = "wire-secret-" + W


@contextlib.asynccontextmanager
async def _wire(world: dict[str, Any], tag: str) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client authenticating as the tenant's device, like a router.

    The wire half of every claim in this file that has one. A component
    test that only calls `resolve_backends` is a test of
    `resolve_backends`; the question is what the endpoint answers.
    """
    from atrium_ddns.auth_device import hash_password

    entry = world[tag]
    async with fixture_writes(f"test_router_hostname_backends.wire/{tag}") as s:
        device = await s.get(m.Device, entry["device_id"])
        # `hash_password` is a coroutine — argon2 is CPU-bound and runs
        # in a worker thread, so it has to be awaited. Forgetting the
        # await stores a coroutine object's repr as the hash and every
        # subsequent call answers `badauth`, which reads exactly like a
        # wrong password.
        device.password_hash = await hash_password(_WIRE_SECRET)

    application = FastAPI()
    application.include_router(router_nic.router)
    token = base64.b64encode(
        f"{entry['device_username']}:{_WIRE_SECRET}".encode()
    ).decode("ascii")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://nic.test",
        headers={"Authorization": f"Basic {token}"},
    ) as client:
        yield client


async def _resolve(hostname_id: int) -> tuple[m.Hostname, list[m.DomainBackend]]:
    """Load a hostname the way both publish paths do, and resolve it.

    Loaded with both relationships eager, because `resolve_backends` is
    a pure function over ORM state and a lazy load inside it would be a
    synchronous database call from an async context.
    """
    from sqlalchemy.orm import selectinload

    async with get_session_factory()() as s:
        row = (
            (
                await s.execute(
                    sa.select(m.Hostname)
                    .where(m.Hostname.id == hostname_id)
                    .options(
                        selectinload(m.Hostname.domain).selectinload(
                            m.Domain.backends
                        ),
                        selectinload(m.Hostname.selected_backends),
                    )
                )
            )
            .scalars()
            .one()
        )
        return row, m.resolve_backends(row)


# ===================================================================== #
# 0. The rows that existed before the migration
# ===================================================================== #
#
# The acceptance criterion the issue leads with, and the one whose
# failure is silent. Read §0 as one argument in three steps: the live
# schema says what `0004` leaves an existing row looking like, the
# revision's own source says it wrote nothing that could have changed
# that, and the behavioural test drives a row of exactly that shape
# through the real publish path.


async def test_the_migration_writes_no_rows():
    """`0004` contains no data-writing statement. Read off its AST.

    The structural half of the reconstruction argument. If this revision
    inserted or updated anything, a row created before it ran would not
    be in the state §0's behavioural test builds by hand, and that test
    would be asserting about a shape the database never holds.

    Parsed rather than grepped so a call spanning several lines, or one
    inside a helper, is still seen — and so the words `INSERT` and
    `UPDATE` appearing in the docstring (they do, at length) are not
    miscounted as code.

    Vacuity: the walk must find `upgrade()` and it must find the two
    schema calls that *are* there. A parse that silently matched nothing
    would report "no writes" about a file it had not read.
    """
    tree = ast.parse(REVISION_0004.read_text(encoding="utf-8"))
    upgrade = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    called = {
        node.func.attr
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"add_column", "create_table"} <= called, (
        f"the AST walk did not find 0004's own schema calls, so 'it writes "
        f"nothing' is a statement about a file this test failed to read. "
        f"Saw: {sorted(called)}"
    )
    writers = called & {
        "execute", "bulk_insert", "insert", "update", "delete", "get_bind"
    }
    assert not writers, (
        f"0004 calls {sorted(writers)}. This revision must not write rows: "
        f"the inherit reading of an empty selection is what keeps existing "
        f"hostnames publishing, and a backfill would freeze each one's "
        f"backend set at migration time — see the revision's docstring."
    )


@pytest.mark.functional  # migrated MySQL schema / dialect-specific
async def test_ttl_is_nullable_with_no_default_in_the_live_schema():
    """The schema half. NULL is the state `0004` leaves every row in.

    `information_schema`, not the model: a `nullable=True` in
    `models.py` and a `NOT NULL DEFAULT 60` in the database is exactly
    the disagreement this repository asserts against everywhere else,
    and it is the one that would make every existing hostname read as
    *explicitly set to 60* rather than as *inheriting*.
    """
    async with get_session_factory()() as s:
        row = (
            await s.execute(
                sa.text(
                    "SELECT IS_NULLABLE, COLUMN_DEFAULT FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ddns_hostname' "
                    "AND COLUMN_NAME = 'ttl'"
                )
            )
        ).one_or_none()
    assert row is not None, "ddns_hostname.ttl does not exist — 0004 did not run"
    assert row[0] == "YES", "ddns_hostname.ttl is NOT NULL; NULL is the inherit state"
    assert row[1] is None, (
        f"ddns_hostname.ttl has a server default of {row[1]!r}. A default makes "
        f"every pre-migration row indistinguishable from one an operator set "
        f"deliberately, and detaches all of them from their zone's setting."
    )


@pytest_asyncio.fixture
async def pre_migration_hostname(
    world: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """A hostname row as `0003` could have held it, inserted as raw SQL.

    Names **only** the columns that existed before `0004`. That is what
    makes it a reconstruction rather than a re-statement: an ORM insert
    would set `ttl` from the model (to NULL, by luck) and would leave
    the door open for a future default to make the test pass for the
    wrong reason. This statement cannot mention `ttl` at all, so the
    value it ends up with is whatever the migration decided — which is
    the thing under test.
    """
    a = world["a"]
    name = f"premigration.{a['domain_name']}"
    async with fixture_writes("test_router_hostname_backends.premigration") as s:
        await s.execute(
            sa.text(
                "INSERT INTO ddns_hostname (domain_id, device_id, name) "
                "VALUES (:domain_id, :device_id, :name)"
            ),
            {"domain_id": a["domain_id"], "device_id": a["device_id"], "name": name},
        )
        row_id = (
            await s.execute(
                sa.text("SELECT id FROM ddns_hostname WHERE name = :name"),
                {"name": name},
            )
        ).scalar_one()
    yield {"id": int(row_id), "name": name}


async def test_a_pre_migration_row_lands_with_no_ttl_and_no_selection(
    pre_migration_hostname: dict[str, Any],
):
    """The state, before anything is asked of it.

    Two columns and a table. This is the vacuity guard for the test
    below: "it publishes to all three" is only interesting if the row
    genuinely has no selection rows to publish to.
    """
    row_id = pre_migration_hostname["id"]
    async with get_session_factory()() as s:
        ttl = (
            await s.execute(
                sa.text("SELECT ttl FROM ddns_hostname WHERE id = :i"), {"i": row_id}
            )
        ).scalar_one()
        selections = (
            await s.execute(
                sa.text(
                    "SELECT COUNT(*) FROM ddns_hostname_backend "
                    "WHERE hostname_id = :i"
                ),
                {"i": row_id},
            )
        ).scalar_one()
    assert ttl is None, f"a row inserted without a ttl came back as {ttl!r}"
    assert selections == 0, (
        f"{selections} selection row(s) exist for a hostname nothing selected "
        f"backends for. If 0004 had backfilled, this is where it would show."
    )


async def test_a_pre_migration_row_still_publishes_to_every_backend(
    world: dict[str, Any], pre_migration_hostname: dict[str, Any]
):
    """**The acceptance criterion.** No selection -> all three backends.

    Driven through `models.resolve_backends`, the function
    `/nic/update` calls, against a row that carries no `ddns_hostname_
    backend` rows — i.e. against every hostname that existed before
    `0004` ran.

    The failure this catches is not a wrong list; it is an *empty* one.
    A resolver written the way the table's shape suggests
    (`return hostname.selected_backends`) returns `[]` here, `/nic/
    update` answers `911`, and the router logs it and retries and looks
    exactly like a router that is working. Asserted as an equality
    against the zone's own backends rather than as `len(...) == 3`, so
    a resolver returning three of the *wrong* rows fails too.
    """
    a = world["a"]
    row, resolved = await _resolve(pre_migration_hostname["id"])

    assert not row.selected_backends, "the fixture is not a pre-migration row"
    assert [backend.id for backend in resolved] == a["backend_ids"], (
        "a hostname with no backend selection did not resolve to its zone's "
        "backends. An empty selection means INHERIT — every hostname that "
        "existed before 0004 has no selection rows, so the other reading "
        "stops the whole migrated fleet publishing, and stops it silently."
    )


async def test_a_pre_migration_row_publishes_on_the_wire(
    world: dict[str, Any], pre_migration_hostname: dict[str, Any]
):
    """The same claim, one level up: through `/nic/update` itself.

    `resolve_backends` is what the previous test reads, and a test of a
    function is a test of a function. This drives the actual wire
    handler over HTTP Basic and asserts the aggregate the three
    scripted backends produce — `good`, because one of the three says
    so. With the selection read as "publish nowhere" the same request
    answers `911`, which is the exact string a stale fleet would be
    receiving.
    """
    a = world["a"]
    compat_stub.reset_calls()
    async with _wire(world, "a") as wire:
        response = await wire.get(
            "/nic/update",
            params={"hostname": pre_migration_hostname["name"], "myip": PUBLISHED},
        )

    assert response.text == f"good {PUBLISHED}", (
        f"/nic/update answered {response.text!r} for a hostname with no backend "
        f"selection. `911 {PUBLISHED}` is what 'empty means nowhere' produces, "
        f"and it is what every migrated router would have received."
    )
    contacted = sorted({call.service for call in compat_stub.CALLS})
    assert contacted == sorted(compat_stub.SLOTS), (
        f"only {contacted} were contacted; the zone has three bindings and a "
        f"name with no selection publishes to all of them"
    )
    assert a["backend_ids"]  # the fixture really did bind three


# ===================================================================== #
# 1. One resolver, reached from both entry points
# ===================================================================== #


async def test_the_editor_and_the_wire_share_one_backend_resolver():
    """`router.resolve_backends` *is* `router_nic.resolve_backends`.

    Identity, not equality of behaviour. "Both return the same list" is
    a property that holds until someone edits one copy; "the two names
    resolve to one function object" cannot stop holding without an
    import being deleted.

    This is the `zone_contains` lesson (#69 §1) applied to #74's join
    table, and the asymmetry it prevents is the same shape: an editor
    that shows three backends while the wire publishes to one reads to
    the owner as a broken provider, and the reverse reads as the UI
    lying.
    """
    assert router_module.resolve_backends is m.resolve_backends
    assert router_nic.resolve_backends is m.resolve_backends
    assert router_module.resolve_ttl is m.resolve_ttl
    assert router_nic.resolve_ttl is m.resolve_ttl


async def test_the_manual_update_runs_the_wires_own_publish_functions():
    """The endpoint imports the wire's publish path rather than copying it.

    Six function objects, each of which the manual update would
    otherwise have needed its own version of: the plan build (which
    backends, whose credentials), the provider loop, the persistence
    rule (`last_ip_*` moves on `good` and not on `nochg`), and the
    event writes. Every one of those has frozen cases behind it on the
    wire and none of them would have on a second implementation.
    """
    for name in (
        "load_plans",
        "run_dns_phase",
        "persist_updates",
        "record_hostname_events",
        "record_event",
        "commit_after_dns",
    ):
        assert getattr(router_module, name) is getattr(router_nic, name), (
            f"router.{name} is not router_nic.{name} — the manual update has "
            f"grown its own copy of the publish path"
        )


# ===================================================================== #
# 2. Selection semantics
# ===================================================================== #


async def test_an_explicit_selection_wins_over_the_zone(world: dict[str, Any]):
    """`backends-explicit-selection-wins`, disposition `preserve`.

    Selecting one of three publishes to one of three — and to *that*
    one, asserted by id rather than by count.
    """
    a = world["a"]
    chosen = a["backend_ids"][1]
    async with _client(a["user"]) as client:
        response = await client.put(
            _publishing_url(a["hostname_id"]),
            json={"backend_ids": [chosen], "ttl": None},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["publishes_to"] == [chosen]
    assert body["inherits_backends"] is False

    _, resolved = await _resolve(a["hostname_id"])
    assert [backend.id for backend in resolved] == [chosen], (
        "the endpoint reported a selection the resolver does not agree with"
    )


async def test_clearing_the_selection_restores_inheritance(world: dict[str, Any]):
    """`backends-empty-selection-resolves-to-all-of-the-domains-backends`.

    And `[]` and `null` are the same request — the legacy form posted no
    checkbox at all when none was ticked, and `get_backends()` fell
    through to the domain. Both spellings are driven here because a UI
    can send either and the two silently parting is the kind of thing
    nobody notices until a zone stops updating.
    """
    a = world["a"]
    async with _client(a["user"]) as client:
        for cleared in ([], None):
            pinned = await client.put(
                _publishing_url(a["hostname_id"]),
                json={"backend_ids": [a["backend_ids"][0]], "ttl": None},
            )
            assert pinned.json()["publishes_to"] == [a["backend_ids"][0]]

            response = await client.put(
                _publishing_url(a["hostname_id"]),
                json={"backend_ids": cleared, "ttl": None},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["inherits_backends"] is True, (
                f"backend_ids={cleared!r} did not clear the selection"
            )
            assert body["publishes_to"] == a["backend_ids"], (
                f"backend_ids={cleared!r} resolved to {body['publishes_to']}; "
                f"an empty selection inherits the zone, it does not mute the "
                f"name"
            )
            assert all(not row["selected"] for row in body["backends"])


async def test_an_inheriting_name_tracks_the_zone_live(world: dict[str, Any]):
    """`backends-empty-selection-tracks-the-domain-live`, `preserve`.

    The one place the two branches are behaviourally distinguishable,
    and the reason a backfill would have been wrong: a backend added to
    a zone is picked up by every inheriting name with no per-name
    action, and by no name that has chosen a subset.

    Both halves are asserted in one test on purpose. Either alone
    passes for a resolver that always inherits or one that never does.
    """
    a = world["a"]
    async with _client(a["user"]) as client:
        # A second name on the same zone, pinned to one backend.
        pinned = await client.post(
            HOSTNAMES,
            json={
                "name": f"pinned.{a['domain_name']}",
                "domain_id": a["domain_id"],
                "device_id": a["device_id"],
            },
        )
        assert pinned.status_code == 201, pinned.text
        pinned_id = pinned.json()["id"]
        await client.put(
            _publishing_url(pinned_id),
            json={"backend_ids": [a["backend_ids"][0]], "ttl": None},
        )

    # A fourth binding appears on the zone. Written directly rather than
    # through the API because `ddns_domain_backend` is UNIQUE on
    # (domain, backend_type) and the three stub slots are used up — the
    # point is a new *row*, not a new provider.
    async with fixture_writes("test_router_hostname_backends.newbinding") as s:
        await unlock_user_secrets(s, a["user_id"])
        extra = m.DomainBackend(
            domain_id=a["domain_id"],
            user_id=a["user_id"],
            backend_type="stub-late",
            config=compat_stub.scripted_config("good"),
        )
        extra.credentials = {"stub_token": "not-a-secret"}
        s.add(extra)
        await s.flush()
        extra_id = extra.id

    _, inheriting = await _resolve(a["hostname_id"])
    _, chosen = await _resolve(pinned_id)

    assert extra_id in [backend.id for backend in inheriting], (
        "a name with no selection did not pick up a backend added to its "
        "zone. Inheritance is resolved at read time precisely so it does."
    )
    assert extra_id not in [backend.id for backend in chosen], (
        "a name with an explicit selection picked up a new binding. An "
        "explicit choice is a choice; widening it silently is the mirror of "
        "narrowing it silently."
    )


async def test_the_selection_is_resolved_in_the_zones_own_order(
    world: dict[str, Any],
):
    """`backends-resolution-order-decides-the-aggregate-error`, `preserve`.

    The aggregate is "the first status that is neither good nor nochg",
    so the order the backends are walked in decides which error reaches
    the wire. Selecting them in reverse must not reverse the walk: the
    resolver filters the *zone's* ordered list rather than returning the
    selection as loaded.
    """
    a = world["a"]
    reversed_ids = list(reversed(a["backend_ids"]))
    async with _client(a["user"]) as client:
        body = (
            await client.put(
                _publishing_url(a["hostname_id"]),
                json={"backend_ids": reversed_ids, "ttl": None},
            )
        ).json()
    assert body["publishes_to"] == a["backend_ids"], (
        f"selecting {reversed_ids} resolved to {body['publishes_to']}. The "
        f"publish order is the zone's, not the order the ids arrived in — "
        f"the aggregate's 'first non-good/nochg' depends on it."
    )


async def test_dropping_the_good_backend_changes_the_aggregate(
    world: dict[str, Any],
):
    """The selection reaches the wire, measured by the answer changing.

    Slots are scripted `nochg`, `good`, `dnserr`. All three -> `good`.
    Drop the second -> `dnserr`, because that is the first status in
    zone order that is neither `good` nor `nochg`. A selection that was
    stored and ignored would answer `good` both times; one read in the
    order the ids arrived would answer `nochg`.
    """
    a = world["a"]
    async with _wire(world, "a") as wire:
        first = await wire.get(
            "/nic/update",
            params={"hostname": a["hostname_name"], "myip": PUBLISHED},
        )
    assert first.text == f"good {PUBLISHED}", first.text

    async with _client(a["user"]) as client:
        response = await client.put(
            _publishing_url(a["hostname_id"]),
            json={
                "backend_ids": [a["backend_ids"][0], a["backend_ids"][2]],
                "ttl": None,
            },
        )
        assert response.status_code == 200, response.text

    async with _wire(world, "a") as wire:
        second = await wire.get(
            "/nic/update",
            params={"hostname": a["hostname_name"], "myip": PUBLISHED},
        )
    assert second.text == f"dnserr {PUBLISHED}", (
        f"/nic/update answered {second.text!r} after the `good` backend was "
        f"deselected. The selection is stored and not read if this is still "
        f"`good`, and read in the wrong order if it is `nochg`."
    )


async def test_unbinding_a_backend_takes_its_selection_rows_with_it(
    world: dict[str, Any],
):
    """`ON DELETE CASCADE`, and what it means for the reading.

    A selection row cannot outlive the binding it names. So a name
    pinned to the only backend that is then unbound does not become a
    name pinned to nothing (which would publish nowhere) — it becomes a
    name with no selection, which inherits whatever the zone still has.
    """
    a = world["a"]
    async with _client(a["user"]) as client:
        await client.put(
            _publishing_url(a["hostname_id"]),
            json={"backend_ids": [a["backend_ids"][0]], "ttl": None},
        )
        removed = await client.delete(
            f"/api/atrium_ddns/backends/{a['backend_ids'][0]}"
        )
        assert removed.status_code == 204, removed.text

    row, resolved = await _resolve(a["hostname_id"])
    assert not row.selected_backends
    assert [backend.id for backend in resolved] == a["backend_ids"][1:], (
        "after its only selected binding was removed, the name should inherit "
        "the two that remain rather than publish to nothing"
    )


# ===================================================================== #
# 3. TTL — three levels, and NULL is never 60
# ===================================================================== #


def _ttls_seen() -> list[int | None]:
    return [call.ttl for call in compat_stub.CALLS if call.op == "create"]


async def _set_binding_ttl(world: dict[str, Any], tag: str, ttl: int) -> None:
    entry = world[tag]
    async with fixture_writes(f"test_router_hostname_backends.bindingttl/{ttl}") as s:
        for backend_id in entry["backend_ids"]:
            backend = await s.get(m.DomainBackend, backend_id)
            backend.config = {**(backend.config or {}), "ttl": ttl}


async def test_the_ttl_reaching_the_provider_is_the_bindings_when_unset(
    world: dict[str, Any],
):
    """Level 2, and level 3. `ttl-reaches-the-create-call`.

    Read off the stub's own call record rather than off the row this
    test wrote — the question is what the provider was called with, and
    a row and an assertion about that row share an author.

    The fixture's bindings carry no `ttl`, so the first reading is level
    3 (`DEFAULT_TTL`). Writing one and re-reading makes "the binding is
    consulted" a change rather than a coincidence.
    """
    a = world["a"]
    compat_stub.reset_calls()
    async with _wire(world, "a") as wire:
        await wire.get(
            "/nic/update",
            params={"hostname": a["hostname_name"], "myip": PUBLISHED},
        )
    assert set(_ttls_seen()) == {DEFAULT_TTL}, (
        f"with no override and no binding ttl, the provider should be called "
        f"at DEFAULT_TTL ({DEFAULT_TTL}); saw {_ttls_seen()}"
    )

    await _set_binding_ttl(world, "a", 300)
    compat_stub.reset_calls()
    async with _wire(world, "a") as wire:
        await wire.get(
            "/nic/update",
            params={"hostname": a["hostname_name"], "myip": PUBLISHED},
        )
    assert set(_ttls_seen()) == {300}, (
        f"the binding's config['ttl'] did not reach the provider; saw "
        f"{_ttls_seen()}"
    )


async def test_the_per_hostname_ttl_overrides_the_binding(world: dict[str, Any]):
    """Level 1, and `ttl-is-stored-per-hostname-not-per-domain`.

    Two names on one zone, one with an override and one without, driven
    through the wire in the same state. If the override leaked onto the
    zone — the shape the pre-`0004` schema forced, and the reason the
    importer refuses a zone whose names disagree — both would come back
    at 900.
    """
    a = world["a"]
    await _set_binding_ttl(world, "a", 300)

    async with _client(a["user"]) as client:
        sibling = await client.post(
            HOSTNAMES,
            json={
                "name": f"sibling.{a['domain_name']}",
                "domain_id": a["domain_id"],
                "device_id": a["device_id"],
            },
        )
        assert sibling.status_code == 201, sibling.text
        body = (
            await client.put(
                _publishing_url(a["hostname_id"]),
                json={"backend_ids": None, "ttl": 900},
            )
        ).json()
    assert body["ttl"] == 900
    assert {row["effective_ttl"] for row in body["backends"]} == {900}
    assert {row["binding_ttl"] for row in body["backends"]} == {300}

    compat_stub.reset_calls()
    async with _wire(world, "a") as wire:
        await wire.get(
            "/nic/update",
            params={"hostname": a["hostname_name"], "myip": PUBLISHED},
        )
        overridden = set(_ttls_seen())
        compat_stub.reset_calls()
        await wire.get(
            "/nic/update",
            params={"hostname": sibling.json()["name"], "myip": PUBLISHED},
        )
        inherited = set(_ttls_seen())

    assert overridden == {900}, f"the override did not reach the provider: {overridden}"
    assert inherited == {300}, (
        f"a sibling with no override was published at {inherited} — the "
        f"override is per name, and one name's TTL must not move another's"
    )


async def test_clearing_the_ttl_restores_the_binding_rather_than_writing_60(
    world: dict[str, Any],
):
    """NULL is *inherit*, not 60. The two are different rows and one lies.

    A `ttl: null` that stored `DEFAULT_TTL` would look identical on this
    screen and would silently stop tracking the binding — so the
    assertion is on the stored value, and then on what a *later* change
    to the binding does.
    """
    a = world["a"]
    async with _client(a["user"]) as client:
        await client.put(
            _publishing_url(a["hostname_id"]), json={"backend_ids": None, "ttl": 900}
        )
        cleared = (
            await client.put(
                _publishing_url(a["hostname_id"]),
                json={"backend_ids": None, "ttl": None},
            )
        ).json()
    assert cleared["ttl"] is None, (
        f"clearing the TTL stored {cleared['ttl']!r}. NULL means inherit; a "
        f"stored 60 is an operator's decision and follows nothing."
    )
    assert {row["effective_ttl"] for row in cleared["backends"]} == {DEFAULT_TTL}

    await _set_binding_ttl(world, "a", 1200)
    compat_stub.reset_calls()
    async with _wire(world, "a") as wire:
        await wire.get(
            "/nic/update",
            params={"hostname": a["hostname_name"], "myip": PUBLISHED},
        )
    assert set(_ttls_seen()) == {1200}, (
        f"after the TTL was cleared, a later change to the binding did not "
        f"reach the name; saw {_ttls_seen()}. That is what 'inherit' means, "
        f"and it is the difference a stored 60 would have hidden."
    )


@pytest.mark.parametrize(
    "ttl,accepted",
    [
        (29, False),
        (30, True),
        (60, True),
        (86400, True),
        (86401, False),
        (0, False),
        (-1, False),
    ],
)
async def test_the_ttl_bounds_are_the_legacy_forms(
    world: dict[str, Any], ttl: int, accepted: bool
):
    """`ttl-below-30-is-rejected` / `-above-86400-` / `-30-and-86400-`.

    Inclusive at both ends, which the legacy suite never actually tested
    — it probed 5 and 100000, neither of which can tell 30 from 31. The
    boundary is read off the validator and asserted here.
    """
    a = world["a"]
    async with _client(a["user"]) as client:
        response = await client.put(
            _publishing_url(a["hostname_id"]),
            json={"backend_ids": None, "ttl": ttl},
        )
    if accepted:
        assert response.status_code == 200, response.text
        assert response.json()["ttl"] == ttl
    else:
        assert response.status_code == 422, (
            f"ttl={ttl} was accepted; the bounds are {m.TTL_MIN}..{m.TTL_MAX} "
            f"inclusive"
        )


async def test_the_write_path_does_not_range_check_a_stored_ttl(
    world: dict[str, Any],
):
    """`ttl-is-not-validated-on-the-ddns-write-path`, `preserve`.

    The bounds live in the editing surface, not in the column and not in
    the publish path. A row written by something that is not this form —
    the importer, an operator's UPDATE — reaches the provider unchanged.
    Directly relevant to V1M4: a migrated row is not a form submission.
    """
    a = world["a"]
    async with fixture_writes("test_router_hostname_backends.rawttl") as s:
        hostname = await s.get(m.Hostname, a["hostname_id"])
        hostname.ttl = 5

    compat_stub.reset_calls()
    async with _wire(world, "a") as wire:
        await wire.get(
            "/nic/update",
            params={"hostname": a["hostname_name"], "myip": PUBLISHED},
        )
    assert set(_ttls_seen()) == {5}, (
        f"a stored TTL of 5 did not reach the provider ({_ttls_seen()}). The "
        f"range check belongs to the form; adding one here would silently "
        f"retune migrated rows."
    )


# ===================================================================== #
# 4. The manual update
# ===================================================================== #


def _update_url(hostname_id: int) -> str:
    return f"{HOSTNAMES}/{hostname_id}/update"


async def _events_for(hostname_id: int) -> list[m.DnsEvent]:
    async with get_session_factory()() as s:
        return list(
            (
                await s.execute(
                    sa.select(m.DnsEvent)
                    .where(m.DnsEvent.hostname_id == hostname_id)
                    .order_by(m.DnsEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def test_a_manual_update_publishes_and_reports_every_backend(
    world: dict[str, Any],
):
    """The endpoint the legacy page's third button had.

    Asserted against the **stub's own call record**, not against the
    response body: the response is the endpoint's report of what it did,
    and a report and the thing reported share an author. Three backends
    scripted `nochg`/`good`/`dnserr` produce three calls, three
    per-backend statuses, and one aggregate.
    """
    a = world["a"]
    compat_stub.reset_calls()
    async with _client(a["user"]) as client:
        response = await client.post(
            _update_url(a["hostname_id"]), json={"ip": PUBLISHED}
        )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "good"
    assert body["published"] is True
    assert body["ip"] == PUBLISHED
    assert body["rtype"] == "A"
    assert [attempt["status"] for attempt in body["attempts"]] == [
        "nochg",
        "good",
        "dnserr",
    ]
    assert [attempt["backend_id"] for attempt in body["attempts"]] == a["backend_ids"]

    calls = [call for call in compat_stub.CALLS if call.op == "create"]
    assert sorted(call.service for call in calls) == sorted(compat_stub.SLOTS), (
        f"the response claimed three attempts; the provider recorded "
        f"{[call.service for call in calls]}"
    )
    assert {call.ip for call in calls} == {PUBLISHED}
    assert {call.rtype for call in calls} == {"A"}


async def test_a_manual_update_normalises_the_address_it_was_given(
    world: dict[str, Any],
):
    """`2001:0db8::0001` publishes as `2001:db8::1`, as an AAAA.

    Through `auth_device.normalise_address` — the same canonicaliser
    `myip=` goes through, so the two agree about what an address is and
    about how it is spelled. The record type is decided by the address
    and not by a parameter, which is what makes a v6 manual update land
    in `last_ip_v6` and leave `last_ip_v4` alone.
    """
    a = world["a"]
    compat_stub.reset_calls()
    async with _client(a["user"]) as client:
        body = (
            await client.post(
                _update_url(a["hostname_id"]), json={"ip": "2001:0db8::0001"}
            )
        ).json()
    assert body["ip"] == "2001:db8::1", body
    assert body["rtype"] == "AAAA"
    assert {call.ip for call in compat_stub.CALLS} == {"2001:db8::1"}

    async with get_session_factory()() as s:
        row = await s.get(m.Hostname, a["hostname_id"])
        await s.refresh(row)
        assert row.last_ip_v6 == "2001:db8::1"
        assert row.last_ip_v4 is None, (
            "a v6 manual update wrote last_ip_v4. One column moves, chosen by "
            "the record type — the mirror the frozen table asserts for the wire."
        )


async def test_a_manual_update_moves_last_ip_only_on_good(world: dict[str, Any]):
    """The persistence rule, unchanged and not re-implemented.

    With only the `nochg` backend selected the aggregate is `nochg`, and
    `nochg` leaves `last_ip_*` and `last_updated_at` untouched — the
    same rule `update-nochg-single-backend` freezes for the wire, held
    here because the endpoint calls `persist_updates` rather than
    writing its own.
    """
    a = world["a"]
    async with _client(a["user"]) as client:
        await client.put(
            _publishing_url(a["hostname_id"]),
            json={"backend_ids": [a["backend_ids"][0]], "ttl": None},
        )
        body = (
            await client.post(_update_url(a["hostname_id"]), json={"ip": PUBLISHED})
        ).json()

    assert body["status"] == "nochg"
    assert body["published"] is False
    async with get_session_factory()() as s:
        row = await s.get(m.Hostname, a["hostname_id"])
        await s.refresh(row)
    assert row.last_ip_v4 is None, (
        "a `nochg` aggregate moved last_ip_v4. This column is a record of the "
        "last *change*, and a manual update must not be the one thing that "
        "makes it a liveness signal."
    )
    assert row.last_updated_at is None


async def test_a_manual_update_is_logged_as_manual_update_not_as_update(
    world: dict[str, Any],
):
    """A distinct `event_type`, and the reason it is distinct.

    `worker_jobs.device_statuses` derives "this device stopped calling
    in" from `event_type == 'update'`. Folding manual publishes into
    that would let the operator's own button answer the board's
    liveness question — a device that has been offline for a week would
    read as active because somebody re-published one of its names.

    One row per backend attempt, carrying that backend's own status
    rather than the aggregate, exactly as the wire writes them.
    """
    a = world["a"]
    async with _client(a["user"]) as client:
        await client.post(_update_url(a["hostname_id"]), json={"ip": PUBLISHED})

    rows = await _events_for(a["hostname_id"])
    assert rows, "the manual update wrote no event row at all"
    assert {row.event_type for row in rows} == {router_nic.EVENT_MANUAL_UPDATE}
    assert router_nic.EVENT_MANUAL_UPDATE in router_module.EVENT_TYPES, (
        "manual_update is written but is not in the log-search vocabulary — a "
        "row that exists and cannot be filtered for"
    )
    assert [row.response_code for row in rows] == ["nochg", "good", "dnserr"], (
        "the log carries the aggregate rather than the per-backend status"
    )
    assert {row.backend_type for row in rows} == set(compat_stub.SLOTS)
    assert {row.client_ip for row in rows} == {None}, (
        "client_ip was populated for a publish nobody called in for. That "
        "column is where the router called *from*; filling it with the "
        "operator's address puts a helpdesk on the board."
    )
    assert {row.device_id for row in rows} == {a["device_id"]}
    assert {row.user_id for row in rows} == {a["user_id"]}


async def test_a_manual_update_refuses_a_name_with_no_device(
    world: dict[str, Any],
):
    """409, with the reason, rather than an unmetered publish path.

    The publish is charged to the hostname's device. An unassigned name
    has no budget to charge, and `/nic/update` cannot publish it either
    — ownership on the wire is checked against the device. Refusing is
    the honest answer; the alternative is an unlimited provider call for
    exactly the names that have no router behind them.
    """
    a = world["a"]
    async with _client(a["user"]) as client:
        created = await client.post(
            HOSTNAMES,
            json={
                "name": f"orphan.{a['domain_name']}",
                "domain_id": a["domain_id"],
                "device_id": None,
            },
        )
        assert created.status_code == 201, created.text
        response = await client.post(
            _update_url(created.json()["id"]), json={"ip": PUBLISHED}
        )
    assert response.status_code == 409, response.text
    assert "not assigned to a device" in response.json()["detail"]


@pytest.mark.parametrize("bad", ["", "not-an-ip", "999.1.1.1", "203.0.113.74/32"])
async def test_a_manual_update_refuses_an_address_that_is_not_one(
    world: dict[str, Any], bad: str
):
    """422 through the wire's own canonicaliser, and no provider call."""
    a = world["a"]
    compat_stub.reset_calls()
    async with _client(a["user"]) as client:
        response = await client.post(_update_url(a["hostname_id"]), json={"ip": bad})
    assert response.status_code == 422, response.text
    assert compat_stub.CALLS == [], (
        f"{bad!r} was refused and a provider was contacted anyway"
    )


async def test_a_manual_update_is_charged_to_the_devices_rate_limit(
    world: dict[str, Any],
):
    """The abuse surface, closed on the control G2 already exposes.

    Two halves, and the second is the one that matters:

    * the manual update **spends** a slot from `ddns_rate_limit_event`,
      the same table `/nic/update` writes;
    * a device already at its limit is refused with 429 — measured by
      setting `ddns_device.rate_limit_per_minute` to 1, spending the
      one slot, and asking again.

    A separate budget would let a caller draw the provider quota twice,
    and providers charge per call. Same cost to the same third party,
    same allowance.
    """
    a = world["a"]
    async with fixture_writes("test_router_hostname_backends.ratelimit") as s:
        device = await s.get(m.Device, a["device_id"])
        device.rate_limit_per_minute = 1

    async def _slots() -> int:
        async with get_session_factory()() as s:
            return (
                await s.execute(
                    sa.select(sa.func.count(m.RateLimitEvent.id)).where(
                        m.RateLimitEvent.device_id == a["device_id"]
                    )
                )
            ).scalar_one()

    assert await _slots() == 0, "the fixture device has already spent slots"

    compat_stub.reset_calls()
    async with _client(a["user"]) as client:
        first = await client.post(
            _update_url(a["hostname_id"]), json={"ip": PUBLISHED}
        )
        assert first.status_code == 200, first.text
        assert first.json()["rate_limit_per_minute"] == 1
        assert await _slots() == 1, (
            "the manual update published without spending a rate-limit slot — "
            "an unmetered path to a metered provider"
        )

        calls_after_first = len(compat_stub.CALLS)
        second = await client.post(
            _update_url(a["hostname_id"]), json={"ip": PUBLISHED}
        )

    assert second.status_code == 429, second.text
    assert second.headers.get("Retry-After") == "60"
    assert len(compat_stub.CALLS) == calls_after_first, (
        "a refused manual update contacted the provider anyway; the limit has "
        "to bite before the money is spent"
    )
    assert await _slots() == 1, (
        "a refused request was recorded in the window. The rows ARE the "
        "window, so recording refusals makes it self-extending — the same "
        "rule check_rate_limit holds for the wire."
    )

    refusals = [
        row
        for row in await _events_for(a["hostname_id"])
        if row.response_code == router_nic.STATUS_ABUSE
    ]
    assert len(refusals) == 1, "the refusal was not recorded in the log"
    assert refusals[0].event_type == router_nic.EVENT_MANUAL_UPDATE
    assert "rate limit exceeded" in (refusals[0].message or "")


async def test_the_wire_and_the_button_share_one_budget(world: dict[str, Any]):
    """The half that a separate limiter would pass.

    A device with one slot per minute spends it on `/nic/update`, and
    the button is then refused. Two limiters would each see an unused
    allowance, which is precisely the doubling this is written to catch.
    """
    a = world["a"]
    async with fixture_writes("test_router_hostname_backends.sharedbudget") as s:
        device = await s.get(m.Device, a["device_id"])
        device.rate_limit_per_minute = 1

    async with _wire(world, "a") as wire:
        spent = await wire.get(
            "/nic/update",
            params={"hostname": a["hostname_name"], "myip": PUBLISHED},
        )
    assert spent.text == f"good {PUBLISHED}", spent.text

    async with _client(a["user"]) as client:
        response = await client.post(
            _update_url(a["hostname_id"]), json={"ip": PUBLISHED}
        )
    assert response.status_code == 429, (
        f"the button answered {response.status_code} after the router had "
        f"spent the device's only slot. The two draw on one budget because "
        f"they cost the provider the same."
    )


# ===================================================================== #
# 5. Tenancy — 404, never 403, on every id in every verb
# ===================================================================== #


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("GET", "/backends", None),
        ("PUT", "/backends", {"backend_ids": None, "ttl": None}),
        ("POST", "/update", {"ip": PUBLISHED}),
    ],
)
async def test_another_tenants_hostname_is_a_404_on_every_route(
    world: dict[str, Any], method: str, suffix: str, body: dict[str, Any] | None
):
    """A's client, B's hostname id. Never 403 — that confirms it exists.

    The vacuity guard is the mirror: the same call against A's own
    hostname is not a 404, so "everything 404s" cannot pass this.
    """
    a, b = world["a"], world["b"]
    async with _client(a["user"]) as client:
        foreign = await client.request(
            method, f"{HOSTNAMES}/{b['hostname_id']}{suffix}", json=body
        )
        own = await client.request(
            method, f"{HOSTNAMES}/{a['hostname_id']}{suffix}", json=body
        )
    assert foreign.status_code == 404, (
        f"{method} against another tenant's hostname answered "
        f"{foreign.status_code}; 403 tells the caller the id exists"
    )
    assert own.status_code != 404, (
        f"{method} against the caller's own hostname answered 404 — the "
        f"assertion above would be vacuous"
    )


async def test_a_backend_from_another_zone_is_refused_by_name(
    world: dict[str, Any],
):
    """422, not a silent drop.

    A selection is stored as ids and resolved through `domain.backends`,
    so an id from elsewhere would simply never appear in `publishes_to`.
    The request would answer 200 and do nothing — a configuration screen
    that accepts a setting and does not apply it, which is the worst
    available outcome and the one that looks best.
    """
    a, b = world["a"], world["b"]
    async with _client(a["user"]) as client:
        response = await client.put(
            _publishing_url(a["hostname_id"]),
            json={"backend_ids": [b["backend_ids"][0]], "ttl": None},
        )
    assert response.status_code == 422, response.text
    assert "not bound to the zone" in response.json()["detail"]

    _, resolved = await _resolve(a["hostname_id"])
    assert [backend.id for backend in resolved] == a["backend_ids"], (
        "the refused request changed the selection anyway"
    )


async def test_the_selection_rows_are_scoped(world: dict[str, Any]):
    """`backends-selection-is-owner-scoped`, at the query layer.

    Two hops to the owner — selection -> hostname -> domain.user_id —
    which is a longer path than any other model here and therefore the
    one most likely to be got wrong. Asserted through the scope itself
    rather than through the endpoint, because the endpoint's own
    404 could come from the hostname fetch and hide a selection-row
    predicate that matches everything.
    """
    from atrium_ddns.scope import DdnsScope

    a, b = world["a"], world["b"]
    async with _client(a["user"]) as client:
        await client.put(
            _publishing_url(a["hostname_id"]),
            json={"backend_ids": [a["backend_ids"][0]], "ttl": None},
        )

    async with get_session_factory()() as s:
        row_id = (
            await s.execute(
                sa.select(m.HostnameBackend.id).where(
                    m.HostnameBackend.hostname_id == a["hostname_id"]
                )
            )
        ).scalar_one()
        mine = DdnsScope.for_user_id(a["user_id"])
        theirs = DdnsScope.for_user_id(b["user_id"])
        assert await mine.get(s, m.HostnameBackend, row_id) is not None, (
            "the owner cannot see their own selection row — the assertion "
            "below would be vacuous"
        )
        assert await theirs.get(s, m.HostnameBackend, row_id) is None


# ===================================================================== #
# 6. The admin pair, cross-tenant, demonstrated
# ===================================================================== #


async def test_an_admin_configures_and_publishes_another_tenants_name(
    world: dict[str, Any],
):
    """The legacy `/admin/users/<uid>/hostnames/<hn>/backends`, closed.

    One caller, `atrium_ddns.admin`, against a zone owned by a
    *different* tenant — the same two endpoints, reached because the
    tenancy predicate widens rather than because the handler grew a
    branch. #69 demonstrated the lifecycle pair this way; this is the
    publishing pair.

    The last assertion is the one that is easy to get wrong: the log row
    is attributed to the **owner**, not to the admin. An admin's id in
    `ddns_event.user_id` would put another tenant's zone activity into
    the admin's own log search and take it out of the owner's — the
    surface built to answer "why is my name not updating" would be
    missing the update that changed it.
    """
    a, b = world["a"], world["b"]
    admin_perms = ALL_PERMS | {CROSS_TENANT_PERMISSION}

    compat_stub.reset_calls()
    async with _client(a["user"], admin_perms) as client:
        read = await client.get(_publishing_url(b["hostname_id"]))
        assert read.status_code == 200, read.text
        assert read.json()["domain_name"] == b["domain_name"]

        configured = await client.put(
            _publishing_url(b["hostname_id"]),
            json={"backend_ids": [b["backend_ids"][1]], "ttl": 120},
        )
        assert configured.status_code == 200, configured.text
        assert configured.json()["publishes_to"] == [b["backend_ids"][1]]
        assert configured.json()["ttl"] == 120

        published = await client.post(
            _update_url(b["hostname_id"]), json={"ip": PUBLISHED}
        )
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "good"

    calls = [call for call in compat_stub.CALLS if call.op == "create"]
    assert [call.service for call in calls] == ["stub2"], (
        f"the admin's selection did not reach the provider: {calls}"
    )
    assert {call.ttl for call in calls} == {120}

    rows = await _events_for(b["hostname_id"])
    assert rows, "the cross-tenant publish wrote no event row"
    assert {row.user_id for row in rows} == {b["user_id"]}, (
        "the log attributed an admin's publish to the admin. The row is about "
        "the owner's zone and belongs in the owner's history."
    )
    assert {row.user_email for row in rows} == {b["email"]}

    # …and the owner sees it, which is the half an attribution bug would
    # leave looking fine from the admin's side.
    async with _client(b["user"]) as owner:
        own = await owner.get(_publishing_url(b["hostname_id"]))
    assert own.status_code == 200
    assert own.json()["ttl"] == 120


async def test_a_plain_tenant_is_not_an_admin(world: dict[str, Any]):
    """The mirror. Without the permission, the same three calls are 404.

    Asserted separately from §5 because §5's client holds every
    *ordinary* permission and this one is about the cross-tenant grant
    specifically: a test that only showed the admin succeeding would
    pass against an endpoint with no scope at all.
    """
    a, b = world["a"], world["b"]
    async with _client(a["user"], ALL_PERMS) as client:
        assert (await client.get(_publishing_url(b["hostname_id"]))).status_code == 404
        assert (
            await client.put(
                _publishing_url(b["hostname_id"]),
                json={"backend_ids": None, "ttl": None},
            )
        ).status_code == 404
        assert (
            await client.post(_update_url(b["hostname_id"]), json={"ip": PUBLISHED})
        ).status_code == 404
