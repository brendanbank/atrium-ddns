"""The hostname lifecycle (#69) — the writer nothing in the app had.

`Hostname` shipped with a table, a tenancy path, a board that renders it
and a wire protocol that updates it, and **no way for a tenant to create
one**. The only construction in the package was a fixture seeder refused
under `ENVIRONMENT=prod`, so the resolution strip — this milestone's
signature element — could not be made to render from an empty account.

Four things this file is written to make fail, in the order they matter:

**1. One validator, not two that agree.** There are exactly two rules
that can make a hostname permanently un-updatable — syntax (`notfqdn`)
and zone containment (`nohost`) — and `/nic/update` already owned both.
If the CRUD endpoint had its own copies they would agree on the day they
were written and diverge the first time either was edited, silently and
asymmetrically: a name the UI accepts and the wire rejects reads to the
owner as a broken router, and the reverse reads as the UI lying. So the
endpoint *imports* `split_hostnames` and `zone_contains`. §1 asserts the
sharing is real (one function object, reached from both entry points) and
then §2 drives a corpus through **both entry points** and demands
identical verdicts — including the three preserved legacy divergences,
which a "stricter validator for the UI" would have got wrong in three
different directions.

**2. Cross-tenant is 404, never 403, on every id in every verb.** Three
ids can each name another tenant's row — `hostname_id`, `domain_id` and
`device_id` — and the third is the one a correctly-scoped-looking
implementation misses: `PATCH` that scopes the hostname and trusts the
device id is a way to point your own name at somebody else's router.

**3. The permission gates something.** `atrium_ddns.hostname.manage` was
seeded by `0002_ddns_core` and referenced by no endpoint, which is worse
than a permission that does not exist because it reads as coverage. The
gate is proved *biting*: a caller holding the two neighbouring
permissions and not this one is refused on all four routes.

**4. Assignment is three operations and `null` is one of them.** Assign,
reassign, unassign — with `device_id: null` an explicit value rather than
an omission, because the model allows a hostname to exist before it is
assigned and to outlive the device it pointed at.

Everything created here is namespaced by `PYTEST_XDIST_WORKER`: ten
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
from app.auth.principal import Principal
from app.auth.rbac import current_principal
from app.models.auth import User
from conftest import fixture_writes, purge_tenants, unusable_password_hash
from fastapi import FastAPI
from httpx import ASGITransport

from atrium_ddns import compat_stub
from atrium_ddns import models as m
from atrium_ddns import router as router_module
from atrium_ddns import router_nic
from atrium_ddns.providers import ProviderAccount, base as providers_base, get_provider
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

#: Every permission the routes under test could want, so a test that is
#: not *about* the gate does not accidentally become one.
ALL_PERMS = {
    DOMAIN_MANAGE_PERMISSION,
    DEVICE_MANAGE_PERMISSION,
    HOSTNAME_MANAGE_PERMISSION,
    BOARD_READ_PERMISSION,
}


# ===================================================================== #
# Fixtures
# ===================================================================== #


@pytest_asyncio.fixture
async def tenants() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a zone and a device. B is the control.

    B exists only to be invisible. Every 404 assertion below names one of
    B's ids from A's client, so "not yours" and "no such row" have to be
    the same answer — a test that only checked A's own ids would pass
    against an endpoint with no scope at all.
    """
    tags = ("a", "b")
    emails = [f"ddns-hostname-{tag}-{W}@example.invalid" for tag in tags]
    await purge_tenants(emails, owner="test_router_hostnames.tenants")

    hashed = unusable_password_hash()
    built: dict[str, Any] = {"emails": emails}
    async with fixture_writes("test_router_hostnames.tenants") as s:
        for tag, email in zip(tags, emails):
            user = User(
                email=email,
                hashed_password=hashed,
                is_active=True,
                is_verified=True,
                full_name=f"DDNS hostname probe {W}",
                preferred_language="en",
            )
            s.add(user)
            await s.flush()
            domain = m.Domain(
                user_id=user.id, name=f"{tag}-names-{W}.example.invalid"
            )
            s.add(domain)
            device = m.Device(
                user_id=user.id,
                username=f"ddns-{tag}-{W}-hn",
                password_hash=hashed,
                name=f"router-{tag}-{W}",
            )
            s.add(device)
            await s.flush()
            # A second device, because "reassign" needs somewhere to
            # reassign *to*, and a test that reassigns a hostname to the
            # device it already has proves nothing.
            other = m.Device(
                user_id=user.id,
                username=f"ddns-{tag}-{W}-hn2",
                password_hash=hashed,
                name=f"router-{tag}-{W}-spare",
            )
            s.add(other)
            await s.flush()
            built[tag] = {
                "user": user,
                "user_id": user.id,
                "email": email,
                "domain_id": domain.id,
                "domain_name": domain.name,
                "device_id": device.id,
                "device_name": device.name,
                "other_device_id": other.id,
            }

        # Two *overlapping* zones, one per tenant, so that a single
        # hostname is legitimately inside both. Zone names are globally
        # unique but nesting is not forbidden, so A owning `wide-…` and B
        # owning `narrow.wide-…` is a state the domain endpoint permits
        # — and it is the only way two different tenants can both have a
        # real claim on one name. Without it, cross-tenant uniqueness
        # could only be asserted against a request that containment
        # refuses first, which would test the wrong rule.
        wide = m.Domain(user_id=built["a"]["user_id"], name=f"wide-{W}.example.invalid")
        s.add(wide)
        await s.flush()
        narrow = m.Domain(
            user_id=built["b"]["user_id"], name=f"narrow.wide-{W}.example.invalid"
        )
        s.add(narrow)
        await s.flush()
        built["a"]["wide_domain_id"] = wide.id
        built["a"]["wide_domain_name"] = wide.name
        built["b"]["narrow_domain_id"] = narrow.id
        built["b"]["narrow_domain_name"] = narrow.name

    yield built

    await purge_tenants(emails, owner="test_router_hostnames.tenants")


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
        base_url="http://hostnames.test",
    )


