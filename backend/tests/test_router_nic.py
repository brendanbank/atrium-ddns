"""``/nic/*`` — and specifically the half the frozen wire table cannot see.

The 131-case table in ``tests/compat/protocol_cases.yaml`` owns the
wire. This file owns everything the wire hides, which #29 measured
rather than argued: ``getiptype`` was mutated on a throwaway legacy
instance to answer ``A`` for **every** address, so every ``AAAA`` the
service would have written became an ``A``, and the whole table was run
against it — **121 executed, 0 failed**. Update's replies carry the
normalised *address* and never the record type; delete's carry neither.
So a host that echoes ``good 2001:db8::1`` and writes an ``A`` passes
every case in the table.

Two things follow, and both are done here:

* **the record type is asserted against the provider's own call log**,
  not against a response body. ``atrium_ddns.compat_stub.CALLS`` records
  ``rtype`` for every ``createrecords`` / ``deleterecords`` call;
* **that assertion is shown to bite.** ``test_the_record_type_guard_is_not
  _vacuous`` re-applies #29's exact mutation — ``getiptype`` answers
  ``A`` for everything — and asserts this file goes red for it, with a
  count. A guard against a defect that has never been demonstrated
  against it is a guard that is believed rather than checked.

The `effects:` gap, in numbers. 23 of the 127 host-selected cases carry
an ``effects:`` block and the wire runner asserts **none** of them; it
prints the count every run so the gap stays a number. This file asserts
the DNS-operation half and the persisted-column half for the shapes
that matter (``rtype`` on both endpoints, ``last_ip_v4`` /
``last_ip_v6`` / ``last_updated_at`` in both directions, and delete
persisting nothing). It does **not** assert them case-by-case against
the table's own ``effects:`` data — see the PR body for which remain
unasserted rather than letting a green run imply otherwise.

Requires a live database, so it runs inside the api container via
``make test-backend``. Everything it creates is namespaced by
``PYTEST_XDIST_WORKER``: domain names, hostnames and device usernames
are globally unique by constraint, so ten workers sharing one MySQL
collide on every one of them.
"""
from __future__ import annotations

import base64
import os
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.db import get_session_factory
from app.models.auth import User
from conftest import fixture_writes, purge_tenants
from fastapi import FastAPI, Request
from httpx import ASGITransport

from atrium_ddns import compat_stub, router_nic
from atrium_ddns.auth_device import client_address, hash_password, normalise_address
from atrium_ddns.compat_stub import CALLS, SLOTS, scripted_config
from atrium_ddns.models import Device, DnsEvent, Domain, DomainBackend, Hostname
from atrium_ddns.providers import BaseProvider

# No `pytestmark = pytest.mark.asyncio`: `backend/pyproject.toml` sets
# `asyncio_mode = "auto"`, and a blanket mark then decorates the sync
# tests in here too — pytest warns on each and the mark does nothing.
W = os.environ.get("PYTEST_XDIST_WORKER", "serial")
SUFFIX = f"{W}.nic.invalid"
EMAIL_DOMAIN = f"nic-{W}.invalid"

#: The fixture's synthetic provider credential. Reaches a stub that
#: contacts nothing.
CREDS = {"stub_token": "not-a-secret"}

#: Raised well past anything a test sends, so the limiter is inert
#: except in the tests that set it deliberately. ``NULL`` would mean
#: *inherit the namespace default* (30/min), which several tests here
#: would then trip by accident and blame on the router.
BIG_LIMIT = 100_000


# --------------------------------------------------------------------- #
# The world
# --------------------------------------------------------------------- #


@dataclass
class World:
    alice_id: int
    carol_id: int
    dave_id: int
    alice_device: int
    alice_spare_device: int
    carol_device: int
    hostnames: dict[str, int]

    def name(self, key: str) -> str:
        return f"{key}.{SUFFIX}"


ALICE_PASSWORD = "alice-basic-secret"
CAROL_PASSWORD = "carol-basic-secret"
DAVE_PASSWORD = "dave-basic-secret"

#: ``key -> (owner, device, [(slot_index, result_or_None, creds_present)])``
#: mirroring the frozen fixture's shapes. ``result=None`` with
#: ``creds=False`` is the ``credentials: absent`` backend; the literal
#: ``"no-such-service"`` is the one the factory must not know.
LAYOUT: dict[str, dict[str, Any]] = {
    "ok": {"owner": "alice", "device": "alice", "backends": [("good", True)]},
    "zz-ok": {"owner": "alice", "device": "alice", "backends": [("good", True)]},
    "nochg": {"owner": "alice", "device": "alice", "backends": [("nochg", True)]},
    "dnserr": {"owner": "alice", "device": "alice", "backends": [("dnserr", True)]},
    "nobackend": {"owner": "alice", "device": "alice", "backends": []},
    "nocreds": {"owner": "alice", "device": "alice", "backends": [("good", False)]},
    "unknownsvc": {
        "owner": "alice",
        "device": "alice",
        "backends": [("no-such-service", True)],
    },
    "mixed": {
        "owner": "alice",
        "device": "alice",
        "backends": [("dnserr", True), ("good", True)],
    },
    "allnochg": {
        "owner": "alice",
        "device": "alice",
        "backends": [("nochg", True), ("nochg", True)],
    },
    "firsterr": {
        "owner": "alice",
        "device": "alice",
        "backends": [("nochg", True), ("good", False), ("dnserr", True)],
    },
    # Alice's hostname, assigned to a *second* device of hers. Plan
    # §3.3: after the migration ownership is per device, so her main
    # router must get `nohost` for it even though the tenant matches.
    "otherdevice": {
        "owner": "alice",
        "device": "alice-spare",
        "backends": [("good", True)],
    },
    "carols": {"owner": "carol", "device": "carol", "backends": [("good", True)]},
}

#: Its domain is deliberately not a suffix of it, which is the whole
#: content of the frozen ``wrong-zone`` backend.
OUT_OF_ZONE_DOMAIN = f"elsewhere.{SUFFIX}"

#: A ``TEST-NET-3`` address (RFC 5737) unique to this xdist worker, used
#: by ``test_badauth_is_recorded``. A badauth row has no tenant to
#: namespace it with — that is the point of the row — so the address is
#: the only handle, and a global count over ``event_type='auth'`` reads
#: nine other workers' rows as well as its own. Derived from the worker
#: id rather than typed, so a run with more workers than this file
#: anticipates still gets distinct values.
BADAUTH_IP = "203.0.113.%d" % (100 + (zlib.crc32(W.encode()) % 100))

#: The same handle for #64's other two refusals, in ranges that cannot
#: collide with :data:`BADAUTH_IP`'s 100–199.
#:
#: Three addresses rather than one because the three rows have to be
#: told apart *and* ``clear_events`` cannot reach all of them: it
#: deletes by ``user_id``, so the attributed rows go and the
#: unattributed one stays. Counting before and after per address is what
#: makes each assertion about this test's own row rather than about the
#: table.
UNKNOWN_USERNAME_IP = "203.0.113.%d" % (200 + (zlib.crc32(W.encode()) % 50))
INACTIVE_OWNER_IP = "192.0.2.%d" % (100 + (zlib.crc32(W.encode()) % 100))


@pytest_asyncio.fixture(scope="module")
async def world() -> Any:
    """The frozen fixture's shapes, namespaced per xdist worker.

    Module-scoped because building it costs three argon2 hashes and a
    bcrypt one (~0.5 s) and nothing in this file mutates it in a way
    the next test can see — the two that do (``last_ip_*``,
    ``password_hash``) reset what they touched.
    """
    from app.host_sdk.crypto import unlock_user_secrets
    from pwdlib.hashers.bcrypt import BcryptHasher

    compat_stub.register_stub_providers(force=True)

    emails = [f"{who}@{EMAIL_DOMAIN}" for who in ("alice", "carol", "dave")]
    await purge_tenants(emails, owner="test_router_nic.world")

    # Hashing happens *before* the guarded region below: four argon2 /
    # bcrypt hashes are ~0.5 s of pure CPU and nothing about them needs
    # the database. Inside the lock they would be 0.5 s during which no
    # other worker can build a fixture.
    unusable = await hash_password("unusable-web-login-" + "x" * 32)
    # alice is stored as **bcrypt** — the migrated-row shape from plan
    # §3.2 — so the auth tests exercise the legacy verify path and the
    # opportunistic re-hash for real rather than against a fabricated
    # hash string.
    alice_hash = BcryptHasher().hash(ALICE_PASSWORD)
    carol_hash = await hash_password(CAROL_PASSWORD)
    dave_hash = await hash_password(DAVE_PASSWORD)

    async with fixture_writes("test_router_nic.world") as s:
        users: dict[str, int] = {}
        for who, active in (("alice", True), ("carol", True), ("dave", False)):
            user = User(
                email=f"{who}@{EMAIL_DOMAIN}",
                hashed_password=unusable,
                is_active=active,
                is_verified=True,
                full_name=f"nic probe {who} {W}",
                preferred_language="en",
            )
            s.add(user)
            await s.flush()
            users[who] = user.id

        devices: dict[str, int] = {}
        for label, who, stored in (
            ("alice", "alice", alice_hash),
            ("alice-spare", "alice", carol_hash),
            ("carol", "carol", carol_hash),
            ("dave", "dave", dave_hash),
        ):
            device = Device(
                user_id=users[who],
                username=f"{label}-{W}",
                password_hash=stored,
                name=f"{label}-router",
                rate_limit_per_minute=BIG_LIMIT,
            )
            s.add(device)
            await s.flush()
            devices[label] = device.id

        for uid in users.values():
            await unlock_user_secrets(s, uid, create=True)

        hostnames: dict[str, int] = {}
        for key, spec in LAYOUT.items():
            hostname = f"{key}.{SUFFIX}"
            domain_name = hostname
            domain = Domain(user_id=users[spec["owner"]], name=domain_name)
            s.add(domain)
            await s.flush()
            for index, (result, creds) in enumerate(spec["backends"]):
                backend_type = (
                    result if result == "no-such-service" else SLOTS[index]
                )
                row = DomainBackend(
                    domain_id=domain.id,
                    user_id=users[spec["owner"]],
                    backend_type=backend_type,
                    config=(
                        scripted_config(result)
                        if result != "no-such-service"
                        else None
                    ),
                )
                if creds:
                    row.credentials = dict(CREDS)
                s.add(row)
                await s.flush()
            row_h = Hostname(
                domain_id=domain.id,
                device_id=devices[spec["device"]],
                name=hostname,
            )
            s.add(row_h)
            await s.flush()
            hostnames[key] = row_h.id

        # The out-of-zone hostname: its domain is `elsewhere.…`, which
        # it is not inside, so `hostnameperzone` refuses it.
        elsewhere = Domain(user_id=users["alice"], name=OUT_OF_ZONE_DOMAIN)
        s.add(elsewhere)
        await s.flush()
        wrong = DomainBackend(
            domain_id=elsewhere.id,
            user_id=users["alice"],
            backend_type=SLOTS[0],
            config=scripted_config("good"),
        )
        wrong.credentials = dict(CREDS)
        s.add(wrong)
        await s.flush()
        out_of_zone = Hostname(
            domain_id=elsewhere.id,
            device_id=devices["alice"],
            name=f"outofzone.{SUFFIX}",
        )
        s.add(out_of_zone)
        await s.flush()
        hostnames["outofzone"] = out_of_zone.id

    built = World(
        alice_id=users["alice"],
        carol_id=users["carol"],
        dave_id=users["dave"],
        alice_device=devices["alice"],
        alice_spare_device=devices["alice-spare"],
        carol_device=devices["carol"],
        hostnames=hostnames,
    )
    built.device_usernames = {  # type: ignore[attr-defined]
        label: f"{label}-{W}" for label in ("alice", "alice-spare", "carol", "dave")
    }
    yield built

    await purge_tenants(emails, owner="test_router_nic.world")


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """A bare FastAPI app carrying **only** this router.

    Deliberately without atrium's SPA catch-all mount. That is what
    makes the method assertions in this file mean something: over the
    wire a ``Mount`` full-matches every method, so ``POST /nic/update``
    reaches the catch-all and answers 405 whether or not the route
    exists — which is exactly why the two frozen
    ``-post-is-405-on-the-host`` cases pass against a host with no
    ``/nic`` routes at all. Here there is nothing else to answer.
    """
    application = FastAPI()
    application.include_router(router_nic.router)
    return application


