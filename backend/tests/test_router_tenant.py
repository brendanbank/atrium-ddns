"""The tenant CRUD surface — domains, provider credentials, devices (#45).

Four acceptance criteria, and each one has an implementation that looks
right and passes a weaker test. This file is written to make the weaker
version fail.

**1. Every endpoint goes through ``DdnsScope``; no hand-written
``user_id`` filters.** Asserted two ways that cannot both be satisfied
by accident: a *structural* sweep over the router's own route table
(every data route declares the scope dependency, and the set that does
not is named and closed), plus a *source* sweep for the shape of a
hand-written tenancy predicate. Then behaviourally — tenant B's rows
are invisible to A on every verb, and A's attempt to mutate one is a
404 rather than a 403, because "not yours" and "no such row" must not be
distinguishable.

**2. No endpoint returns a credential in cleartext.** Structural: walk
every route's response model, flatten its JSON schema, and assert no
property is named after any provider's credential key — derived from
``providers``, so a provider added later is covered without this file
changing. Behavioural: store a credential with a value nothing else in
the fixture uses, then assert that string appears in no response body
from any endpoint. And the one that proves the read path *cannot* leak
rather than merely does not: with the tenant's encryption key
**shredded**, the listing still answers 200 — an endpoint that decrypted
would raise.

**3. Rotate shows the secret once, and the second read does not carry
it.** The trap is that "rotate returns a random string" passes a naive
version of this. So the test also verifies the *new* secret against the
stored hash and verifies that the *old* one no longer does — the
rotation has to have actually happened for the absence to mean anything.

**4. Blank-preserves in all three directions.** Clear, preserve,
replace, each with two instruments: the decrypted plaintext read back
through the ``UserSecret`` descriptor, and the **raw ciphertext bytes**
read with SQL. The bytes matter because encryption is nonced — a
re-encrypt of the same plaintext produces different bytes — so
byte-identity is the only evidence that a preserve did not quietly
re-encrypt, and a plaintext read alone cannot tell the two apart.

Everything created here is namespaced by ``PYTEST_XDIST_WORKER``: ten
workers share one MySQL and a hardcoded email or zone name produces
collisions that read as flakiness.
"""
from __future__ import annotations

import os
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.auth.principal import Principal
from app.auth.rbac import current_principal
from app.db import get_session_factory
from app.host_sdk.crypto import shred_user_key, unlock_user_secrets
from app.models.auth import User
from app.models.ops import AuditLog
from fastapi import FastAPI
from httpx import ASGITransport

from atrium_ddns import models as m
from atrium_ddns import router as tenant_router
from atrium_ddns.auth_device import verify_password
from atrium_ddns.providers import known_services, provider_class
from atrium_ddns.router import (
    DEVICE_MANAGE_PERMISSION,
    DOMAIN_MANAGE_PERMISSION,
    PRESERVE,
    CredentialOrigin,
    _all_secret_keys,
    credential_origin,
    router,
)
from atrium_ddns.scope import CROSS_TENANT_PERMISSION, get_scope

pytestmark = pytest.mark.asyncio

W = os.environ.get("PYTEST_XDIST_WORKER", "serial")

#: A credential value that appears nowhere else in this repository, so a
#: substring search for it in a response body is a search for *this*
#: secret and not for a word that happens to occur.
CANARY = f"canary-secret-{W}-3f9a1c7e5b2d"
CANARY_TWO = f"canary-replaced-{W}-8c4e0a6f1d93"

#: The provider whose credential keys the fixtures use. Read off the
#: registry rather than typed, so this file follows a rename.
SERVICE = "route53"


def _keys() -> tuple[str, ...]:
    cls = provider_class(SERVICE)
    assert cls is not None, f"{SERVICE} is not registered; this file is vacuous"
    return tuple(cls.REQUIRED_CREDENTIALS)


def _creds(value: str) -> dict[str, str]:
    """A complete credential set for :data:`SERVICE`, all fields set.

    Derived from ``REQUIRED_CREDENTIALS`` so the payload stays complete
    if the provider grows a key — a hardcoded two-key dict would start
    failing ``_require_complete_credentials`` and read as a test bug.
    """
    return {key: f"{value}-{key}" for key in _keys()}


def _password_hash() -> str:
    from fastapi_users.password import PasswordHelper

    return PasswordHelper().hash("unusable-" + "x" * 24)


async def _purge(emails: list[str]) -> None:
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            sa.text("DELETE FROM ddns_event WHERE user_email IN :e").bindparams(
                sa.bindparam("e", expanding=True)
            ),
            {"e": emails},
        )
        for email in emails:
            row = (
                await s.execute(
                    sa.text("SELECT id FROM users WHERE email = :e"), {"e": email}
                )
            ).first()
            if row is not None:
                await s.execute(
                    sa.text("DELETE FROM user_secret_keys WHERE user_id = :u"),
                    {"u": row[0]},
                )
            await s.execute(sa.text("DELETE FROM users WHERE email = :e"), {"e": email})
        await s.commit()