# ===================================================================== #
# 1. One validator, reached from both entry points
# ===================================================================== #


async def test_the_crud_endpoint_and_the_wire_share_one_syntax_function():
    """`router.split_hostnames` *is* `router_nic.split_hostnames`.

    Identity, not equality of behaviour. "Both return the same answer"
    is a property that holds right up until someone edits one copy; "the
    two names resolve to one function object" cannot stop holding
    without the import being deleted.
    """
    assert router_module.split_hostnames is router_nic.split_hostnames


async def test_the_crud_endpoint_and_the_wire_share_one_containment_function():
    """One `zone_contains`, reached three ways.

    The third assertion is the load-bearing one. `BaseProvider.
    domaininhostname` is what `/nic/update` actually calls, and it
    resolves `zone_contains` out of its own module globals — so this
    asserts the wire's *call site*, not merely that a function with that
    name exists somewhere importable.
    """
    assert router_module.zone_contains is providers_base.zone_contains

    domaininhostname = providers_base.BaseProvider.domaininhostname
    assert "zone_contains" in domaininhostname.__code__.co_names, (
        "BaseProvider.domaininhostname no longer references zone_contains — "
        "the wire has grown its own copy of the containment rule and the "
        "CRUD endpoint's answers can now drift from /nic/update's."
    )
    assert (
        domaininhostname.__globals__["zone_contains"] is router_module.zone_contains
    )


async def test_the_preserved_quirk_is_still_preserved():
    """`notexample.com` is inside `example.com` — no label boundary.

    Pinned here as well as in `test_providers.py` because this file is
    where someone would come to "fix" it: the containment rule now has a
    second caller, and tightening it to satisfy the CRUD endpoint would
    silently change what `/nic/update` answers for every tenant.
    """
    assert providers_base.zone_contains("example.com", "notexample.com") is True
    assert providers_base.zone_contains("example.com", "foo.example.com") is True
    assert providers_base.zone_contains("example.com", "example.com") is True
    assert providers_base.zone_contains("example.com", "foo.example.com.") is False
    assert providers_base.zone_contains("example.com", "foo") is False


# ===================================================================== #
# 2. The corpus — both entry points, identical verdicts
# ===================================================================== #