@pytest.fixture(scope="module")
def spa_dir(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("spa")
    (directory / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    return directory


@pytest.fixture(scope="module")
def mounted_app(spa_dir: Path) -> FastAPI:
    """The router **behind a catch-all mount**, the way atrium serves it.

    Not a nicety. ``app`` above has nothing but the router, and a
    ``HEAD`` on a ``GET``-only route answers 405 there whether or not
    this repository declares a ``HEAD`` handler — so an assertion made
    against ``app`` alone cannot see the handler at all. **Measured:**
    deleting both ``HEAD`` routes from ``router_nic`` left every test
    in this file green until this fixture existed.

    Atrium mounts the built SPA at ``/`` as the last route, and a
    ``Mount`` matches every path *and every method*. Starlette prefers a
    full match over a partial one, so the 405 the ``GET``-only route
    would have produced is overtaken by the mount — which then answers
    something that is not 405.

    ``app.static.SPAStaticFiles``, the real class from the image,
    rather than a stand-in: what it answers is exactly the question,
    and a plain ``StaticFiles`` is a different class with a different
    404 path.
    """
    from app.static import SPAStaticFiles

    application = FastAPI()
    application.include_router(router_nic.router)
    application.mount(
        "/", SPAStaticFiles(directory=str(spa_dir), html=True), name="spa"
    )
    return application


@pytest.fixture(scope="module")
def mounted_app_without_head_routes(spa_dir: Path) -> FastAPI:
    """The **natural** implementation: a ``GET`` route and the mount.

    A faithful reproduction rather than a mutation of the real router,
    so the comparison in
    ``test_the_head_refusal_does_not_fall_out_of_the_framework``
    is between two live stacks in one process.
    """
    from app.static import SPAStaticFiles

    application = FastAPI()

    @application.get("/nic/update")
    async def _update() -> str:  # pragma: no cover — status only
        return "good"

    application.mount(
        "/", SPAStaticFiles(directory=str(spa_dir), html=True), name="spa"
    )
    return application


@pytest.fixture(scope="module")
def catch_all_only_app(spa_dir: Path) -> FastAPI:
    """The mount and nothing else — the shape this repo shipped until now."""
    from app.static import SPAStaticFiles

    application = FastAPI()
    application.mount(
        "/", SPAStaticFiles(directory=str(spa_dir), html=True), name="spa"
    )
    return application


def _client(application: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=application), base_url="http://nic.test"
    )


@pytest_asyncio.fixture
async def client(app: FastAPI) -> Any:
    async with _client(app) as instance:
        yield instance


@pytest_asyncio.fixture
async def mounted_client(mounted_app: FastAPI) -> Any:
    async with _client(mounted_app) as instance:
        yield instance


@pytest.fixture(autouse=True)
def clear_calls() -> Iterator[None]:
    CALLS.clear()
    yield
    CALLS.clear()


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def alice(world: World) -> dict[str, str]:
    return basic(world.device_usernames["alice"], ALICE_PASSWORD)  # type: ignore[attr-defined]


def xff(address: str) -> dict[str, str]:
    return {"X-Forwarded-For": address}


async def get(
    client: httpx.AsyncClient,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    **params: str,
) -> httpx.Response:
    merged = {**xff("203.0.113.10"), **(headers or {})}
    return await client.get(path, params=params, headers=merged)


def creates(rtype: str | None = None) -> list[compat_stub.StubCall]:
    return [
        call
        for call in CALLS
        if call.op == "create" and (rtype is None or call.rtype == rtype)
    ]


def deletes() -> list[compat_stub.StubCall]:
    return [call for call in CALLS if call.op == "delete"]


async def hostname_row(hostname_id: int) -> Hostname:
    factory = get_session_factory()
    async with factory() as s:
        return (
            await s.execute(sa.select(Hostname).where(Hostname.id == hostname_id))
        ).scalar_one()


async def events_for(user_id: int) -> list[DnsEvent]:
    factory = get_session_factory()
    async with factory() as s:
        return list(
            (
                await s.execute(
                    sa.select(DnsEvent)
                    .where(DnsEvent.user_id == user_id)
                    .order_by(DnsEvent.id)
                )
            )
            .scalars()
            .all()
        )


async def clear_events() -> None:
    """Drop this module's event rows between tests, by ``user_id``.

    It used to read ``WHERE user_email LIKE '%@<domain>'``.
    ``user_email`` carries no index and ``LIKE '%…'`` cannot use one
    even if it did, so every one of the dozen calls scanned
    ``ddns_event`` whole and took next-key locks across it — while nine
    other workers were inserting into the same table. It never appeared
    in a logged deadlock, but "has not deadlocked yet" is not the same
    reading as "cannot": #65's whole subject is a statement that was
    fine in isolation and contended between modules.

    ``user_id`` is the leading column of ``ix_ddns_event_user_created``,
    and the ids come from an equality on ``users.email``'s unique index.
    Behaviour is unchanged: a ``LIKE`` on ``user_email`` never matched
    the unattributed (``user_email IS NULL``) rows either.
    """
    async with fixture_writes("test_router_nic.clear_events") as s:
        user_ids = list(
            (
                await s.execute(
                    sa.select(User.id).where(User.email.like(f"%@{EMAIL_DOMAIN}"))
                )
            ).scalars()
        )
        if user_ids:
            await s.execute(
                sa.text("DELETE FROM ddns_event WHERE user_id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True)
                ),
                {"ids": user_ids},
            )


# ===================================================================== #
# 1. The record type — what the wire cannot see
# ===================================================================== #


async def test_an_ipv4_update_writes_an_a_record(client, world) -> None:
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    assert response.text == "good 203.0.113.10"
    assert [call.rtype for call in creates()] == ["A"]
    assert [call.ip for call in creates()] == ["203.0.113.10"]


async def test_an_ipv6_update_writes_an_aaaa_record(client, world) -> None:
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("ok"), myip="2001:db8:113::20",
    )
    assert response.text == "good 2001:db8:113::20"
    assert [call.rtype for call in creates()] == ["AAAA"]


async def test_an_ipv4_mapped_ipv6_stays_an_aaaa_record(client, world) -> None:
    """``::ffff:192.0.2.1`` — two plausible ways to get this wrong.

    Unwrapping via ``.ipv4_mapped`` changes the **body**; dispatching on
    "does it contain a dot" changes the **record type**. Only the first
    is visible on the wire, which is why the pair is asserted together.
    """
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("ok"), myip="::ffff:192.0.2.1",
    )
    assert response.text == "good ::ffff:192.0.2.1"
    assert [call.rtype for call in creates()] == ["AAAA"]


async def test_a_nat64_literal_is_renormalised_and_stays_aaaa(client, world) -> None:
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("ok"), myip="64:ff9b::192.0.2.33",
    )
    assert response.text == "good 64:ff9b::c000:221"
    assert [call.rtype for call in creates()] == ["AAAA"]
    assert [call.ip for call in creates()] == ["64:ff9b::c000:221"]


async def test_a_scoped_ipv6_literal_round_trips(client, world) -> None:
    """``fe80::1%eth0`` — a v6-only parse path with no IPv4 analogue.

    A stricter validator answers ``911`` and no other case notices.
    """
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("ok"), myip="fe80::1%eth0",
    )
    assert response.text == "good fe80::1%eth0"
    assert [call.rtype for call in creates()] == ["AAAA"]


async def test_the_family_comes_from_the_client_address_when_myip_is_absent(
    client, world
) -> None:
    """The shape of most real traffic — plan §3.3.1, 305 of 448 events v6.

    There is no ``myip`` to read a family off, so the fallback has to
    pick ``AAAA`` from the client address alone.
    """
    response = await get(
        client, "/nic/update", headers={**alice(world), **xff("2001:db8:113::77")},
        hostname=world.name("ok"),
    )
    assert response.text == "good 2001:db8:113::77"
    assert [call.rtype for call in creates()] == ["AAAA"]


async def test_a_mixed_backend_set_carries_the_family_to_every_backend(
    client, world
) -> None:
    """Both stub calls carry ``AAAA``, including the one answering ``dnserr``.

    ``update-aggregate-any-good-wins-ipv6``'s ``effects:`` block says
    exactly this and no runner asserts it.
    """
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("mixed"), myip="2001:db8:113::20",
    )
    assert response.text == "good 2001:db8:113::20"
    assert [call.rtype for call in creates()] == ["AAAA", "AAAA"]
    assert [call.result for call in creates()] == ["dnserr", "good"]


async def test_delete_without_myip_passes_rtype_none_for_both_families(
    client, world
) -> None:
    """``rtype=None`` is a **third state**, not a missing one.

    ``BaseProvider.rtypes_for(None)`` walks both families; a delete that
    defaulted to ``A`` would leave every ``AAAA`` in place, and the wire
    would say ``good`` either way (divergence 1 — delete replies carry
    no address at all).
    """
    response = await get(
        client, "/nic/delete", headers=alice(world), hostname=world.name("ok")
    )
    assert response.text == "good"
    assert [call.rtype for call in deletes()] == [None]


async def test_delete_with_an_ipv4_myip_deletes_a_only(client, world) -> None:
    response = await get(
        client, "/nic/delete", headers=alice(world),
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    assert response.text == "good"
    assert [call.rtype for call in deletes()] == ["A"]


async def test_delete_with_an_ipv6_myip_deletes_aaaa_only(client, world) -> None:
    response = await get(
        client, "/nic/delete", headers=alice(world),
        hostname=world.name("ok"), myip="2001:db8::1",
    )
    assert response.text == "good"
    assert [call.rtype for call in deletes()] == ["AAAA"]


async def test_delete_with_an_ipv4_mapped_ipv6_deletes_aaaa_only(
    client, world
) -> None:
    """The case whose own ``note:`` says it cannot fail on the wire.

    ``delete-myip-ipv4-mapped-ipv6-deletes-aaaa-only``: #29 replayed
    every legacy-selected case with every IPv4 literal swapped for an
    IPv6 one and **all 36 executable delete cases answered
    byte-identically**, while the DNS operation underneath changed
    ``rtype`` from ``A`` to ``AAAA``. This is the assertion that case
    was written for.
    """
    response = await get(
        client, "/nic/delete", headers=alice(world),
        hostname=world.name("ok"), myip="::ffff:192.0.2.1",
    )
    assert response.text == "good"
    assert [call.rtype for call in deletes()] == ["AAAA"]


async def test_an_empty_myip_on_delete_deletes_both_families(client, world) -> None:
    response = await get(
        client, "/nic/delete", headers=alice(world),
        hostname=world.name("ok"), myip="",
    )
    assert response.text == "good"
    assert [call.rtype for call in deletes()] == [None]


async def test_provider_calls_do_not_run_on_the_event_loop(
    client, world
) -> None:
    """The blocking-call rule, asserted rather than trusted.

    boto3 has no async API, and #15's adapters call boto3, httpx and
    dnspython synchronously — its own module docstring says ``/nic/*``
    "must therefore call ``createrecords`` / ``deleterecords`` through
    ``anyio.to_thread.run_sync`` … a blocking boto3 call on the event
    loop stalls every other request in the process, **and it will not
    show up in a unit test**."

    It shows up in this one. ``await``-ing the provider instead of
    handing it to a thread is a one-word edit that changes no response
    body, no status and no ``effects:`` — the only observable is which
    thread the call ran on, so that is what is observed.
    """
    import threading

    loop_thread = threading.get_ident()
    await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("mixed"), myip="203.0.113.10",
    )
    assert creates(), "no provider call — the assertion below would be vacuous"
    assert all(call.thread_id != loop_thread for call in creates()), (
        f"a provider call ran on the event loop thread ({loop_thread})"
    )

    CALLS.clear()
    await get(
        client, "/nic/delete", headers=alice(world), hostname=world.name("ok")
    )
    assert deletes()
    assert all(call.thread_id != loop_thread for call in deletes())