@pytest_asyncio.fixture
async def tenants() -> AsyncIterator[dict[str, Any]]:
    """Three tenants: A, B (the cross-tenant control) and S (shreddable).

    Three rather than two because one of the tests destroys an
    encryption key permanently, and a test that needs to destroy
    something must create the thing it destroys —
    ``test_user_scope_secrets.py`` learned that by destroying a seeded
    admin's key.
    """
    factory = get_session_factory()
    tags = ("a", "b", "s")
    emails = [f"ddns-tenant-{tag}-{W}@example.invalid" for tag in tags]
    await _purge(emails)

    built: dict[str, Any] = {"emails": emails}
    async with factory() as s:
        for tag, email in zip(tags, emails):
            user = User(
                email=email,
                hashed_password=_password_hash(),
                is_active=True,
                is_verified=True,
                full_name=f"DDNS tenant probe {W}",
                preferred_language="en",
            )
            s.add(user)
            await s.flush()
            domain = m.Domain(user_id=user.id, name=f"{tag}-crud-{W}.example.invalid")
            s.add(domain)
            await s.flush()
            built[tag] = {
                "user": user,
                "user_id": user.id,
                "email": email,
                "domain_id": domain.id,
                "domain_name": domain.name,
            }
        await s.commit()

    yield built

    await _purge(emails)


ALL_PERMS = {DOMAIN_MANAGE_PERMISSION, DEVICE_MANAGE_PERMISSION}


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
    perms = ALL_PERMS if permissions is None else permissions
    return httpx.AsyncClient(
        transport=ASGITransport(app=_app(user, perms)),
        base_url="http://tenant.test",
    )


async def _ciphertext(backend_id: int) -> bytes | None:
    """The raw stored bytes. The second instrument on every credential
    assertion, and the only one that can see a needless re-encrypt."""
    factory = get_session_factory()
    async with factory() as s:
        return (
            await s.execute(
                sa.text(
                    "SELECT credentials_ct FROM ddns_domain_backend WHERE id = :i"
                ),
                {"i": backend_id},
            )
        ).scalar_one()


async def _plaintext(backend_id: int) -> Any:
    """The decrypted credential, through the descriptor the app uses.

    Deliberately the same path ``/nic/update`` takes — unlock by the
    owner read off the row, then ``.reveal()`` — rather than a
    re-implementation, so a test that passes here is evidence about the
    thing the hot path does.
    """
    factory = get_session_factory()
    async with factory() as s:
        row = await s.get(m.DomainBackend, backend_id)
        if row is None or row.credentials_ct is None:
            return None
        await unlock_user_secrets(s, row.user_id)
        revealed = row.credentials
        return None if revealed is None else revealed.reveal()


async def _make_backend(client: httpx.AsyncClient, domain_id: int, **body: Any):
    payload: dict[str, Any] = {"backend_type": SERVICE, "config": {"ttl": 60}}
    payload.update(body)
    return await client.post(f"/api/atrium_ddns/domains/{domain_id}/backends", json=payload)


# ===================================================================== #
# 1. Every endpoint goes through DdnsScope
# ===================================================================== #

#: The routes on this router that legitimately take no scope, and why.
#: A *set*, closed and named, rather than a rule of thumb: the whole
#: point is that a route added later without a scope shows up here as a
#: failure with its own path in the message.
UNSCOPED_ROUTES: dict[str, str] = {
    "GET /api/atrium_ddns/state": (
        "the scaffold's installation-wide demo singleton (id=1); "
        "atrium_ddns.scope.UNSCOPED records the same reason"
    ),
    "POST /api/atrium_ddns/bump": "the same singleton, written",
    "GET /api/atrium_ddns/providers": (
        "the provider catalogue is a property of the build, not of a "
        "tenant — it reads no table at all"
    ),
}


def _route_key(route: Any) -> str:
    method = sorted(route.methods - {"HEAD", "OPTIONS"})[0]
    return f"{method} {route.path}"


async def test_every_data_route_declares_the_scope_dependency():
    """Swept off the router's own route table, to a negative result.

    "Here are the three that do not take a scope, and here is why each
    one is allowed to" closes the question. A test that checked three
    named routes *have* a scope would pass unchanged after a fourth was
    added without one.
    """
    missing: list[str] = []
    for route in router.routes:
        key = _route_key(route)
        takes_scope = any(
            sub.call is get_scope for sub in route.dependant.dependencies
        )
        if not takes_scope and key not in UNSCOPED_ROUTES:
            missing.append(key)
    assert missing == [], (
        f"{missing} reach the database without atrium_ddns.scope.get_scope. "
        f"Add the dependency, or add the route to UNSCOPED_ROUTES with a "
        f"written reason."
    )
    # And the exemption list does not outlive its routes: an entry for a
    # path that no longer exists is a stale claim nobody would notice.
    live = {_route_key(route) for route in router.routes}
    assert set(UNSCOPED_ROUTES) <= live, (
        f"UNSCOPED_ROUTES names routes that no longer exist: "
        f"{sorted(set(UNSCOPED_ROUTES) - live)}"
    )


async def test_no_hand_written_tenancy_predicate_in_the_router():
    """The rule ``scope.py`` states, checked against the source.

    ``DdnsScope`` exists because *"the one that forgets is the leak, and
    it will be the one written against a table added after this file was
    reviewed"*. A comparison against a ``user_id`` column anywhere in
    this module is that forgetting; assignment (``Domain(user_id=…)``)
    is not, and the two are distinguishable by the operator.
    """
    import inspect
    import re

    source = inspect.getsource(tenant_router)
    # Strip docstrings and comments: this file's own prose says
    # `user_id` a dozen times, and a guard that fails on a comment gets
    # deleted rather than investigated.
    code = re.sub(r"#.*", "", source)
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    offenders = re.findall(r"[\w.]*user_id\s*==[^\n]*", code)
    assert offenders == [], (
        f"hand-written tenancy predicates in atrium_ddns.router: {offenders}. "
        f"Every filter goes through DdnsScope."
    )
    # Vacuity: the sweep must actually be reading the module.
    assert "def create_domain" in code, "the source sweep read nothing"


