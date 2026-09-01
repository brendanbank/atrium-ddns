"""The two on-demand health-check actions (#75, ui-parity §3.3 G3).

`POST /admin/health-checks/run` and `/clear` were both `405`. The check
is scheduled only, so an operator who has just fixed a provider outage
watches a stale board for up to `health_check_interval_minutes` — 15 by
default — with no way to say *look again*.

Four things this file is written to make fail, in the order they matter:

**1. `run` is the scheduled job, not a second sweep.** Asserted by
identity — `router.run_health_checks` is `worker_jobs.run_health_checks`
is the callable in `worker_jobs.JOBS` — because "both do the same thing"
is a property that holds until one copy is edited, and a second
implementation would lose the ERROR/MISSING split first. The batch
ceiling and the concurrency semaphore are the scheduled path's own.

**2. The forced run actually re-checks a name that is not due.** This is
the assertion the endpoint exists for and the one a naive implementation
fails *silently*: reusing the scheduled predicate means every name an
operator just watched get checked is not stale, the run reports
`0 checked`, and the board does not move. That is indistinguishable
from a working button against a healthy estate. So the test drives both
paths over one name whose `dns_checked_at` is *now* and demands they
disagree.

**3. The debounce bites, and it bites per actor.** Shown failing as well
as passing: a second press inside the cooldown is `429` with
`Retry-After`, the *other* tenant's press in the same window is not, and
with the cooldown configured to `0` the second press succeeds — so the
`429` is the control biting rather than the endpoint being broken.

**4. Reach is the scope's, on both routes.** Tenant B's names are
untouched by A's run and by A's clear, in the columns rather than in the
response body. And `clear` deletes nothing: no hostname, no published
address, and no `ddns_event` row — clearing a *result* and clearing a
*log* are different operations on different tables, and the log's route
is the one the operator struck (ui-parity §3.4).

Nothing here opens a socket. `worker_jobs.make_resolver` is replaced
with a scripted resolver for the length of each test, which is the same
seam `test_worker_jobs.py` uses; a suite that resolved real names would
be asserting about somebody else's nameservers.

Everything created here is namespaced by `PYTEST_XDIST_WORKER`: ten
workers share one MySQL and a hardcoded email or zone name produces
collisions that read as flakiness.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.auth.principal import Principal
from app.auth.rbac import current_principal
from app.db import get_session_factory
from app.models.auth import User
from app.models.ops import AuditLog
from app.services.app_config import get_namespace
import conftest
from conftest import fixture_writes, purge_tenants, unusable_password_hash
from fastapi import FastAPI
from httpx import ASGITransport

from atrium_ddns import models as m
from atrium_ddns import router as router_module
from atrium_ddns import worker_jobs
from atrium_ddns.router import (
    AUDIT_ACTION_CLEAR,
    AUDIT_ACTION_RUN,
    AUDIT_ENTITY_HEALTH_CHECK,
    BOARD_READ_PERMISSION,
    DOMAIN_MANAGE_PERMISSION,
    _cooldown_remaining,
    router,
)
from atrium_ddns.scope import CROSS_TENANT_PERMISSION, DdnsScope
from atrium_ddns.worker_jobs import (
    CONFIG_NAMESPACE,
    HEALTH_CHECK_JOB_ID,
    JOBS,
    DdnsConfig,
    DnsCheckStatus,
    stored_dns_status,
)

pytestmark = pytest.mark.asyncio

W = os.environ.get("PYTEST_XDIST_WORKER", "serial")

RUN = "/api/atrium_ddns/health-checks/run"
CLEAR = "/api/atrium_ddns/health-checks/clear"

#: The address every seeded name claims to have published. RFC 5737
#: documentation space, and it is what the scripted resolver answers, so
#: a check over these names lands on ``ok``.
PUBLISHED_V4 = "192.0.2.75"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest_asyncio.fixture
async def tenants() -> AsyncIterator[dict[str, Any]]:
    """Two tenants, one zone and one already-published name each.

    B exists to be untouched. Every scope assertion below reads B's
    columns after A has acted; a test that only checked A's own rows
    would pass against an endpoint with no scope at all.
    """
    tags = ("a", "b")
    emails = [f"ddns-hc-{tag}-{W}@example.invalid" for tag in tags]
    await purge_tenants(emails, owner="test_router_health_checks.tenants")

    hashed = unusable_password_hash()
    built: dict[str, Any] = {"emails": emails}
    async with fixture_writes("test_router_health_checks.tenants") as s:
        for tag, email in zip(tags, emails):
            user = User(
                email=email,
                hashed_password=hashed,
                is_active=True,
                is_verified=True,
                full_name=f"DDNS health-check probe {W}",
                preferred_language="en",
            )
            s.add(user)
            await s.flush()
            domain = m.Domain(user_id=user.id, name=f"{tag}-hc-{W}.example.invalid")
            s.add(domain)
            await s.flush()
            hostname = m.Hostname(
                domain_id=domain.id,
                name=f"box.{domain.name}",
                last_ip_v4=PUBLISHED_V4,
                last_updated_at=_now(),
                # Checked *just now*, and this is the whole point: under
                # the scheduled predicate this row is not due, so a
                # forced run and a due-only run must disagree about it.
                dns_checked_at=_now(),
                dns_ip_v4=PUBLISHED_V4,
            )
            s.add(hostname)
            await s.flush()
            built[tag] = {
                "user": user,
                "user_id": user.id,
                "email": email,
                "domain_id": domain.id,
                "domain_name": domain.name,
                "hostname_id": hostname.id,
                "hostname": hostname.name,
            }

    yield built

    await purge_tenants(emails, owner="test_router_health_checks.tenants")


ALL_PERMS = {BOARD_READ_PERMISSION, DOMAIN_MANAGE_PERMISSION}


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
        base_url="http://healthchecks.test",
    )


def _scripted(answers: dict[str, list[str]]):
    """A resolver factory that answers from a dict and opens no socket.

    Installed over ``worker_jobs.make_resolver``, which is what
    ``run_health_checks`` calls when no ``resolve=`` is passed — and the
    endpoint deliberately passes none, because injecting one would be a
    seam production could take.
    """

    def factory(_timeout: float):
        async def resolve(name: str, rdtype: str) -> list[str]:
            return answers.get(f"{name}/{rdtype}", [])

        return resolve

    return factory


async def _read_columns(hostname_id: int) -> dict[str, Any]:
    """The four persisted health-check columns, by SQL.

    The second instrument. Every verdict below is asserted against the
    *database*, not against the response body the same handler built:
    an endpoint that reported a clean run and wrote nothing would pass a
    body-only assertion.
    """
    factory = get_session_factory()
    async with factory() as s:
        row = (
            await s.execute(
                sa.text(
                    "SELECT dns_checked_at, dns_ip_v4, dns_ip_v6, dns_check_error, "
                    "last_ip_v4 FROM ddns_hostname WHERE id = :i"
                ),
                {"i": hostname_id},
            )
        ).one()
    return {
        "dns_checked_at": row[0],
        "dns_ip_v4": row[1],
        "dns_ip_v6": row[2],
        "dns_check_error": row[3],
        "last_ip_v4": row[4],
    }


def _pin_config(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> DdnsConfig:
    """Give the handler a known config, without writing the shared row.

    ``atrium_ddns``'s KV row is **one row for the whole compose
    project**: this worktree's worker container reads it on every tick,
    and ``test_worker_jobs.py`` legitimately moves it for the length of
    its own tests. Ten xdist workers share that database, so a test here
    that wrote the cooldown into the row would be racing a file it never
    imports — and the symptom would be a ``429`` that came and went.

    **What a monkeypatch cannot do, and what now does it instead.**
    This is in-process, and the worker *container* is a different
    process reading the row directly. #117 measured that row absent for
    **91.8 %** of a full run, and absent means the container falls back
    to ``health_check_enabled=True`` and sweeps cross-tenant — including
    over the two tests here that create ``last_ip`` rows and then assert
    on the persisted ``dns_*`` columns. That half is closed by
    ``conftest._pin_ddns_config``, which holds the row present and the
    sweep off for the whole session and refuses a session that ends
    otherwise. Nothing in this file had to change for it, which is the
    point: the protection is structural rather than per-test.

    So the seam is ``load_config``, the module global each caller
    resolves at call time — patched in **both** modules, because
    ``router`` imported the name and rebinding one binding does not move
    the other. What that gives up is the round trip through
    ``app_settings``, and it is given up knowingly:
    ``test_the_manual_cooldown_lives_on_the_registered_namespace``
    covers that half against the real namespace, reading only.
    """
    pinned = DdnsConfig(**overrides)

    async def _load(_session: Any) -> DdnsConfig:
        return pinned

    monkeypatch.setattr(router_module, "load_config", _load)
    monkeypatch.setattr(worker_jobs, "load_config", _load)
    return pinned


@pytest.fixture(autouse=True)
def pinned_config(monkeypatch: pytest.MonkeyPatch) -> DdnsConfig:
    """Every test in this module starts from ``DdnsConfig()``'s defaults.

    Autouse rather than opt-in because the failure it prevents is
    silent: ``test_worker_jobs.py`` pins ``health_check_enabled=False``
    into the shared row, and a run against that config reports every
    count as zero and ``enabled: false`` — which reads exactly like an
    estate with nothing to check.
    """
    return _pin_config(monkeypatch)


async def _drop_run_audits(user_id: int) -> None:
    """Forget this actor's manual-run history.

    The debounce keys on ``audit_log``, which nothing in this suite
    prunes, so two tests that both press the button for the same user
    would otherwise interact through it. Deleting by
    ``(entity, actor_user_id)`` touches only the rows this file wrote.
    """
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            sa.delete(AuditLog).where(
                AuditLog.entity == AUDIT_ENTITY_HEALTH_CHECK,
                AuditLog.actor_user_id == user_id,
            )
        )
        await s.commit()


# ===================================================================== #
# 1. `run` is the scheduled job, reached by one path
# ===================================================================== #


async def test_the_endpoint_and_the_scheduler_share_one_function_object():
    """Identity, not equality of behaviour.

    "Both check hostnames the same way" holds right up until somebody
    edits one copy. That the two names resolve to one function object,
    and that the object is the one the scheduler registers, cannot stop
    holding without the import being deleted.
    """
    assert router_module.run_health_checks is worker_jobs.run_health_checks
    body, _seconds = JOBS[HEALTH_CHECK_JOB_ID]
    assert body is worker_jobs.run_health_checks, (
        "the scheduler no longer registers the function the manual "
        "trigger calls — there are two implementations again"
    )


async def test_the_manual_run_is_bounded_by_the_scheduled_settings():
    """The knobs are the namespace's, and there is no second set.

    Read off ``DdnsConfig``'s own fields rather than listed, so a
    setting renamed in the model fails here instead of leaving this
    assertion true about a field nobody reads.
    """
    fields = set(DdnsConfig.model_fields)
    for knob in (
        "health_check_batch_size",
        "health_check_concurrency",
        "health_check_timeout_seconds",
    ):
        assert knob in fields
    # And the one setting that *is* new is scoped to the manual path by
    # its name, so a reader cannot mistake it for a scheduler cadence.
    assert "health_check_manual_cooldown_seconds" in fields


async def test_the_manual_cooldown_lives_on_the_registered_namespace():
    """The half `_pin_config` gives up, taken once and read-only.

    ``load_config`` is one function object reached from both modules, so
    the handler cannot be reading a different namespace from the
    scheduler; and the field is present on what
    ``get_namespace(atrium_ddns)`` actually returns from the database,
    which is the thing an operator would `PUT` to. Read rather than
    written: this row belongs to the whole compose project.
    """
    assert router_module.load_config is worker_jobs.load_config

    factory = get_session_factory()
    async with factory() as s:
        served = await get_namespace(s, CONFIG_NAMESPACE)
    assert hasattr(served, "health_check_manual_cooldown_seconds"), (
        "the namespace the API serves does not carry the debounce "
        "setting — it would be unreachable by an operator"
    )


# ===================================================================== #
# 2. The forced run re-checks what the scheduled run would skip
# ===================================================================== #


async def test_a_run_checks_a_name_the_scheduled_sweep_would_call_not_due(
    tenants: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """The assertion the whole endpoint exists for.

    The fixture's name was checked *now*, so the scheduled predicate
    excludes it. Both paths are driven over the same row and they must
    disagree: the due-only sweep checks nothing, the forced one checks
    it. Without the first half, an endpoint that had kept the staleness
    clause would report ``0 checked`` and look exactly like a healthy
    estate with nothing to do.
    """
    a = tenants["a"]
    monkeypatch.setattr(
        worker_jobs,
        "make_resolver",
        _scripted({f"{a['hostname']}/A": [PUBLISHED_V4]}),
    )
    scope = DdnsScope.for_principal(
        Principal(
            user=a["user"],
            permissions=frozenset(ALL_PERMS),
            auth_method="password",
            token_id=None,
            auth_session_id=None,
        )
    )

    due = await worker_jobs.run_health_checks(scope=scope)
    assert due.hostnames_checked == 0, (
        "the fixture's name was checked a moment ago and is not stale; a "
        "due-only sweep that checks it makes the rest of this test vacuous"
    )
    assert due.forced is False

    await _drop_run_audits(a["user_id"])
    async with _client(a["user"]) as client:
        response = await client.post(RUN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["forced"] is True
    assert body["hostnames_checked"] == 1, body
    assert body["records_checked"] == 1, body
    assert body["ok"] == 1, body


async def test_the_run_reports_every_denominator_and_the_counters_balance(
    tenants: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """`0 problems` over an unstated population is not a measurement.

    A second name with nothing published is added: it is *counted* in
    ``hostnames_never_written`` and not resolved, which is the ``n/a``
    slice this codebase refuses to drop silently.
    """
    a = tenants["a"]
    async with fixture_writes("test_router_health_checks.never-written") as s:
        s.add(
            m.Hostname(
                domain_id=a["domain_id"], name=f"unpublished.{a['domain_name']}"
            )
        )
    monkeypatch.setattr(
        worker_jobs,
        "make_resolver",
        _scripted({f"{a['hostname']}/A": [PUBLISHED_V4]}),
    )

    await _drop_run_audits(a["user_id"])
    async with _client(a["user"]) as client:
        body = (await client.post(RUN)).json()

    assert body["hostnames_considered"] == 2, body
    assert body["hostnames_never_written"] == 1, body
    assert body["hostnames_checked"] == 1, body
    assert body["ok"] + body["mismatch"] + body["missing"] + body["error"] == (
        body["records_checked"]
    )
    assert body["truncated"] is False
    assert body["batch_size"] == DdnsConfig().health_check_batch_size


async def test_a_run_transitions_a_stale_error_back_to_ok(
    tenants: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """The operator's actual scenario, end to end.

    A name left in ``ERROR`` by an outage that is now over: the
    scheduled sweep will not revisit it for up to fifteen minutes, and
    the board reads broken until it does. One press moves it, and the
    verdict is read back off the columns through
    ``stored_dns_status`` — the same function the board renders from,
    not the response body the handler just built.
    """
    a = tenants["a"]
    async with fixture_writes("test_router_health_checks.outage") as s:
        await s.execute(
            sa.text(
                "UPDATE ddns_hostname SET dns_check_error = :e, dns_ip_v4 = NULL, "
                "dns_checked_at = :t WHERE id = :i"
            ),
            {"e": "A: no nameservers would answer", "t": _now(), "i": a["hostname_id"]},
        )

    before = await _read_columns(a["hostname_id"])
    assert (
        stored_dns_status(
            last_ip=before["last_ip_v4"],
            dns_ip=before["dns_ip_v4"],
            dns_checked_at=before["dns_checked_at"],
            dns_check_error=before["dns_check_error"],
        )
        is DnsCheckStatus.ERROR
    )

    monkeypatch.setattr(
        worker_jobs,
        "make_resolver",
        _scripted({f"{a['hostname']}/A": [PUBLISHED_V4]}),
    )
    await _drop_run_audits(a["user_id"])
    async with _client(a["user"]) as client:
        body = (await client.post(RUN)).json()
    assert body["transitions"] == 1, body

    after = await _read_columns(a["hostname_id"])
    assert (
        stored_dns_status(
            last_ip=after["last_ip_v4"],
            dns_ip=after["dns_ip_v4"],
            dns_checked_at=after["dns_checked_at"],
            dns_check_error=after["dns_check_error"],
        )
        is DnsCheckStatus.OK
    )


# ===================================================================== #
# 3. The debounce, shown biting and shown letting go
# ===================================================================== #


async def test_the_cooldown_arithmetic_keeps_never_run_apart_from_long_ago():
    """Pure, and the two zeros are different facts.

    ``last_run is None`` is *never pressed*; a timestamp older than the
    window is *pressed, long ago*. Both permit the run and only one is a
    measurement, so they are separate branches rather than one
    ``or``-ed condition that would coerce ``None`` into a date.
    """
    now = _now()
    assert _cooldown_remaining(None, 60, now) == 0
    assert _cooldown_remaining(now - timedelta(seconds=61), 60, now) == 0
    assert _cooldown_remaining(now - timedelta(seconds=10), 60, now) == 50
    # A disabled debounce is spellable and is not the same as "just ran".
    assert _cooldown_remaining(now, 0, now) == 0
    # Rounding is upwards: reporting 0 while still refusing would send a
    # client straight back into another 429.
    assert _cooldown_remaining(now - timedelta(seconds=59.5), 60, now) == 1


async def test_a_second_press_inside_the_window_is_refused_with_retry_after(
    tenants: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """The control biting. The next test is the control letting go."""
    a = tenants["a"]
    monkeypatch.setattr(worker_jobs, "make_resolver", _scripted({}))
    _pin_config(monkeypatch, health_check_manual_cooldown_seconds=60)
    await _drop_run_audits(a["user_id"])

    async with _client(a["user"]) as client:
        first = await client.post(RUN)
        assert first.status_code == 200, first.text
        second = await client.post(RUN)

    assert second.status_code == 429, second.text
    assert "Retry-After" in second.headers
    assert 0 < int(second.headers["Retry-After"]) <= 60
    assert "nameservers" in second.json()["detail"]


async def test_the_same_press_is_admitted_when_the_cooldown_is_zero(
    tenants: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """Without this, a handler that returned 429 unconditionally passes.

    Same two presses, same actor, same window — one setting different.
    ``0`` is the documented way to disable the debounce, so this also
    asserts that the disabled state is reachable rather than merely
    describable.
    """
    a = tenants["a"]
    monkeypatch.setattr(worker_jobs, "make_resolver", _scripted({}))
    _pin_config(monkeypatch, health_check_manual_cooldown_seconds=0)
    await _drop_run_audits(a["user_id"])

    async with _client(a["user"]) as client:
        assert (await client.post(RUN)).status_code == 200
        second = await client.post(RUN)
    assert second.status_code == 200, second.text


async def test_the_debounce_is_per_actor_and_not_installation_wide(
    tenants: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """One operator's press must not block another tenant's board.

    The mirror of the test above: A is inside the window and refused, B
    presses in the same window and is admitted. A debounce keyed on
    anything installation-wide passes the refusal half and fails here.
    """
    a, b = tenants["a"], tenants["b"]
    monkeypatch.setattr(worker_jobs, "make_resolver", _scripted({}))
    _pin_config(monkeypatch, health_check_manual_cooldown_seconds=60)
    await _drop_run_audits(a["user_id"])
    await _drop_run_audits(b["user_id"])

    async with _client(a["user"]) as client:
        assert (await client.post(RUN)).status_code == 200
        assert (await client.post(RUN)).status_code == 429
    async with _client(b["user"]) as client:
        assert (await client.post(RUN)).status_code == 200


async def test_the_claim_is_committed_before_the_fan_out(
    tenants: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """A run that fails still consumed its cooldown, on purpose.

    A debounce that records the press *after* the work is not a
    debounce: two requests arriving together both read "nothing recent"
    and both fan out. Here the fan-out is made to raise, and the second
    press must still be refused — which is only true if the audit row
    was committed first.
    """
    a = tenants["a"]

    def exploding(_timeout: float):
        async def resolve(_name: str, _rdtype: str) -> list[str]:
            raise RuntimeError("scripted resolver failure")

        return resolve

    monkeypatch.setattr(worker_jobs, "make_resolver", exploding)
    _pin_config(monkeypatch, health_check_manual_cooldown_seconds=60)
    await _drop_run_audits(a["user_id"])

    async with _client(a["user"]) as client:
        # `_resolve_one` catches everything and classifies it as ERROR,
        # so the request itself succeeds — the point is the claim, not
        # the exception.
        first = await client.post(RUN)
        assert first.status_code == 200, first.text
        assert first.json()["error"] == 1, first.text
        assert (await client.post(RUN)).status_code == 429


# ===================================================================== #
# 4. Reach is the scope's, on both routes
# ===================================================================== #


async def test_a_tenants_run_does_not_touch_another_tenants_rows(
    tenants: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """Read off B's columns, not out of A's response body.

    A response that simply omitted B's rows would pass a body-only
    assertion while the ``UPDATE`` had rewritten them.
    """
    a, b = tenants["a"], tenants["b"]
    monkeypatch.setattr(
        worker_jobs,
        "make_resolver",
        # Answers for *both* names, so an unscoped run would visibly
        # succeed rather than merely error out on B's name.
        _scripted(
            {
                f"{a['hostname']}/A": [PUBLISHED_V4],
                f"{b['hostname']}/A": ["192.0.2.99"],
            }
        ),
    )
    before = await _read_columns(b["hostname_id"])

    await _drop_run_audits(a["user_id"])
    async with _client(a["user"]) as client:
        body = (await client.post(RUN)).json()
    assert body["hostnames_considered"] == 1, body

    after = await _read_columns(b["hostname_id"])
    assert after == before, "A's run rewrote B's health-check columns"


async def test_an_admin_run_reaches_every_tenant(
    installation_wide_sweep: None,
    tenants: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
):
    """The other half, so the scoping is not merely a broken query.

    Same endpoint, same body, one permission different. Without this,
    an implementation that checked *nothing* would pass the test above.

    **`installation_wide_sweep` is not decoration — #149.** This is the
    only test in the suite that performs an unbounded cross-tenant write
    to `ddns_hostname`, and "every tenant" is not a figure of speech:
    the sweep reaches every hostname in the database with a published
    address, ten xdist workers' worth, and rewrites four columns on up
    to `health_check_batch_size` (200) of them. Instrumented over five
    full runs it considered 30/23/17/4/21 rows against **2** of its own,
    and one run was caught holding `b0.a-jobs-gw5.example.invalid` —
    a row `test_worker_jobs.py` had created moments earlier on another
    worker, which is the non-reproducing failure #149 was opened for.
    The fixture holds `conftest.DDNS_CONFIG_LOCK` for the length of this
    test, so no reader of those columns is running while it sweeps.

    Nothing about the assertions changes. The population is still the
    installation's, and `>= 2` is still the honest comparison for a
    count whose denominator this test does not own.
    """
    a, b = tenants["a"], tenants["b"]
    monkeypatch.setattr(
        worker_jobs,
        "make_resolver",
        _scripted(
            {
                f"{a['hostname']}/A": [PUBLISHED_V4],
                f"{b['hostname']}/A": ["192.0.2.99"],
            }
        ),
    )
    await _drop_run_audits(a["user_id"])
    # The runtime half of #149's guard, and the half a census cannot
    # give: `test_harness_guards.test_the_cross_tenant_sweep_has_one_writer`
    # reads this test's *signature*, so a spelling it does not recognise
    # reads as clean. This reads the lock itself, on the way past, and a
    # sweep that reaches nine other workers' rows unheld fails here
    # rather than somewhere else an hour later.
    assert conftest._CONFIG_HELD_BY is not None, (
        "about to sweep every tenant's hostnames without holding "
        f"{conftest.DDNS_CONFIG_LOCK!r}. Take `installation_wide_sweep` — #149"
    )
    async with _client(
        a["user"], permissions=ALL_PERMS | {CROSS_TENANT_PERMISSION}
    ) as client:
        body = (await client.post(RUN)).json()

    assert body["hostnames_considered"] >= 2, body
    b_after = await _read_columns(b["hostname_id"])
    # B publishes 192.0.2.75 and the resolver answers 192.0.2.99, so the
    # admin sweep leaves a MISMATCH on B's row — a state only a run that
    # actually reached it can produce.
    assert b_after["dns_ip_v4"] == "192.0.2.99", b_after


async def test_clear_resets_to_never_checked_and_not_to_missing(
    tenants: dict[str, Any]
):
    """Three states, and the reset is the honest one.

    Clearing the addresses and leaving ``dns_checked_at`` set would read
    back as ``MISSING`` — *a check was made and the record is not
    there* — which is a measurement this endpoint has not taken. All
    four columns go to ``NULL``, which reads back as ``NEVER_CHECKED``.
    """
    a = tenants["a"]
    before = await _read_columns(a["hostname_id"])
    assert before["dns_checked_at"] is not None

    async with _client(a["user"]) as client:
        response = await client.post(CLEAR)
    assert response.status_code == 200, response.text
    assert response.json() == {"cleared": 1, "in_scope": 1}

    after = await _read_columns(a["hostname_id"])
    assert after["dns_checked_at"] is None
    assert after["dns_ip_v4"] is None
    assert after["dns_ip_v6"] is None
    assert after["dns_check_error"] is None
    assert (
        stored_dns_status(
            last_ip=after["last_ip_v4"],
            dns_ip=after["dns_ip_v4"],
            dns_checked_at=after["dns_checked_at"],
            dns_check_error=after["dns_check_error"],
        )
        is DnsCheckStatus.NEVER_CHECKED
    )
    # And what it did *not* touch: the published address is the tenant's
    # data, not a health-check result.
    assert after["last_ip_v4"] == PUBLISHED_V4


async def test_clear_does_not_reach_another_tenant(tenants: dict[str, Any]):
    a, b = tenants["a"], tenants["b"]
    before = await _read_columns(b["hostname_id"])
    async with _client(a["user"]) as client:
        assert (await client.post(CLEAR)).status_code == 200
    assert await _read_columns(b["hostname_id"]) == before


async def test_clear_deletes_no_hostname_and_no_event(tenants: dict[str, Any]):
    """`POST /admin/events/clear` is the struck route, and this is not it.

    The operator's decision (ui-parity §3.4) named that route and only
    that route. Asserted rather than asserted-in-prose because the
    cheapest wrong implementation of "clear the health checks" is a
    ``DELETE``, and it would look tidy in review.
    """
    a = tenants["a"]
    factory = get_session_factory()
    async with factory() as s:
        s.add(
            m.DnsEvent(
                user_id=a["user_id"],
                hostname_id=a["hostname_id"],
                hostname=a["hostname"],
                event_type="update",
                response_code="good",
                ip=PUBLISHED_V4,
            )
        )
        await s.commit()

    async with _client(a["user"]) as client:
        assert (await client.post(CLEAR)).status_code == 200

    async with factory() as s:
        hostnames = int(
            (
                await s.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM ddns_hostname WHERE domain_id = :d"
                    ),
                    {"d": a["domain_id"]},
                )
            ).scalar_one()
        )
        events = int(
            (
                await s.execute(
                    sa.text("SELECT COUNT(*) FROM ddns_event WHERE user_id = :u"),
                    {"u": a["user_id"]},
                )
            ).scalar_one()
        )
    assert hostnames == 1, "clear deleted a hostname"
    assert events == 1, "clear deleted a log row — that is the struck route"


async def test_the_denominator_is_reported_beside_the_count(
    tenants: dict[str, Any]
):
    """`cleared: 0` is a measurement only when `in_scope` is beside it.

    A second clear removes nothing, because the first one already did.
    ``0 / 1`` and ``0 / 0`` are different facts and the body renders
    both.
    """
    a = tenants["a"]
    async with _client(a["user"]) as client:
        assert (await client.post(CLEAR)).json() == {"cleared": 1, "in_scope": 1}
        again = (await client.post(CLEAR)).json()
    # This is the assertion that found a real defect. Without the "has a
    # result" predicate on the UPDATE, `rowcount` on this driver reports
    # rows *matched* rather than rows *changed*, so a second clear said
    # `cleared: 1` about a row that carried nothing. Pressing the button
    # once agrees with both implementations; pressing it twice does not.
    assert again["cleared"] == 0
    assert again["in_scope"] == 1


# ===================================================================== #
# 5. The gate, and the audit trail
# ===================================================================== #


async def test_the_permission_gate_bites_on_both_routes(tenants: dict[str, Any]):
    """A caller holding the neighbouring permission and not this one.

    ``permissions=set()`` alone would pass against an endpoint gated on
    anything at all; holding ``atrium_ddns.domain.manage`` and being
    refused is what names the specific gate.
    """
    a = tenants["a"]
    async with _client(a["user"], permissions={DOMAIN_MANAGE_PERMISSION}) as client:
        assert (await client.post(RUN)).status_code == 403
        assert (await client.post(CLEAR)).status_code == 403
    async with _client(a["user"], permissions={BOARD_READ_PERMISSION}) as client:
        assert (await client.post(CLEAR)).status_code == 200


async def test_both_actions_write_an_audit_row_naming_the_reach(
    tenants: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """An operator action that leaves no trace is not an operator action.

    ``cross_tenant`` is on the row because *"an admin re-checked
    everything"* and *"a tenant re-checked their own"* are different
    events, and the endpoint is the same one.
    """
    a = tenants["a"]
    monkeypatch.setattr(worker_jobs, "make_resolver", _scripted({}))
    await _drop_run_audits(a["user_id"])

    async with _client(a["user"]) as client:
        assert (await client.post(RUN)).status_code == 200
        assert (await client.post(CLEAR)).status_code == 200

    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                sa.select(AuditLog.action, AuditLog.diff)
                .where(
                    AuditLog.entity == AUDIT_ENTITY_HEALTH_CHECK,
                    AuditLog.actor_user_id == a["user_id"],
                )
                .order_by(AuditLog.id)
            )
        ).all()
    actions = [row[0] for row in rows]
    assert actions == [AUDIT_ACTION_RUN, AUDIT_ACTION_CLEAR], actions
    assert rows[0][1]["cross_tenant"] is False
    assert rows[1][1]["in_scope"] == 1