async def test_password_hashing_does_not_run_on_the_event_loop(
    client, world, monkeypatch
) -> None:
    """The same rule for argon2id and bcrypt.

    argon2id at atrium's parameters is tens of milliseconds and bcrypt
    at cost 12 is hundreds, *per verify*. A fleet checking in on a
    five-minute timer makes that the busiest CPU-bound thing the api
    process does; on the loop it would serialise every other request
    behind it, and — like the provider calls — no response body would
    change.
    """
    import threading

    from atrium_ddns import auth_device

    seen: list[int] = []
    real = auth_device._verify_sync

    def spy(stored: str, presented: str):
        seen.append(threading.get_ident())
        return real(stored, presented)

    monkeypatch.setattr(auth_device, "_verify_sync", spy)

    loop_thread = threading.get_ident()
    await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    assert seen, "no verify happened — the assertion below would be vacuous"
    assert all(thread != loop_thread for thread in seen), seen


async def test_the_record_type_guard_is_not_vacuous(
    client, world, monkeypatch
) -> None:
    """#29's mutation, re-applied here — and this file goes red for it.

    ``getiptype`` answering ``A`` for every address ran the whole
    131-case wire table at **121 executed, 0 failed**. Against the
    assertions above it must fail, or they are not assertions about the
    record type at all.

    Both endpoints, because they reach the family through different
    code: update reads it once per request from the validated address,
    delete reads it inside its own ``if myip:`` branch that update's
    path never executes.
    """
    monkeypatch.setattr(
        BaseProvider, "getiptype", staticmethod(lambda ip: "A"), raising=True
    )

    await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("ok"), myip="2001:db8:113::20",
    )
    mutated_update = [call.rtype for call in creates()]
    CALLS.clear()

    await get(
        client, "/nic/delete", headers=alice(world),
        hostname=world.name("ok"), myip="2001:db8::1",
    )
    mutated_delete = [call.rtype for call in deletes()]

    # Under the mutation both read "A"; unmutated they read "AAAA".
    # Reported as the two readings rather than as a bare `!=`, so a
    # failure here names what changed.
    assert mutated_update == ["A"], mutated_update
    assert mutated_delete == ["A"], mutated_delete

    # And the wire is unmoved, which is the whole point: the body is
    # byte-identical to the unmutated one.
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("ok"), myip="2001:db8:113::20",
    )
    assert response.text == "good 2001:db8:113::20"


# ===================================================================== #
# 2. Persisted columns — the other half of `effects:`
# ===================================================================== #


async def test_a_good_ipv4_update_moves_last_ip_v4_and_not_v6(
    client, world
) -> None:
    hostname_id = world.hostnames["zz-ok"]
    before = await hostname_row(hostname_id)
    assert before.last_ip_v6 is None

    await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("zz-ok"), myip="203.0.113.10",
    )

    after = await hostname_row(hostname_id)
    assert after.last_ip_v4 == "203.0.113.10"
    assert after.last_ip_v6 is None
    assert after.last_updated_at is not None


async def test_a_good_ipv6_update_moves_last_ip_v6_and_not_v4(
    client, world
) -> None:
    """The mirror, and the one the v2 table did not have.

    A host writing **both** columns on every update passed the frozen
    table until #29 added ``update-ipv4-persists-last-ip-v4-not-v6``,
    and neither direction is visible on the wire. Asserting one without
    the other is the same hole one column along.
    """
    hostname_id = world.hostnames["ok"]
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            sa.update(Hostname)
            .where(Hostname.id == hostname_id)
            .values(last_ip_v4=None, last_ip_v6=None, last_updated_at=None)
        )
        await s.commit()

    await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("ok"), myip="2001:db8::2",
    )

    after = await hostname_row(hostname_id)
    assert after.last_ip_v6 == "2001:db8::2"
    assert after.last_ip_v4 is None


async def test_a_nochg_persists_nothing(client, world) -> None:
    hostname_id = world.hostnames["nochg"]
    before = await hostname_row(hostname_id)

    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("nochg"), myip="203.0.113.10",
    )
    assert response.text == "nochg 203.0.113.10"

    after = await hostname_row(hostname_id)
    assert (after.last_ip_v4, after.last_ip_v6, after.last_updated_at) == (
        before.last_ip_v4,
        before.last_ip_v6,
        before.last_updated_at,
    )


async def test_delete_persists_nothing(client, world) -> None:
    hostname_id = world.hostnames["zz-ok"]
    await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("zz-ok"), myip="203.0.113.10",
    )
    before = await hostname_row(hostname_id)
    assert before.last_ip_v4 == "203.0.113.10"

    response = await get(
        client, "/nic/delete", headers=alice(world), hostname=world.name("zz-ok")
    )
    assert response.text == "good"

    after = await hostname_row(hostname_id)
    assert (after.last_ip_v4, after.last_ip_v6, after.last_updated_at) == (
        before.last_ip_v4,
        before.last_ip_v6,
        before.last_updated_at,
    )


async def test_the_default_ttl_is_60(client, world) -> None:
    await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    assert [call.ttl for call in creates()] == [60]


async def test_a_backend_config_ttl_overrides_the_default(client, world) -> None:
    factory = get_session_factory()
    async with factory() as s:
        row = (
            await s.execute(
                sa.select(DomainBackend)
                .join(Domain, Domain.id == DomainBackend.domain_id)
                .where(Domain.name == world.name("ok"))
            )
        ).scalar_one()
        original = dict(row.config or {})
        row.config = {**original, "ttl": 300}
        await s.commit()
    try:
        await get(
            client, "/nic/update", headers=alice(world),
            hostname=world.name("ok"), myip="203.0.113.10",
        )
        assert [call.ttl for call in creates()] == [300]
    finally:
        async with factory() as s:
            row = (
                await s.execute(
                    sa.select(DomainBackend)
                    .join(Domain, Domain.id == DomainBackend.domain_id)
                    .where(Domain.name == world.name("ok"))
                )
            ).scalar_one()
            row.config = original
            await s.commit()


# ===================================================================== #
# 3. Ownership — device, then tenant, and neither is redundant
# ===================================================================== #


async def test_another_tenants_hostname_is_nohost(client, world) -> None:
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("carols"), myip="203.0.113.10",
    )
    assert response.text == "nohost 203.0.113.10"
    assert CALLS == []


async def test_the_same_tenants_other_device_is_also_nohost(client, world) -> None:
    """Plan §3.3, and the invariant the importer has to preserve.

    ``otherdevice.…`` belongs to alice and is assigned to her *spare*
    device. Her main router asks for it and must be told ``nohost`` —
    which is exactly the failure mode §3.3 warns about if a migrated
    user's hostnames are split across devices: a 200, a ``nohost``, and
    no error anywhere.
    """
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("otherdevice"), myip="203.0.113.10",
    )
    assert response.text == "nohost 203.0.113.10"
    assert CALLS == []

    # …and the spare device resolves it, so the `nohost` above is about
    # ownership and not about the row being unreachable.
    spare = basic(world.device_usernames["alice-spare"], CAROL_PASSWORD)
    response = await get(
        client, "/nic/update", headers=spare,
        hostname=world.name("otherdevice"), myip="203.0.113.10",
    )
    assert response.text == "good 203.0.113.10"


async def test_the_tenant_scope_bites_independently_of_the_device_filter(
    world,
) -> None:
    """The scope is load-bearing on its own, not a duplicate of ``device_id``.

    ``load_plans`` is called directly with a :class:`DeviceAuth` whose
    device is alice's — so the ``device_id`` filter matches — but whose
    ``user_id`` is carol's. The hostname must not resolve.

    Asserted behaviourally rather than by reading the SQL, because #17
    measured that reading the SQL cannot answer it: ``sa.true()``
    survives compilation only as a lone predicate, and once ANDed with
    anything else SQLAlchemy elides it, so a composed statement carries
    no trace of which scope produced it.
    """
    from atrium_ddns.auth_device import DeviceAuth

    factory = get_session_factory()
    async with factory() as s:
        device = (
            await s.execute(
                sa.select(Device).where(Device.id == world.alice_device)
            )
        ).scalar_one()

        correct = DeviceAuth(
            device=device, user_id=world.alice_id, user_email="a@x"
        )
        plans = await router_nic.load_plans(s, correct, [world.name("ok")])
        assert plans[0].resolved, "the control did not resolve — fixture problem"

        wrong_tenant = DeviceAuth(
            device=device, user_id=world.carol_id, user_email="c@x"
        )
        plans = await router_nic.load_plans(s, wrong_tenant, [world.name("ok")])
        assert not plans[0].resolved


def test_no_hand_written_user_id_filter_reaches_the_database() -> None:
    """Structural: the scope is the only thing that writes tenancy.

    ``scope.py`` says it in its own docstring — "**Every host query goes
    through this module. None of them write a ``user_id`` filter by
    hand** — the one that forgets is the leak, and it will be the one
    written against a table added after this file was reviewed."

    A behavioural test cannot see a *new* query somebody adds next
    month; this can. It fails the moment a comparison against a
    ``user_id`` column appears in either module.
    """
    offenders: dict[str, list[str]] = {}
    for module in (router_nic, __import__("atrium_ddns.auth_device", fromlist=["x"])):
        source = Path(module.__file__).read_text(encoding="utf-8")
        # Strip docstrings and comments: this file's own prose names
        # `user_id` repeatedly and a text search that could not tell
        # code from commentary would be unusable.
        code = re.sub(r"#.*", "", source)
        code = re.sub(r'"""(?:.|\n)*?"""', "", code)
        hits = [
            line.strip()
            for line in code.splitlines()
            if re.search(r"\.user_id\s*==|user_id\s*==\s*\w", line)
        ]
        if hits:
            offenders[module.__name__] = hits
    assert offenders == {}, (
        f"hand-written tenancy filters: {offenders}. Use DdnsScope — "
        "scope.select / scope.apply / scope.get / scope.predicate."
    )


# ===================================================================== #
# 4. Method handling — HEAD, and the non-vacuous half of the POST cases
# ===================================================================== #


@pytest.mark.parametrize("path", ["/nic/update", "/nic/delete"])
async def test_head_is_refused_on_the_writing_endpoints(
    mounted_client, path
) -> None:
    """405, ``application/json``, empty body — **behind the mount**.

    Deliberately not against the bare ``client``. That version was
    written first and it was a probe that could not fail: Starlette
    answers 405 for a ``HEAD`` on a ``GET``-only route all by itself,
    so deleting both hand-written ``HEAD`` routes from ``router_nic``
    left this file at **0 red**. Behind the catch-all — which is how
    the code actually runs — the same deletion answers 200, and this
    goes red.
    """
    response = await mounted_client.head(
        path, params={"hostname": "x.example.com"}
    )
    assert response.status_code == 405
    assert response.headers["content-type"] == "application/json"
    assert response.content == b""