#: `(template, expected_ok, why)`. `{zone}` is the tenant's own zone and
#: `{ZONE}` the same string upper-cased — templated rather than written
#: against a literal `example.com` because the fixture's zone is
#: namespaced per xdist worker, and a literal would collide.
#:
#: Every `False` here has to be a `False` on the wire too, and every
#: `True` a `True`, or a tenant gets a row that can be created and never
#: updated (or the reverse).
CORPUS: tuple[tuple[str, bool, str], ...] = (
    ("foo.{zone}", True, "the ordinary case"),
    ("{zone}", True, "the zone apex is a name like any other"),
    ("a.b.c.{zone}", True, "depth is not limited"),
    ("FOO.{ZONE}", True, "case-insensitive on both sides"),
    ("xn--bcher-kva.{zone}", True, "punycode is just labels"),
    ("foo-bar.{zone}", True, "an interior hyphen is legal"),
    (
        "not{zone}",
        True,
        "PRESERVED DIVERGENCE: rfind with no label-boundary check, so "
        "this matches the zone. The wire accepts it; refusing it here "
        "would be the asymmetry, not the fix.",
    ),
    (
        "foo\n",
        False,
        "PRESERVED DIVERGENCE: the label regex's `$` matches before a "
        "final newline so the *syntax* check passes — and containment "
        "then refuses it, exactly as the wire answers nohost.",
    ),
    (
        "foo.{zone}\n",
        False,
        "same divergence, inside the zone: syntax passes, containment "
        "refuses because the trailing newline moves the end offset.",
    ),
    (
        "foo",
        False,
        "PRESERVED DIVERGENCE: a dotless label passes the regex, then "
        "falls through containment to nohost.",
    ),
    ("foo.{zone}.", False, "a trailing dot is not stripped for the lookup"),
    ("other.invalid", False, "outside the zone entirely"),
    ("-foo.{zone}", False, "a label may not start with a hyphen"),
    ("foo-.{zone}", False, "…nor end with one"),
    ("foo_bar.{zone}", False, "underscore is not in the label class"),
    ("foo..{zone}", False, "an empty label"),
    ("", False, "empty is refused before the regex runs"),
    ("a" * 64 + ".{zone}", False, "a label over 63 characters"),
)


def _wire_verdict(name: str, zone: str) -> bool:
    """What `/nic/update` would decide about `name`, before the database.

    Deliberately assembled from the wire's *own* two calls in their own
    order — `split_hostnames` (which is `BaseProvider.isvalidhostname`)
    then `provider.hostnameperzone`, which is what `router_nic.
    _run_backend` calls — rather than from `zone_contains` directly. A
    helper that called the shared function would be asserting the shared
    function against itself, which is the "probe that could not fail"
    shape: it would return `True` for a fork of the wire path just as
    happily.

    The provider is built exactly as `router_nic` builds it, with
    `domains=(domain.name,)` — a single zone, the hostname's own.
    """
    parts = router_nic.split_hostnames(name)
    if parts is None or len(parts) != 1:
        return False  # notfqdn
    provider = get_provider(
        ProviderAccount(service="nsupdate", domains=(zone,), credentials={})
    )
    assert provider is not None, "nsupdate is not registered; this probe is vacuous"
    # `hostnameperzone` answers False — not a partial map — as soon as one
    # hostname is outside every configured zone, and `_run_backend` turns
    # that into `nohost`.
    return bool(provider.hostnameperzone([parts[0]]))


async def test_the_corpus_is_not_vacuous():
    """Both verdicts occur, and the divergences are actually in the list.

    A corpus that had drifted to all-True (or that had quietly lost the
    three divergent cases in a tidy-up) would make the agreement test
    below pass while proving nothing. Named cases rather than a count,
    so deleting one fails here rather than shrinking the evidence
    silently.
    """
    accepted = [name for name, ok, _ in CORPUS if ok]
    refused = [name for name, ok, _ in CORPUS if not ok]
    assert len(accepted) >= 5 and len(refused) >= 5

    templates = {name for name, _, _ in CORPUS}
    for required in ("not{zone}", "foo\n", "foo"):
        assert required in templates, (
            f"{required!r} is a preserved legacy divergence and the reason "
            f"this corpus exists. Do not remove it."
        )


