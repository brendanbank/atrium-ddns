"""``GET /api/atrium_ddns/board`` — the device board and the strips.

Four things this file is written to *prove*, because each is an
acceptance criterion on #44 and each has a plausible-looking
implementation that passes a weaker test.

**1. ``n/a`` is not ``0``, and it is not one dash either.** The
``; answered`` station has five states and **three of them carry a null
address**: ``never_checked`` (we have not looked), ``error`` (we looked
and could not measure) and ``missing`` (we looked and the name does not
resolve). ``test_the_three_null_address_states_are_three_states_on_the_wire``
drives all three through a real database and asserts three distinct
``status`` strings, three distinct upper-joint verdicts, and — the part
that catches the collapse — that no two of them produce the same
``(status, upper_joint)`` pair. The pair that has to stay apart is
``never_checked`` and ``missing``: both have ``dns_ip IS NULL`` **and**
``dns_check_error IS NULL``, and differ only by whether
``dns_checked_at`` is set.

**2. The lower joint refuses to fire on a NAT'd client.**
``Device.last_ip_*`` is the address the device *called from*; for a
device sending an explicit ``myip=`` that is permanently and correctly
different from what it publishes. A strip that compared them
unconditionally would show a divergence on every render, forever — an
indicator that is always on cannot indicate. So the comparison is
conditional on the most recent successful update event having
``client_ip == ip``, and both directions are asserted: the NAT'd device
renders ``not_applicable``/``declared_myip``, and a device that
genuinely moved renders ``diverged``.

**3. The gate bites in both directions.** ``atrium_ddns.device.manage``
present → 200; absent → 403. The negative half is asserted on the
*status code*, not on the absence of a key in the body: a permission
test that passes on a failed login is the defect
``docs/ops/overnight-template.md`` records under "assertions on the
report" — 19 cells passing on an account that could not log in at all.

**4. Nothing is recomputed.** ``worker_jobs.stored_dns_status`` is the
one function that reads the five states off the columns, and
``test_the_board_reads_the_states_through_worker_jobs`` asserts the
router's own mapping table is total over ``DnsCheckStatus`` — so a sixth
state added upstream fails here rather than silently falling through to
a default.

Everything created here is namespaced by ``PYTEST_XDIST_WORKER``: ten
workers share one MySQL, and a test that hardcodes an email or a
hostname produces collisions that read as flakiness.
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
from conftest import fixture_writes, purge_tenants, unusable_password_hash
from fastapi import FastAPI
from httpx import ASGITransport

from atrium_ddns import models as m
from atrium_ddns import router as board_router
from atrium_ddns import worker_jobs as wj
from atrium_ddns.router import (
    BOARD_READ_PERMISSION,
    JointVerdict,
    LowerJointReason,
    build_strip,
    router,
)
from atrium_ddns.worker_jobs import DnsCheckStatus, Liveness

W = os.environ.get("PYTEST_XDIST_WORKER", "serial")


def _now() -> datetime:
    return wj._now()


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def tenants() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, each with a domain and a device.

    Two rather than one because the scope is asserted behaviourally:
    tenant A asks for the board and tenant B's rows must not be in it.
    A single-tenant fixture cannot fail that assertion.
    """
    emails = [f"ddns-board-a-{W}@example.invalid", f"ddns-board-b-{W}@example.invalid"]
    await purge_tenants(emails, owner="test_router_board.tenants")

    hashed = unusable_password_hash()
    built: dict[str, Any] = {"emails": emails}
    async with fixture_writes("test_router_board.tenants") as s:
        for tag, email in (("a", emails[0]), ("b", emails[1])):
            user = User(
                email=email,
                hashed_password=hashed,
                is_active=True,
                is_verified=True,
                full_name=f"DDNS board probe {W}",
                preferred_language="en",
            )
            s.add(user)
            await s.flush()
            domain = m.Domain(user_id=user.id, name=f"{tag}-board-{W}.example.invalid")
            device = m.Device(
                user_id=user.id,
                username=f"ddns-board-{tag}-{W}",
                password_hash="$argon2id$v=19$m=65536,t=3,p=4$fake$fake",
                name=f"router-{tag}",
            )
            s.add_all([domain, device])
            await s.flush()
            built[tag] = {
                "user": user,
                "user_id": user.id,
                "email": email,
                "domain_id": domain.id,
                "domain_name": domain.name,
                "device_id": device.id,
            }

    yield built

    await purge_tenants(emails, owner="test_router_board.tenants")


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