async def test_the_head_refusal_does_not_fall_out_of_the_framework(
    mounted_app, mounted_app_without_head_routes, catch_all_only_app
) -> None:
    """Four readings, taken here — and one of them corrects the record.

    ==============================================  ====  =====
    stack                                           HEAD  POST
    ==============================================  ====  =====
    bare FastAPI, ``GET``-only route                405   405
    the mount alone, no ``/nic`` route              404   405
    ``GET``-only route **behind the mount**         404   405
    this repository's router behind the mount       405   405
    ==============================================  ====  =====

    **Row three reads 404, not 200.** Plan §1's table, the frozen
    table's ``known_gaps`` and #11's write-up all record **200** for it
    — "the catch-all serves it". Measured against
    ``app.static.SPAStaticFiles`` as shipped in ``atrium:0.28``, it
    answers 404: the SPA fallback to ``index.html`` is guarded by
    ``scope["method"] == "GET"``, so a ``HEAD`` miss re-raises the 404
    instead of being served the shell. Re-measured with a plain
    ``StaticFiles(html=True)`` too — also 404.

    The decision is unaffected and the code is unaffected: 404 is no
    more the frozen 405 than 200 was, and the two
    ``-head-is-refused-by-the-host`` cases fail without a hand-written
    handler either way. What changes is the *reason*, and it is worth
    correcting because the recorded one — "the catch-all serves it" —
    reads as though making the mount decline non-GET would restore the
    405. It already declines non-GET. A ``Mount`` is a **full** route
    match whatever it then answers, so the ``GET``-only route's own 405
    never runs; the only thing that produces the frozen status is the
    hand-written handler this file asserts.

    **Corrected by #18: the reason this test gave for not editing the
    frozen table was itself wrong.** It read "``known_gaps`` is inside
    the digest and rewriting it is a version bump".
    ``_content_digest`` in ``tests/compat/test_protocol.py`` hashes the
    parsed ``cases`` and ``deleted_cases`` and nothing else, so
    ``known_gaps`` is outside it — and a version bump over unmoved data
    is refused by ``test_the_version_advances_exactly_when_the_data_does``
    in as many words. The entry is corrected at v3, and this test is
    what makes the corrected reading executable rather than prose.

    Row two is what this repository answered before this issue, and it
    is why the two frozen ``-post-is-405-on-the-host`` cases passed
    against a host with no ``/nic`` routes at all: the POST column is
    405 on every row, so nothing on the wire distinguishes "no route"
    from "GET-only route".
    """
    bare = FastAPI()

    @bare.get("/nic/update")
    async def _update() -> str:  # pragma: no cover — status only
        return "good"

    readings: dict[str, tuple[int, int]] = {}
    for label, application in (
        ("bare", bare),
        ("mount-only", catch_all_only_app),
        ("get-route-behind-mount", mounted_app_without_head_routes),
        ("this-router-behind-mount", mounted_app),
    ):
        async with _client(application) as c:
            head = (await c.head("/nic/update")).status_code
            post = (await c.post("/nic/update")).status_code
        readings[label] = (head, post)

    assert readings == {
        "bare": (405, 405),
        "mount-only": (404, 405),
        "get-route-behind-mount": (404, 405),
        "this-router-behind-mount": (405, 405),
    }, readings


async def test_head_is_preserved_on_checkip(mounted_client) -> None:
    """Read-only, so ``HEAD`` is safe and is kept — plan §1's split.

    ``Content-Length`` is the full wrapper because a body is generated
    and then suppressed, which is what makes this a useful liveness
    probe rather than a special case.

    Behind the mount for the same reason as its siblings — except that
    here the *content type* is what carries the meaning. Drop
    ``"HEAD"`` from the ``checkip`` route and the catch-all serves the
    SPA's ``index.html``: still 200, still ``text/html``, and the
    ``Content-Length`` assertion below is the only thing that tells the
    two apart.
    """
    response = await mounted_client.head(
        "/nic/checkip", headers=xff("203.0.113.10")
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.content == b""
    assert int(response.headers["content-length"]) == len(
        router_nic.CHECKIP_HTML.format(ip="203.0.113.10")
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/nic/checkip", "/nic/update", "/nic/delete"])
async def test_non_get_methods_are_refused(client, method, path) -> None:
    """Divergence 7, asserted where it cannot pass vacuously.

    The two frozen ``-post-is-405-on-the-host`` cases pass against a
    host with **no** ``/nic`` routes at all, because a non-``GET`` on an
    unmatched path meets atrium's SPA catch-all, which also declares
    only ``GET``. They assert "POST is refused", never "the endpoint
    exists". This app has no catch-all, so the refusal here is the
    route's own.
    """
    response = await client.request(method, path)
    assert response.status_code == 405


# ===================================================================== #
# 5. Authentication, on the wire
# ===================================================================== #


@pytest.mark.parametrize("path", ["/nic/update", "/nic/delete"])
async def test_no_credentials_is_badauth_with_http_200(client, path, world) -> None:
    """A 401 would break every router in the field — divergence 4.

    Clients parse the body, not the status.
    """
    response = await get(client, path, hostname=world.name("ok"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.text == "badauth"


async def test_a_bcrypt_row_verifies_and_is_rehashed_to_argon2id(
    client, world
) -> None:
    """The fleet migrates itself as routers check in — plan §3.2.

    ``alice`` is seeded bcrypt. One successful call must leave her row
    argon2id, and the *next* call must still succeed against the new
    hash — the second half matters, because a re-hash that wrote a hash
    of the wrong thing would look identical in the column and lock the
    device out on its next poll.

    The bcrypt hash is written here rather than relied on from the
    fixture. It was relied on at first and the test failed: the
    re-hash had **already** happened, because any earlier test in this
    file that authenticates as alice performs it. A precondition that
    another test can satisfy is a precondition that will one day be
    satisfied for the wrong reason — the version that read the seeded
    value would have passed on a file where the upgrade never fired,
    as long as some other test fired it first.
    """
    from pwdlib.hashers.bcrypt import BcryptHasher

    factory = get_session_factory()
    async with factory() as s:
        original = (
            await s.execute(
                sa.select(Device.password_hash).where(
                    Device.id == world.alice_device
                )
            )
        ).scalar_one()
        await s.execute(
            sa.update(Device)
            .where(Device.id == world.alice_device)
            .values(password_hash=BcryptHasher().hash(ALICE_PASSWORD))
        )
        await s.commit()
    try:
        async with factory() as s:
            seeded = (
                await s.execute(
                    sa.select(Device.password_hash).where(
                        Device.id == world.alice_device
                    )
                )
            ).scalar_one()
        assert seeded.startswith("$2"), seeded[:4]

        first = await get(
            client, "/nic/update", headers=alice(world),
            hostname=world.name("ok"), myip="203.0.113.10",
        )
        assert first.text == "good 203.0.113.10"

        async with factory() as s:
            upgraded = (
                await s.execute(
                    sa.select(Device.password_hash).where(
                        Device.id == world.alice_device
                    )
                )
            ).scalar_one()
        assert upgraded.startswith("$argon2"), upgraded[:16]

        second = await get(
            client, "/nic/update", headers=alice(world),
            hostname=world.name("ok"), myip="203.0.113.10",
        )
        assert second.text == "good 203.0.113.10"

        # And it does not churn: an argon2id row is left alone.
        async with factory() as s:
            settled = (
                await s.execute(
                    sa.select(Device.password_hash).where(
                        Device.id == world.alice_device
                    )
                )
            ).scalar_one()
        assert settled == upgraded
    finally:
        async with factory() as s:
            await s.execute(
                sa.update(Device)
                .where(Device.id == world.alice_device)
                .values(password_hash=original)
            )
            await s.commit()


async def test_an_inactive_owner_is_badauth_with_correct_credentials(
    client, world
) -> None:
    """``dave`` holds a working device secret and a deactivated account."""
    response = await get(
        client, "/nic/update",
        headers=basic(world.device_usernames["dave"], DAVE_PASSWORD),
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    assert response.text == "badauth"


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer atr_pat_something",
        "Basic",
        "Basic !!!not-base64!!!",
        "Basic " + base64.b64encode(b"no-colon-here").decode(),
    ],
    ids=[
        "absent", "empty", "bearer", "basic-no-token", "basic-bad-base64",
        "basic-no-colon",
    ],
)
async def test_malformed_authorization_headers_are_badauth_not_500(
    client, world, header
) -> None:
    headers = {} if header is None else {"Authorization": header}
    response = await get(
        client, "/nic/update", headers=headers,
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    assert response.status_code == 200
    assert response.text == "badauth"


async def test_a_password_longer_than_bcrypt_allows_is_badauth_not_500(
    client, world
) -> None:
    """bcrypt >= 4.1 raises on a password over 72 bytes rather than truncating.

    Uncaught, that is a 500 on a path whose entire contract is "answer
    200 with a status token", and it is reachable by anyone who can
    send a long string. ``alice`` is the bcrypt row, so this exercises
    the branch that has a hasher to refuse it.
    """
    response = await get(
        client, "/nic/update",
        headers=basic(world.device_usernames["alice"], "x" * 200),
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    assert response.status_code == 200
    assert response.text == "badauth"


async def test_an_unrecognisable_stored_hash_is_badauth_not_500(
    client, world
) -> None:
    factory = get_session_factory()
    async with factory() as s:
        original = (
            await s.execute(
                sa.select(Device.password_hash).where(
                    Device.id == world.carol_device
                )
            )
        ).scalar_one()
        await s.execute(
            sa.update(Device)
            .where(Device.id == world.carol_device)
            .values(password_hash="not-a-hash-at-all")
        )
        await s.commit()
    try:
        response = await get(
            client, "/nic/update",
            headers=basic(world.device_usernames["carol"], CAROL_PASSWORD),
            hostname=world.name("carols"), myip="203.0.113.10",
        )
        assert response.status_code == 200
        assert response.text == "badauth"
    finally:
        async with factory() as s:
            await s.execute(
                sa.update(Device)
                .where(Device.id == world.carol_device)
                .values(password_hash=original)
            )
            await s.commit()


async def test_nic_needs_no_cookie_and_no_csrf_token(app, world) -> None:
    """The AC, asserted rather than assumed.

    A router sends HTTP Basic and nothing else. Atrium ships no CSRF
    middleware at all (measured: ``git grep -il csrf`` over
    ``atrium@v0.28.0``'s backend returns nothing), so "CSRF-exempt" is
    structural here — but a session cookie arriving alongside Basic
    must also not change the answer, which is the half a future CSRF or
    cookie-preference change would break.
    """
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://nic.test",
        # On the client rather than per request: httpx deprecates the
        # per-request form because cookie persistence is ambiguous, and
        # a DeprecationWarning on a passing assertion is noise that
        # eventually gets silenced along with the next real one.
        cookies={"atrium_auth": "not-a-real-session", "csrftoken": "nonsense"},
    ) as cookied:
        response = await cookied.get(
            "/nic/update",
            params={"hostname": world.name("ok"), "myip": "203.0.113.10"},
            headers={**alice(world), **xff("203.0.113.10")},
        )
    assert response.status_code == 200
    assert response.text == "good 203.0.113.10"


# ===================================================================== #
# 6. Rate limiting
# ===================================================================== #


async def _set_limit(device_id: int, limit: int | None) -> None:
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            sa.update(Device)
            .where(Device.id == device_id)
            .values(rate_limit_per_minute=limit)
        )
        await s.execute(
            sa.text("DELETE FROM ddns_rate_limit_event WHERE device_id = :d"),
            {"d": device_id},
        )
        await s.commit()


async def test_the_limit_answers_abuse_and_is_per_device(client, world) -> None:
    await _set_limit(world.alice_device, 2)
    try:
        bodies = [
            (
                await get(
                    client, "/nic/update", headers=alice(world),
                    hostname=world.name("ok"), myip="203.0.113.10",
                )
            ).text
            for _ in range(4)
        ]
        assert bodies == [
            "good 203.0.113.10",
            "good 203.0.113.10",
            "abuse",
            "abuse",
        ]

        # …and carol's device is untouched by alice's exhaustion.
        response = await get(
            client, "/nic/update",
            headers=basic(world.device_usernames["carol"], CAROL_PASSWORD),
            hostname=world.name("carols"), myip="203.0.113.10",
        )
        assert response.text == "good 203.0.113.10"
    finally:
        await _set_limit(world.alice_device, BIG_LIMIT)


async def test_a_refused_request_is_not_counted_so_the_window_drains(
    client, world
) -> None:
    """The rows **are** the window, so recording refusals self-extends it.

    A client at ten times the limit would otherwise stay refused
    indefinitely rather than for one minute. Measured as a row count,
    not inferred: two admitted requests and two refusals leave two rows.
    """
    await _set_limit(world.alice_device, 2)
    try:
        for _ in range(4):
            await get(
                client, "/nic/update", headers=alice(world),
                hostname=world.name("ok"), myip="203.0.113.10",
            )
        factory = get_session_factory()
        async with factory() as s:
            counted = (
                await s.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM ddns_rate_limit_event "
                        "WHERE device_id = :d"
                    ),
                    {"d": world.alice_device},
                )
            ).scalar_one()
        assert counted == 2
    finally:
        await _set_limit(world.alice_device, BIG_LIMIT)


async def test_a_zero_limit_is_not_the_same_as_an_absent_one(
    client, world
) -> None:
    """``NULL`` means *inherit*; ``0`` means *may never call*.

    ``device.rate_limit_per_minute or default`` reads ``0`` as absent
    and hands an explicitly muted device the installation default —
    which is the whole reason ``effective_rate_limit`` exists rather
    than the expression being inlined.
    """
    await _set_limit(world.alice_device, 0)
    try:
        response = await get(
            client, "/nic/update", headers=alice(world),
            hostname=world.name("ok"), myip="203.0.113.10",
        )
        assert response.text == "abuse"
    finally:
        await _set_limit(world.alice_device, BIG_LIMIT)

    await _set_limit(world.alice_device, None)
    try:
        # NULL inherits the namespace default (30/min), which one
        # request cannot exhaust.
        response = await get(
            client, "/nic/update", headers=alice(world),
            hostname=world.name("ok"), myip="203.0.113.10",
        )
        assert response.text == "good 203.0.113.10"
    finally:
        await _set_limit(world.alice_device, BIG_LIMIT)


async def test_the_api_can_tighten_a_limit_and_the_wire_honours_it(
    client, world
) -> None:
    """#73's ``PATCH /devices/{id}``, joined to the wire in one process.

    Two facts are already covered separately: ``test_router_tenant``
    proves the PATCH stores the value, and
    ``test_the_limit_answers_abuse_and_is_per_device`` proves
    ``/nic/update`` honours the column. **Neither sees the seam.** A
    PATCH that wrote to a different column, or a cache between the row
    and the limiter, would leave both green — and the whole point of the
    route is that an operator can slow an abusive device *now*, from a
    browser, without rotating its credential.

    So the limit is changed the way the settings surface changes it —
    through the API, as the device's owner — and the very next request
    the device makes is the assertion. The credential used on the wire
    is the one the device already had, which is the other half of #73:
    the tightening must not have broken the device it was aimed at.
    """
    from app.auth.principal import Principal
    from app.auth.rbac import current_principal

    from atrium_ddns.router import DEVICE_MANAGE_PERMISSION
    from atrium_ddns.router import router as tenant_router

    factory = get_session_factory()
    async with factory() as session:
        owner = await session.get(User, world.alice_id)
        assert owner is not None
        # Detach fully-loaded rather than holding a session open for the
        # length of the test: the route builds its own.
        session.expunge(owner)

    tenant_app = FastAPI()
    tenant_app.include_router(tenant_router)

    async def _principal() -> Principal:
        return Principal(
            user=owner,
            permissions=frozenset({DEVICE_MANAGE_PERMISSION}),
            auth_method="password",
            token_id=None,
            auth_session_id=None,
        )

    tenant_app.dependency_overrides[current_principal] = _principal

    await _set_limit(world.alice_device, BIG_LIMIT)
    try:
        # Baseline: the device is well inside its limit and says so.
        first = await get(
            client, "/nic/update", headers=alice(world),
            hostname=world.name("ok"), myip="203.0.113.10",
        )
        assert first.text == "good 203.0.113.10"

        async with httpx.AsyncClient(
            transport=ASGITransport(app=tenant_app),
            base_url="http://tenant.test",
        ) as api:
            patched = await api.patch(
                f"/api/atrium_ddns/devices/{world.alice_device}",
                json={"rate_limit_per_minute": 0},
            )
        assert patched.status_code == 200, patched.text
        assert patched.json()["effective_rate_limit_per_minute"] == 0

        # The next call on the wire, with the *same* credential.
        refused = await get(
            client, "/nic/update", headers=alice(world),
            hostname=world.name("ok"), myip="203.0.113.10",
        )
        assert refused.text == "abuse", (
            "the limit written through the API did not reach the limiter"
        )

        # And back up again, through the same route — so this is a
        # setting and not a one-way door.
        async with httpx.AsyncClient(
            transport=ASGITransport(app=tenant_app),
            base_url="http://tenant.test",
        ) as api:
            restored = await api.patch(
                f"/api/atrium_ddns/devices/{world.alice_device}",
                json={"rate_limit_per_minute": BIG_LIMIT},
            )
        assert restored.status_code == 200, restored.text
        allowed = await get(
            client, "/nic/update", headers=alice(world),
            hostname=world.name("ok"), myip="203.0.113.10",
        )
        assert allowed.text == "good 203.0.113.10", (
            "the device stayed refused after its limit was raised — the "
            "credential or the window did not survive the change"
        )
    finally:
        await _set_limit(world.alice_device, BIG_LIMIT)


async def test_abuse_precedes_the_hostname_parameter(client, world) -> None:
    """``update-abuse-precedes-911`` — one of the three the runner skips.

    It carries ``rate_limited: true``, which is fixture state and not a
    request, so the wire runner skips it with the precondition named
    and it is "measured out of band". This is out of band.
    """
    await _set_limit(world.alice_device, 0)
    try:
        response = await get(
            client, "/nic/update", headers=alice(world), myip="203.0.113.10"
        )
        assert response.text == "abuse"
        response = await get(client, "/nic/delete", headers=alice(world))
        assert response.text == "abuse"
    finally:
        await _set_limit(world.alice_device, BIG_LIMIT)


async def test_badauth_precedes_abuse(client, world) -> None:
    """A muted device with wrong credentials still answers ``badauth``.

    The limiter is per-device and there is no device until the
    credential verifies, so the order is forced — but stating it means
    a future refactor that resolves the device before verifying it
    fails here rather than leaking "this username exists, and it is
    over its limit".
    """
    await _set_limit(world.alice_device, 0)
    try:
        response = await get(
            client, "/nic/update",
            headers=basic(world.device_usernames["alice"], "wrong"),
            hostname=world.name("ok"), myip="203.0.113.10",
        )
        assert response.text == "badauth"
    finally:
        await _set_limit(world.alice_device, BIG_LIMIT)


# ===================================================================== #
# 7. The event log — the table that had no writer
# ===================================================================== #


async def test_one_event_row_per_backend_attempt_carrying_its_own_status(
    client, world
) -> None:
    """``event-one-row-per-backend-attempt`` + ``…-not-the-aggregate``.

    The wire shows one line per *hostname*; the log holds one row per
    *backend attempt*, and no row carries the aggregate. Anyone sizing
    the table from the response body is out by the backend count, and
    plan §3.4's retention estimate depends on this number.
    """
    await clear_events()
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("firsterr"), myip="203.0.113.10",
    )
    assert response.text == "911 203.0.113.10"

    rows = await events_for(world.alice_id)
    assert [row.response_code for row in rows] == ["nochg", "911", "dnserr"]
    assert {row.event_type for row in rows} == {"update"}
    assert all(row.hostname == world.name("firsterr") for row in rows)
    assert all(row.device_id == world.alice_device for row in rows)
    assert all(row.ip == "203.0.113.10" for row in rows)