@pytest.mark.parametrize(
    "template,expected,why", CORPUS, ids=[c[0] or "<empty>" for c in CORPUS]
)
async def test_wire_and_crud_agree_on_the_corpus(
    tenants: dict[str, Any], template: str, expected: bool, why: str
):
    """One name, two entry points, one verdict.

    The CRUD half goes over HTTP through the real handler, and the wire
    half goes through `split_hostnames` + `hostnameperzone`. They are
    different code paths that happen to bottom out in the same two
    functions, which is the property under test — so the assertion is
    that the two *answers* match, and §1 is what makes that agreement
    structural rather than a coincidence this test would have to be
    re-run to notice.
    """
    a = tenants["a"]
    zone = a["domain_name"]
    subject = template.format(zone=zone, ZONE=zone.upper())

    wire_ok = _wire_verdict(subject, zone)
    assert wire_ok is expected, (
        f"the wire's own verdict on {subject!r} changed: expected "
        f"{expected} ({why})"
    )

    async with _client(a["user"]) as client:
        response = await client.post(
            HOSTNAMES, json={"name": subject, "domain_id": a["domain_id"]}
        )
    crud_ok = response.status_code == 201

    assert crud_ok is wire_ok, (
        f"{subject!r}: /nic/update would say "
        f"{'valid' if wire_ok else 'invalid'} and POST {HOSTNAMES} answered "
        f"{response.status_code}. A hostname that can be created and never "
        f"updated (or updated and never created) is the defect this test "
        f"exists for. Case: {why}"
    )
    if not crud_ok:
        # The refusal has to be a refusal, not a crash.
        assert response.status_code == 422, response.text
        detail = response.json().get("detail")
        if isinstance(detail, str):
            # The handler's own refusal names the wire status, so a
            # reader who has seen `notfqdn` or `nohost` in the log
            # recognises it. An empty name is refused by the request
            # schema before the handler runs (`min_length=1`), and that
            # detail is pydantic's list — still a refusal, and still the
            # same verdict, so it is not held to the wording.
            assert "notfqdn" in detail or "nohost" in detail, detail
    else:
        # The stored name is what `/nic/update` will look up, and the
        # lookup is `requested.lower()`. Asserted because the round-trip
        # is what makes the agreement useful: a row stored in a shape the
        # wire cannot find would satisfy the status-code comparison above
        # and still be un-updatable. Lower-casing is the *only*
        # transformation — `.strip()` would make this endpoint accept a
        # string the wire refuses.
        assert response.json()["name"] == subject.lower()


# ===================================================================== #
# 3. CRUD
# ===================================================================== #