def _client(application: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=application), base_url="http://board.test"
    )


async def _board(user: User, permissions: set[str] | None = None) -> httpx.Response:
    perms = {BOARD_READ_PERMISSION} if permissions is None else permissions
    async with _client(_app(user, perms)) as client:
        return await client.get("/api/atrium_ddns/board")


async def _add_hostname(domain_id: int, name: str, **kwargs: Any) -> int:
    factory = get_session_factory()
    async with factory() as s:
        hostname = m.Hostname(domain_id=domain_id, name=name, **kwargs)
        s.add(hostname)
        await s.flush()
        hid = hostname.id
        await s.commit()
    return hid


async def _add_event(**kwargs: Any) -> None:
    factory = get_session_factory()
    async with factory() as s:
        s.add(m.DnsEvent(**kwargs))
        await s.commit()


async def _touch_device(device_id: int, **values: Any) -> None:
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            sa.update(m.Device).where(m.Device.id == device_id).values(**values)
        )
        await s.commit()


def _strip(payload: dict[str, Any], hostname: str, family: str) -> dict[str, Any]:
    for device in payload["devices"]:
        for host in device["hostnames"]:
            if host["name"] == hostname:
                for strip in host["strips"]:
                    if strip["family"] == family:
                        return strip
    for host in payload["unassigned_hostnames"]:
        if host["name"] == hostname:
            for strip in host["strips"]:
                if strip["family"] == family:
                    return strip
    raise AssertionError(
        f"no {family} strip for {hostname}; the board carried "
        f"{[h['name'] for d in payload['devices'] for h in d['hostnames']]} "
        f"under devices and "
        f"{[h['name'] for h in payload['unassigned_hostnames']]} unassigned"
    )


# ===================================================================== #
# 1. `n/a` is not `0` — the three null-address states, on the wire
# ===================================================================== #