async def test_a_refusal_decided_before_any_backend_still_writes_a_row(
    client, world
) -> None:
    """``event-911-and-notfqdn-write-no-row``, disposition ``fix``.

    The legacy service wrote nothing for ``911``, ``notfqdn`` or
    ``badauth``, so a router sending a malformed hostname was refused on
    every attempt and appeared in the log as **complete silence** —
    indistinguishable from a router that had stopped calling. That is
    the support case the old service could not answer.
    """
    await clear_events()
    assert (
        await get(client, "/nic/update", headers=alice(world), myip="203.0.113.10")
    ).text == "911"
    assert (
        await get(
            client, "/nic/update", headers=alice(world),
            hostname="_dmarc.example.com", myip="203.0.113.10",
        )
    ).text == "notfqdn"
    assert (
        await get(
            client, "/nic/update", headers=alice(world),
            hostname=world.name("nobackend"), myip="203.0.113.10",
        )
    ).text == "911 203.0.113.10"

    rows = await events_for(world.alice_id)
    assert [row.response_code for row in rows] == ["911", "notfqdn", "911"]


async def _badauth_rows(ip: str) -> list[Any]:
    """Every ``auth`` row this worker's address produced, newest first."""
    factory = get_session_factory()
    async with factory() as s:
        return list(
            (
                await s.execute(
                    sa.select(DnsEvent)
                    .where(DnsEvent.event_type == "auth")
                    .where(DnsEvent.client_ip == ip)
                    .order_by(DnsEvent.id.desc())
                )
            )
            .scalars()
            .all()
        )