async def test_one_tenants_rows_are_invisible_and_immutable_to_another(
    tenants: dict[str, Any],
):
    a, b = tenants["a"], tenants["b"]

    async with _client(b["user"]) as client:
        created = await _make_backend(client, b["domain_id"], credentials=_creds(CANARY))
        assert created.status_code == 201, created.text
        b_backend_id = created.json()["id"]
        device = await client.post(
            "/api/atrium_ddns/devices", json={"name": f"b-router-{W}"}
        )
        assert device.status_code == 201, device.text
        b_device_id = device.json()["device"]["id"]

    async with _client(a["user"]) as client:
        domains = await client.get("/api/atrium_ddns/domains")
        assert domains.status_code == 200
        assert [d["name"] for d in domains.json()] == [a["domain_name"]]

        devices = await client.get("/api/atrium_ddns/devices")
        assert devices.status_code == 200
        assert devices.json() == []

        # Every mutating verb, and every one of them 404 rather than
        # 403: a 403 would confirm the row exists.
        assert (
            await client.patch(
                f"/api/atrium_ddns/backends/{b_backend_id}", json={"config": {}}
            )
        ).status_code == 404
        assert (
            await client.delete(f"/api/atrium_ddns/backends/{b_backend_id}")
        ).status_code == 404
        assert (
            await client.delete(f"/api/atrium_ddns/domains/{b['domain_id']}")
        ).status_code == 404
        assert (
            await client.post(f"/api/atrium_ddns/devices/{b_device_id}/rotate")
        ).status_code == 404
        assert (
            await client.delete(f"/api/atrium_ddns/devices/{b_device_id}")
        ).status_code == 404
        assert (
            await _make_backend(client, b["domain_id"])
        ).status_code == 404

    # …and B's rows are all still there.
    assert await _ciphertext(b_backend_id) is not None


async def test_the_gate_bites_in_both_directions(tenants: dict[str, Any]):
    """Asserted on the status code, not on the shape of the body.

    A permission test that passes on a failed request is the defect
    ``overnight-template.md`` records under "assertions on the report":
    19 cells passing on an account that could not log in at all.
    """
    a = tenants["a"]
    async with _client(a["user"], permissions=set()) as client:
        assert (await client.get("/api/atrium_ddns/domains")).status_code == 403
        assert (await client.get("/api/atrium_ddns/devices")).status_code == 403
        assert (await client.get("/api/atrium_ddns/providers")).status_code == 403
        assert (
            await client.post("/api/atrium_ddns/domains", json={"name": "x.invalid"})
        ).status_code == 403

    async with _client(a["user"], permissions={DOMAIN_MANAGE_PERMISSION}) as client:
        assert (await client.get("/api/atrium_ddns/domains")).status_code == 200
        # Holding the domain permission is not holding the device one.
        assert (await client.get("/api/atrium_ddns/devices")).status_code == 403

    async with _client(a["user"], permissions={DEVICE_MANAGE_PERMISSION}) as client:
        assert (await client.get("/api/atrium_ddns/devices")).status_code == 200
        assert (await client.get("/api/atrium_ddns/domains")).status_code == 403


async def test_a_backend_is_owned_by_the_domain_not_by_the_caller(
    tenants: dict[str, Any],
):
    """The line an administrator's request would otherwise get wrong.

    A caller holding ``atrium_ddns.admin`` reaches every tenant's rows.
    Taking ``user_id`` from the scope would encrypt the credential under
    the *administrator's* key — and then ``/nic/update``, which unlocks
    the device owner's key, could not read it, and the scope would hide
    the row from the tenant who owns the zone. Two silent failures.
    """
    admin, b = tenants["a"], tenants["b"]
    async with _client(
        admin["user"], permissions=ALL_PERMS | {CROSS_TENANT_PERMISSION}
    ) as client:
        created = await _make_backend(
            client, b["domain_id"], credentials=_creds(CANARY)
        )
        assert created.status_code == 201, created.text
        backend_id = created.json()["id"]

    factory = get_session_factory()
    async with factory() as s:
        owner = (
            await s.execute(
                sa.text("SELECT user_id FROM ddns_domain_backend WHERE id = :i"),
                {"i": backend_id},
            )
        ).scalar_one()
    assert owner == b["user_id"], (
        f"the credential row is owned by {owner}, the caller, rather than by "
        f"{b['user_id']}, the zone's owner — it is encrypted under the wrong key"
    )

    # And the decisive half: it decrypts under B's key, which is the one
    # /nic/update unlocks.
    assert await _plaintext(backend_id) == _creds(CANARY)

    # …and B can see it through their own scope.
    async with _client(b["user"]) as client:
        payload = (await client.get("/api/atrium_ddns/domains")).json()
    assert [backend["id"] for d in payload for backend in d["backends"]] == [backend_id]


# ===================================================================== #
# 2. No endpoint returns a credential in cleartext
# ===================================================================== #


def _property_names(schema: dict[str, Any], components: dict[str, Any]) -> set[str]:
    """Every property name reachable from a response model's schema.

    Recursive through ``$ref``, ``items``, ``anyOf`` and nested objects,
    because a leak one level down is still a leak — the flat reading is
    the version of this check that would have passed.
    """
    seen: set[str] = set()
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str):
            key = ref.rsplit("/", 1)[-1]
            if key not in seen:
                seen.add(key)
                walk(components.get(key, {}))
            return
        for key, value in node.get("properties", {}).items():
            names.add(key)
            walk(value)
        for key in ("items", "anyOf", "oneOf", "allOf", "additionalProperties"):
            if key in node:
                walk(node[key])

    walk(schema)
    return names