async def test_the_three_null_address_states_are_three_states_on_the_wire(
    tenants: dict[str, Any],
):
    """Never-checked, check-failed and no-such-record, end to end.

    All three have ``dns_ip_v6 IS NULL``. Two of them
    (``never_checked``, ``missing``) *also* have
    ``dns_check_error IS NULL`` and differ only by ``dns_checked_at`` —
    which is exactly the pair #44 singles out and exactly the pair a
    renderer collapses first.

    The assertion is on the **set of pairs**, not on three separate
    equalities. Three ``assert x == 'never_checked'`` lines all pass on
    an implementation that returns the right status and one shared
    verdict; a set of size three cannot.
    """
    a = tenants["a"]
    checked = _now() - timedelta(minutes=1)

    await _add_hostname(
        a["domain_id"],
        f"never-{W}.{a['domain_name']}",
        device_id=a["device_id"],
        last_ip_v6="2001:0db8:0000:0000:0000:0000:0000:0001",
        last_updated_at=checked,
        # dns_checked_at left NULL — nothing has looked yet.
    )
    await _add_hostname(
        a["domain_id"],
        f"failed-{W}.{a['domain_name']}",
        device_id=a["device_id"],
        last_ip_v6="2001:0db8:0000:0000:0000:0000:0000:0002",
        last_updated_at=checked,
        dns_checked_at=checked,
        dns_check_error="AAAA: resolver timed out",
    )
    await _add_hostname(
        a["domain_id"],
        f"norecord-{W}.{a['domain_name']}",
        device_id=a["device_id"],
        last_ip_v6="2001:0db8:0000:0000:0000:0000:0000:0003",
        last_updated_at=checked,
        dns_checked_at=checked,
        # checked, no error, no address: the zone genuinely does not
        # carry the record. An outage, and the loud one of the three.
    )

    response = await _board(a["user"])
    assert response.status_code == 200, response.text
    payload = response.json()

    never = _strip(payload, f"never-{W}.{a['domain_name']}", "AAAA")
    failed = _strip(payload, f"failed-{W}.{a['domain_name']}", "AAAA")
    missing = _strip(payload, f"norecord-{W}.{a['domain_name']}", "AAAA")

    # Every one of the three has a null address. That is the whole
    # difficulty, and asserting it makes the test's subject explicit
    # rather than incidental.
    assert never["answered"]["address"] is None
    assert failed["answered"]["address"] is None
    assert missing["answered"]["address"] is None

    pairs = {
        (strip["answered"]["status"], strip["upper_joint"])
        for strip in (never, failed, missing)
    }
    assert len(pairs) == 3, (
        f"three null-address states collapsed into {len(pairs)} rendering(s): {pairs}"
    )
    assert never["answered"]["status"] == DnsCheckStatus.NEVER_CHECKED.value
    assert never["upper_joint"] == JointVerdict.NOT_MEASURED_NEVER.value
    assert failed["answered"]["status"] == DnsCheckStatus.ERROR.value
    assert failed["upper_joint"] == JointVerdict.NOT_MEASURED_FAILED.value
    assert missing["answered"]["status"] == DnsCheckStatus.MISSING.value
    assert missing["upper_joint"] == JointVerdict.DIVERGED.value

    # The resolver's own words survive to the client. Redact secrets,
    # never diagnostics: an error class reduced to `type(exc).__name__`
    # turned a one-line diagnosis into three deploys.
    assert failed["answered"]["error"] == "AAAA: resolver timed out"
    assert never["answered"]["checked_at"] is None
    assert failed["answered"]["checked_at"] is not None

    # And the quiet/loud split is not the aggregation rank.
    # `worker_jobs._STATUS_RANK` puts ERROR *above* MISMATCH, correctly,
    # for folding two families into one verdict. Visual weight runs the
    # other way, and an implementation that reused the rank would paint
    # every resolver hiccup as an outage.
    assert wj._STATUS_RANK[DnsCheckStatus.ERROR] > wj._STATUS_RANK[DnsCheckStatus.MISSING]
    assert failed["upper_joint"] != JointVerdict.DIVERGED.value
    assert missing["upper_joint"] == JointVerdict.DIVERGED.value


async def test_a_null_timestamp_never_becomes_an_epoch(tenants: dict[str, Any]):
    """``last_seen_at IS NULL`` ships as ``null``, not as ``1970-01-01``.

    ``now - 0`` is fifty-six years. A board fed an epoch sentinel makes
    every freshness rule fire for a full cadence after each deploy, and
    the wrong value is *plausible* rather than obviously broken.
    """
    a = tenants["a"]
    response = await _board(a["user"])
    device = response.json()["devices"][0]

    assert device["last_seen_at"] is None
    assert device["liveness"] == Liveness.NEVER_SEEN.value
    # `—`, not `0`. A device that has never called has no window
    # measurement; `0 updates in the last 7 days` about a device nobody
    # has ever heard from is true and misleading.
    assert device["updates_in_window"] is None
    assert device["updates_display"] == "—"
    assert device["marked"] is True