async def test_badauth_is_recorded_and_attributed_to_the_username_it_tried(
    client, world
) -> None:
    """``event-badauth-writes-no-row``, disposition ``change`` — and #64.

    The seam's own note: "a device secret being brute-forced currently
    leaves no trace in either store". It leaves one now, under
    ``event_type 'auth'``.

    **What #64 changed is whose row it is.** The row used to carry
    ``user_id NULL`` for every refusal, including this one — a wrong
    password against a device that exists. A tenant filtering their log
    for ``badauth`` therefore got zero however many times their router
    had failed, and zero is also what a healthy account looks like.
    ``authenticate_device`` had already resolved this username to a
    device before rejecting the password; it now returns that
    resolution and the row carries the owner.

    Asserted as a **pair with the next test**, because "the row has a
    user_id" on its own is satisfiable by attributing every refusal to
    somebody, which would be far worse than the defect. The unknown
    username must stay NULL.

    **Counted by this worker's own address, not globally.** The first
    version of this test read ``SELECT COUNT(*) FROM ddns_event WHERE
    event_type='auth'`` before and after, and asserted the difference
    was one. That count spans ten workers sharing one table, and
    ``test_router_events.py`` writes and then deletes a row of exactly
    this shape. Observed once in 65 full-suite runs as
    ``assert 909 == 909 + 1``: the sibling module's teardown removed one
    row inside the window while this request added one, and the two
    cancelled.
    """
    await clear_events()
    before = len(await _badauth_rows(BADAUTH_IP))

    response = await get(
        client, "/nic/update",
        headers={**basic(world.device_usernames["alice"], "wrong"), **xff(BADAUTH_IP)},
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    # The wire is unchanged, and that is half the point: attribution
    # must not become a way to tell a valid username from an invalid one
    # by reading the response.
    assert response.text == "badauth"

    rows = await _badauth_rows(BADAUTH_IP)
    assert len(rows) == before + 1
    row = rows[0]
    assert row.response_code == "badauth"
    assert row.client_ip == BADAUTH_IP
    # The tenant, by reference and by value. Both halves, because the
    # id is what the filter indexes and the name is what survives the
    # device being deleted — which is exactly when this log is read.
    assert row.user_id == world.alice_id
    assert row.device_id == world.alice_device
    assert row.user_email is not None
    assert row.device_name is not None
    # `backend_type` stays NULL: decided before any provider was
    # reached. `event-backend-type-is-null-for-outcomes-decided-before-
    # any-backend` is `preserve` and attribution does not touch it.
    assert row.backend_type is None


async def test_a_badauth_for_a_username_nobody_holds_stays_unattributed(
    client, world
) -> None:
    """The other half of the pair, and the one that stops #64 overreaching.

    A username no device holds resolves to nothing, so there is no owner
    to attribute the attempt to. ``user_id`` stays NULL and that NULL is
    a *meaning* — *no account this could belong to* — not a value the
    writer failed to supply.

    Without this test the previous one is satisfiable by attributing
    every refusal to an arbitrary account, which would be a far worse
    defect than the zero it replaced: a tenant would see failures that
    were never about them, and the count on their own screen would be
    noise. It is also what keeps the enumeration surface exactly where
    it was — an attacker submitting a guessed username still produces a
    row nobody can read.
    """
    await clear_events()
    before = len(await _badauth_rows(UNKNOWN_USERNAME_IP))

    response = await get(
        client, "/nic/update",
        headers={
            **basic(f"no-such-device-{W}", "irrelevant"),
            **xff(UNKNOWN_USERNAME_IP),
        },
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    assert response.text == "badauth"

    rows = await _badauth_rows(UNKNOWN_USERNAME_IP)
    assert len(rows) == before + 1
    row = rows[0]
    assert row.response_code == "badauth"
    assert row.user_id is None, "an attempt on a username nobody holds was attributed"
    assert row.device_id is None
    assert row.user_email is None
    assert row.device_name is None


async def test_an_inactive_owners_badauth_is_attributed_to_that_owner(
    client, world
) -> None:
    """The fourth refusal, and the one an operator will actually chase.

    ``dave`` is an inactive user holding *correct* device credentials —
    ``update-badauth-inactive-user`` is the frozen case. His router
    answers ``badauth`` forever and, before #64, wrote a row nobody
    could see. The row is his: the username resolved, the password
    verified, and only the account gate refused.
    """
    await clear_events()
    before = len(await _badauth_rows(INACTIVE_OWNER_IP))

    response = await get(
        client, "/nic/update",
        headers={
            **basic(world.device_usernames["dave"], DAVE_PASSWORD),
            **xff(INACTIVE_OWNER_IP),
        },
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    assert response.text == "badauth"

    rows = await _badauth_rows(INACTIVE_OWNER_IP)
    assert len(rows) == before + 1, rows
    assert rows[0].user_id == world.dave_id
    # Dave's device id is not on the fixture, so the assertion is that
    # a device was named at all — the verified-password path resolved
    # one, and only the account gate refused.
    assert rows[0].device_id is not None
    assert rows[0].device_name is not None


async def test_a_failed_sign_in_turns_the_owners_device_red_on_the_board(
    client, world
) -> None:
    """The consequence of #64 that nothing asked for, stated rather than left.

    ``worker_jobs.device_statuses`` classifies a device by *its most
    recent event carrying a ``device_id``*. Before #64 a ``badauth`` row
    carried none, so a router whose password had been changed kept
    whatever colour its last successful call had left behind — ``ACTIVE``
    if it had updated inside the window, ``IDLE`` if it had not. The
    board built to answer *which of my devices stopped working* showed a
    broken router green.

    Attribution changes that as a side effect, and the side effect is
    the point rather than an accident: the row now carries the device,
    so the device now reads ``LAST_CALL_FAILED``.

    Driven through the real endpoint deliberately. ``test_worker_jobs``
    already asserts this classification, but it does so over a
    hand-written ``badauth`` row carrying a ``device_id`` — a shape
    **nothing in this codebase wrote** until now. That test was
    exercising a branch with no writer; this one supplies the writer.
    """
    from atrium_ddns.scope import DdnsScope
    from atrium_ddns.worker_jobs import Liveness, device_statuses

    await clear_events()
    factory = get_session_factory()

    # A device that worked a moment ago, established through the real
    # endpoint: `_touch_device` stamps `last_seen_at` and the update
    # writes a `good` row, so the classification below is over data the
    # service produced rather than over a fixture's idea of it.
    ok = await get(
        client, "/nic/update",
        headers={**basic(world.device_usernames["alice"], ALICE_PASSWORD),
        **xff(BADAUTH_IP)},
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    assert ok.text.startswith("good"), ok.text

    async with factory() as s:
        before = {
            st.device_id: st.liveness
            for st in await device_statuses(
                s, scope=DdnsScope.for_user_id(world.alice_id), window_days=7
            )
        }
    assert before[world.alice_device] is Liveness.ACTIVE, before

    # Now the router's password is wrong — one real request, no fixture.
    response = await get(
        client, "/nic/update",
        headers={**basic(world.device_usernames["alice"], "wrong"), **xff(BADAUTH_IP)},
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    assert response.text == "badauth"

    async with factory() as s:
        after = {
            st.device_id: st.liveness
            for st in await device_statuses(
                s, scope=DdnsScope.for_user_id(world.alice_id), window_days=7
            )
        }
    assert after[world.alice_device] is Liveness.LAST_CALL_FAILED, after
    # And the two readings differ, which is the whole assertion: the
    # same device, the same window, one failed sign-in in between.
    assert before[world.alice_device] is not after[world.alice_device]


async def test_a_rate_limited_delete_is_logged_as_a_delete(client, world) -> None:
    """``event-a-rate-limited-delete-is-logged-as-dns_update``, ``fix``.

    The legacy delete handler's rate-limit branch passes the literal
    ``dns_update`` — copied from the update handler and never adjusted —
    so "show me this device's refused deletes" silently returned
    nothing. Invisible on the wire: both endpoints answer ``abuse`` and
    the frozen ``delete-abuse-rate-limited`` passes either way.
    """
    await clear_events()
    await _set_limit(world.alice_device, 0)
    try:
        assert (
            await get(
                client, "/nic/delete", headers=alice(world),
                hostname=world.name("ok"),
            )
        ).text == "abuse"
    finally:
        await _set_limit(world.alice_device, BIG_LIMIT)

    rows = await events_for(world.alice_id)
    assert [row.event_type for row in rows] == ["delete"]
    assert [row.response_code for row in rows] == ["abuse"]
    # `event-detail-is-populated-only-for-rate-limit-refusals`.
    assert rows[0].message and "rate limit" in rows[0].message


async def test_message_is_set_on_rate_limit_refusals_and_nothing_else(
    client, world
) -> None:
    await clear_events()
    await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("mixed"), myip="203.0.113.10",
    )
    await get(
        client, "/nic/update", headers=alice(world),
        hostname="never-registered.example.com", myip="203.0.113.10",
    )
    rows = await events_for(world.alice_id)
    assert rows, "no rows written — the assertion below would be vacuous"
    assert [row.message for row in rows] == [None] * len(rows)


async def test_backend_type_names_the_provider_on_a_per_attempt_row(
    client, world
) -> None:
    """``event-backend-type-is-null-for-outcomes-decided-before-any-backend``.

    ``preserve``, and it was **not expressible** until #18's ``0003``
    added the column: ``_record_hostname_events`` already unpacked the
    backend type out of ``result.attempts`` and threw it away, because
    there was nowhere to put it.

    ``mixed`` has two backends, ``stub1`` (dnserr) and ``stub2`` (good),
    so one request writes two rows and they must name *different*
    providers in the order the attempts ran. Asserting the pair rather
    than "the column is not null" is what makes this fail if every row
    were stamped with the same value — which is what a writer reading
    the aggregate instead of the attempt would produce.
    """
    await clear_events()
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("mixed"), myip="203.0.113.10",
    )
    assert response.text == "good 203.0.113.10"

    rows = await events_for(world.alice_id)
    assert [(row.backend_type, row.response_code) for row in rows] == [
        ("stub1", "dnserr"),
        ("stub2", "good"),
    ], [(row.backend_type, row.response_code) for row in rows]


@pytest.mark.parametrize(
    "hostname_key,expected",
    [
        # No provider is reachable at all: the domain has none.
        ("nobackend", "911 203.0.113.10"),
        # The name resolves to nothing this device owns, so resolution
        # stops before any backend is looked at.
        (None, "nohost 203.0.113.10"),
    ],
)
async def test_backend_type_is_null_when_the_outcome_precedes_any_backend(
    client, world, hostname_key, expected
) -> None:
    """The NULL half — and NULL here is a *meaning*, not a missing value.

    ``backend_type IS NULL`` is the filter for "refused before any
    provider was contacted". If these rows carried a placeholder
    instead, that filter would return nothing and read like a healthy
    estate.
    """
    await clear_events()
    hostname = (
        world.name(hostname_key)
        if hostname_key
        else "never-registered.example.com"
    )
    response = await get(
        client, "/nic/update", headers=alice(world),
        hostname=hostname, myip="203.0.113.10",
    )
    assert response.text == expected

    rows = await events_for(world.alice_id)
    assert rows, "no rows written — the assertion below would be vacuous"
    assert [row.backend_type for row in rows] == [None] * len(rows)


async def test_badauth_and_abuse_rows_carry_no_backend_type(
    client, world
) -> None:
    """The two refusals decided before a hostname is even parsed.

    Read back with the filter the column exists to serve —
    ``backend_type IS NULL`` in SQL — rather than through the ORM
    attribute, so the assertion is about what the database stores and
    not about what the mapper hands back.
    """
    await clear_events()
    await get(
        client, "/nic/update",
        headers=basic(world.device_usernames["alice"], "wrong"),
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    await _set_limit(world.alice_device, 0)
    try:
        await get(
            client, "/nic/update", headers=alice(world),
            hostname=world.name("ok"), myip="203.0.113.10",
        )
    finally:
        await _set_limit(world.alice_device, BIG_LIMIT)

    factory = get_session_factory()
    async with factory() as s:
        total, nulls = (
            await s.execute(
                sa.text(
                    "SELECT COUNT(*), SUM(backend_type IS NULL) "
                    "FROM ddns_event WHERE response_code IN "
                    "('badauth', 'abuse')"
                )
            )
        ).one()
    # The denominator is published beside the zero, so "0 rows carry a
    # backend type" cannot be satisfied by there being no rows.
    assert total >= 2, f"only {total} refusal rows — the ratio below is vacuous"
    assert int(nulls) == total, f"{total - int(nulls)} of {total} carry one"


async def test_delete_logs_the_normalised_address(client, world) -> None:
    """``event-ip-address-on-delete-is-the-raw-myip-parameter``, ``fix``.

    Update logged ``2001:db8::1`` and delete logged
    ``2001:0DB8:0000::1`` from the same request, so a log grouped by
    address split one address across every spelling a client happened
    to use. The one endpoint where the normalisation is invisible from
    outside — delete replies carry no IP suffix at all — is the one
    that skipped it.
    """
    await clear_events()
    await get(
        client, "/nic/delete", headers=alice(world),
        hostname=world.name("ok"), myip="2001:0DB8:0000::1",
    )
    rows = await events_for(world.alice_id)
    assert [row.ip for row in rows] == ["2001:db8::1"]

    await clear_events()
    await get(
        client, "/nic/delete", headers=alice(world), hostname=world.name("ok")
    )
    rows = await events_for(world.alice_id)
    # NULL because there genuinely was no address, not because it was
    # dropped: `n/a` and a value are two states.
    assert [row.ip for row in rows] == [None]


async def test_the_device_liveness_columns_move(client, world) -> None:
    """What ``worker_jobs.device_statuses`` reads. It had no writer before.

    ``last_seen_at`` answers "which of my devices stopped calling",
    which is a different question from "which last succeeded" — so it
    moves on a refused call too, and collapsing the two would merge two
    of plan §3.4's three states.
    """
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            sa.update(Device)
            .where(Device.id == world.alice_device)
            .values(last_seen_at=None, last_ip_v4=None, last_user_agent=None)
        )
        await s.commit()

    await get(
        client, "/nic/update",
        headers={**alice(world), "User-Agent": "ddclient/3.11.2"},
        hostname=world.name("ok"), myip="203.0.113.10",
    )

    async with factory() as s:
        row = (
            await s.execute(sa.select(Device).where(Device.id == world.alice_device))
        ).scalar_one()
    assert row.last_seen_at is not None
    assert row.last_ip_v4 == "203.0.113.10"
    assert row.last_user_agent == "ddclient/3.11.2"


async def test_the_event_type_matches_what_the_status_board_filters_on(
    client, world
) -> None:
    """A third spelling would silently empty ``device_statuses``.

    ``worker_jobs`` counts ``DnsEvent.event_type == "update"``. If this
    router wrote ``dns_update`` (the legacy vocabulary, and the one the
    model cases are phrased in) every device would read zero updates in
    the window with no error anywhere — the confident-zero shape.
    Derived from ``worker_jobs``' own source rather than restated, so
    a change there takes this with it.
    """
    source = Path(
        __import__("atrium_ddns.worker_jobs", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    assert 'DnsEvent.event_type == "update"' in source, (
        "worker_jobs no longer filters on 'update' — re-derive this test "
        "and router_nic.EVENT_UPDATE from whatever it filters on now"
    )
    assert router_nic.EVENT_UPDATE == "update"

    await clear_events()
    await get(
        client, "/nic/update", headers=alice(world),
        hostname=world.name("ok"), myip="203.0.113.10",
    )
    rows = await events_for(world.alice_id)
    assert [row.event_type for row in rows] == ["update"]


async def test_checkip_writes_no_event(client, world) -> None:
    """A decision, not an omission — and one worth being able to see.

    The caller is unauthenticated and unattributable, and a liveness
    probe polls this endpoint far more often than a router updates.
    """
    await clear_events()
    await client.get("/nic/checkip", headers=xff("203.0.113.10"))
    factory = get_session_factory()
    async with factory() as s:
        count = (
            await s.execute(
                sa.text(
                    "SELECT COUNT(*) FROM ddns_event WHERE event_type='checkip'"
                )
            )
        ).scalar_one()
    assert count == 0


# ===================================================================== #
# 8. checkip — the client-address rules
# ===================================================================== #


async def test_checkip_takes_the_rightmost_forwarded_for(client) -> None:
    """``ProxyFix(x_for=1)`` takes ``values[-trusted]``, i.e. the last.

    Taking the leftmost would let any client prepend an address of its
    choosing — and on ``/nic/update`` that address is what gets written
    into DNS.
    """
    response = await client.get(
        "/nic/checkip",
        params={"format": "plain"},
        headers=xff("198.51.100.9, 203.0.113.10"),
    )
    assert response.text == "203.0.113.10"


async def test_checkip_does_not_honour_client_ip(client) -> None:
    """Divergence 9, both directions.

    §1.7 of the protocol document names ``Client-IP`` beside
    ``X-Forwarded-For``; ``ProxyFix`` reads only the latter, so half the
    clause is implemented. The assertion is that the header is
    **inert**, not that it is absent.
    """
    unparseable = await client.get(
        "/nic/checkip",
        params={"format": "plain"},
        headers={"X-Forwarded-For": "not-an-ip", "Client-IP": "198.51.100.5"},
    )
    assert unparseable.text == ""

    both = await client.get(
        "/nic/checkip",
        params={"format": "plain"},
        headers={"X-Forwarded-For": "203.0.113.10", "Client-IP": "198.51.100.5"},
    )
    assert both.text == "203.0.113.10"


async def test_client_ip_does_not_supply_a_missing_myip(client, world) -> None:
    """Divergence 9 on the endpoint that writes DNS.

    An implementation honouring ``Client-IP`` would answer ``good
    198.51.100.5`` and would have pointed a DNS record at an address
    carried in a header nothing validates.
    """
    response = await client.get(
        "/nic/update",
        params={"hostname": world.name("ok")},
        headers={
            **alice(world),
            "X-Forwarded-For": "not-an-ip",
            "Client-IP": "198.51.100.5",
        },
    )
    assert response.text == "911"
    assert CALLS == []


# ===================================================================== #
# 9. Aggregation
# ===================================================================== #


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["good"], "good"),
        (["nochg"], "nochg"),
        (["dnserr", "good"], "good"),
        (["nochg", "nochg"], "nochg"),
        (["nochg", "911", "dnserr"], "911"),
        (["dnserr", "911"], "dnserr"),
        (["nochg", "dnserr", "911"], "dnserr"),
    ],
)
def test_aggregate(statuses, expected) -> None:
    """The **first** status that is neither ``good`` nor ``nochg``.

    Not the last error and not the first error overall — the frozen
    ``firsterr`` fixture is ordered ``nochg, 911, dnserr`` precisely so
    an implementation returning either wrong answer fails, and the last
    two rows here are the pair that tells "first" from "worst" apart.
    """
    assert router_nic.aggregate(statuses) == expected


def test_aggregate_refuses_an_empty_set() -> None:
    """"Zero backends" is answered before aggregation, not by it.

    A default here would be a plausible ``911`` nobody could explain.
    """
    with pytest.raises(ValueError):
        router_nic.aggregate([])


# ===================================================================== #
# 10. `effects:` replayed FROM the frozen table
#
# The first entry in `frozen.known_gaps` is the largest thing the wire
# table does not check: "`effects:` is data, not an assertion. 25 cases
# carry a DNS operation or a persisted column and no runner asserts
# either … Closing this needs an `effects:` assertion, and it is the
# single largest thing the table does not check."
#
# This section is that assertion. It reads the blocks out of
# `protocol_cases.yaml` and replays each case in-process against the
# fixture world above, comparing the recorded provider calls and the
# `last_ip_*` columns with what the case says must happen. Derived from
# the table rather than restated, so a case whose `effects:` changes is
# checked against the new value and a case that grows one is picked up
# without editing this file.
#
# Two things it is not. It is not a second wire runner — the bodies are
# asserted here as a by-product, over ASGI rather than a socket, which
# makes it a differently shaped instrument on the same expectations
# rather than a replacement for one. And it is not complete: the
# accounting test at the end names exactly which host-selected
# `effects:` cases it reaches and which it does not, as a number.
# ===================================================================== #

TABLE_PATH = Path("/opt/compat_tests/compat/protocol_cases.yaml")

#: Case ids this replay deliberately does not execute, with the reason.
#: Empty is a legitimate value; a bare "not covered" is not, which is
#: why this is a mapping and not a list.
EFFECTS_NOT_REPLAYED: dict[str, str] = {}


def _load_table() -> dict[str, Any]:
    import yaml

    return yaml.safe_load(TABLE_PATH.read_text(encoding="utf-8"))


def _resolved_cases() -> list[dict[str, Any]]:
    table = _load_table()
    defaults = table["defaults"]
    out = []
    for case in table["cases"]:
        merged = {k: v for k, v in defaults.items() if k != "expect"}
        merged.update({k: v for k, v in case.items() if k != "expect"})
        expect = dict(defaults.get("expect", {}))
        expect.update(case.get("expect", {}))
        merged["expect"] = expect
        out.append(merged)
    return out


def _effects_cases() -> list[dict[str, Any]]:
    if not TABLE_PATH.is_file():
        return []
    return [
        case
        for case in _resolved_cases()
        if "host" in case["targets"]
        and "effects" in case
        and case["id"] not in EFFECTS_NOT_REPLAYED
    ]


def _rename(value: str) -> str:
    """``ok.example.com`` -> this worker's ``ok.<worker>.nic.invalid``.

    The fixture world here is namespaced per xdist worker because
    ``ddns_hostname.name`` and ``ddns_domain.name`` are globally unique
    and ten workers share one MySQL. The table's names are not, so the
    replay maps them. Only names the fixture actually holds are
    rewritten: ``never-registered.example.com`` is meant to miss, and
    rewriting it would make it miss for a different reason.
    """
    for key in LAYOUT:
        if value == f"{key}.example.com":
            return f"{key}.{SUFFIX}"
    if value == "outofzone.example.com":
        return f"outofzone.{SUFFIX}"
    return value


def _rename_list(value: str) -> str:
    return ",".join(_rename(part) for part in value.split(","))


EFFECTS_IDS = [case["id"] for case in _effects_cases()]


@pytest.mark.skipif(
    not TABLE_PATH.is_file(),
    reason=(
        f"{TABLE_PATH} is absent — it reaches the image through the "
        "Dockerfile's `dev` stage. The frozen table's `effects:` blocks "
        "have NOT been replayed here."
    ),
)
@pytest.mark.parametrize(
    "case", _effects_cases(), ids=EFFECTS_IDS or ["no-table"]
)
async def test_effects_from_the_frozen_table(app, world, case) -> None:
    hostname_param = _rename_list(str(case.get("query", {}).get("hostname", "")))
    watched = [
        name for name in hostname_param.split(",") if name.endswith(SUFFIX)
    ]

    before = {name: await _snapshot(name) for name in watched}

    query = {
        key: _rename_list(str(value)) if key == "hostname" else str(value)
        for key, value in (case.get("query") or {}).items()
    }
    client_ip = case.get("client_ip")
    headers = {
        "X-Forwarded-For": "not-an-ip" if client_ip is None else str(client_ip),
        **{str(k): str(v) for k, v in (case.get("headers") or {}).items()},
    }
    auth = case.get("auth") or {"type": "none"}
    if auth.get("type") == "basic":
        # The table's fixture credentials name `alice`; this world's
        # devices are namespaced and the password is the same string.
        headers.update(
            basic(world.device_usernames[auth["username"]], auth["password"])
        )

    CALLS.clear()
    async with _client(app) as c:
        response = await c.request(
            case.get("method", "GET"), case["path"], params=query, headers=headers
        )

    # The wire half, over ASGI rather than a socket. Not the point of
    # this test, but free — and a disagreement with the socket runner
    # would be worth knowing about.
    expect = case["expect"]
    assert response.status_code == expect["status"], case["id"]
    if "body" in expect and case.get("method", "GET") != "HEAD":
        assert response.text == expect["body"], case["id"]

    _assert_dns_effects(case)
    await _assert_persist_effects(case, before, watched)


async def _snapshot(name: str) -> tuple[Any, Any, Any]:
    factory = get_session_factory()
    async with factory() as s:
        row = (
            await s.execute(sa.select(Hostname).where(Hostname.name == name))
        ).scalar_one_or_none()
    if row is None:
        return (None, None, None)
    return (row.last_ip_v4, row.last_ip_v6, row.last_updated_at)


#: ``effects.dns[].backend`` names the fixture backend; the stub records
#: the *scripted result*, and for the backends that appear in a ``dns:``
#: block the two are the same word. Mapped explicitly rather than
#: compared by luck, so a block naming a backend this map does not know
#: is skipped visibly instead of silently comparing nothing.
_BACKEND_RESULT = {"good": "good", "dnserr": "dnserr", "nochg-a": "nochg"}


def _assert_dns_effects(case: dict[str, Any]) -> None:
    expected = case["effects"].get("dns")
    if expected is None:
        return
    actual = list(CALLS)
    assert len(actual) == len(expected), (
        f"{case['id']}: {len(expected)} DNS operations expected, "
        f"{len(actual)} recorded: {actual}"
    )
    for index, (want, got) in enumerate(zip(expected, actual)):
        where = f"{case['id']}[{index}]"
        assert got.op == want["op"], f"{where}: op {got.op} != {want['op']}"
        # `rtype: null` is a THIRD state — "both families" — not a
        # missing key, and it is the one the wire can never see.
        assert got.rtype == want.get("rtype"), (
            f"{where}: rtype {got.rtype!r} != {want.get('rtype')!r}"
        )
        if "ip" in want:
            assert got.ip == str(want["ip"]), (
                f"{where}: ip {got.ip!r} != {want['ip']!r}"
            )
        if "hostname" in want:
            assert got.hostname == _rename(str(want["hostname"])), where
        if want.get("backend") in _BACKEND_RESULT:
            assert got.result == _BACKEND_RESULT[want["backend"]], (
                f"{where}: backend {want['backend']} answered {got.result}"
            )


async def _assert_persist_effects(
    case: dict[str, Any], before: dict[str, tuple], watched: list[str]
) -> None:
    effects = case["effects"]
    # The table spells this key two ways — `persist` on most cases and
    # `persisted` on three. Both are read rather than one being treated
    # as the typo, because the table is frozen and picking one would
    # silently skip the other three.
    if "persist" in effects:
        block = effects["persist"]
    elif "persisted" in effects:
        block = effects["persisted"]
    else:
        return

    if block in (None, "none"):
        for name in watched:
            assert await _snapshot(name) == before[name], (
                f"{case['id']}: {name} changed but the table says nothing "
                "is persisted"
            )
        return

    name = _rename(str(block.get("hostname", watched[0] if watched else "")))
    assert name, f"{case['id']}: no hostname to check"
    was = before.get(name, (None, None, None))
    now = await _snapshot(name)
    columns = {"last_ip_v4": 0, "last_ip_v6": 1, "last_updated_at": 2}

    for column, index in columns.items():
        if column not in block:
            continue
        want = block[column]
        if want == "unchanged":
            assert now[index] == was[index], (
                f"{case['id']}: {column} moved {was[index]!r} -> "
                f"{now[index]!r} but the table says unchanged"
            )
        elif want == "set":
            assert now[index] is not None, f"{case['id']}: {column} is NULL"
        else:
            assert now[index] == str(want), (
                f"{case['id']}: {column} is {now[index]!r}, table says {want!r}"
            )


def test_the_effects_replay_reaches_every_host_selected_effects_case() -> None:
    """The accounting, so the remaining gap is a number and not a feeling.

    ``frozen.known_gaps`` counts the ``effects:`` cases the wire runner
    does not assert; this counts the ones *this file* does. A case that
    grows an ``effects:`` block, or one this replay stops reaching, has
    to be added to :data:`EFFECTS_NOT_REPLAYED` **with a reason** — an
    empty reason is not accepted, for the same argument
    ``scope.UNSCOPED`` makes about an unscoped model.
    """
    if not TABLE_PATH.is_file():
        pytest.skip(
            f"{TABLE_PATH} is absent, so the count has NOT been re-derived"
        )
    host_selected_with_effects = {
        case["id"]
        for case in _resolved_cases()
        if "host" in case["targets"] and "effects" in case
    }
    replayed = set(EFFECTS_IDS)
    excused = set(EFFECTS_NOT_REPLAYED)

    assert replayed | excused == host_selected_with_effects, {
        "unaccounted": sorted(host_selected_with_effects - replayed - excused),
        "stale_excuses": sorted(excused - host_selected_with_effects),
    }
    assert all(
        reason.strip() for reason in EFFECTS_NOT_REPLAYED.values()
    ), "an entry in EFFECTS_NOT_REPLAYED carries no reason"
    # Two readings of the same population, so the count cannot be a
    # literal that quietly stopped describing the table.
    assert len(host_selected_with_effects) == len(replayed) + len(excused)


# ===================================================================== #
# 11. The socket peer — the `else` branch of `client_address`
# ===================================================================== #
#
# `auth_device.client_address` is two branches:
#
#     forwarded = request.headers.get(FORWARDED_FOR)
#     if forwarded is not None and forwarded.strip():
#         candidate = forwarded.split(",")[-1].strip()
#     else:
#         candidate = request.client.host if request.client else ""
#
# Section 8 and the frozen table own the first. The `else` is what
# decides a device's address when the service is reached **directly**
# rather than through a proxy, and it is what writes that address into
# the log a tenant reads.
#
# It was not uncovered. It was worse than uncovered: measured on
# `99a4e65` with `coverage run --branch`, that line executes — every
# request in this file that does not go through `get()` takes it, on
# httpx's default peer of `127.0.0.1`. A line-coverage report said the
# line was fine. **Nothing asserted it.** Three mutations of that one
# line, each run against the whole 865-test suite:
#
#     candidate = "0.0.0.0"                # 865 passed
#     candidate = ""                       # 865 passed
#     candidate = request.client.host      # 865 passed  (guard removed)
#
# The first is the exact value `normalise_address`'s docstring says the
# absent case must never be rendered as. The third deletes the
# `request.client is None` guard outright and nothing reached it. The
# tests below are the ones those three mutations have to break.


def peer_client(application: FastAPI, peer: tuple[str, int]) -> httpx.AsyncClient:
    """A client whose ASGI scope carries `peer` as the socket peer.

    `ASGITransport(client=…)` is the transport's own parameter — it
    writes `scope["client"]` and nothing else — so the request arrives
    exactly as it would from that address with no proxy in front of it.
    `_client` above leaves it at httpx's default `("127.0.0.1", 123)`,
    which is why every reading here uses a documentation address the
    default cannot produce by accident.
    """
    return httpx.AsyncClient(
        transport=ASGITransport(app=application, client=peer),
        base_url="http://nic.test",
    )


class _WithoutPeer:
    """The router behind a transport that reports **no** peer at all.

    `scope["client"]` is optional in ASGI and Starlette's
    `Request.client` answers `None` for it. This is not a contrived
    shape: `uvicorn.protocols.utils.get_remote_addr` (0.44.0, read in
    the image) returns `None` whenever `socket.getpeername()` answers
    something that is not a `(host, port)` tuple — which is every
    **Unix-domain-socket** connection, the way an app is fronted when
    the reverse proxy and the service share a host.

    Written as an ASGI wrapper rather than by passing `client=None` to
    `ASGITransport`, whose signature says `tuple[str, int]`: a test that
    depends on a library tolerating a type violation is a test that
    breaks on an upgrade for a reason unrelated to its subject.
    """

    def __init__(self, application: FastAPI) -> None:
        self.application = application

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.application({**scope, "client": None}, receive, send)


def peerless_client(application: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=_WithoutPeer(application)),
        base_url="http://nic.test",
    )


async def _client_ip_is_sql_null(event_id: int) -> bool:
    """`client_ip IS NULL` asked of the database, not of the ORM.

    The second instrument on the one distinction this section exists
    for. `row.client_ip is None` in Python is also what the string
    `"None"`, or a column the mapper failed to load, would look like;
    a `WHERE client_ip IS NULL` that counts the row is the store's own
    answer about the same cell.
    """
    factory = get_session_factory()
    async with factory() as s:
        return (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(DnsEvent)
                .where(DnsEvent.id == event_id)
                .where(DnsEvent.client_ip.is_(None))
            )
        ).scalar_one() == 1