async def test_no_response_model_declares_a_provider_credential_field():
    """A sweep over the router's own routes, to a negative result.

    Derived from ``providers`` rather than from a list typed here, so a
    provider added next year is covered by this test without it being
    edited. "We looked, and there are exactly zero" closes the question;
    "here are the three we found" invites the next agent to rediscover
    it.
    """
    application = FastAPI()
    application.include_router(router)
    schema = application.openapi()
    components = schema.get("components", {}).get("schemas", {})

    secret_keys = _all_secret_keys()
    assert secret_keys, "no provider declares a credential key — this is vacuous"

    leaks: dict[str, list[str]] = {}
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for code, response in operation.get("responses", {}).items():
                if not code.startswith("2"):
                    continue
                content = response.get("content", {}).get("application/json", {})
                found = _property_names(content.get("schema", {}), components)
                offending = sorted(found & secret_keys)
                if offending:
                    leaks[f"{method.upper()} {path}"] = offending
    assert leaks == {}, f"credential-shaped fields on response models: {leaks}"


async def test_the_only_route_that_returns_a_secret_is_create_and_rotate():
    """`secret` is allowed to exist on exactly two responses.

    Read off the schema rather than off a list of paths, so the check
    survives a rename of the handler and fails on a *third* route
    growing the field.
    """
    application = FastAPI()
    application.include_router(router)
    schema = application.openapi()
    components = schema.get("components", {}).get("schemas", {})

    carriers: list[str] = []
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for code, response in operation.get("responses", {}).items():
                if not code.startswith("2"):
                    continue
                content = response.get("content", {}).get("application/json", {})
                if "secret" in _property_names(content.get("schema", {}), components):
                    carriers.append(f"{method.upper()} {path}")
    assert sorted(carriers) == [
        "POST /api/atrium_ddns/devices",
        "POST /api/atrium_ddns/devices/{device_id}/rotate",
    ], carriers


async def test_no_endpoint_body_carries_a_stored_credential(tenants: dict[str, Any]):
    """Behavioural half. The canary is a value nothing else uses."""
    a = tenants["a"]
    async with _client(a["user"]) as client:
        created = await _make_backend(client, a["domain_id"], credentials=_creds(CANARY))
        assert created.status_code == 201, created.text
        backend_id = created.json()["id"]

        # The credential really is stored — otherwise every assertion
        # below is about an empty column.
        assert await _plaintext(backend_id) == _creds(CANARY)

        bodies = {
            "create": created.text,
            "domains": (await client.get("/api/atrium_ddns/domains")).text,
            "providers": (await client.get("/api/atrium_ddns/providers")).text,
            "patch": (
                await client.patch(
                    f"/api/atrium_ddns/backends/{backend_id}",
                    json={"config": {"ttl": 120}},
                )
            ).text,
            "devices": (await client.get("/api/atrium_ddns/devices")).text,
        }

    for where, body in bodies.items():
        assert CANARY not in body, f"{where} returned the credential in cleartext"
        for value in _creds(CANARY).values():
            assert value not in body, f"{where} returned {value!r}"
    # `credentials_set` is the boolean that replaces it, and it is true —
    # so the absence above is not the absence of a credential.
    assert '"credentials_set":true' in bodies["domains"].replace(" ", "")


async def test_the_listing_answers_for_a_tenant_whose_key_is_shredded(
    tenants: dict[str, Any],
):
    """The read path *cannot* decrypt, rather than merely does not.

    A listing that reached the plaintext would raise once the key is
    gone. Shredding is permanent and by design, which is why this runs
    against a tenant the fixture created for the purpose.
    """
    s_tenant = tenants["s"]
    async with _client(s_tenant["user"]) as client:
        created = await _make_backend(
            client, s_tenant["domain_id"], credentials=_creds(CANARY)
        )
        assert created.status_code == 201, created.text
        backend_id = created.json()["id"]
    assert await _ciphertext(backend_id) is not None

    factory = get_session_factory()
    async with factory() as session:
        assert await shred_user_key(session, s_tenant["user_id"]) is True
        await session.commit()

    async with _client(s_tenant["user"]) as client:
        listing = await client.get("/api/atrium_ddns/domains")
    assert listing.status_code == 200, listing.text
    backends = [b for d in listing.json() for b in d["backends"]]
    assert [b["id"] for b in backends] == [backend_id]
    # It still reports that a credential is stored — which is true, and
    # is a different fact from "it is readable".
    assert backends[0]["credentials_set"] is True


# ===================================================================== #
# 3. Rotate shows the secret once
# ===================================================================== #