async def test_idle_renders_a_measured_zero_and_is_not_marked(
    tenants: dict[str, Any],
):
    """The fourth state, and the one the marker must not claim.

    M3 measured half this estate's fleet producing zero events in a
    24-hour window. Marking idle would paint half the board and destroy
    the marker, so idle is unmarked — and it renders ``0``, which is a
    statement, not a silence.
    """
    a = tenants["a"]
    await _touch_device(a["device_id"], last_seen_at=_now() - timedelta(hours=2))
    await _add_event(
        user_id=a["user_id"],
        user_email=a["email"],
        device_id=a["device_id"],
        event_type="update",
        response_code="good",
        created_at=_now() - timedelta(days=99),
    )

    response = await _board(a["user"])
    device = response.json()["devices"][0]

    assert device["liveness"] == Liveness.IDLE.value
    assert device["updates_in_window"] == 0
    assert device["updates_display"] == "0"
    assert device["marked"] is False
    # The denominator travels beside the numerator, so a caller cannot
    # render the count without being able to render what it is out of.
    assert device["window_days"] >= 1
    assert response.json()["window_days"] == device["window_days"]


# ===================================================================== #
# 2. The lower joint, and the indicator that would always be on
# ===================================================================== #


async def test_a_declared_myip_makes_the_lower_joint_not_applicable(
    tenants: dict[str, Any],
):
    """The NAT'd client: ``client_ip != ip``, permanently and correctly.

    ``router_nic.py`` says it in as many words — the stored addresses are
    the ones the device called *from*, not the ones it asked to publish.
    For a device behind IPv4 NAT declaring ``myip=`` those differ on
    every single update, so a strip comparing them unconditionally shows
    a divergence forever. The verdict here must be ``not_applicable``
    with the reason attached, and **no** rail segment.
    """
    a = tenants["a"]
    name = f"nat-{W}.{a['domain_name']}"
    hid = await _add_hostname(
        a["domain_id"],
        name,
        device_id=a["device_id"],
        last_ip_v4="203.0.113.7",
        last_updated_at=_now(),
        dns_ip_v4="203.0.113.7",
        dns_checked_at=_now(),
    )
    # The device called from its NAT'd address and asked us to publish a
    # different one. Both facts are on the same event row.
    await _touch_device(
        a["device_id"], last_seen_at=_now(), last_ip_v4="198.51.100.4"
    )
    await _add_event(
        user_id=a["user_id"],
        user_email=a["email"],
        device_id=a["device_id"],
        hostname_id=hid,
        hostname=name,
        event_type="update",
        response_code="good",
        client_ip="198.51.100.4",
        ip="203.0.113.7",
    )

    strip = _strip((await _board(a["user"])).json(), name, "A")

    assert strip["called_from"]["reason"] == LowerJointReason.DECLARED_MYIP.value
    assert strip["lower_joint"] == JointVerdict.NOT_APPLICABLE.value
    # What it declared travels too, so the UI can say *why* rather than
    # leaving the reader to infer it from an absent comparison.
    assert strip["called_from"]["declared_address"] == "203.0.113.7"
    assert strip["called_from"]["address"] == "198.51.100.4"
    # The two addresses differ. An implementation that compared them
    # would call this diverged, which is the failure being excluded.
    assert strip["called_from"]["address"] != strip["published"]["address"]

    # The denominator moves with it: 1 comparison, 1 n/a.
    assert strip["joints_compared"] == 1
    assert strip["joints_agreed"] == 1
    assert strip["joints_not_applicable"] == 1
    assert strip["collapsible"] is True