async def test_create_list_and_delete_a_hostname(tenants: dict[str, Any]):
    a = tenants["a"]
    name = f"crud.{a['domain_name']}"

    async with _client(a["user"]) as client:
        created = await client.post(
            HOSTNAMES,
            json={
                "name": name,
                "domain_id": a["domain_id"],
                "device_id": a["device_id"],
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == name
        assert body["domain_name"] == a["domain_name"]
        assert body["device_id"] == a["device_id"]
        assert body["device_name"] == a["device_name"]
        # Nothing has ever been published, and that is three NULLs rather
        # than three zeros.
        assert body["last_ip_v4"] is None
        assert body["last_ip_v6"] is None
        assert body["last_updated_at"] is None

        listed = await client.get(HOSTNAMES)
        assert listed.status_code == 200
        assert [row["name"] for row in listed.json()] == [name]

        # The counts on the neighbouring surfaces move, because they are
        # read off the same scoped select rather than kept by hand. Keyed
        # by id: the fixture gives tenant A two zones, and an assertion on
        # list position would break the next time it gains a third.
        domains = await client.get("/api/atrium_ddns/domains")
        by_domain = {d["id"]: d["hostname_count"] for d in domains.json()}
        assert by_domain[a["domain_id"]] == 1
        assert by_domain[a["wide_domain_id"]] == 0
        devices = await client.get("/api/atrium_ddns/devices")
        counts = {d["id"]: d["hostname_count"] for d in devices.json()}
        assert counts[a["device_id"]] == 1
        assert counts[a["other_device_id"]] == 0

        dropped = await client.delete(f"{HOSTNAMES}/{body['id']}")
        assert dropped.status_code == 204
        assert (await client.get(HOSTNAMES)).json() == []


async def test_a_hostname_can_be_created_before_it_is_assigned(
    tenants: dict[str, Any],
):
    """`device_id` omitted, and `device_id: null` — both are *unassigned*.

    The model allows this on purpose and the board renders it in its own
    `unassigned_hostnames` list, so an endpoint that required a device
    would have made a documented state unreachable.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        omitted = await client.post(
            HOSTNAMES,
            json={"name": f"omitted.{a['domain_name']}", "domain_id": a["domain_id"]},
        )
        assert omitted.status_code == 201, omitted.text
        assert omitted.json()["device_id"] is None
        assert omitted.json()["device_name"] is None

        explicit = await client.post(
            HOSTNAMES,
            json={
                "name": f"explicit.{a['domain_name']}",
                "domain_id": a["domain_id"],
                "device_id": None,
            },
        )
        assert explicit.status_code == 201, explicit.text
        assert explicit.json()["device_id"] is None


async def test_assign_reassign_and_unassign(tenants: dict[str, Any]):
    """Three operations, one endpoint, and `null` is one of them."""
    a = tenants["a"]
    async with _client(a["user"]) as client:
        created = await client.post(
            HOSTNAMES,
            json={"name": f"move.{a['domain_name']}", "domain_id": a["domain_id"]},
        )
        hid = created.json()["id"]
        assert created.json()["device_id"] is None

        # assign
        assigned = await client.patch(
            f"{HOSTNAMES}/{hid}", json={"device_id": a["device_id"]}
        )
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["device_id"] == a["device_id"]
        assert assigned.json()["device_name"] == a["device_name"]

        # reassign — to a *different* device, or this proves nothing
        moved = await client.patch(
            f"{HOSTNAMES}/{hid}", json={"device_id": a["other_device_id"]}
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["device_id"] == a["other_device_id"]

        # unassign
        cleared = await client.patch(f"{HOSTNAMES}/{hid}", json={"device_id": None})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["device_id"] is None
        assert cleared.json()["device_name"] is None

        # …and it stuck, read back through a different endpoint.
        assert (await client.get(HOSTNAMES)).json()[0]["device_id"] is None


async def test_deleting_a_device_orphans_its_hostnames(tenants: dict[str, Any]):
    """`ON DELETE SET NULL`, exercised through the API rather than assumed.

    The model's comment says deleting a router must not destroy the
    names it maintained. Nothing tested it from the outside, and the
    hostname list is the surface where the consequence shows up.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        created = await client.post(
            HOSTNAMES,
            json={
                "name": f"orphan.{a['domain_name']}",
                "domain_id": a["domain_id"],
                "device_id": a["device_id"],
            },
        )
        assert created.status_code == 201, created.text

        assert (
            await client.delete(f"/api/atrium_ddns/devices/{a['device_id']}")
        ).status_code == 204

        rows = (await client.get(HOSTNAMES)).json()
        assert len(rows) == 1, "the hostname was destroyed with its device"
        assert rows[0]["device_id"] is None
        assert rows[0]["device_name"] is None


async def test_deleting_a_domain_takes_its_hostnames(tenants: dict[str, Any]):
    """The other cascade, and it is the opposite direction on purpose."""
    a = tenants["a"]
    async with _client(a["user"]) as client:
        assert (
            await client.post(
                HOSTNAMES,
                json={
                    "name": f"cascade.{a['domain_name']}",
                    "domain_id": a["domain_id"],
                },
            )
        ).status_code == 201
        assert (
            await client.delete(f"/api/atrium_ddns/domains/{a['domain_id']}")
        ).status_code == 204
        assert (await client.get(HOSTNAMES)).json() == []


# ===================================================================== #
# 4. Uniqueness — a 409 with a sentence, not a 500
# ===================================================================== #


async def test_a_duplicate_hostname_is_409_within_a_tenant(tenants: dict[str, Any]):
    a = tenants["a"]
    name = f"dup.{a['domain_name']}"
    async with _client(a["user"]) as client:
        assert (
            await client.post(
                HOSTNAMES, json={"name": name, "domain_id": a["domain_id"]}
            )
        ).status_code == 201
        again = await client.post(
            HOSTNAMES, json={"name": name, "domain_id": a["domain_id"]}
        )
        assert again.status_code == 409, again.text
        assert name in again.json()["detail"]
        # The session is still usable afterwards — a handler that let the
        # IntegrityError escape would leave it in a failed transaction and
        # the *next* request would be the confusing one.
        assert (await client.get(HOSTNAMES)).status_code == 200


async def test_a_duplicate_hostname_across_tenants_is_409_and_says_nothing_about_who(
    tenants: dict[str, Any],
):
    """Global uniqueness, and it has to be.

    `/nic/update?hostname=…` looks the row up with no tenant context, so
    two tenants holding one name would make the lookup ambiguous at the
    exact moment there is nobody to ask.

    The fixture's overlapping zones are what make this a real test:
    `shared.narrow.wide-…` is inside **A's** `wide-…` and inside **B's**
    `narrow.wide-…`, so both tenants pass containment and the *only*
    thing that can refuse the second one is the UNIQUE index. B claims
    it; A is refused with a sentence that names the hostname and nothing
    about who holds it — "already registered" and "already registered by
    someone else" are the same sentence here on purpose, because the
    alternative is an ownership oracle.
    """
    a, b = tenants["a"], tenants["b"]
    name = f"shared.{b['narrow_domain_name']}"

    async with _client(b["user"]) as client:
        first = await client.post(
            HOSTNAMES, json={"name": name, "domain_id": b["narrow_domain_id"]}
        )
        assert first.status_code == 201, first.text

    async with _client(a["user"]) as client:
        second = await client.post(
            HOSTNAMES, json={"name": name, "domain_id": a["wide_domain_id"]}
        )
        assert second.status_code == 409, (
            f"expected the UNIQUE index to refuse a name another tenant holds, "
            f"got {second.status_code}: {second.text}"
        )
        detail = second.json()["detail"]
        assert name in detail
        assert b["email"] not in detail
        assert str(b["user_id"]) not in detail
        # A still sees only A's rows, and the failed insert did not leave
        # the session unusable.
        assert (await client.get(HOSTNAMES)).json() == []


# ===================================================================== #
# 5. Tenancy — 404 on every id, in every verb
# ===================================================================== #


async def test_a_cross_tenant_hostname_is_404_not_403(tenants: dict[str, Any]):
    a, b = tenants["a"], tenants["b"]
    async with _client(b["user"]) as client:
        created = await client.post(
            HOSTNAMES,
            json={"name": f"bs.{b['domain_name']}", "domain_id": b["domain_id"]},
        )
        assert created.status_code == 201, created.text
        b_hostname = created.json()["id"]

    async with _client(a["user"]) as client:
        assert (await client.get(HOSTNAMES)).json() == [], "A can see B's hostname"
        patched = await client.patch(
            f"{HOSTNAMES}/{b_hostname}", json={"device_id": a["device_id"]}
        )
        assert patched.status_code == 404, patched.text
        deleted = await client.delete(f"{HOSTNAMES}/{b_hostname}")
        assert deleted.status_code == 404, deleted.text

    # …and B's row is untouched, which is what makes the 404s meaningful
    # rather than merely polite.
    async with _client(b["user"]) as client:
        rows = (await client.get(HOSTNAMES)).json()
        assert len(rows) == 1 and rows[0]["device_id"] is None


async def test_creating_under_another_tenants_domain_is_404(tenants: dict[str, Any]):
    a, b = tenants["a"], tenants["b"]
    async with _client(a["user"]) as client:
        response = await client.post(
            HOSTNAMES,
            json={"name": f"steal.{b['domain_name']}", "domain_id": b["domain_id"]},
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "no such domain"


async def test_pointing_a_hostname_at_another_tenants_device_is_404(
    tenants: dict[str, Any],
):
    """The id a correctly-scoped-*looking* handler forgets.

    Scoping the hostname and trusting `device_id` would leave `PATCH` as
    a way to attach your own name to somebody else's router — through an
    endpoint whose read side is entirely correct. Asserted on both the
    create and the patch path, because they take the id independently.
    """
    a, b = tenants["a"], tenants["b"]
    async with _client(a["user"]) as client:
        on_create = await client.post(
            HOSTNAMES,
            json={
                "name": f"borrow.{a['domain_name']}",
                "domain_id": a["domain_id"],
                "device_id": b["device_id"],
            },
        )
        assert on_create.status_code == 404, on_create.text
        assert on_create.json()["detail"] == "no such device"
        # Refused *before* the row was written, not after.
        assert (await client.get(HOSTNAMES)).json() == []

        mine = await client.post(
            HOSTNAMES,
            json={"name": f"mine.{a['domain_name']}", "domain_id": a["domain_id"]},
        )
        hid = mine.json()["id"]
        on_patch = await client.patch(
            f"{HOSTNAMES}/{hid}", json={"device_id": b["device_id"]}
        )
        assert on_patch.status_code == 404, on_patch.text
        assert (await client.get(HOSTNAMES)).json()[0]["device_id"] is None


async def test_the_cross_tenant_permission_widens_the_list(tenants: dict[str, Any]):
    """`atrium_ddns.admin` is the legacy `/admin/users/<uid>/hostnames`.

    The old service had a separate set of routes for an admin acting on
    another tenant's names. Here it is the same endpoint under a wider
    scope, so this asserts the widening actually happens rather than
    assuming the scope registry covers it.
    """
    a, b = tenants["a"], tenants["b"]
    async with _client(b["user"]) as client:
        assert (
            await client.post(
                HOSTNAMES,
                json={"name": f"seen.{b['domain_name']}", "domain_id": b["domain_id"]},
            )
        ).status_code == 201

    async with _client(a["user"]) as client:
        assert (await client.get(HOSTNAMES)).json() == []

    async with _client(a["user"], ALL_PERMS | {CROSS_TENANT_PERMISSION}) as client:
        names = {row["name"] for row in (await client.get(HOSTNAMES)).json()}
        assert f"seen.{b['domain_name']}" in names


# ===================================================================== #
# 6. The permission gates something, and the gate bites
# ===================================================================== #


#: Every hostname route, derived from the router's own table rather than
#: listed — a fifth route added without the gate fails this rather than
#: quietly not being covered.
def _hostname_routes() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for route in router.routes:
        path = getattr(route, "path", "")
        if "/hostnames" not in path:
            continue
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return out


async def test_every_hostname_route_is_gated_on_hostname_manage(
    tenants: dict[str, Any],
):
    """The gate bites — proved by removing exactly one permission.

    `atrium_ddns.hostname.manage` was seeded by `0002_ddns_core` and
    gated nothing at all, which is worse than a permission that does not
    exist because it reads as coverage. The caller here holds the two
    neighbouring permissions and the board's, and is refused on every
    route; the same caller *with* the permission is not. A test that only
    checked the second half would pass against an ungated endpoint.
    """
    a = tenants["a"]
    routes = _hostname_routes()
    # Eight: the four lifecycle routes, `GET`/`PUT /{id}/backends`,
    # `POST /{id}/update` (#74) and `POST /{id}/adopt-zone`. The number is
    # asserted rather than derived-and-trusted precisely so that adding a
    # route is a decision someone takes here — this line failing on the
    # commit that added them is the guard doing its job, and it is how
    # each new route came to be covered by the loop below rather than
    # silently not being.
    assert len(routes) == 8, f"expected eight hostname routes, found {routes}"

    without = ALL_PERMS - {HOSTNAME_MANAGE_PERMISSION}
    async with _client(a["user"], without) as client:
        for method, path in routes:
            # A concrete id: the refusal has to come from the gate, and a
            # 404 from an unparseable path parameter would hide it.
            url = path.replace("{hostname_id}", "1")
            response = await client.request(method, url, json={"device_id": None})
            assert response.status_code == 403, (
                f"{method} {path} answered {response.status_code} for a caller "
                f"without {HOSTNAME_MANAGE_PERMISSION} — the permission gates "
                f"nothing on this route."
            )

    # The mirror: with the permission, the same calls are *not* 403. This
    # is what stops the block above passing because the routes are broken.
    async with _client(a["user"], ALL_PERMS) as client:
        for method, path in routes:
            url = path.replace("{hostname_id}", "1")
            response = await client.request(method, url, json={"device_id": None})
            assert response.status_code != 403, f"{method} {path} still 403"


# ===================================================================== #
# 7. The point of the whole issue — a strip renders
# ===================================================================== #


async def test_creating_a_hostname_puts_it_on_the_board_with_no_strip_yet(
    tenants: dict[str, Any],
):
    """A new hostname reaches the board and draws **zero** strips.

    This corrects the issue's wording rather than satisfying it. #69's
    acceptance criterion is "create a domain, a device and a hostname,
    and see a resolution strip render" — three steps. Measured, three
    steps produce a hostname on the board with an empty `strips` list,
    and that is **correct**: `_strips_for` renders a family only when the
    name has been published or answered in it, which is #44's own
    argued-for correction to `ui-design.md` §3.4 (the alternative gives
    every v6-only hostname a permanent empty `A` rail). The board says
    so in as many words — *"nothing published yet — no strip to draw"*.

    So the strip needs a fourth step, and the fourth step is the device
    doing the thing the device exists for. The test below takes it.
    Asserted here separately so that the intermediate state is pinned as
    a *state* rather than looking like the strip test's setup: an empty
    `strips` list on a freshly created name is the right answer, and a
    later change that started emitting two blank rails would pass the
    strip test and fail this one.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        board = (await client.get("/api/atrium_ddns/board")).json()
        assert board["unassigned_hostnames"] == []
        assert all(device["hostnames"] == [] for device in board["devices"])

        name = f"nostrip.{a['domain_name']}"
        created = await client.post(
            HOSTNAMES,
            json={
                "name": name,
                "domain_id": a["domain_id"],
                "device_id": a["device_id"],
            },
        )
        assert created.status_code == 201, created.text

        board = (await client.get("/api/atrium_ddns/board")).json()
        devices = {device["id"]: device for device in board["devices"]}
        hostnames = devices[a["device_id"]]["hostnames"]
        assert len(hostnames) == 1, "the hostname did not reach the board"
        assert hostnames[0]["name"] == name
        assert hostnames[0]["strips"] == [], (
            "a hostname with nothing published in either family drew a strip. "
            "Two blank rails is the rendering #44 argued against in _strips_for."
        )

        # …and unassigning it moves the same hostname to the other list
        # rather than removing it from the board.
        await client.patch(
            f"{HOSTNAMES}/{created.json()['id']}", json={"device_id": None}
        )
        board = (await client.get("/api/atrium_ddns/board")).json()
        assert [h["name"] for h in board["unassigned_hostnames"]] == [name]
        assert all(
            h["name"] != name
            for device in board["devices"]
            for h in device["hostnames"]
        )


async def test_a_tenant_can_reach_a_rendered_resolution_strip(
    tenants: dict[str, Any],
):
    """Empty account -> zone -> provider -> device -> name -> update -> strip.

    The milestone's signature element, reached from a standing start
    with **nothing written to the database by this test except through
    the API**. The device's secret is read out of the one response that
    carries it and used as HTTP Basic on `/nic/update`, exactly as a
    router would.

    The DNS backend is the compat stub, scripted to `good` — the same
    mechanism the frozen protocol table uses. It contacts no nameserver,
    which is why this can run in the gate; what it does not stub is any
    part of the router, the persistence rule (`last_ip_*` is written only
    on a `good` aggregate) or the board's arithmetic.

    This is the component-scale half of #69's acceptance criterion. The
    end-to-end half runs against a live stack through the browser bundle
    and is pasted into the PR, because a test and the code it tests share
    an author and this one would keep passing if the bundle never shipped
    the page.
    """
    compat_stub.register_stub_providers(force=True)
    a = tenants["a"]
    published = "203.0.113.69"

    async with _client(a["user"]) as client:
        # 1. a provider binding on the zone, or every update answers 911
        backend = await client.post(
            f"/api/atrium_ddns/domains/{a['domain_id']}/backends",
            json={
                "backend_type": "stub1",
                "config": compat_stub.scripted_config("good"),
                "credentials": {"stub_token": "not-a-secret"},
            },
        )
        assert backend.status_code == 201, backend.text

        # 2. a device, and its secret — the only time it is ever shown
        device = await client.post(
            "/api/atrium_ddns/devices", json={"name": f"strip-router-{W}"}
        )
        assert device.status_code == 201, device.text
        username = device.json()["device"]["username"]
        secret = device.json()["secret"]
        device_id = device.json()["device"]["id"]

        # 3. the name
        name = f"strip.{a['domain_name']}"
        created = await client.post(
            HOSTNAMES,
            json={"name": name, "domain_id": a["domain_id"], "device_id": device_id},
        )
        assert created.status_code == 201, created.text

        # …no strip yet, which is the state the previous test pins.
        board = (await client.get("/api/atrium_ddns/board")).json()
        on_board = {d["id"]: d for d in board["devices"]}[device_id]["hostnames"]
        assert on_board[0]["strips"] == []

    # 4. the device calls in, over HTTP Basic, like a router
    nic = FastAPI()
    nic.include_router(router_nic.router)
    token = base64.b64encode(f"{username}:{secret}".encode()).decode("ascii")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=nic), base_url="http://nic.test"
    ) as wire:
        response = await wire.get(
            "/nic/update",
            params={"hostname": name, "myip": published},
            headers={
                "Authorization": f"Basic {token}",
                "X-Forwarded-For": published,
            },
        )
    assert response.status_code == 200, response.text
    assert response.text.startswith("good"), (
        f"/nic/update answered {response.text!r}; the chain this test builds "
        f"is meant to be updatable end to end, and a `nohost` here would mean "
        f"the CRUD endpoint minted a row the wire cannot find."
    )

    # 5. the strip
    async with _client(a["user"]) as client:
        board = (await client.get("/api/atrium_ddns/board")).json()
        hostnames = {d["id"]: d for d in board["devices"]}[device_id]["hostnames"]
        strips = hostnames[0]["strips"]
        assert [strip["family"] for strip in strips] == ["A"], (
            "expected exactly one strip, for the family that was published. "
            "Two would mean the empty AAAA rail #44 argued against."
        )
        strip = strips[0]
        assert strip["published"]["address"] == published
        assert strip["published"]["updated_at"] is not None
        # Nothing has *answered* yet — the health-check job has not run —
        # and that is a fourth state, not a zero.
        assert strip["answered"]["status"] == "never_checked"
        assert strip["answered"]["address"] is None
        assert strip["upper_joint"] == "not_measured_never"
        # The device published the address it called from, so the lower
        # joint is a real verdict rather than a refusal to compare.
        assert strip["called_from"]["address"] == published
        assert strip["called_from"]["reason"] == "evaluated"
        assert strip["lower_joint"] == "agreed"