async def test_the_second_read_does_not_carry_the_secret(tenants: dict[str, Any]):
    """Create, rotate, and prove both secrets are gone from every read.

    The verification at the end is what makes the absence mean
    something: without it, an implementation that returned a fresh
    random string and never touched the row would pass every assertion
    above it.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        created = await client.post(
            "/api/atrium_ddns/devices", json={"name": f"router-{W}"}
        )
        assert created.status_code == 201, created.text
        first = created.json()["secret"]
        device_id = created.json()["device"]["id"]
        assert created.json()["device"]["credential_origin"] == "issued"

        listing = await client.get("/api/atrium_ddns/devices")
        assert listing.status_code == 200
        assert "secret" not in listing.text
        assert first not in listing.text
        assert set(listing.json()[0]) == {
            "id",
            "name",
            "username",
            "created_at",
            "last_seen_at",
            "rate_limit_per_minute",
            "credential_origin",
            "hostname_count",
        }

        rotated = await client.post(f"/api/atrium_ddns/devices/{device_id}/rotate")
        assert rotated.status_code == 200, rotated.text
        second = rotated.json()["secret"]
        assert second != first

        after = await client.get("/api/atrium_ddns/devices")
        assert first not in after.text
        assert second not in after.text
        assert "secret" not in after.text

    stored = await _stored_hash(device_id)
    verified_new, _ = await verify_password(stored, second)
    verified_old, _ = await verify_password(stored, first)
    assert verified_new is True, "the rotated secret does not authenticate"
    assert verified_old is False, "the previous secret still authenticates"


async def _stored_hash(device_id: int) -> str:
    factory = get_session_factory()
    async with factory() as s:
        return (
            await s.execute(
                sa.text("SELECT password_hash FROM ddns_device WHERE id = :i"),
                {"i": device_id},
            )
        ).scalar_one()


async def test_neither_secret_reaches_the_audit_log(tenants: dict[str, Any]):
    """The audit table is durable and has a retention policy.

    ``app.services.audit`` redacts a ``MaskedSecret`` to ``***``, but a
    plain ``str`` sails straight through it — so this asserts on the
    stored rows rather than on the intent.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        created = await client.post(
            "/api/atrium_ddns/devices", json={"name": f"audited-{W}"}
        )
        first = created.json()["secret"]
        device_id = created.json()["device"]["id"]
        rotated = await client.post(f"/api/atrium_ddns/devices/{device_id}/rotate")
        second = rotated.json()["secret"]

    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                sa.select(AuditLog.action, AuditLog.diff).where(
                    AuditLog.entity == "ddns_device",
                    AuditLog.entity_id == device_id,
                )
            )
        ).all()
    assert {row.action for row in rows} == {"create", "rotate_secret"}, rows
    blob = repr(rows)
    assert first not in blob
    assert second not in blob
    # …and the rows say something, so the absence is not the absence of
    # an audit trail.
    assert "issued" in blob


# ===================================================================== #
# 4. Blank-preserves — clear, preserve, replace
# ===================================================================== #


async def test_blank_preserves_in_all_three_directions(tenants: dict[str, Any]):
    """One backend, four requests, two instruments on every step.

    The ciphertext bytes are the instrument the plaintext read cannot
    replace. Encryption is nonced, so re-encrypting the same credential
    produces *different bytes with the same plaintext* — which is
    exactly what a well-meaning implementation that reassigns on every
    PATCH produces, and exactly what a plaintext-only assertion cannot
    see.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        created = await _make_backend(client, a["domain_id"], credentials=_creds(CANARY))
        assert created.status_code == 201, created.text
        backend_id = created.json()["id"]

        stored = await _ciphertext(backend_id)
        assert stored is not None
        assert await _plaintext(backend_id) == _creds(CANARY)

        # --- preserve: the field is absent entirely --------------- #
        response = await client.patch(
            f"/api/atrium_ddns/backends/{backend_id}", json={"config": {"ttl": 300}}
        )
        assert response.status_code == 200, response.text
        assert response.json()["config"] == {"ttl": 300}
        assert response.json()["credentials_set"] is True
        assert await _plaintext(backend_id) == _creds(CANARY)
        assert await _ciphertext(backend_id) == stored, (
            "the ciphertext changed on a request that did not mention the "
            "credential — it was re-encrypted rather than preserved"
        )

        # --- preserve: the field is present and is "" -------------- #
        response = await client.patch(
            f"/api/atrium_ddns/backends/{backend_id}",
            json={"config": {"ttl": 301}, "credentials": PRESERVE},
        )
        assert response.status_code == 200, response.text
        assert await _plaintext(backend_id) == _creds(CANARY)
        assert await _ciphertext(backend_id) == stored

        # --- replace ---------------------------------------------- #
        response = await client.patch(
            f"/api/atrium_ddns/backends/{backend_id}",
            json={"credentials": _creds(CANARY_TWO)},
        )
        assert response.status_code == 200, response.text
        assert response.json()["credentials_set"] is True
        assert await _plaintext(backend_id) == _creds(CANARY_TWO)
        replaced = await _ciphertext(backend_id)
        assert replaced is not None and replaced != stored
        # The config was not mentioned, so it is untouched — the mirror
        # of the preserve above, on the plaintext field.
        assert response.json()["config"] == {"ttl": 301}

        # --- clear ------------------------------------------------- #
        response = await client.patch(
            f"/api/atrium_ddns/backends/{backend_id}", json={"credentials": None}
        )
        assert response.status_code == 200, response.text
        assert response.json()["credentials_set"] is False
        assert await _ciphertext(backend_id) is None
        assert await _plaintext(backend_id) is None

        # …and a preserve after a clear preserves the *cleared* state
        # rather than resurrecting anything.
        response = await client.patch(
            f"/api/atrium_ddns/backends/{backend_id}", json={"config": {"ttl": 60}}
        )
        assert response.json()["credentials_set"] is False
        assert await _ciphertext(backend_id) is None


async def test_replacing_only_the_credential_actually_persists(
    tenants: dict[str, Any],
):
    """The upstream no-op, guarded from the outside.

    ``UserSecret.__set__`` writes the plaintext into a private
    ``__dict__`` slot and leaves the mapped column alone, so the row is
    not dirty and ``_encrypt_pending_user_secrets`` — which iterates
    ``session.new | session.dirty`` — never visits it. Measured against
    atrium 0.28 in this image with a throwaway table::

        A. set on a NEW object            -> STORED
        B. set on a CLEAN persistent row  -> discarded, silently
        C. set alongside a column change  -> stored

    Case B *is* this request: a form that changes the credential and
    nothing else, which is the most common reason to open a credential
    form at all. The commit succeeds, the response is 200, and the
    stored ciphertext is the old one — the mirror image of the
    blank-preserves hazard, and equally invisible from the response.

    Written as its own test rather than folded into the three-direction
    one because it fails for a *different reason*: the three-direction
    test would still pass if a config change were sent alongside, which
    is exactly case C.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        backend_id = (
            await _make_backend(client, a["domain_id"], credentials=_creds(CANARY))
        ).json()["id"]
        before = await _ciphertext(backend_id)

        # `credentials` and nothing else. No config key, no other field.
        response = await client.patch(
            f"/api/atrium_ddns/backends/{backend_id}",
            json={"credentials": _creds(CANARY_TWO)},
        )
        assert response.status_code == 200, response.text

    assert await _plaintext(backend_id) == _creds(CANARY_TWO), (
        "the replacement was discarded: the request answered 200 and the "
        "stored ciphertext is still the old credential"
    )
    assert await _ciphertext(backend_id) != before