async def test_a_device_that_moved_does_diverge_on_the_lower_joint(
    tenants: dict[str, Any],
):
    """The other direction, and the reason the guard is not just "off".

    A guard that answers ``not_applicable`` to everything would pass the
    test above and be worthless. Same fixture shape, one difference —
    ``client_ip == ip`` on the event, so the device publishes the
    address it calls from — and the joint has to fire.
    """
    a = tenants["a"]
    name = f"moved-{W}.{a['domain_name']}"
    hid = await _add_hostname(
        a["domain_id"],
        name,
        device_id=a["device_id"],
        last_ip_v6="2001:0db8:0000:0000:0000:0000:0000:0001",
        last_updated_at=_now() - timedelta(hours=3),
        dns_ip_v6="2001:0db8:0000:0000:0000:0000:0000:0001",
        dns_checked_at=_now(),
    )
    # The device has since called from a different address; the name has
    # not followed. This is the most actionable signal in the product.
    await _touch_device(
        a["device_id"],
        last_seen_at=_now(),
        last_ip_v6="2001:0db8:0000:0000:0000:0000:0000:0099",
    )
    await _add_event(
        user_id=a["user_id"],
        user_email=a["email"],
        device_id=a["device_id"],
        hostname_id=hid,
        hostname=name,
        event_type="update",
        response_code="good",
        client_ip="2001:0db8:0000:0000:0000:0000:0000:0001",
        ip="2001:0db8:0000:0000:0000:0000:0000:0001",
    )

    strip = _strip((await _board(a["user"])).json(), name, "AAAA")

    assert strip["called_from"]["reason"] == LowerJointReason.EVALUATED.value
    assert strip["lower_joint"] == JointVerdict.DIVERGED.value
    assert strip["upper_joint"] == JointVerdict.AGREED.value
    assert strip["joints_compared"] == 2
    assert strip["joints_agreed"] == 1
    assert strip["collapsible"] is False


async def test_no_successful_update_on_record_is_not_a_divergence(
    tenants: dict[str, Any],
):
    """No event at all, and a *failed* event, are both "we cannot tell".

    The event filter is ``response_code IN ('good', 'nochg')``. A
    hostname whose only update was refused tells us nothing about which
    shape the device is, and guessing "it publishes its call-from
    address" would fire the joint on a comparison nobody authorised.
    """
    a = tenants["a"]
    name = f"nosuccess-{W}.{a['domain_name']}"
    hid = await _add_hostname(
        a["domain_id"],
        name,
        device_id=a["device_id"],
        last_ip_v4="203.0.113.10",
        last_updated_at=_now(),
    )
    await _touch_device(
        a["device_id"], last_seen_at=_now(), last_ip_v4="203.0.113.11"
    )
    await _add_event(
        user_id=a["user_id"],
        user_email=a["email"],
        device_id=a["device_id"],
        hostname_id=hid,
        hostname=name,
        event_type="update",
        response_code="nohost",
        client_ip="203.0.113.11",
        ip="203.0.113.11",
    )

    strip = _strip((await _board(a["user"])).json(), name, "A")

    assert (
        strip["called_from"]["reason"] == LowerJointReason.NO_UPDATE_ON_RECORD.value
    )
    assert strip["lower_joint"] == JointVerdict.NOT_APPLICABLE.value


async def test_an_unassigned_hostname_is_listed_and_never_marked(
    tenants: dict[str, Any],
):
    """``device_id IS NULL`` is a configuration state, not a fault.

    §4.1: the third station renders "no device assigned" and the lower
    joint draws no segment. And the row is *listed* rather than dropped —
    a name nobody can update is exactly what this board exists to
    surface, and hiding it because it has no parent would make the
    board's silence mean two things.
    """
    a = tenants["a"]
    name = f"orphan-{W}.{a['domain_name']}"
    await _add_hostname(
        a["domain_id"],
        name,
        device_id=None,
        last_ip_v4="203.0.113.20",
        last_updated_at=_now(),
        dns_ip_v4="203.0.113.20",
        dns_checked_at=_now(),
    )

    payload = (await _board(a["user"])).json()
    assert [h["name"] for h in payload["unassigned_hostnames"]] == [name]

    strip = _strip(payload, name, "A")
    assert strip["called_from"]["reason"] == LowerJointReason.NO_DEVICE.value
    assert strip["lower_joint"] == JointVerdict.NOT_APPLICABLE.value
    assert strip["upper_joint"] == JointVerdict.AGREED.value