async def test_checkip_falls_back_to_the_socket_peer_with_no_forwarded_for(
    app,
) -> None:
    """No `X-Forwarded-For` at all -> the peer, echoed verbatim.

    Asserted with a *control* in the same test, because "checkip echoes
    198.51.100.77" is satisfiable by an implementation that reads the
    peer and ignores the header entirely — which is the security
    decision this fallback sits underneath, inverted. The header must
    still win when it is present.
    """
    async with peer_client(app, ("198.51.100.77", 41234)) as c:
        no_header = await c.get("/nic/checkip", params={"format": "plain"})
        with_header = await c.get(
            "/nic/checkip", params={"format": "plain"}, headers=xff("203.0.113.10")
        )

    assert no_header.text == "198.51.100.77"
    # The fallback is a fallback. The peer is not consulted while a
    # parseable header is there, and it is not consulted *instead* of an
    # unparseable one either — `client_ip: null` in the frozen table is
    # arranged by sending an unparseable header precisely because the
    # peer underneath it is perfectly parseable, which is only true if
    # this is the ordering.
    assert with_header.text == "203.0.113.10"


async def test_an_update_with_no_forwarded_for_uses_the_socket_peer(
    app, world
) -> None:
    """The whole path, read by four instruments of different shape.

    The wire, the provider's own call log, the persisted column and the
    event row all have to say `198.51.100.77`. The wire alone would not
    do: #29 measured that update's reply carries the normalised address
    and never the record type, so a host that echoes an address and
    writes something else entirely passes every case in the table.
    """
    await clear_events()
    hostname_id = world.hostnames["ok"]

    async with peer_client(app, ("198.51.100.77", 41234)) as c:
        response = await c.get(
            "/nic/update",
            params={"hostname": world.name("ok")},
            headers=alice(world),
        )

    # 1. the wire
    assert response.text == "good 198.51.100.77"
    # 2. the provider's call log — address *and* family, neither of
    #    which the reply can be read for
    assert [(call.rtype, call.ip) for call in creates()] == [("A", "198.51.100.77")]
    # 3. the persisted column
    after = await hostname_row(hostname_id)
    assert after.last_ip_v4 == "198.51.100.77"
    # 4. the event row the tenant reads. `client_ip` is the address the
    #    request came from and `ip` is the address written to DNS; with
    #    no `myip` they are the same value arrived at two ways, and both
    #    come off the socket peer here.
    rows = await events_for(world.alice_id)
    assert [row.response_code for row in rows] == ["good"]
    assert rows[0].client_ip == "198.51.100.77"
    assert rows[0].ip == "198.51.100.77"