async def test_the_audit_row_names_which_of_the_three_happened(
    tenants: dict[str, Any],
):
    """`preserved` is recorded, not merely implied by an absent entry.

    An audit trail that writes nothing when a credential is untouched
    reads identically to one written by a build that forgot to preserve
    it.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        backend_id = (
            await _make_backend(client, a["domain_id"], credentials=_creds(CANARY))
        ).json()["id"]
        await client.patch(
            f"/api/atrium_ddns/backends/{backend_id}", json={"config": {"ttl": 90}}
        )
        await client.patch(
            f"/api/atrium_ddns/backends/{backend_id}",
            json={"credentials": _creds(CANARY_TWO)},
        )
        await client.patch(
            f"/api/atrium_ddns/backends/{backend_id}", json={"credentials": None}
        )

    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                sa.select(AuditLog.diff)
                .where(
                    AuditLog.entity == "ddns_domain_backend",
                    AuditLog.entity_id == backend_id,
                )
                .order_by(AuditLog.id)
            )
        ).scalars().all()
    assert [row["credentials"] for row in rows] == [
        "replaced",
        "preserved",
        "replaced",
        "cleared",
    ]
    for row in rows:
        assert CANARY not in repr(row)
        assert CANARY_TWO not in repr(row)


async def test_an_empty_object_and_a_blank_value_are_both_refused(
    tenants: dict[str, Any],
):
    """The two spellings of "the form sent an empty string meaning empty".

    ``{}`` is ambiguous between clear and preserve; a blank value inside
    the object is a text box the user cleared. Storing either replaces a
    working credential with nothing, which is the failure the whole rule
    exists to prevent — arriving one level below the field it guards.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        backend_id = (
            await _make_backend(client, a["domain_id"], credentials=_creds(CANARY))
        ).json()["id"]

        empty = await client.patch(
            f"/api/atrium_ddns/backends/{backend_id}", json={"credentials": {}}
        )
        assert empty.status_code == 422, empty.text
        assert "ambiguous" in empty.text

        keys = _keys()
        blanked = dict(_creds(CANARY_TWO))
        blanked[keys[0]] = ""
        blank = await client.patch(
            f"/api/atrium_ddns/backends/{backend_id}", json={"credentials": blanked}
        )
        assert blank.status_code == 422, blank.text
        assert keys[0] in blank.text
        # The refusal names the key and never the value.
        assert blanked[keys[-1]] not in blank.text

        # Neither refusal touched the stored credential.
        assert await _plaintext(backend_id) == _creds(CANARY)


async def test_no_refusal_echoes_the_credential_it_is_refusing(
    tenants: dict[str, Any],
):
    """The defect that shaped ``_CredentialsField``'s ``Any`` typing.

    The obvious model — ``dict[str, str] | Literal[""] | None`` with a
    ``@field_validator`` — was written first, and its 422 came back
    carrying the submitted credential::

        {"detail":[{…,"input":{"aws_access_key_id":"AKIA…", …}}]}

    FastAPI's ``RequestValidationError`` handler serialises
    ``exc.errors()`` and every entry carries ``input``, the value that
    failed. So a *validation* rejection publishes the credential to the
    response body, the browser's network tab, and anything that logs
    response bodies — the guard committing the disclosure it exists to
    prevent.

    Every malformed shape is driven here, including the three that used
    to echo, and the assertion is that no refusal body contains any
    submitted value. It is asserted on the **body**, not on the model
    definition: a later change that reintroduces a Pydantic-level
    constraint on this field fails here rather than in a review.
    """
    a = tenants["a"]
    keys = _keys()
    secret = "AKIA-DO-NOT-ECHO-THIS-VALUE"
    shapes: dict[str, Any] = {
        "empty object": {},
        "blank value": {keys[0]: "", keys[-1]: secret},
        "non-string value": {keys[0]: 12345, keys[-1]: secret},
        "wrong type entirely": [secret],
        "partial set": {keys[0]: secret},
    }
    async with _client(a["user"]) as client:
        backend_id = (
            await _make_backend(client, a["domain_id"], credentials=_creds(CANARY))
        ).json()["id"]
        for label, payload in shapes.items():
            response = await client.patch(
                f"/api/atrium_ddns/backends/{backend_id}",
                json={"credentials": payload},
            )
            assert response.status_code == 422, f"{label}: {response.text}"
            assert secret not in response.text, (
                f"the refusal for {label!r} echoed the credential it refused"
            )
            for value in _creds(CANARY).values():
                assert value not in response.text, label

        # The same shape posted to *create*, which is a different model.
        created = await _make_backend(
            client,
            a["domain_id"],
            backend_type="hetzner",
            credentials={keys[0]: "", keys[-1]: secret},
        )
        assert created.status_code == 422, created.text
        assert secret not in created.text

    # …and nothing was stored by any of them.
    assert await _plaintext(backend_id) == _creds(CANARY)