# ===================================================================== #
# 3. The gate, both directions — and the scope
# ===================================================================== #


async def test_the_board_is_refused_without_the_permission(
    tenants: dict[str, Any],
):
    """403, on the status code, not on an absent key in the body.

    A permission test that reads the *body* passes on a failed login,
    on a 500, and on a route that does not exist. The template records
    the measured version of that: 19 cells passing on an account that
    could not log in at all.
    """
    a = tenants["a"]
    await _add_hostname(
        a["domain_id"],
        f"gated-{W}.{a['domain_name']}",
        device_id=a["device_id"],
        last_ip_v4="203.0.113.30",
    )

    allowed = await _board(a["user"], {BOARD_READ_PERMISSION})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["devices"], "the positive half must be non-vacuous"

    # Every *other* seeded permission, and still no board: the gate is
    # this code and not "holds any atrium_ddns permission".
    others = set(m.PERMISSIONS) - {BOARD_READ_PERMISSION}
    refused = await _board(a["user"], others)
    assert refused.status_code == 403, refused.text

    empty = await _board(a["user"], set())
    assert empty.status_code == 403, empty.text


async def test_a_tenant_sees_only_their_own_rows(tenants: dict[str, Any]):
    """Behavioural, not a source guard: B's rows in the database, A's board.

    ``DdnsScope`` is the control and the strongest available instrument
    is to put another tenant's data where an unscoped query would find
    it. A hand-written ``user_id`` filter that was forgotten on one of
    the four reads fails here.
    """
    a, b = tenants["a"], tenants["b"]
    a_name = f"mine-{W}.{a['domain_name']}"
    b_name = f"theirs-{W}.{b['domain_name']}"
    await _add_hostname(
        a["domain_id"], a_name, device_id=a["device_id"], last_ip_v4="203.0.113.40"
    )
    await _add_hostname(
        b["domain_id"], b_name, device_id=b["device_id"], last_ip_v4="203.0.113.41"
    )

    payload = (await _board(a["user"])).json()
    names = [
        host["name"]
        for device in payload["devices"]
        for host in device["hostnames"]
    ] + [host["name"] for host in payload["unassigned_hostnames"]]
    device_names = [device["name"] for device in payload["devices"]]

    assert a_name in names, "vacuity guard: A's own row must be present"
    assert b_name not in names
    assert device_names == ["router-a"]


# ===================================================================== #
# 4. Nothing is recomputed, and the ordering is the opinion
# ===================================================================== #


def test_the_board_reads_the_states_through_worker_jobs():
    """The router adds no sixth reading of the columns.

    Derived, not restated: the mapping table is checked for totality
    against ``DnsCheckStatus`` itself, so a state added upstream fails
    here rather than falling through to a default. A hardcoded list of
    five names is the identical defect one release later.
    """
    assert set(board_router._UPPER_JOINT) == set(DnsCheckStatus)
    assert set(board_router._LIVENESS_ORDER) == set(Liveness)
    # One canonicaliser for the whole host. If these ever become two,
    # the board and the health check can disagree about whether
    # `2001:db8::1` and `2001:0db8:0000::0001` are the same address.
    assert board_router.canonical_address is wj._canonical


def test_the_marker_covers_exactly_the_two_states_the_design_names():
    """`!` on never-seen and last-call-failed. Not on idle, not on active.

    Asserted as a set equality rather than two membership checks, so a
    third state quietly added to the marker fails.
    """
    assert board_router.MARKED_LIVENESS == frozenset(
        {Liveness.NEVER_SEEN, Liveness.LAST_CALL_FAILED}
    )


