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

import base64
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
from conftest import fixture_writes, purge_tenants, unusable_password_hash
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


@pytest_asyncio.fixture
async def tenants() -> AsyncIterator[dict[str, Any]]:
    """Three tenants: A, B (the cross-tenant control) and S (shreddable).

    Three rather than two because one of the tests destroys an
    encryption key permanently, and a test that needs to destroy
    something must create the thing it destroys —
    ``test_user_scope_secrets.py`` learned that by destroying a seeded
    admin's key.
    """
    tags = ("a", "b", "s")
    emails = [f"ddns-tenant-{tag}-{W}@example.invalid" for tag in tags]
    await purge_tenants(emails, owner="test_router_tenant.tenants")

    hashed = unusable_password_hash()
    built: dict[str, Any] = {"emails": emails}
    async with fixture_writes("test_router_tenant.tenants") as s:
        for tag, email in zip(tags, emails):
            user = User(
                email=email,
                hashed_password=hashed,
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

    yield built

    await purge_tenants(emails, owner="test_router_tenant.tenants")


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
    "GET /api/atrium_ddns/config/schema": (
        "#73. The *shape* of a global config namespace — the types, "
        "bounds and defaults read out of DdnsConfig's own JSON schema. "
        "It declares no session and reads no table, so there is no "
        "tenant row for a scope to filter; it is gated on atrium's "
        "`app_setting.manage` instead, the same permission that gates "
        "the values themselves. See tests/test_settings_schema.py"
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


#: ``user_id ==`` comparisons in ``router.py`` that are **not** tenancy,
#: each with a written reason. Same shape as ``UNSCOPED_ROUTES`` above,
#: and for the same purpose: an exemption nobody has to justify is not an
#: exemption, it is a hole.
#:
#: Keyed by the exact matched text, so a *different* comparison against
#: the same column is still an offender. An entry that stops matching is
#: caught by the staleness assertion at the bottom of the test — the
#: rule this file already applies to ``UNSCOPED_ROUTES``.
ALLOWED_USER_ID_COMPARISONS: dict[str, str] = {
    "DnsEvent.user_id == filters.user_id)": (
        "#46's log-search *filter*, not a tenancy predicate, and the "
        "difference is the whole reason this entry is written down. The "
        "statement it is added to is `scope.select(DnsEvent)`, so the "
        "scope's own predicate is already on it; this narrows an "
        "already-scoped query to one tenant *by request*, which is a "
        "search feature and not an authorisation decision. Deleting the "
        "line would remove a filter, not close a leak. "
        "Two things hold the safety property instead of this guard: "
        "`get_events` refuses with 403 when a caller without "
        "`atrium_ddns.events.read.all` names another tenant "
        "(`test_filtering_to_another_tenant_is_refused_not_narrowed`), "
        "and dropping the scope from that statement reds nine tests in "
        "`test_router_events.py` — measured by mutation, not assumed."
    ),
    "AuditLog.actor_user_id == actor_user_id,": (
        "#75's health-check debounce, and the row it compares is not a "
        "host table at all: `audit_log` is atrium's, it has no "
        "`DdnsScope` tenancy path, and `classify()` would refuse it — "
        "correctly, because those rows are *actor* history rather than "
        "tenant data. The comparison is the debounce key. It narrows "
        "nothing a tenant could otherwise reach: the statement selects "
        "one aggregate (`MAX(created_at)`) over the requesting user's "
        "own audit rows for one entity, returns a timestamp and no row, "
        "and the only thing that value can do is make the caller's own "
        "request fail with 429. Deleting the line would widen the "
        "debounce to installation-wide — one operator's press blocking "
        "every other tenant's board — which is a bug in the opposite "
        "direction from a leak. The tenancy of the *work* the endpoint "
        "then does belongs to the scope, and "
        "`test_router_health_checks.py` asserts it against a second "
        "tenant's rows."
    ),
}


async def test_no_hand_written_tenancy_predicate_in_the_router():
    """The rule ``scope.py`` states, checked against the source.

    ``DdnsScope`` exists because *"the one that forgets is the leak, and
    it will be the one written against a table added after this file was
    reviewed"*. A comparison against a ``user_id`` column anywhere in
    this module is that forgetting; assignment (``Domain(user_id=…)``)
    is not, and the two are distinguishable by the operator.

    **One class of comparison is neither**, and #46 found it: a
    ``?user_id=`` search filter applied *on top of* a scoped statement.
    The pattern this guard matches on is syntactic (``user_id ==``) and
    the property it means is *tenancy is enforced by the scope*, and
    those came apart the first time somebody wanted to filter by user
    rather than to authorise by user. Rather than widen the pattern —
    which would stop it catching ``user_id == scope.user_id``, the
    genuinely dangerous spelling — the exceptions are enumerated with
    reasons.
    """
    import inspect
    import re

    source = inspect.getsource(tenant_router)
    # Strip docstrings and comments: this file's own prose says
    # `user_id` a dozen times, and a guard that fails on a comment gets
    # deleted rather than investigated.
    code = re.sub(r"#.*", "", source)
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    found = re.findall(r"[\w.]*user_id\s*==[^\n]*", code)
    offenders = [
        match for match in found if match.strip() not in ALLOWED_USER_ID_COMPARISONS
    ]
    assert offenders == [], (
        f"hand-written tenancy predicates in atrium_ddns.router: {offenders}. "
        f"Every filter goes through DdnsScope. If one of these is a search "
        f"filter on an already-scoped statement rather than a tenancy "
        f"predicate, add it to ALLOWED_USER_ID_COMPARISONS with a reason."
    )
    # Vacuity: the sweep must actually be reading the module.
    assert "def create_domain" in code, "the source sweep read nothing"
    # And the exemptions do not outlive the lines they exempt. A stale
    # entry silently widens the guard's blind spot by exactly one
    # spelling, which is how an allowlist stops being an allowlist.
    stripped = {match.strip() for match in found}
    assert set(ALLOWED_USER_ID_COMPARISONS) <= stripped, (
        f"ALLOWED_USER_ID_COMPARISONS names comparisons that are no longer "
        f"in router.py: {sorted(set(ALLOWED_USER_ID_COMPARISONS) - stripped)}"
    )


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
            # #73. The stored value and the resolved one, both, because
            # "30/min" and "30/min because nobody set one" are different
            # facts.
            "effective_rate_limit_per_minute",
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


# ===================================================================== #
# 6b. The zone and its first provider, in one submission (#88)
#
# `docs/ops/ui-design.md` Part II §10.1. The create path could only ever
# produce a zone with no provider bound to it, and that is not an
# incomplete draft: `tests/compat/protocol_cases.yaml:211`
# (`update/no-backends-911`) freezes it as **911 for every update under
# that zone** — the DynDNS v2 code for *the service is broken, stop
# asking*.
#
# The tests below assert the part that a two-request browser flow cannot
# give: **atomicity**. A credential the adapter refuses must take the
# zone with it, or the operator is told their submission failed while
# holding exactly the state the feature exists to prevent.
# ===================================================================== #


async def test_a_zone_and_its_first_provider_arrive_in_one_transaction(
    tenants: dict[str, Any],
):
    a = tenants["a"]
    name = f"one-shot-{W}.example.invalid"
    async with _client(a["user"]) as client:
        created = await client.post(
            "/api/atrium_ddns/domains",
            json={
                "name": name,
                "backend": {
                    "backend_type": SERVICE,
                    "config": {"ttl": 60},
                    "credentials": _creds(CANARY),
                },
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == name
        # The binding comes back on the response, so the browser does
        # not have to re-read the list to learn whether it landed.
        assert len(body["backends"]) == 1, body
        binding = body["backends"][0]
        assert binding["backend_type"] == SERVICE
        assert binding["credentials_set"] is True
        # …and never the credential itself, on the one response that
        # has the plaintext in the same request.
        assert CANARY not in created.text

    # Second instrument: the stored ciphertext, read with SQL, and the
    # plaintext read back through the descriptor `/nic/update` uses.
    # A response saying `credentials_set: true` is the endpoint's own
    # report; these two are the row.
    assert await _ciphertext(binding["id"]) is not None
    assert await _plaintext(binding["id"]) == _creds(CANARY)


async def test_a_refused_credential_takes_the_zone_with_it(
    tenants: dict[str, Any],
):
    """The assertion the whole one-submission design rests on.

    Two requests from the browser can half-succeed: the zone lands, the
    credential is refused, and the tenant now owns a zone that answers
    ``911`` for every update under it — while their screen says the
    submission failed. One transaction cannot.

    The refusal used is a *partial* credential, because it is the one an
    operator actually produces: they fill one box and miss the other.
    """
    a = tenants["a"]
    name = f"rolled-back-{W}.example.invalid"
    partial = _creds(CANARY)
    dropped = sorted(partial)[-1]
    del partial[dropped]

    async with _client(a["user"]) as client:
        refused = await client.post(
            "/api/atrium_ddns/domains",
            json={
                "name": name,
                "backend": {
                    "backend_type": SERVICE,
                    "config": {},
                    "credentials": partial,
                },
            },
        )
        assert refused.status_code == 422, refused.text
        # Names the field, never the value it is refusing.
        assert dropped in refused.text
        assert CANARY not in refused.text

        # The zone is not there. Not "is there and empty" — absent.
        names = [
            d["name"] for d in (await client.get("/api/atrium_ddns/domains")).json()
        ]
        assert name not in names, (
            "the zone survived a refused credential — this is precisely the "
            "zero-provider zone that answers 911 for every update under it"
        )

        # And the name is still free, which is the operator-visible half:
        # a rolled-back create must not have consumed the zone name
        # through the global UNIQUE index.
        retry = await client.post(
            "/api/atrium_ddns/domains",
            json={
                "name": name,
                "backend": {
                    "backend_type": SERVICE,
                    "config": {},
                    "credentials": _creds(CANARY),
                },
            },
        )
        assert retry.status_code == 201, retry.text


async def test_an_unknown_service_takes_the_zone_with_it_too(
    tenants: dict[str, Any],
):
    """The other refusal on the same path, asserted separately.

    A different branch of ``_bind_backend`` raises it — the adapter
    lookup rather than the credential completeness check — and a rollback
    that worked for one and not the other would be invisible to a test
    that only drove one.
    """
    a = tenants["a"]
    name = f"unknown-svc-{W}.example.invalid"
    async with _client(a["user"]) as client:
        refused = await client.post(
            "/api/atrium_ddns/domains",
            json={
                "name": name,
                "backend": {"backend_type": f"nosuch-{W}", "config": {}},
            },
        )
        assert refused.status_code == 422, refused.text
        names = [
            d["name"] for d in (await client.get("/api/atrium_ddns/domains")).json()
        ]
        assert name not in names


async def test_omitting_the_provider_is_still_allowed_and_is_audited_as_such(
    tenants: dict[str, Any],
):
    """"Add a provider later" stays open — staging a migration is a real
    reason — and the audit row records **which** of the two shapes the
    call was.

    ``with_backend: false`` is a measurement of a deliberate act, and it
    is the one an audit reader wants to be able to find, because it is
    the state that answers ``911``. Recording nothing for it would make
    the two indistinguishable after the fact.
    """
    a = tenants["a"]
    name = f"staged-{W}.example.invalid"
    async with _client(a["user"]) as client:
        created = await client.post(
            "/api/atrium_ddns/domains", json={"name": name, "backend": None}
        )
        assert created.status_code == 201, created.text
        assert created.json()["backends"] == []
        domain_id = created.json()["id"]

        # The pre-#88 body — no `backend` key at all — is still accepted.
        # Any caller written against the old shape keeps working.
        legacy = await client.post(
            "/api/atrium_ddns/domains", json={"name": f"legacy-{W}.example.invalid"}
        )
        assert legacy.status_code == 201, legacy.text
        assert legacy.json()["backends"] == []

    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                sa.select(AuditLog)
                .where(AuditLog.entity == "ddns_domain")
                .where(AuditLog.entity_id == domain_id)
                .where(AuditLog.action == "create")
            )
        ).scalars().all()
    assert len(rows) == 1, rows
    assert rows[0].diff["with_backend"] is False


async def test_the_one_shot_create_audits_both_rows(tenants: dict[str, Any]):
    """One submission, two audit rows — and the credential in neither.

    The zone row says a provider came with it; the binding row says what
    happened to the secret. Two entities, two rows, because an audit that
    folded them would make "who added this provider" unanswerable.
    """
    a = tenants["a"]
    name = f"audited-{W}.example.invalid"
    async with _client(a["user"]) as client:
        created = await client.post(
            "/api/atrium_ddns/domains",
            json={
                "name": name,
                "backend": {
                    "backend_type": SERVICE,
                    "config": {"ttl": 60},
                    "credentials": _creds(CANARY),
                },
            },
        )
        assert created.status_code == 201, created.text
        domain_id = created.json()["id"]
        backend_id = created.json()["backends"][0]["id"]

    factory = get_session_factory()
    async with factory() as s:
        zone_row = (
            await s.execute(
                sa.select(AuditLog)
                .where(AuditLog.entity == "ddns_domain")
                .where(AuditLog.entity_id == domain_id)
            )
        ).scalar_one()
        binding_row = (
            await s.execute(
                sa.select(AuditLog)
                .where(AuditLog.entity == "ddns_domain_backend")
                .where(AuditLog.entity_id == backend_id)
            )
        ).scalar_one()

    assert zone_row.diff["with_backend"] is True
    assert binding_row.diff["credentials"] == "replaced"
    # Key names, never values. `_credential_audit` is what keeps that
    # true and this is the assertion that it was reached on this path.
    assert CANARY not in str(zone_row.diff)
    assert CANARY not in str(binding_row.diff)


async def test_the_first_provider_is_owned_by_the_zone_not_by_the_caller(
    tenants: dict[str, Any],
):
    """The same rule ``_bind_backend``'s docstring spends its length on,
    exercised through the create path.

    A cross-tenant administrator creating a zone *for* themselves is the
    ordinary case; the assertion is that the binding's ``user_id`` is
    read off the zone row either way, because taking it from the scope
    would encrypt the credential under a key ``/nic/update`` never
    unlocks.
    """
    a = tenants["a"]
    async with _client(a["user"], ALL_PERMS | {CROSS_TENANT_PERMISSION}) as client:
        created = await client.post(
            "/api/atrium_ddns/domains",
            json={
                "name": f"owner-{W}.example.invalid",
                "backend": {
                    "backend_type": SERVICE,
                    "config": {},
                    "credentials": _creds(CANARY),
                },
            },
        )
        assert created.status_code == 201, created.text
        backend_id = created.json()["backends"][0]["id"]

    factory = get_session_factory()
    async with factory() as s:
        row = await s.get(m.DomainBackend, backend_id)
        assert row is not None
        assert row.user_id == a["user_id"]
    # Decryptable by the owner's key, which is the half a `user_id`
    # assertion alone cannot see.
    assert await _plaintext(backend_id) == _creds(CANARY)


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


# ===================================================================== #
# 7. The zone rename (#75, ui-parity §3.3 G4)
#
# `PATCH /domains/{id}` both existed as `405` and had no equivalent:
# `DELETE` cascades, so "delete and recreate" costs the tenant every name
# under the zone and every stored provider credential.
#
# The rule the endpoint has to hold is not "the string changed". It is
# that every `ddns_hostname` under the zone still satisfies
# `zone_contains` afterwards — through the **same function object**
# `/nic/update` and `POST /hostnames` reach — or the rename has minted
# rows that are creatable once and updatable never. The endpoint refuses
# rather than rewriting, and the tests below assert **both** directions:
# a rename that would orphan is refused *and nothing moved*, and a rename
# that keeps every name inside is allowed. A test that only checked the
# refusal would pass against an endpoint that refused everything.
# ===================================================================== #


async def _seed_names(domain_id: int, *names: str) -> list[int]:
    """Hostname rows under one zone, written directly.

    Directly rather than through `POST /hostnames` because this file's
    `ALL_PERMS` deliberately does not carry `atrium_ddns.hostname.manage`
    — widening it here would weaken
    `test_the_gate_bites_in_both_directions`, which is a real assertion
    about a different endpoint. The rows are cleaned up by the fixture's
    `purge_tenants`, which reaches them through the user cascade.
    """
    ids: list[int] = []
    async with fixture_writes("test_router_tenant._seed_names") as s:
        for name in names:
            row = m.Hostname(domain_id=domain_id, name=name)
            s.add(row)
            await s.flush()
            ids.append(row.id)
    return ids


async def _zone_of(domain_id: int) -> str:
    factory = get_session_factory()
    async with factory() as s:
        return (
            await s.execute(
                sa.text("SELECT name FROM ddns_domain WHERE id = :i"),
                {"i": domain_id},
            )
        ).scalar_one()


async def test_a_rename_that_would_orphan_a_name_is_refused_and_writes_nothing(
    tenants: dict[str, Any],
):
    """The chosen disposition, asserted rather than described.

    Two instruments on the same refusal: the ``409`` status **and** the
    stored zone name read back with SQL afterwards. The status alone
    would pass against a handler that refused after committing — which
    is not a hypothetical shape, it is what `domain.name = …` followed
    by a raise produces if the session is not rolled back.

    The message is asserted to carry the *count* and at least one name,
    because a refusal an operator cannot act on gets worked around.
    """
    a = tenants["a"]
    zone = a["domain_name"]
    await _seed_names(a["domain_id"], f"box.{zone}", f"nas.{zone}")

    async with _client(a["user"]) as client:
        response = await client.patch(
            f"/api/atrium_ddns/domains/{a['domain_id']}",
            json={"name": f"elsewhere-{W}.example.invalid"},
        )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "2 of 2 hostnames" in detail, detail
    assert f"box.{zone}" in detail, detail

    assert await _zone_of(a["domain_id"]) == zone, (
        "the zone name moved despite the refusal — the 409 is being raised "
        "after the write rather than instead of it"
    )


async def test_a_rename_that_keeps_every_name_inside_the_zone_is_allowed(
    tenants: dict[str, Any],
):
    """The other direction, and the reason it is not decoration.

    Without it, an endpoint hardcoded to ``raise _conflict(...)`` would
    pass every assertion in the test above. Narrowing
    ``a-crud-….example.invalid`` to ``deep.a-crud-….example.invalid``
    when the one name already sits under ``deep`` keeps containment true,
    so it is allowed — and **no hostname row is rewritten**, which is the
    property the whole disposition rests on.
    """
    a = tenants["a"]
    zone = a["domain_name"]
    name = f"box.deep.{zone}"
    (hostname_id,) = await _seed_names(a["domain_id"], name)

    async with _client(a["user"]) as client:
        response = await client.patch(
            f"/api/atrium_ddns/domains/{a['domain_id']}",
            json={"name": f"deep.{zone}"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == f"deep.{zone}"
    assert body["hostname_count"] == 1

    assert await _zone_of(a["domain_id"]) == f"deep.{zone}"
    factory = get_session_factory()
    async with factory() as s:
        stored = (
            await s.execute(
                sa.text("SELECT name FROM ddns_hostname WHERE id = :i"),
                {"i": hostname_id},
            )
        ).scalar_one()
    assert stored == name, (
        "the rename rewrote a hostname. That is the option this endpoint "
        "deliberately did not take — HostnameAssignIn already records why "
        "a hostname's name is not editable."
    )


async def test_the_rename_uses_the_wires_own_containment_function(
    tenants: dict[str, Any],
):
    """One `zone_contains`, quirks and all — not a second suffix test.

    The load-bearing assertion is the third one. `notexample.com` **is**
    inside `example.com` under the preserved legacy rule, so a rename
    that produced that relationship must be *allowed* here. An endpoint
    that had grown its own stricter containment check would refuse it,
    and the tenant would be unable to rename into a zone the wire is
    perfectly happy to serve.
    """
    from atrium_ddns.providers import base as providers_base

    assert tenant_router.zone_contains is providers_base.zone_contains

    a = tenants["a"]
    zone = a["domain_name"]
    await _seed_names(a["domain_id"], f"not{zone}")

    async with _client(a["user"]) as client:
        response = await client.patch(
            f"/api/atrium_ddns/domains/{a['domain_id']}",
            json={"name": zone},  # unchanged; the row above is the point
        )
        assert response.status_code == 200, response.text
        # `not<zone>` ends with `<zone>`, so containment holds and the
        # name is not counted as an orphan.
        narrower = await client.patch(
            f"/api/atrium_ddns/domains/{a['domain_id']}",
            json={"name": f"t{zone}"},
        )
    assert narrower.status_code == 200, narrower.text
    assert providers_base.zone_contains(f"t{zone}", f"not{zone}") is True


async def test_renaming_another_tenants_zone_is_a_404_not_a_403(
    tenants: dict[str, Any],
):
    """"Not yours" and "no such row" are one answer, on this verb too.

    The refusal has to come from the scope rather than from a name
    check: B's zone name is real and free of orphans, so an endpoint
    that looked the row up with `session.get` would rename it happily.
    """
    a, b = tenants["a"], tenants["b"]
    async with _client(a["user"]) as client:
        response = await client.patch(
            f"/api/atrium_ddns/domains/{b['domain_id']}",
            json={"name": f"stolen-{W}.example.invalid"},
        )
    assert response.status_code == 404, response.text
    assert await _zone_of(b["domain_id"]) == b["domain_name"]


async def test_renaming_onto_a_zone_another_tenant_holds_is_a_conflict_naming_nobody(
    tenants: dict[str, Any],
):
    """The `create` rule, on the rename path.

    Zone names are globally unique, so this conflict is reachable for a
    name a *different* tenant owns — and the message must not confirm
    that, for the same reason
    `test_a_zone_claimed_by_another_tenant_is_a_conflict_that_names_nobody`
    gives.
    """
    a, b = tenants["a"], tenants["b"]
    async with _client(a["user"]) as client:
        response = await client.patch(
            f"/api/atrium_ddns/domains/{a['domain_id']}",
            json={"name": b["domain_name"]},
        )
    assert response.status_code == 409, response.text
    assert b["email"] not in response.text
    assert str(b["user_id"]) not in response.json()["detail"]
    assert await _zone_of(a["domain_id"]) == a["domain_name"]


async def test_the_rename_normalises_and_an_unchanged_submit_audits_nothing(
    tenants: dict[str, Any],
):
    """Normalisation is the create path's, and a no-op writes no history.

    A form that submits an unchanged field is a normal thing for a form
    to do; if that minted an audit row, a rename audit would stop being
    a record of renames.
    """
    a = tenants["a"]
    factory = get_session_factory()

    async def _rename_audits() -> int:
        async with factory() as s:
            return int(
                (
                    await s.execute(
                        sa.select(sa.func.count())
                        .select_from(AuditLog)
                        .where(
                            AuditLog.entity == "ddns_domain",
                            AuditLog.action == "rename",
                            AuditLog.actor_user_id == a["user_id"],
                        )
                    )
                ).scalar_one()
            )

    before = await _rename_audits()
    async with _client(a["user"]) as client:
        unchanged = await client.patch(
            f"/api/atrium_ddns/domains/{a['domain_id']}",
            json={"name": f"  {a['domain_name'].upper()}.  "},
        )
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["name"] == a["domain_name"]
        assert await _rename_audits() == before, (
            "an unchanged submit wrote a rename audit row"
        )

        changed = await client.patch(
            f"/api/atrium_ddns/domains/{a['domain_id']}",
            json={"name": f"  RENAMED-{W}.Example.INVALID.  "},
        )
    assert changed.status_code == 200, changed.text
    assert changed.json()["name"] == f"renamed-{W}.example.invalid"
    assert await _rename_audits() == before + 1

    async with factory() as s:
        diff = (
            await s.execute(
                sa.select(AuditLog.diff)
                .where(
                    AuditLog.entity == "ddns_domain",
                    AuditLog.action == "rename",
                    AuditLog.actor_user_id == a["user_id"],
                )
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
        ).scalar_one()
    # Both sides. An audit row carrying only the new value cannot answer
    # the only question anyone reads a rename audit for.
    assert diff["name"]["from"] == a["domain_name"]
    assert diff["name"]["to"] == f"renamed-{W}.example.invalid"


async def test_put_is_still_405_and_that_is_the_decision(tenants: dict[str, Any]):
    """The measurement in ui-parity §3.3 G4 named two verbs; one moved.

    Recorded as a test rather than as prose because "we chose PATCH" and
    "we forgot PUT" are indistinguishable from the outside, and the next
    reader re-measuring this row deserves to know which it is.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        response = await client.put(
            f"/api/atrium_ddns/domains/{a['domain_id']}",
            json={"name": f"put-{W}.example.invalid"},
        )
    assert response.status_code == 405, response.text
    assert await _zone_of(a["domain_id"]) == a["domain_name"]


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


# ===================================================================== #
# 5. The per-device rate limit is editable, and the secret is not touched
#
# #73. Before ``PATCH /devices/{id}`` the only way to tighten one
# device's limit was delete-and-recreate, which mints a new username and
# a new secret — so an operator's only route to slowing an abusive
# device was to break it until its owner reconfigured the router. The
# tests below are written so that an implementation which "helpfully"
# re-hashed, rotated or otherwise touched the credential fails, because
# such an implementation satisfies every assertion about the limit.
# ===================================================================== #


async def test_the_limit_can_be_changed_without_touching_the_secret(
    tenants: dict[str, Any],
):
    """Two instruments on the credential, and neither alone is enough.

    The **bytes** of the stored hash cannot tell a hash that still
    verifies from one that does not — argon2 is salted, so a re-hash of
    the same plaintext produces a different string, but a byte
    comparison also passes for a value that was never a hash of
    anything. The **verification** cannot see a hash quietly upgraded in
    place: a re-hash of the same secret still authenticates.

    Together they close it: the row is byte-identical *and* the original
    secret still opens it.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        created = await client.post(
            "/api/atrium_ddns/devices",
            json={"name": f"limited-{W}", "rate_limit_per_minute": None},
        )
        assert created.status_code == 201, created.text
        device_id = created.json()["device"]["id"]
        secret = created.json()["secret"]
        assert created.json()["device"]["rate_limit_per_minute"] is None
        # NULL resolves to the namespace default, and the resolved value
        # travels beside the stored one.
        assert created.json()["device"]["effective_rate_limit_per_minute"] == 30

        before_hash = await _stored_hash(device_id)

        patched = await client.patch(
            f"/api/atrium_ddns/devices/{device_id}",
            json={"rate_limit_per_minute": 3},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["rate_limit_per_minute"] == 3
        assert patched.json()["effective_rate_limit_per_minute"] == 3
        # The response is a *read* model. It has no field that could
        # carry a secret — the structural sweep in section 2 covers every
        # route on this router, including this one, by construction —
        # and this is the behavioural half.
        assert "secret" not in patched.text
        assert secret not in patched.text

        after_hash = await _stored_hash(device_id)
        assert after_hash == before_hash, (
            "PATCH rewrote password_hash. The whole point of this route "
            "is that changing a limit does not rotate the credential."
        )

        # The second instrument: the original secret still authenticates
        # through the same verifier `/nic/*` uses.
        verified, _ = await verify_password(after_hash, secret)
        assert verified is True, (
            "the stored hash no longer verifies the secret the device "
            "was issued — it was changed, whatever the bytes say"
        )
        # …and that verifier is not answering True to everything.
        wrong, _ = await verify_password(after_hash, secret + "x")
        assert wrong is False

        # The username is the other half of the credential pair and is
        # equally untouched.
        assert patched.json()["username"] == created.json()["device"]["username"]


async def test_null_and_zero_survive_the_round_trip_as_two_states(
    tenants: dict[str, Any],
):
    """``NULL`` means *inherit*; ``0`` means *may never call*.

    Collapsing them is how a per-device override silently stops
    overriding, and a PATCH is where it would happen — a body of
    ``{"rate_limit_per_minute": 0}`` read as "unset" hands an explicitly
    muted device the installation default.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        device_id = (
            await client.post(
                "/api/atrium_ddns/devices",
                json={"name": f"twostate-{W}", "rate_limit_per_minute": 9},
            )
        ).json()["device"]["id"]

        muted = await client.patch(
            f"/api/atrium_ddns/devices/{device_id}",
            json={"rate_limit_per_minute": 0},
        )
        assert muted.status_code == 200, muted.text
        assert muted.json()["rate_limit_per_minute"] == 0
        assert muted.json()["effective_rate_limit_per_minute"] == 0

        inherited = await client.patch(
            f"/api/atrium_ddns/devices/{device_id}",
            json={"rate_limit_per_minute": None},
        )
        assert inherited.status_code == 200, inherited.text
        assert inherited.json()["rate_limit_per_minute"] is None
        assert inherited.json()["effective_rate_limit_per_minute"] == 30

        # And the two readings come from the stored row, not from the
        # response the request that wrote it happened to build.
        listing = (await client.get("/api/atrium_ddns/devices")).json()
        row = next(r for r in listing if r["id"] == device_id)
        assert row["rate_limit_per_minute"] is None
        assert row["effective_rate_limit_per_minute"] == 30

        # An omitted key is a 422, not a silent un-mute. `None` is a
        # *value* on this field, so a default of `None` would make
        # "leave it alone" and "return it to the default" one request.
        omitted = await client.patch(
            f"/api/atrium_ddns/devices/{device_id}", json={}
        )
        assert omitted.status_code == 422, omitted.text
        # A negative limit is refused rather than stored and resolved
        # into something surprising later.
        negative = await client.patch(
            f"/api/atrium_ddns/devices/{device_id}",
            json={"rate_limit_per_minute": -1},
        )
        assert negative.status_code == 422, negative.text


async def test_another_tenants_device_cannot_be_rate_limited(
    tenants: dict[str, Any],
):
    """404, not 403 — "not yours" and "no such row" must not be
    distinguishable, or the response is an enumeration oracle."""
    a, b = tenants["a"], tenants["b"]
    async with _client(b["user"]) as client:
        victim = (
            await client.post(
                "/api/atrium_ddns/devices", json={"name": f"b-router-{W}"}
            )
        ).json()["device"]["id"]
    before = await _stored_hash(victim)

    async with _client(a["user"]) as client:
        refused = await client.patch(
            f"/api/atrium_ddns/devices/{victim}",
            json={"rate_limit_per_minute": 0},
        )
    assert refused.status_code == 404, refused.text
    # The refusal is not a partial write: nothing was committed on the
    # way to it.
    async with _client(b["user"]) as client:
        row = next(
            r
            for r in (await client.get("/api/atrium_ddns/devices")).json()
            if r["id"] == victim
        )
    assert row["rate_limit_per_minute"] is None
    assert await _stored_hash(victim) == before


async def test_the_limit_change_is_audited_with_both_readings(
    tenants: dict[str, Any],
):
    """``before`` and ``after``, because ``null`` and ``0`` are different
    states and "the limit changed" does not say which."""
    a = tenants["a"]
    async with _client(a["user"]) as client:
        device_id = (
            await client.post(
                "/api/atrium_ddns/devices", json={"name": f"audited-limit-{W}"}
            )
        ).json()["device"]["id"]
        await client.patch(
            f"/api/atrium_ddns/devices/{device_id}",
            json={"rate_limit_per_minute": 0},
        )

    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                sa.select(AuditLog.action, AuditLog.diff).where(
                    AuditLog.entity == "ddns_device",
                    AuditLog.entity_id == device_id,
                    AuditLog.action == "update",
                )
            )
        ).all()
    assert len(rows) == 1, rows
    diff = rows[0].diff["rate_limit_per_minute"]
    assert diff == {"before": None, "after": 0}, diff


# ===================================================================== #
# 6. The device can be renamed, the conflict is surfaced, and the
#    credential is still not touched
#
# #89. #73's `DeviceUpdateIn` documented its own refusal — "there is no
# `name` here … renaming is a separate change (the create path has a
# uniqueness conflict to handle and this route does not)". The reasoning
# was sound; the conclusion expired. The conflict is
# `uq_ddns_device_user_name`, `UNIQUE(user_id, name)`, and it is now
# handled here the way the create path handles it.
#
# The tests below are written so that three tempting implementations
# fail:
#
#   * one that avoids the conflict by minting `router (2)` — caught by
#     asserting the 409 *and* the stored name;
#   * one that treats the constraint as installation-wide — caught by
#     renaming onto a name a *different* tenant holds and requiring it
#     to succeed;
#   * one that re-hashes the credential on the way past — caught by the
#     same two instruments #73 used, re-taken on the rename path,
#     because evidence gathered on the limit path is evidence about the
#     limit path.
# ===================================================================== #


def _nic_app() -> FastAPI:
    """``/nic/*`` with nothing else mounted.

    Built here rather than reusing ``_app`` because the two
    authenticate differently and that is the point: ``_app`` overrides
    ``current_principal`` with a fixture, so every request through it is
    authenticated *by the test*. ``/nic/*`` reads HTTP Basic off the
    wire and verifies it against ``ddns_device.password_hash`` — the
    only instrument in this file that can say the stored hash still
    opens for the secret the device was issued.
    """
    from atrium_ddns import router_nic

    application = FastAPI()
    application.include_router(router_nic.router)
    return application


def _basic(username: str, secret: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def _attach_hostname(domain_id: int, device_id: int, name: str) -> int:
    factory = get_session_factory()
    async with factory() as s:
        row = m.Hostname(domain_id=domain_id, device_id=device_id, name=name)
        s.add(row)
        await s.flush()
        row_id = row.id
        await s.commit()
    return row_id


async def test_a_device_can_be_renamed_and_the_secret_is_untouched(
    tenants: dict[str, Any],
):
    """The rename lands, and both of #73's instruments are re-taken on it.

    #73 proved the *limit* path leaves the credential alone. A rename is
    a different write to the same row, so that reading is not evidence
    about this path — it is re-taken here rather than inherited.

    The second instrument is stronger than #73's: instead of calling
    ``verify_password`` (the function the router also calls, which makes
    the test and the implementation share an author), this drives
    ``/nic/update`` over HTTP Basic with the original secret. The frozen
    table's ``update-badauth-precedes-911`` fixes the order — ``badauth``
    is decided before ``911`` — so a ``911`` answer is only reachable
    *after* the credential verified, and the wrong-secret request below
    shows the probe can still say ``badauth``.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        created = await client.post(
            "/api/atrium_ddns/devices",
            json={"name": f"typo-{W}", "rate_limit_per_minute": 7},
        )
        assert created.status_code == 201, created.text
        device = created.json()["device"]
        device_id = device["id"]
        username = device["username"]
        secret = created.json()["secret"]

    # A name this device owns, in a zone with no backends — so a
    # successfully authenticated update answers `911` and reaches no
    # provider and no nameserver.
    hostname = f"renamed-{W}.{a['domain_name']}"
    await _attach_hostname(a["domain_id"], device_id, hostname)

    before_hash = await _stored_hash(device_id)

    async with _client(a["user"]) as client:
        renamed = await client.patch(
            f"/api/atrium_ddns/devices/{device_id}",
            json={"name": f"fixed-{W}", "rate_limit_per_minute": 7},
        )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == f"fixed-{W}"
    # The credential pair's readable half is unchanged, and the response
    # is still the read model.
    assert renamed.json()["username"] == username
    assert "secret" not in renamed.text
    assert secret not in renamed.text
    # …and the limit was carried through untouched by the rename.
    assert renamed.json()["rate_limit_per_minute"] == 7

    # The stored row, not the response the write happened to build.
    async with _client(a["user"]) as client:
        row = (await client.get(f"/api/atrium_ddns/devices/{device_id}")).json()
    assert row["name"] == f"fixed-{W}"
    assert row["username"] == username

    # Instrument one: the bytes.
    after_hash = await _stored_hash(device_id)
    assert after_hash == before_hash, (
        "the rename rewrote password_hash. Renaming a device must not "
        "rotate its credential — the field device would stop working "
        "and nobody asked for that."
    )

    # Instrument two: the wire. `911` means the request got past
    # authentication and ownership and found a zone with no backends.
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_nic_app()), base_url="http://nic.test"
    ) as nic:
        good = await nic.get(
            "/nic/update",
            params={"hostname": hostname, "myip": "192.0.2.10"},
            headers=_basic(username, secret),
        )
        bad = await nic.get(
            "/nic/update",
            params={"hostname": hostname, "myip": "192.0.2.10"},
            headers=_basic(username, secret + "x"),
        )
    assert good.status_code == 200, good.text
    assert good.text.split()[0] == "911", (
        f"the secret the device was issued no longer authenticates after "
        f"the rename: /nic/update answered {good.text!r}"
    )
    # The probe is not answering `911` to everything: change one
    # character of the secret and the same request is refused.
    assert bad.text.split()[0] == "badauth", bad.text


async def test_renaming_onto_a_name_this_tenant_already_uses_is_a_409(
    tenants: dict[str, Any],
):
    """The conflict is surfaced, not avoided.

    No suffix is minted and no duplicate is stored: the refusal carries
    the offending name — which is disclosable precisely because the
    constraint is per user, so it can only ever be about a device the
    caller already owns and can already list.
    """
    a = tenants["a"]
    taken = f"occupied-{W}"
    async with _client(a["user"]) as client:
        assert (
            await client.post("/api/atrium_ddns/devices", json={"name": taken})
        ).status_code == 201
        mover_id = (
            await client.post(
                "/api/atrium_ddns/devices", json={"name": f"mover-{W}"}
            )
        ).json()["device"]["id"]

        refused = await client.patch(
            f"/api/atrium_ddns/devices/{mover_id}",
            json={"name": taken, "rate_limit_per_minute": None},
        )
        assert refused.status_code == 409, refused.text
        assert taken in refused.json()["detail"], refused.text

        # Nothing was written on the way to the refusal, and no
        # suffixed variant was invented behind it.
        listing = (await client.get("/api/atrium_ddns/devices")).json()
        names = [row["name"] for row in listing]
        assert names.count(taken) == 1, names
        assert next(r for r in listing if r["id"] == mover_id)["name"] == (
            f"mover-{W}"
        )
        assert not [n for n in names if n.startswith(f"{taken} (")], names

    # …and the session survived the rollback: a later write on the same
    # route still works, which is the half a bare `except` would break.
    async with _client(a["user"]) as client:
        ok = await client.patch(
            f"/api/atrium_ddns/devices/{mover_id}",
            json={"name": f"moved-{W}", "rate_limit_per_minute": None},
        )
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == f"moved-{W}"


async def test_renaming_onto_a_name_another_tenant_uses_succeeds(
    tenants: dict[str, Any],
):
    """``UNIQUE(user_id, name)`` — the scope is the *user*.

    A test that only checked "a duplicate is a 409" would be asserting
    the wrong scope: it passes identically against an implementation
    that made device names unique installation-wide, which would let one
    tenant's naming choices refuse another's and would disclose that
    somebody, somewhere, already has a device called ``router``.
    """
    a, b = tenants["a"], tenants["b"]
    shared = f"router-{W}"
    async with _client(b["user"]) as client:
        b_id = (
            await client.post("/api/atrium_ddns/devices", json={"name": shared})
        ).json()["device"]["id"]

    async with _client(a["user"]) as client:
        a_id = (
            await client.post(
                "/api/atrium_ddns/devices", json={"name": f"a-side-{W}"}
            )
        ).json()["device"]["id"]
        allowed = await client.patch(
            f"/api/atrium_ddns/devices/{a_id}",
            json={"name": shared, "rate_limit_per_minute": None},
        )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["name"] == shared

    # Both rows exist, both are called the same thing, and each tenant
    # sees exactly one of them.
    async with _client(a["user"]) as client:
        a_names = [
            r["name"] for r in (await client.get("/api/atrium_ddns/devices")).json()
        ]
    async with _client(b["user"]) as client:
        b_rows = (await client.get("/api/atrium_ddns/devices")).json()
    assert a_names.count(shared) == 1, a_names
    assert [r["id"] for r in b_rows if r["name"] == shared] == [b_id]
    assert a_id != b_id


async def test_renaming_another_tenants_device_is_a_404_and_writes_nothing(
    tenants: dict[str, Any],
):
    """404, not 403 — "not yours" and "no such row" must not be
    distinguishable, or the response is an enumeration oracle. Asserted
    on the *name* as well as on the status, because a route that
    refused after writing would still answer 404."""
    a, b = tenants["a"], tenants["b"]
    original = f"b-owned-{W}"
    async with _client(b["user"]) as client:
        victim = (
            await client.post("/api/atrium_ddns/devices", json={"name": original})
        ).json()["device"]["id"]
    before = await _stored_hash(victim)

    async with _client(a["user"]) as client:
        refused = await client.patch(
            f"/api/atrium_ddns/devices/{victim}",
            json={"name": f"a-took-it-{W}", "rate_limit_per_minute": None},
        )
        # The detail route is scoped the same way, and by the same
        # helper — so it is checked here rather than trusted.
        unseen = await client.get(f"/api/atrium_ddns/devices/{victim}")
    assert refused.status_code == 404, refused.text
    assert unseen.status_code == 404, unseen.text

    async with _client(b["user"]) as client:
        row = (await client.get(f"/api/atrium_ddns/devices/{victim}")).json()
    assert row["name"] == original
    assert await _stored_hash(victim) == before


async def test_the_rename_is_audited_with_both_readings_and_only_when_it_moved(
    tenants: dict[str, Any],
):
    """``before`` and ``after`` on the name, and no ``name`` key at all
    when the name did not change.

    A diff that recorded ``{"before": x, "after": x}`` on every limit
    change would make "this device was renamed" unsearchable, which is
    the only question the key exists to answer.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        device_id = (
            await client.post(
                "/api/atrium_ddns/devices", json={"name": f"audited-name-{W}"}
            )
        ).json()["device"]["id"]
        # 1: a rename.
        assert (
            await client.patch(
                f"/api/atrium_ddns/devices/{device_id}",
                json={"name": f"audited-new-{W}", "rate_limit_per_minute": None},
            )
        ).status_code == 200
        # 2: a limit change with the name resubmitted unchanged.
        assert (
            await client.patch(
                f"/api/atrium_ddns/devices/{device_id}",
                json={"name": f"audited-new-{W}", "rate_limit_per_minute": 5},
            )
        ).status_code == 200
        # 3: a limit change with no name key at all — the #73 shape,
        #    which must still be accepted.
        assert (
            await client.patch(
                f"/api/atrium_ddns/devices/{device_id}",
                json={"rate_limit_per_minute": 6},
            )
        ).status_code == 200

    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                sa.select(AuditLog.diff)
                .where(
                    AuditLog.entity == "ddns_device",
                    AuditLog.entity_id == device_id,
                    AuditLog.action == "update",
                )
                .order_by(AuditLog.id)
            )
        ).all()
    assert len(rows) == 3, rows
    assert rows[0].diff["name"] == {
        "before": f"audited-name-{W}",
        "after": f"audited-new-{W}",
    }, rows[0].diff
    assert "name" not in rows[1].diff, rows[1].diff
    assert "name" not in rows[2].diff, rows[2].diff
    # The limit half is unchanged by any of this.
    assert rows[2].diff["rate_limit_per_minute"] == {"before": 5, "after": 6}


async def test_one_device_name_validator_serves_create_and_rename(
    tenants: dict[str, Any],
):
    """§13.1's rule, one object along: the composition may happen twice,
    the validation may not.

    Whitespace-only is the case that proves it. Before ``DeviceName``,
    ``min_length=1`` was measured against the *unstripped* string while
    the handler stripped afterwards, so ``"   "`` created a device named
    ``""``. A rename validated separately would have had its own version
    of that hole.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        blank_create = await client.post(
            "/api/atrium_ddns/devices", json={"name": "   "}
        )
        assert blank_create.status_code == 422, blank_create.text

        device_id = (
            await client.post(
                "/api/atrium_ddns/devices", json={"name": f"  padded-{W}  "}
            )
        ).json()["device"]["id"]
        # Stripped on the way in, by the validator and not by the
        # handler.
        stored = (await client.get(f"/api/atrium_ddns/devices/{device_id}")).json()
        assert stored["name"] == f"padded-{W}"

        blank_rename = await client.patch(
            f"/api/atrium_ddns/devices/{device_id}",
            json={"name": "\t\n ", "rate_limit_per_minute": None},
        )
        assert blank_rename.status_code == 422, blank_rename.text

        padded_rename = await client.patch(
            f"/api/atrium_ddns/devices/{device_id}",
            json={"name": f"  trimmed-{W} ", "rate_limit_per_minute": None},
        )
        assert padded_rename.status_code == 200, padded_rename.text
        assert padded_rename.json()["name"] == f"trimmed-{W}"

        # `null` is *absent* on this field and only on this field —
        # `ddns_device.name` is NOT NULL, so there is no state it could
        # name. It leaves the name alone rather than being refused.
        untouched = await client.patch(
            f"/api/atrium_ddns/devices/{device_id}",
            json={"name": None, "rate_limit_per_minute": 4},
        )
        assert untouched.status_code == 200, untouched.text
        assert untouched.json()["name"] == f"trimmed-{W}"
        assert untouched.json()["rate_limit_per_minute"] == 4

        too_long = await client.patch(
            f"/api/atrium_ddns/devices/{device_id}",
            json={"name": "x" * 256, "rate_limit_per_minute": None},
        )
        assert too_long.status_code == 422, too_long.text
        # …and the strip happens *before* the length check, so trailing
        # whitespace is not what makes a 255-character name too long.
        at_limit = await client.patch(
            f"/api/atrium_ddns/devices/{device_id}",
            json={"name": "y" * 255 + "   ", "rate_limit_per_minute": None},
        )
        assert at_limit.status_code == 200, at_limit.text
        assert at_limit.json()["name"] == "y" * 255


async def test_the_detail_route_counts_the_same_names_the_list_does(
    tenants: dict[str, Any],
):
    """``GET /devices/{id}`` — the route ``/atrium-ddns/devices/:id``
    reads, and the reason the list not scaling is fixable at all.

    Two instruments on one number: the detail row and the list row for
    the same device must agree field for field. They are built by the
    same ``_render_device``, which is the point — a detail route that
    counted a device's hostnames with its own query is how the list and
    the detail come to display two different numbers for one device.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        device_id = (
            await client.post(
                "/api/atrium_ddns/devices",
                json={"name": f"detail-{W}", "rate_limit_per_minute": 11},
            )
        ).json()["device"]["id"]

    for index in range(2):
        await _attach_hostname(
            a["domain_id"], device_id, f"detail{index}-{W}.{a['domain_name']}"
        )

    async with _client(a["user"]) as client:
        detail = await client.get(f"/api/atrium_ddns/devices/{device_id}")
        listing = (await client.get("/api/atrium_ddns/devices")).json()
        missing = await client.get("/api/atrium_ddns/devices/9999999")
    assert detail.status_code == 200, detail.text
    assert detail.json()["hostname_count"] == 2
    assert detail.json() == next(r for r in listing if r["id"] == device_id)
    # A read model, so there is no field that could carry a secret —
    # asserted structurally for every route in section 2, and
    # behaviourally here.
    assert "secret" not in detail.text
    assert missing.status_code == 404, missing.text


async def test_a_caller_without_the_device_permission_cannot_read_the_detail(
    tenants: dict[str, Any],
):
    """The detail route is gated on the same permission as the list.

    Gating the two differently would produce a list a reader can open
    and rows they cannot follow — which is the shape #45 avoided for the
    board and the devices page, one level down.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        device_id = (
            await client.post(
                "/api/atrium_ddns/devices", json={"name": f"ungated-{W}"}
            )
        ).json()["device"]["id"]

    async with _client(a["user"], permissions={DOMAIN_MANAGE_PERMISSION}) as client:
        refused = await client.get(f"/api/atrium_ddns/devices/{device_id}")
        refused_list = await client.get("/api/atrium_ddns/devices")
    assert refused.status_code == refused_list.status_code, (
        f"the detail route and the list disagree about the gate: "
        f"{refused.status_code} vs {refused_list.status_code}"
    )
    assert refused.status_code == 403, refused.text