async def test_the_socket_peer_is_normalised_and_picks_the_record_family(
    app, world
) -> None:
    """The peer goes through `normalise_address`, like a header value.

    `2001:0DB8:0113::0077` is written in the form the wire table uses to
    assert canonicalisation, so an implementation that echoed the peer
    through unchanged is red on the first assertion; and there is no
    `myip` to read a family off, so `AAAA` can only have come from the
    peer. Both halves are invisible to line coverage — the line runs
    either way.
    """
    hostname_id = world.hostnames["ok"]
    before = await hostname_row(hostname_id)

    async with peer_client(app, ("2001:0DB8:0113::0077", 41234)) as c:
        response = await c.get(
            "/nic/update",
            params={"hostname": world.name("ok")},
            headers=alice(world),
        )

    assert response.text == "good 2001:db8:113::77"
    assert [(call.rtype, call.ip) for call in creates()] == [
        ("AAAA", "2001:db8:113::77")
    ]
    after = await hostname_row(hostname_id)
    assert after.last_ip_v6 == "2001:db8:113::77"
    # The v4 column is a different family and this write is not about
    # it. Compared against its own prior reading rather than asserted
    # absolutely, so the test says "unchanged" and not "happens to be
    # whatever the test before me left".
    assert after.last_ip_v4 == before.last_ip_v4


def test_no_peer_yields_none_and_the_empty_string_is_not_the_wildcard() -> None:
    """`request.client is None` -> `""` -> `None`. **Not** `0.0.0.0`.

    The unit-shaped half of the pair below, and the only reading that
    can see the `""` in the middle. `normalise_address`'s docstring
    calls this distinction out as one that must never be blurred, and
    the two assertions at the end are why it can be: `0.0.0.0` is a
    perfectly parseable address that means something — the unspecified
    address — so folding *no address* into it does not lose a
    formatting nicety, it invents a claim.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/nic/checkip",
        "raw_path": b"/nic/checkip",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": None,
        "server": ("nic.test", 80),
    }
    assert Request(scope).client is None
    assert client_address(Request(scope)) is None

    # The two states the `else` branch collapses into one type, told
    # apart at the normaliser they both pass through.
    assert normalise_address("") is None
    assert normalise_address("0.0.0.0") == "0.0.0.0"


async def test_checkip_renders_a_missing_peer_as_empty_not_as_a_wildcard(
    app,
) -> None:
    """The wire-shaped half. `checkip` is `client_address(request) or ""`.

    The control is not decoration: with the wrapper removed this
    request goes out on httpx's default peer and answers `127.0.0.1`,
    so a green run here is evidence the peer was really taken away and
    not evidence that `checkip` returns `""` for everything.
    """
    async with peerless_client(app) as c:
        plain = await c.get("/nic/checkip", params={"format": "plain"})
        html = await c.get("/nic/checkip")
    async with _client(app) as c:
        control = await c.get("/nic/checkip", params={"format": "plain"})

    assert plain.text == ""
    assert plain.text != "0.0.0.0"
    assert "0.0.0.0" not in html.text
    assert control.text == "127.0.0.1"


async def test_no_peer_and_no_myip_answers_911_and_logs_a_null_client_ip(
    app, world
) -> None:
    """`update-911-no-myip-and-no-client-ip`, reached the way it happens.

    The frozen table arranges this case with an unparseable
    `X-Forwarded-For`, because over a socket there is normally a peer.
    The other way in is the one this asserts: no header and no peer,
    which is what a Unix-domain-socket connection produces.

    The stored `client_ip` is the point. A `0.0.0.0` there is a tenant
    reading their own log and seeing an address the request did not come
    from; `NULL` is the log saying it does not know, which is a
    different and true statement. Read twice — once off the ORM
    attribute and once as the database's own `IS NULL`.
    """
    await clear_events()

    async with peerless_client(app) as c:
        response = await c.get(
            "/nic/update",
            params={"hostname": world.name("ok")},
            headers=alice(world),
        )

    assert response.text == "911"
    # No address means no DNS write. `911` with a call behind it would
    # be the worse failure of the two.
    assert CALLS == []

    rows = await events_for(world.alice_id)
    assert [row.response_code for row in rows] == ["911"]
    row = rows[0]
    assert row.client_ip is None
    assert row.client_ip != "0.0.0.0"
    assert row.client_ip != ""
    assert row.ip is None
    assert await _client_ip_is_sql_null(row.id)