async def test_a_partial_credential_set_is_refused_when_replacing(
    tenants: dict[str, Any],
):
    """A row that fails ``BaseProvider.has_credentials`` answers
    ``dnserr`` on every update. Better a 422 now than a DNS failure
    later with nothing pointing at the cause."""
    keys = _keys()
    if len(keys) < 2:  # pragma: no cover — route53 has two
        pytest.skip(f"{SERVICE} has a single credential key")
    a = tenants["a"]
    async with _client(a["user"]) as client:
        response = await _make_backend(
            client, a["domain_id"], credentials={keys[0]: "only-half"}
        )
    assert response.status_code == 422, response.text
    assert keys[1] in response.text


# ===================================================================== #
# The plaintext config column, and the migrated device
# ===================================================================== #


async def test_a_credential_key_may_not_be_written_into_the_plaintext_config(
    tenants: dict[str, Any],
):
    """``ddns_domain_backend.config`` has no encryption on it.

    ``nsupdate`` already logs ``provider.secret_in_plaintext_config``
    when it finds one there — i.e. the shape is known to happen and the
    current handling is to notice it *after* it is stored. Refused at
    the door instead, against the union across every provider: an AWS
    key in an nsupdate row's config is still an AWS key in a plaintext
    column.
    """
    a = tenants["a"]
    keys = _keys()
    async with _client(a["user"]) as client:
        response = await _make_backend(
            client,
            a["domain_id"],
            config={"ttl": 60, keys[0]: "AKIA-in-the-wrong-column"},
            credentials=_creds(CANARY),
        )
    assert response.status_code == 422, response.text
    assert keys[0] in response.text
    assert "AKIA-in-the-wrong-column" not in response.text, (
        "the refusal echoed the value it was refusing — committing the "
        "disclosure it exists to prevent"
    )


async def test_a_migrated_device_is_described_rather_than_blanked(
    tenants: dict[str, Any],
):
    """A bcrypt row's plaintext was never ours, and the API says so.

    The interface renders this sentence instead of an empty secret
    field. Rotating is the only way such a device acquires a secret
    anyone knows, and doing so moves it to ``issued`` — asserted here,
    because "the label never changes" would pass a test that only
    checked the initial value.
    """
    from pwdlib.hashers.bcrypt import BcryptHasher

    a = tenants["a"]
    factory = get_session_factory()
    async with factory() as s:
        device = m.Device(
            user_id=a["user_id"],
            username=f"legacy-{W}",
            password_hash=BcryptHasher().hash("the-old-services-secret"),
            name=f"migrated-{W}",
        )
        s.add(device)
        await s.flush()
        device_id = device.id
        await s.commit()

    async with _client(a["user"]) as client:
        listing = (await client.get("/api/atrium_ddns/devices")).json()
        row = next(r for r in listing if r["id"] == device_id)
        assert row["credential_origin"] == "migrated"

        rotated = await client.post(f"/api/atrium_ddns/devices/{device_id}/rotate")
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["device"]["credential_origin"] == "issued"

        listing = (await client.get("/api/atrium_ddns/devices")).json()
        row = next(r for r in listing if r["id"] == device_id)
        assert row["credential_origin"] == "issued"

    # The old secret stopped working, which is the sharp edge the
    # interface has to warn about before the button is pressed.
    stored = await _stored_hash(device_id)
    verified, _ = await verify_password(stored, "the-old-services-secret")
    assert verified is False


async def test_credential_origin_is_three_states_and_asks_the_hashers():
    """Derived from ``auth_device._PASSWORD_HASH``, not from a prefix.

    The three inputs are the three real cases: a hash this build issues,
    a hash it verifies but does not issue, and something no hasher
    recognises — which cannot authenticate either
    (``auth_device._verify_sync`` answers ``badauth`` for it), so
    rendering it as a working device would be a lie.
    """
    from pwdlib.hashers.bcrypt import BcryptHasher

    from atrium_ddns.auth_device import _PASSWORD_HASH

    issued = _PASSWORD_HASH.current_hasher.hash("x")
    assert credential_origin(issued) is CredentialOrigin.ISSUED
    assert (
        credential_origin(BcryptHasher().hash("x")) is CredentialOrigin.MIGRATED
    )
    assert credential_origin("not-a-hash") is CredentialOrigin.UNRECOGNISED
    assert credential_origin("") is CredentialOrigin.UNRECOGNISED
    assert credential_origin(None) is CredentialOrigin.UNRECOGNISED
    # The classification follows the tuple rather than a `$argon2`
    # prefix: whatever the current hasher is, its own output is
    # `issued` and every *other* verifying hasher's output is
    # `migrated`. Derived, so reordering the tuple moves this with it.
    assert len(_PASSWORD_HASH.hashers) >= 2, (
        "only one hasher is configured, so `migrated` is unreachable and "
        "this test is vacuous"
    )