def test_the_upper_joint_is_not_the_aggregation_rank():
    """§4.3, as a guard rather than as a comment.

    ``_STATUS_RANK`` ranks ``ERROR`` above ``MISMATCH`` — right for
    ``worst()``, wrong for colour. An implementation that reused it
    would paint every resolver hiccup as an outage. The property that
    catches the reuse: the two statuses the rank separates most are the
    two this table renders *least* alike.
    """
    loud = {
        status
        for status, verdict in board_router._UPPER_JOINT.items()
        if verdict is JointVerdict.DIVERGED
    }
    assert loud == {DnsCheckStatus.MISSING, DnsCheckStatus.MISMATCH}
    assert DnsCheckStatus.ERROR not in loud


async def test_devices_arrive_in_liveness_order_oldest_first(
    tenants: dict[str, Any],
):
    """The ordering is the opinion, and the server owns it.

    §3.6: ``never_seen`` → ``last_call_failed`` → ``idle`` → ``active``,
    oldest ``last_seen_at`` first inside a bucket. A device that has
    gone quiet is at the top of the page without anyone asking, which
    is the whole reason the sortable-table arrangement was rejected.
    """
    a = tenants["a"]
    factory = get_session_factory()
    async with factory() as s:
        for suffix, seen_at in (
            ("active", _now() - timedelta(minutes=2)),
            ("stale", _now() - timedelta(days=3)),
        ):
            s.add(
                m.Device(
                    user_id=a["user_id"],
                    username=f"ddns-board-{suffix}-{W}",
                    password_hash="$argon2id$v=19$m=65536,t=3,p=4$fake$fake",
                    name=f"router-{suffix}",
                    last_seen_at=seen_at,
                )
            )
        await s.commit()

    # `router-active` is genuinely active; `router-stale` last called
    # three days ago and failed. `router-a` has never called at all.
    async with factory() as s:
        devices = {
            device.name: device.id
            for device in (
                await s.execute(
                    sa.select(m.Device).where(m.Device.user_id == a["user_id"])
                )
            )
            .scalars()
            .all()
        }
    await _add_event(
        user_id=a["user_id"],
        user_email=a["email"],
        device_id=devices["router-active"],
        event_type="update",
        response_code="good",
    )
    await _add_event(
        user_id=a["user_id"],
        user_email=a["email"],
        device_id=devices["router-stale"],
        event_type="update",
        response_code="badauth",
        created_at=_now() - timedelta(days=3),
    )

    payload = (await _board(a["user"])).json()
    order = [(device["name"], device["liveness"]) for device in payload["devices"]]

    assert order == [
        ("router-a", Liveness.NEVER_SEEN.value),
        ("router-stale", Liveness.LAST_CALL_FAILED.value),
        ("router-active", Liveness.ACTIVE.value),
    ], order


# ===================================================================== #
# 5. `build_strip` — the pure half, driven directly
# ===================================================================== #


@pytest.mark.parametrize(
    "reason,kwargs",
    [
        (LowerJointReason.NO_DEVICE, {"has_device": False}),
        (
            LowerJointReason.NO_UPDATE_ON_RECORD,
            {"event_client_ip": None, "event_ip": None},
        ),
        (
            LowerJointReason.DECLARED_MYIP,
            {"event_client_ip": "198.51.100.1", "event_ip": "203.0.113.1"},
        ),
        (LowerJointReason.NOT_COMPARABLE, {"device_ip": None}),
    ],
)
def test_every_lower_joint_refusal_is_reachable(
    reason: LowerJointReason, kwargs: dict[str, Any]
):
    """All four refusals, driven without a database.

    A reason nothing can produce is a reason that will be wrong the
    first time it is needed. This sweeps to a negative result: the
    parametrisation covers every member of the enum bar ``EVALUATED``,
    which the two database tests above exercise in both directions.
    """
    base: dict[str, Any] = {
        "family": "A",
        "last_ip": "203.0.113.1",
        "last_updated_at": None,
        "dns_ip": "203.0.113.1",
        "dns_checked_at": _now(),
        "dns_check_error": None,
        "has_device": True,
        "device_ip": "203.0.113.1",
        "device_seen_at": None,
        "event_client_ip": "203.0.113.1",
        "event_ip": "203.0.113.1",
    }
    strip = build_strip(**{**base, **kwargs})
    assert strip.called_from.reason is reason
    assert strip.lower_joint is JointVerdict.NOT_APPLICABLE

    # The control: unmodified, the same inputs produce a real verdict.
    assert build_strip(**base).lower_joint is JointVerdict.AGREED


def test_the_refusal_set_is_exhaustive():
    """Every ``LowerJointReason`` bar ``EVALUATED`` is covered above.

    Derived from the enum rather than counted by hand, so a reason added
    later fails this test instead of quietly going untested.
    """
    covered = {
        LowerJointReason.NO_DEVICE,
        LowerJointReason.NO_UPDATE_ON_RECORD,
        LowerJointReason.DECLARED_MYIP,
        LowerJointReason.NOT_COMPARABLE,
    }
    assert covered | {LowerJointReason.EVALUATED} == set(LowerJointReason)


def test_addresses_are_compared_canonically_not_as_strings():
    """`2001:db8::1` and `2001:0db8:0000::0001` are one address.

    M1 measured this estate at 68–97% IPv6 by update event, so a string
    comparison would report a divergence on the majority of traffic. The
    health check already goes through ``ipaddress``; so does this.
    """
    strip = build_strip(
        family="AAAA",
        last_ip="2001:db8::1",
        last_updated_at=None,
        dns_ip="2001:0db8:0000:0000:0000:0000:0000:0001",
        dns_checked_at=_now(),
        dns_check_error=None,
        has_device=True,
        device_ip="2001:0DB8::1",
        device_seen_at=None,
        event_client_ip="2001:db8::1",
        event_ip="2001:0db8::1",
    )
    assert strip.upper_joint is JointVerdict.AGREED
    assert strip.lower_joint is JointVerdict.AGREED
    assert strip.joints_agreed == 2
    assert strip.joints_compared == 2
    assert strip.collapsible is True


def test_a_strip_with_no_agreement_never_collapses():
    """Collapse is defined as "agrees", so nothing else may take the shape.

    An all-``not_applicable`` strip has ``0 of 0``, which is vacuously
    "every applicable joint agrees". Collapsing it would make the
    collapsed shape mean two things, so it stays expanded.
    """
    strip = build_strip(
        family="A",
        last_ip="203.0.113.1",
        last_updated_at=None,
        dns_ip=None,
        dns_checked_at=None,
        dns_check_error=None,
        has_device=False,
        device_ip=None,
        device_seen_at=None,
        event_client_ip=None,
        event_ip=None,
    )
    assert strip.upper_joint is JointVerdict.NOT_MEASURED_NEVER
    assert strip.lower_joint is JointVerdict.NOT_APPLICABLE
    assert strip.joints_compared == 0
    assert strip.joints_agreed == 0
    assert strip.collapsible is False


def test_only_families_the_hostname_tracks_get_a_strip():
    """§3.4, with the correction argued in ``_strips_for``'s docstring.

    A family is rendered when *this name* has been published in it or
    answered in it. Read literally, §3.4's "across the three stations"
    includes ``Device.last_ip_*`` — so a v6-only hostname on a router
    that also holds an IPv4 address would grow a permanent empty ``A``
    strip, which is the opposite of §3.4's own stated intent.
    """
    hostname = m.Hostname(
        domain_id=1,
        name="v6only.example.invalid",
        last_ip_v6="2001:db8::1",
        last_updated_at=_now(),
    )
    device = m.Device(
        user_id=1,
        username="x",
        password_hash="x",
        name="router",
        last_ip_v4="203.0.113.1",
        last_ip_v6="2001:db8::1",
        last_seen_at=_now(),
    )
    strips = board_router._strips_for(hostname, device, {})
    assert [strip.family for strip in strips] == ["AAAA"]