# ===================================================================== #
# The provider catalogue, and the ordinary CRUD paths
# ===================================================================== #


async def test_the_catalogue_is_derived_from_the_provider_registry(
    tenants: dict[str, Any],
):
    """The form's field list comes from the classes, not from TypeScript.

    A hardcoded field list in the frontend is the identical defect one
    release later; this is the endpoint that stops it existing.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        response = await client.get("/api/atrium_ddns/providers")
    assert response.status_code == 200, response.text
    payload = response.json()["providers"]
    assert [p["service"] for p in payload] == list(known_services())
    for entry in payload:
        cls = provider_class(entry["service"])
        assert cls is not None
        assert entry["credential_keys"] == list(cls.REQUIRED_CREDENTIALS)
    # Aliases resolve but are not offered — a new row minted under one
    # would be a second spelling of one provider inside a
    # UNIQUE(domain_id, backend_type) constraint.
    assert "aws" not in [p["service"] for p in payload]


async def test_domains_round_trip_and_normalise(tenants: dict[str, Any]):
    a = tenants["a"]
    async with _client(a["user"]) as client:
        created = await client.post(
            "/api/atrium_ddns/domains", json={"name": f"  MiXeD-{W}.Example.INVALID.  "}
        )
        assert created.status_code == 201, created.text
        assert created.json()["name"] == f"mixed-{W}.example.invalid"
        domain_id = created.json()["id"]

        # The UNIQUE index is the only thing stopping two spellings
        # becoming two zones, and an index cannot normalise.
        again = await client.post(
            "/api/atrium_ddns/domains", json={"name": f"MIXED-{W}.example.invalid"}
        )
        assert again.status_code == 409, again.text

        assert (
            await client.delete(f"/api/atrium_ddns/domains/{domain_id}")
        ).status_code == 204
        names = [d["name"] for d in (await client.get("/api/atrium_ddns/domains")).json()]
        assert f"mixed-{W}.example.invalid" not in names


async def test_a_zone_claimed_by_another_tenant_is_a_conflict_that_names_nobody(
    tenants: dict[str, Any],
):
    """DNS is global, so the conflict is real — but the message is not a
    disclosure. "Already claimed" and "already claimed by you" are the
    same sentence on purpose."""
    a, b = tenants["a"], tenants["b"]
    async with _client(a["user"]) as client:
        response = await client.post(
            "/api/atrium_ddns/domains", json={"name": b["domain_name"]}
        )
    assert response.status_code == 409, response.text
    assert b["email"] not in response.text
    assert str(b["user_id"]) not in response.json()["detail"]


async def test_an_unknown_service_is_refused_on_create_but_the_column_still_holds_one(
    tenants: dict[str, Any],
):
    """The database must be able to hold a migrated row naming a service
    this build does not ship (``models.py`` says so, and the compat
    table's ``unknownsvc`` depends on it). A row *minted here* could only
    ever answer ``911``, so this endpoint refuses it — and the listing
    reports ``known_service: false`` for one that arrived another way."""
    a = tenants["a"]
    async with _client(a["user"]) as client:
        refused = await _make_backend(client, a["domain_id"], backend_type="unknownsvc")
    assert refused.status_code == 422, refused.text
    assert "unknownsvc" in refused.text

    factory = get_session_factory()
    async with factory() as s:
        row = m.DomainBackend(
            domain_id=a["domain_id"],
            user_id=a["user_id"],
            backend_type="unknownsvc",
            config={},
        )
        s.add(row)
        await s.flush()
        row_id = row.id
        await s.commit()

    async with _client(a["user"]) as client:
        backends = [
            b
            for d in (await client.get("/api/atrium_ddns/domains")).json()
            for b in d["backends"]
        ]
    entry = next(b for b in backends if b["id"] == row_id)
    assert entry["known_service"] is False
    assert entry["credential_keys"] == []
    assert entry["credentials_set"] is False


async def test_deleting_a_device_orphans_its_hostnames_rather_than_destroying_them(
    tenants: dict[str, Any],
):
    """``ON DELETE SET NULL`` by design — deleting a router must not
    destroy the names it was maintaining, and the board lists an
    unassigned hostname rather than dropping it."""
    a = tenants["a"]
    async with _client(a["user"]) as client:
        device_id = (
            await client.post("/api/atrium_ddns/devices", json={"name": f"doomed-{W}"})
        ).json()["device"]["id"]

    factory = get_session_factory()
    async with factory() as s:
        hostname = m.Hostname(
            domain_id=a["domain_id"],
            device_id=device_id,
            name=f"orphan-{W}.{a['domain_name']}",
        )
        s.add(hostname)
        await s.flush()
        hostname_id = hostname.id
        await s.commit()

    async with _client(a["user"]) as client:
        listing = (await client.get("/api/atrium_ddns/devices")).json()
        assert next(r for r in listing if r["id"] == device_id)["hostname_count"] == 1
        assert (
            await client.delete(f"/api/atrium_ddns/devices/{device_id}")
        ).status_code == 204

    async with factory() as s:
        row = await s.get(m.Hostname, hostname_id)
        assert row is not None, "the hostname was destroyed with its device"
        assert row.device_id is None
