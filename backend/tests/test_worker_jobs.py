"""The worker's two jobs — health checks and the retention prune.

Four things this file is written to *prove* rather than assert in
prose, because each is an acceptance criterion on #17 and each has a
plausible-looking implementation that would pass a weaker test.

**1. A job that raises does not stop the scheduler.**
``test_a_raising_job_does_not_stop_the_scheduler`` starts a real
``AsyncIOScheduler``, registers a body that raises on every fire
alongside one that does not, lets both tick several times and asserts
the healthy one kept firing. Its sibling
``test_the_scheduler_survives_an_unguarded_raiser_too`` runs the same
experiment *without* :func:`~atrium_ddns.worker_jobs.guarded` and
reports what that measures — which is the honest answer, and not the
one the issue implies. See that test's docstring.

**2. ``n/a`` is not ``0``.** Twice over, because there are two boards.
Per device, :meth:`DeviceStatus.render_updates` has to produce three
*different* strings for never-called / last-call-failed / zero-in-window
and only the last of them may be ``"0"``. Per hostname, the three
states have to survive a round trip through MySQL: the job's own
classification and an independent re-derivation from the persisted
columns (:func:`stored_dns_status`) are two instruments of different
shape, and ``test_the_three_hostname_states_are_distinguishable_in_the_database``
reads the columns back with raw SQL rather than through the ORM that
wrote them.

**3. The prune is bounded, keyed and non-vacuous.** #14 found a
``DELETE`` next door that could never match a row and still took a
scan-wide lock. So: rows are seeded on **both** sides of the cutoff and
the tests assert the count removed *and* the count kept; the captured
SQL is checked for the ``DELETE … WHERE id IN (…)`` shape and for the
*absence* of a bare ``DELETE … WHERE created_at < …``; and the batch
ceiling is exercised by making it bite.

**4. Every query goes through ``DdnsScope``.** The strongest available
instrument is behavioural and it is used: the prune and the health
check are run under **tenant B's** scope over a database holding
tenant A's rows, and tenant A's rows have to survive untouched. A
statement that skipped the scope would delete or stamp them. A source
guard backs it up.

Parallel-safety, and why it is not incidental
---------------------------------------------
The ``worker`` container of the very stack these tests run against is
executing the same two jobs on a 60 s / 3600 s tick, cross-tenant,
against this database. A test that seeds a due hostname and then
asserts nothing stamped it is racing a real scheduler.

Two things make it deterministic. The namespace is pinned to
``health_check_enabled=False`` for the whole file (``config`` fixture),
so the deployed worker's tick returns immediately; and the direct calls
pass their own :class:`DdnsConfig`. The tests that must exercise the
namespace path assert on values the worker cannot move — a cutoff
computed inside the call, a summary flag — rather than on rows.

Everything created here is namespaced by ``PYTEST_XDIST_WORKER``.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Any, Iterator

import dns.exception
import dns.resolver
import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.db import get_engine, get_session_factory
from app.models.auth import User
from app.models.ops import AppSetting
from app.services.app_config import NAMESPACES, get_namespace, put_namespace

from atrium_ddns import models as m
from atrium_ddns import worker_jobs as wj
from atrium_ddns.scope import DdnsScope
from atrium_ddns.worker_jobs import (
    CONFIG_NAMESPACE,
    DdnsConfig,
    DeviceStatus,
    DnsCheckStatus,
    Liveness,
    classify_record,
    device_statuses,
    guarded,
    load_config,
    register_jobs,
    run_health_checks,
    run_retention_prune,
    stored_dns_status,
    worst,
)

# No `pytestmark = pytest.mark.asyncio`: `asyncio_mode = "auto"` already
# collects the coroutine tests, and the blanket mark makes pytest warn
# on every synchronous test in the file.

W = os.environ.get("PYTEST_XDIST_WORKER", "serial")


def _now() -> datetime:
    return wj._now()


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #


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
            await s.execute(sa.text("DELETE FROM users WHERE email = :e"), {"e": email})
        await s.commit()


@pytest_asyncio.fixture
async def tenants():
    """Two tenants, each with a domain and a device. Rows are added per
    test; this only builds the skeleton every test needs."""
    factory = get_session_factory()
    emails = [f"ddns-jobs-a-{W}@example.invalid", f"ddns-jobs-b-{W}@example.invalid"]
    await _purge(emails)

    built: dict[str, Any] = {"emails": emails}
    async with factory() as s:
        for tag, email in (("a", emails[0]), ("b", emails[1])):
            user = User(
                email=email,
                hashed_password=_password_hash(),
                is_active=True,
                is_verified=True,
                full_name=f"DDNS jobs probe {W}",
                preferred_language="en",
            )
            s.add(user)
            await s.flush()
            domain = m.Domain(user_id=user.id, name=f"{tag}-jobs-{W}.example.invalid")
            device = m.Device(
                user_id=user.id,
                username=f"ddns-jobs-{tag}-{W}",
                password_hash="$argon2id$v=19$m=65536,t=3,p=4$fake$fake",
                name=f"router-{tag}",
            )
            s.add_all([domain, device])
            await s.flush()
            built[tag] = {
                "user_id": user.id,
                "email": email,
                "domain_id": domain.id,
                "device_id": device.id,
            }
        await s.commit()

    yield built

    await _purge(emails)


@pytest_asyncio.fixture
async def config():
    """Own the ``atrium_ddns`` KV row for the duration, and restore it.

    Pins ``health_check_enabled=False`` on entry so the stack's own
    worker container stops sweeping while these tests assert on rows.
    """
    factory = get_session_factory()
    async with factory() as s:
        before = (
            await s.execute(
                sa.select(AppSetting.value).where(AppSetting.key == CONFIG_NAMESPACE)
            )
        ).scalar_one_or_none()

    async def _write(cfg: DdnsConfig) -> None:
        async with factory() as s:
            await put_namespace(s, CONFIG_NAMESPACE, cfg.model_dump(mode="json"))

    await _write(DdnsConfig(health_check_enabled=False))
    yield _write

    async with factory() as s:
        if before is None:
            await s.execute(
                sa.delete(AppSetting).where(AppSetting.key == CONFIG_NAMESPACE)
            )
        else:
            await s.execute(
                sa.update(AppSetting)
                .where(AppSetting.key == CONFIG_NAMESPACE)
                .values(value=before)
            )
        await s.commit()


@contextlib.contextmanager
def capture_sql() -> Iterator[list[str]]:
    """Every statement the driver actually sent while the block ran.

    ``before_cursor_execute`` is downstream of the ORM and of anything
    this module does in Python, so what lands here is what MySQL was
    asked — which is a different instrument from reading the code that
    built the statement.
    """
    engine = get_engine().sync_engine
    seen: list[str] = []

    def _on(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        seen.append(statement)

    sa.event.listen(engine, "before_cursor_execute", _on)
    try:
        yield seen
    finally:
        sa.event.remove(engine, "before_cursor_execute", _on)


class FakeResolver:
    """``(name, rdtype) -> answers``, or an exception to raise.

    Returning ``[]`` is *measured and absent*; raising is *could not
    measure*. The two are separate inputs here for the same reason they
    are separate states downstream.
    """

    def __init__(self, answers: dict[tuple[str, str], Any]):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, name: str, rdtype: str) -> list[str]:
        self.calls.append((name, rdtype))
        value = self.answers.get((name, rdtype), [])
        if isinstance(value, BaseException):
            raise value
        return list(value)


async def _add_hostname(domain_id: int, name: str, **kwargs: Any) -> int:
    factory = get_session_factory()
    async with factory() as s:
        hostname = m.Hostname(domain_id=domain_id, name=name, **kwargs)
        s.add(hostname)
        await s.flush()
        hid = hostname.id
        await s.commit()
    return hid


async def _add_events(rows: list[dict[str, Any]]) -> None:
    factory = get_session_factory()
    async with factory() as s:
        for row in rows:
            s.add(m.DnsEvent(**row))
        await s.commit()


async def _count(sql: str, **params: Any) -> int:
    factory = get_session_factory()
    async with factory() as s:
        return int((await s.execute(sa.text(sql), params)).scalar_one())


# ===================================================================== #
# 1. The namespace is registered at import time, in both processes
# ===================================================================== #


async def test_the_namespace_is_registered_and_carries_the_config_model():
    assert CONFIG_NAMESPACE in NAMESPACES
    assert NAMESPACES[CONFIG_NAMESPACE].model is DdnsConfig
    # Operator settings, not branding: it must not ride the
    # unauthenticated public-config bundle.
    assert NAMESPACES[CONFIG_NAMESPACE].public is False


#: Every module in the host package, so the AST guard below sweeps to a
#: negative result rather than reporting the two files it happened to
#: look at. Derived from the package directory, so a module added by a
#: later issue is covered the moment it exists.
def _package_modules() -> list[pathlib.Path]:
    root = pathlib.Path(inspect.getsourcefile(wj)).parent
    return sorted(root.rglob("*.py"))


def test_register_namespace_is_never_called_inside_a_function():
    """The structural half of the trap, read off the AST, package-wide.

    Atrium imports ``bootstrap`` from the api process *and* the worker
    process, but only the api calls ``init_app``. A
    ``register_namespace`` inside ``init_app`` leaves the worker's
    ``NAMESPACES`` without the key, and every ``get_namespace`` on a
    scheduler tick raises ``KeyError`` where nobody is looking.

    Asserted on the syntax tree rather than by grepping, so indentation
    is what is measured and a comment mentioning ``init_app`` cannot
    satisfy it — and swept over every module in the package rather than
    the one that happens to hold the call today.
    """
    top_level: list[str] = []
    nested: list[str] = []
    for path in _package_modules():
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "register_namespace"
            ):
                top_level.append(path.name)
        for parent in tree.body:
            if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(parent):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "register_namespace"
                ):
                    nested.append(f"{path.name}:{parent.name}")

    assert top_level == ["worker_jobs.py"], (
        f"expected exactly one module-level register_namespace call, in the "
        f"module that reads the namespace; found {top_level}"
    )
    assert nested == [], (
        f"register_namespace is called inside {nested}. Only the api process "
        f"calls init_app; the worker imports the same module and does not, so "
        f"a namespace registered in a function is invisible to every worker "
        f"job that reads it."
    )


@pytest.mark.parametrize(
    "module",
    [
        # What ``ATRIUM_HOST_MODULE`` names — the api's and the worker's
        # own import path.
        "atrium_ddns.bootstrap",
        # …and the module that *reads* the key, imported on its own.
        # This is the case that actually bit: the pytest session imports
        # `worker_jobs` and never `bootstrap`, and with the registration
        # in bootstrap.py the first fixture raised
        # `KeyError: 'atrium_ddns'` out of `put_namespace`.
        "atrium_ddns.worker_jobs",
    ],
)
def test_importing_the_host_module_is_enough_to_register_the_namespace(module: str):
    """A fresh interpreter, one import, no ``init_app`` call."""
    code = (
        f"import {module}\n"
        "from app.services.app_config import NAMESPACES\n"
        "print('PRESENT' if 'atrium_ddns' in NAMESPACES else 'MISSING')\n"
        "print(sorted(NAMESPACES))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == "PRESENT", (
        f"a process that imported {module} and called nothing does not have "
        f"the namespace. stdout={proc.stdout!r} stderr={proc.stderr[-800:]!r}"
    )


async def test_get_namespace_answers_with_the_defaults_when_nothing_is_seeded(config):
    """``get_namespace`` must not need a migration to have written a row."""
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(sa.delete(AppSetting).where(AppSetting.key == CONFIG_NAMESPACE))
        await s.commit()
    async with factory() as s:
        value = await get_namespace(s, CONFIG_NAMESPACE)
    assert isinstance(value, DdnsConfig)
    assert value.event_retention_days == 30


# ===================================================================== #
# 2. Retention: default, tunable, read at run time
# ===================================================================== #


def test_the_retention_default_is_thirty_days():
    assert DdnsConfig().event_retention_days == 30
    # …and the limiter's window is a *separate* knob. models.py: hanging
    # the limiter off the log's retention makes "how long do we keep
    # logs" silently also mean "how far back does the limiter look".
    assert DdnsConfig().rate_limit_event_retention_hours == 24
    assert (
        "rate_limit_event_retention_hours"
        in DdnsConfig.model_fields  # noqa: SIM118 — pydantic mapping
    )


def test_the_defaults_that_were_measured_off_the_legacy_service():
    """Not invented. ``health_check_config.check_interval_minutes``
    defaults to 15 and ``enabled`` to True in the legacy model;
    ``dyndns.py`` builds its global limiter at
    ``requests_per_minute=30``."""
    cfg = DdnsConfig()
    assert cfg.health_check_interval_minutes == 15
    assert cfg.health_check_enabled is True
    assert cfg.rate_limit_per_minute == 30


async def test_retention_is_read_at_run_time_not_captured_at_import(config, tenants):
    """Change the namespace, run again, watch the cutoff move.

    The cutoff is computed inside the call and returned on the summary,
    so this asserts on a value the concurrently-running worker cannot
    touch. If the window were read at import — a module constant, a
    default argument evaluated once — the second cutoff would equal the
    first.
    """
    scope = DdnsScope.for_user_id(tenants["a"]["user_id"])

    await config(DdnsConfig(health_check_enabled=False, event_retention_days=30))
    before = _now()
    first = await run_retention_prune(scope=scope)
    after = _now()
    event_first = next(s for s in first if s.table == "ddns_event")
    assert before - timedelta(days=30) <= event_first.cutoff <= after - timedelta(days=30)

    await config(DdnsConfig(health_check_enabled=False, event_retention_days=90))
    before = _now()
    second = await run_retention_prune(scope=scope)
    after = _now()
    event_second = next(s for s in second if s.table == "ddns_event")
    assert before - timedelta(days=90) <= event_second.cutoff <= after - timedelta(days=90)

    # Vacuity guard: the two readings have to actually differ, or this
    # test passes against an implementation that ignores the setting.
    assert event_second.cutoff < event_first.cutoff - timedelta(days=59)


async def test_the_config_round_trips_through_mysql(config):
    """Written through ``put_namespace``, read back through
    ``load_config`` — the path the jobs use, not a Python round trip."""
    await config(
        DdnsConfig(
            health_check_enabled=False,
            event_retention_days=7,
            prune_batch_size=3,
            device_idle_window_days=2,
        )
    )
    factory = get_session_factory()
    async with factory() as s:
        loaded = await load_config(s)
    assert (loaded.event_retention_days, loaded.prune_batch_size) == (7, 3)
    assert loaded.device_idle_window_days == 2
    assert loaded.health_check_enabled is False


# ===================================================================== #
# 3. The prune: non-vacuous, bounded, idempotent, keyed
# ===================================================================== #


async def _seed_events(tenant: dict[str, Any], *, old: int, fresh: int) -> None:
    now = _now()
    rows = []
    for i in range(old):
        rows.append(
            dict(
                user_id=tenant["user_id"],
                device_id=tenant["device_id"],
                domain_id=tenant["domain_id"],
                user_email=tenant["email"],
                event_type="update",
                response_code="good",
                created_at=now - timedelta(days=40 + i),
            )
        )
    for i in range(fresh):
        rows.append(
            dict(
                user_id=tenant["user_id"],
                device_id=tenant["device_id"],
                domain_id=tenant["domain_id"],
                user_email=tenant["email"],
                event_type="update",
                response_code="good",
                created_at=now - timedelta(days=i),
            )
        )
    await _add_events(rows)


async def _event_counts(email: str, cutoff: datetime) -> tuple[int, int]:
    older = await _count(
        "SELECT COUNT(*) FROM ddns_event WHERE user_email = :e AND created_at < :c",
        e=email,
        c=cutoff,
    )
    newer = await _count(
        "SELECT COUNT(*) FROM ddns_event WHERE user_email = :e AND created_at >= :c",
        e=email,
        c=cutoff,
    )
    return older, newer


async def test_the_prune_removes_what_it_should_and_keeps_what_it_should(
    config, tenants
):
    """Non-vacuous, both directions, counted before and after.

    #14's warning applied literally: a prune that cannot match a row
    still takes a lock, and a test that only asserts ``deleted >= 0``
    passes for it. So the seed straddles the cutoff and both sides are
    asserted — and the *pre* counts are asserted too, so the test
    cannot pass on an empty table.
    """
    await config(DdnsConfig(health_check_enabled=False, event_retention_days=30))
    a, b = tenants["a"], tenants["b"]
    await _seed_events(a, old=7, fresh=5)
    await _seed_events(b, old=4, fresh=2)

    cutoff = _now() - timedelta(days=30)
    a_old_before, a_new_before = await _event_counts(a["email"], cutoff)
    b_old_before, b_new_before = await _event_counts(b["email"], cutoff)
    assert (a_old_before, a_new_before) == (7, 5), "the seed did not land"
    assert (b_old_before, b_new_before) == (4, 2), "the seed did not land"

    # Tenant A's scope. Nothing of B's may move — which is also the
    # proof that every statement went through DdnsScope: an unscoped
    # DELETE would take B's four with it.
    summaries = await run_retention_prune(scope=DdnsScope.for_user_id(a["user_id"]))
    event_summary = next(s for s in summaries if s.table == "ddns_event")

    a_old_after, a_new_after = await _event_counts(a["email"], cutoff)
    b_old_after, b_new_after = await _event_counts(b["email"], cutoff)

    assert event_summary.deleted == 7
    assert (a_old_after, a_new_after) == (0, 5)
    assert (b_old_after, b_new_after) == (4, 2), (
        "the prune reached outside its scope — tenant B's rows moved"
    )
    assert event_summary.truncated is False


async def test_the_prune_is_idempotent(config, tenants):
    await config(DdnsConfig(health_check_enabled=False, event_retention_days=30))
    a = tenants["a"]
    await _seed_events(a, old=3, fresh=2)
    scope = DdnsScope.for_user_id(a["user_id"])

    first = next(
        s for s in await run_retention_prune(scope=scope) if s.table == "ddns_event"
    )
    second = next(
        s for s in await run_retention_prune(scope=scope) if s.table == "ddns_event"
    )
    assert first.deleted == 3
    assert second.deleted == 0
    assert second.batches == 0
    cutoff = _now() - timedelta(days=30)
    assert await _event_counts(a["email"], cutoff) == (0, 2)


async def test_the_prune_is_bounded_and_says_so_when_it_stops_early(config, tenants):
    """Make the ceiling bite, then finish the job on the next run.

    ``truncated`` is the flag that stops a partial sweep reading
    identically to a complete one — the same defect as a probe that
    prints the same string whether or not the thing it measures exists.
    """
    await config(DdnsConfig(health_check_enabled=False, event_retention_days=30))
    a = tenants["a"]
    await _seed_events(a, old=7, fresh=1)
    scope = DdnsScope.for_user_id(a["user_id"])
    cutoff = _now() - timedelta(days=30)

    partial = next(
        s
        for s in await run_retention_prune(
            scope=scope,
            config=DdnsConfig(
                health_check_enabled=False,
                event_retention_days=30,
                prune_batch_size=2,
                prune_max_batches=2,
            ),
        )
        if s.table == "ddns_event"
    )
    assert partial.deleted == 4
    assert partial.batches == 2
    assert partial.truncated is True, "a partial sweep reported itself as complete"
    assert await _event_counts(a["email"], cutoff) == (3, 1)

    rest = next(
        s for s in await run_retention_prune(scope=scope) if s.table == "ddns_event"
    )
    assert rest.deleted == 3
    assert rest.truncated is False
    assert await _event_counts(a["email"], cutoff) == (0, 1)


async def test_the_delete_that_reaches_mysql_is_keyed_and_batched(config, tenants):
    """The second instrument: the SQL, not the summary.

    A summary saying ``deleted=7, batches=4`` is produced by the same
    code that chose the statement. This reads what the driver sent.
    Two properties, and the negative one is the load-bearing one:

    - every ``DELETE FROM ddns_event`` carries ``id IN (…)`` — record
      locks on named primary keys;
    - **no** statement is a bare ``DELETE … WHERE created_at < …``,
      which is the legacy write-path shape and takes locks
      proportional to the history rather than to the batch.
    """
    await config(DdnsConfig(health_check_enabled=False, event_retention_days=30))
    a = tenants["a"]
    await _seed_events(a, old=7, fresh=0)
    scope = DdnsScope.for_user_id(a["user_id"])

    with capture_sql() as seen:
        summary = next(
            s
            for s in await run_retention_prune(
                scope=scope,
                config=DdnsConfig(
                    health_check_enabled=False,
                    event_retention_days=30,
                    prune_batch_size=2,
                ),
            )
            if s.table == "ddns_event"
        )

    deletes = [s for s in seen if s.strip().upper().startswith("DELETE FROM DDNS_EVENT")]
    assert summary.deleted == 7
    assert summary.batches == 4  # 2 + 2 + 2 + 1
    assert len(deletes) == 4, f"expected one DELETE per batch, got {len(deletes)}"
    for statement in deletes:
        assert "ddns_event.id IN" in statement.replace("\n", " "), statement
        assert "created_at <" not in statement, (
            "a DELETE reached MySQL keyed on a range rather than on primary "
            f"keys — that is the unbounded lock this job exists to avoid: {statement}"
        )
    # …and the tenancy predicate is in the SQL, not applied in Python.
    for statement in deletes:
        assert "user_id" in statement, statement


async def test_a_cross_tenant_sweep_reaches_every_tenant_and_says_why(config, tenants):
    """The prune's production scope: unrestricted, and it says why.

    ``DdnsScope.cross_tenant`` refuses an empty reason and the sweep's
    is recorded at the site that builds it. The behavioural claim is
    the one that matters — both tenants' expired rows go, and both
    tenants' fresh rows stay.
    """
    await config(DdnsConfig(health_check_enabled=False, event_retention_days=30))
    a, b = tenants["a"], tenants["b"]
    await _seed_events(a, old=2, fresh=1)
    await _seed_events(b, old=3, fresh=1)
    cutoff = _now() - timedelta(days=30)

    scope = wj._sweep_scope()
    assert scope.cross_tenant_reason and scope.cross_tenant_reason.strip()
    assert scope.reaches_all_tenants(m.DnsEvent) is True

    with capture_sql() as seen:
        summaries = await run_retention_prune(scope=scope)

    assert await _event_counts(a["email"], cutoff) == (0, 1)
    assert await _event_counts(b["email"], cutoff) == (0, 1)
    assert next(s for s in summaries if s.table == "ddns_event").deleted >= 5

    deletes = [s for s in seen if s.strip().upper().startswith("DELETE FROM DDNS_EVENT")]
    assert deletes, "no DELETE reached MySQL"
    for statement in deletes:
        flat = statement.replace("\n", " ")
        assert "ddns_event.id IN" in flat, flat
        # No tenancy predicate, because there is no tenant. The
        # *difference* from the scoped case is the instrument —
        # `test_the_delete_that_reaches_mysql_is_keyed_and_batched`
        # asserts `user_id` is present when a tenant scope is used, so
        # these two readings together show the scope is doing work
        # rather than being decorative.
        assert "user_id" not in flat, flat


def test_the_literal_true_survives_a_lone_predicate_and_not_a_composed_one():
    """A correction to #14's ``scope.py``, recorded as a test.

    ``scope.py`` states — and ``test_tenant_isolation.py`` asserts —
    that an unrestricted scope is "spelled in the SQL, as a literal
    ``true``, rather than being the absence of a clause", so that an
    unscoped query and a deliberately cross-tenant one are
    distinguishable in a slow-query log.

    That is true of ``scope.select(Model)``, where the constant is the
    *only* predicate, and it stops being true the moment anything is
    ANDed onto it: SQLAlchemy's ``BooleanClauseList`` drops ``True_``
    operands, so this module's ``DELETE … WHERE created_at < …`` came
    out as a bare ``WHERE ddns_event.id IN (…)`` with no trace of the
    scope. Found by an assertion written from #14's docstring, which is
    the only reason it is written down instead of assumed.

    Nothing here is wrong — the predicate is a no-op and eliding a
    no-op is correct — but the *observability* claim does not survive
    composition, and a reviewer relying on it to audit a slow-query log
    would be reading the wrong thing. This test pins both halves so the
    boundary is explicit rather than folklore.
    """
    from sqlalchemy.dialects import mysql

    scope = wj._sweep_scope()

    def _sql(stmt: Any) -> str:
        return str(
            stmt.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
        ).replace("\n", " ")

    constant = _const_where(sa.true())
    assert constant == "true = 1"

    # Alone: visible, exactly as scope.py claims.
    assert constant in _sql(scope.select(m.DnsEvent))

    # Composed: elided. Both the SELECT the prune issues…
    composed = scope.select(m.DnsEvent, m.DnsEvent.id).where(
        m.DnsEvent.created_at < _now()
    )
    assert constant not in _sql(composed)
    # …and the DELETE.
    deletion = scope.apply(
        sa.delete(m.DnsEvent).where(m.DnsEvent.id.in_([1, 2])), m.DnsEvent
    )
    assert constant not in _sql(deletion)

    # And the tenant-restricted form is not elided, which is what makes
    # the elision safe: the predicate that carries information survives.
    tenant = DdnsScope.for_user_id(4242)
    assert "user_id" in _sql(
        tenant.apply(sa.delete(m.DnsEvent).where(m.DnsEvent.id.in_([1, 2])), m.DnsEvent)
    )


def _const_where(clause: Any) -> str:
    """How a constant predicate renders on this dialect, asked of
    SQLAlchemy rather than typed out — ``sa.true()`` compiles to
    ``true = 1`` on MySQL, and hardcoding that is a defect one
    SQLAlchemy release later."""
    from sqlalchemy.dialects import mysql

    compiled = str(
        sa.select(sa.literal_column("1"))
        .where(clause)
        .compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
    )
    return compiled.split("WHERE", 1)[1].strip()


async def test_the_rate_limit_table_is_pruned_on_its_own_window(config, tenants):
    """``ddns_rate_limit_event`` has an index built for a sweep. A table
    with a sweep-shaped index and no sweep is the artefact-with-no-writer
    defect; this is the writer, and it runs on its own knob."""
    await config(
        DdnsConfig(
            health_check_enabled=False,
            event_retention_days=30,
            rate_limit_event_retention_hours=24,
        )
    )
    a = tenants["a"]
    now = _now()
    factory = get_session_factory()
    async with factory() as s:
        s.add(m.RateLimitEvent(device_id=a["device_id"], created_at=now - timedelta(hours=48)))
        s.add(m.RateLimitEvent(device_id=a["device_id"], created_at=now - timedelta(hours=30)))
        s.add(m.RateLimitEvent(device_id=a["device_id"], created_at=now - timedelta(minutes=5)))
        await s.commit()

    def _mine() -> str:
        return (
            "SELECT COUNT(*) FROM ddns_rate_limit_event WHERE device_id = :d "
            "AND created_at < :c"
        )

    cutoff = now - timedelta(hours=24)
    assert await _count(_mine(), d=a["device_id"], c=cutoff) == 2

    summaries = await run_retention_prune(scope=DdnsScope.for_user_id(a["user_id"]))
    rl = next(s for s in summaries if s.table == "ddns_rate_limit_event")
    assert rl.deleted == 2
    assert await _count(_mine(), d=a["device_id"], c=cutoff) == 0
    assert (
        await _count(
            "SELECT COUNT(*) FROM ddns_rate_limit_event WHERE device_id = :d",
            d=a["device_id"],
        )
        == 1
    ), "the fresh row was taken too"


# ===================================================================== #
# 4. Health checks
# ===================================================================== #


async def test_the_health_check_classifies_ok_mismatch_missing_and_error(
    config, tenants
):
    a = tenants["a"]
    zone = f"a-jobs-{W}.example.invalid"
    ok = f"ok.{zone}"
    mismatch = f"mismatch.{zone}"
    missing = f"missing.{zone}"
    error = f"error.{zone}"
    never = f"never.{zone}"

    ids = {
        "ok": await _add_hostname(a["domain_id"], ok, last_ip_v4="192.0.2.10"),
        "mismatch": await _add_hostname(a["domain_id"], mismatch, last_ip_v4="192.0.2.20"),
        "missing": await _add_hostname(a["domain_id"], missing, last_ip_v4="192.0.2.30"),
        "error": await _add_hostname(a["domain_id"], error, last_ip_v4="192.0.2.40"),
        # No last_ip at all: nothing to compare against. This is the
        # `n/a` slice — counted as `hostnames_never_written`, never
        # resolved, and its dns_checked_at stays NULL.
        "never": await _add_hostname(a["domain_id"], never),
    }

    resolver = FakeResolver(
        {
            (ok, "A"): ["192.0.2.10"],
            (mismatch, "A"): ["198.51.100.1"],
            (missing, "A"): [],
            (error, "A"): dns.exception.Timeout(),
        }
    )
    summary = await run_health_checks(
        resolve=resolver,
        scope=DdnsScope.for_user_id(a["user_id"]),
        config=DdnsConfig(health_check_enabled=True),
    )

    assert summary.hostnames_considered == 5
    assert summary.hostnames_never_written == 1
    assert summary.hostnames_checked == 4
    assert summary.records_checked == 4
    assert (summary.ok, summary.mismatch, summary.missing, summary.error) == (1, 1, 1, 1)
    summary.assert_consistent()

    # The `n/a` hostname was never resolved — not resolved-and-found-nothing.
    assert (never, "A") not in resolver.calls
    assert (never, "AAAA") not in resolver.calls

    factory = get_session_factory()
    async with factory() as s:
        rows = {
            row.id: row
            for row in (
                await s.execute(
                    sa.text(
                        "SELECT id, dns_ip_v4, dns_checked_at, dns_check_error "
                        "FROM ddns_hostname WHERE id IN :ids"
                    ).bindparams(sa.bindparam("ids", expanding=True)),
                    {"ids": list(ids.values())},
                )
            ).all()
        }

    assert rows[ids["ok"]].dns_ip_v4 == "192.0.2.10"
    assert rows[ids["ok"]].dns_check_error is None
    assert rows[ids["mismatch"]].dns_ip_v4 == "198.51.100.1"
    assert rows[ids["missing"]].dns_ip_v4 is None
    assert rows[ids["missing"]].dns_check_error is None
    assert rows[ids["error"]].dns_ip_v4 is None
    assert rows[ids["error"]].dns_check_error is not None
    assert rows[ids["never"]].dns_checked_at is None


async def test_the_three_hostname_states_are_distinguishable_in_the_database(
    config, tenants
):
    """``n/a`` / error / measured-zero, read back off the columns.

    The second instrument. :func:`classify_record` is what the job
    decided in memory; :func:`stored_dns_status` re-derives the state
    from the three persisted columns using nothing the job kept. If the
    columns cannot tell ``ERROR`` from ``MISSING`` from ``NEVER_CHECKED``
    then the status board — which reads the columns — is where the
    collapse happens, and no in-memory test would see it.
    """
    a = tenants["a"]
    zone = f"a-jobs-{W}.example.invalid"
    missing = f"m3.{zone}"
    error = f"e3.{zone}"
    never = f"n3.{zone}"
    ids = {
        "missing": await _add_hostname(a["domain_id"], missing, last_ip_v4="192.0.2.30"),
        "error": await _add_hostname(a["domain_id"], error, last_ip_v4="192.0.2.40"),
    }

    resolver = FakeResolver(
        {
            (missing, "A"): [],
            (error, "A"): dns.resolver.NoNameservers(),
        }
    )
    await run_health_checks(
        resolve=resolver,
        scope=DdnsScope.for_user_id(a["user_id"]),
        config=DdnsConfig(health_check_enabled=True),
    )
    # Added *after* the sweep, so it is never-checked by construction
    # rather than by whichever rows a batch ceiling happened to leave —
    # a state that depends on an unspecified ordering is not a state.
    ids["never"] = await _add_hostname(a["domain_id"], never, last_ip_v4="192.0.2.50")

    factory = get_session_factory()
    async with factory() as s:
        rows = {
            row.id: row
            for row in (
                await s.execute(
                    sa.text(
                        "SELECT id, last_ip_v4, dns_ip_v4, dns_checked_at, "
                        "dns_check_error FROM ddns_hostname WHERE id IN :ids"
                    ).bindparams(sa.bindparam("ids", expanding=True)),
                    {"ids": list(ids.values())},
                )
            ).all()
        }

    derived = {
        key: stored_dns_status(
            last_ip=rows[hid].last_ip_v4,
            dns_ip=rows[hid].dns_ip_v4,
            dns_checked_at=rows[hid].dns_checked_at,
            dns_check_error=rows[hid].dns_check_error,
        )
        for key, hid in ids.items()
    }
    assert derived["never"] is DnsCheckStatus.NEVER_CHECKED
    assert derived["error"] is DnsCheckStatus.ERROR
    assert derived["missing"] is DnsCheckStatus.MISSING
    assert len(set(derived.values())) == 3, (
        f"the three states collapsed in storage: {derived}"
    )

    # …and the raw column triples are pairwise distinct too, so the
    # distinction is in the data rather than only in the reader.
    triples = {
        key: (rows[hid].dns_ip_v4, rows[hid].dns_checked_at is None, rows[hid].dns_check_error)
        for key, hid in ids.items()
    }
    assert len(set(triples.values())) == 3, triples


async def test_an_ipv6_answer_spelled_differently_is_not_a_mismatch(config, tenants):
    """The legacy check compared address *strings*.

    ``2001:db8::1`` and ``2001:0db8:0000:0000:0000:0000:0000:0001`` are
    one address and two strings, and plan §3.3.1 measured this estate
    at 305 of 448 events IPv6 — so a string comparison would have
    reported mismatches for the majority of real traffic. The regression
    guard is the second assertion: raw strings differ, canonical
    comparison says OK.
    """
    a = tenants["a"]
    name = f"v6.a-jobs-{W}.example.invalid"
    stored_ip = "2001:db8::1"
    answered = "2001:0db8:0000:0000:0000:0000:0000:0001"
    assert stored_ip != answered, "the fixture stopped exercising the spelling"

    hid = await _add_hostname(a["domain_id"], name, last_ip_v6=stored_ip)
    resolver = FakeResolver({(name, "AAAA"): [answered]})
    summary = await run_health_checks(
        resolve=resolver,
        scope=DdnsScope.for_user_id(a["user_id"]),
        config=DdnsConfig(health_check_enabled=True),
    )
    assert (summary.ok, summary.mismatch) == (1, 0)

    factory = get_session_factory()
    async with factory() as s:
        row = (
            await s.execute(
                sa.text("SELECT dns_ip_v6 FROM ddns_hostname WHERE id = :i"), {"i": hid}
            )
        ).one()
    assert row.dns_ip_v6 == "2001:db8::1"


async def test_a_second_answer_in_the_set_still_counts_as_a_match(config, tenants):
    """The legacy check read ``answers[0]`` and compared that one.

    A name with two A records then matched only when the resolver
    happened to return the tracked address first — a coin flip that
    reads as an intermittent mismatch alert.
    """
    a = tenants["a"]
    name = f"multi.a-jobs-{W}.example.invalid"
    hid = await _add_hostname(a["domain_id"], name, last_ip_v4="192.0.2.30")
    resolver = FakeResolver({(name, "A"): ["198.51.100.7", "192.0.2.30"]})
    summary = await run_health_checks(
        resolve=resolver,
        scope=DdnsScope.for_user_id(a["user_id"]),
        config=DdnsConfig(health_check_enabled=True),
    )
    assert (summary.ok, summary.mismatch) == (1, 0)
    factory = get_session_factory()
    async with factory() as s:
        row = (
            await s.execute(
                sa.text("SELECT dns_ip_v4 FROM ddns_hostname WHERE id = :i"), {"i": hid}
            )
        ).one()
    # The address written back is the matching one, not answers[0].
    assert row.dns_ip_v4 == "192.0.2.30"


async def test_the_health_check_honours_its_enabled_flag_from_the_namespace(
    config, tenants
):
    """No injected config: the flag comes from MySQL, at run time."""
    a = tenants["a"]
    name = f"off.a-jobs-{W}.example.invalid"
    await _add_hostname(a["domain_id"], name, last_ip_v4="192.0.2.60")
    resolver = FakeResolver({(name, "A"): ["192.0.2.60"]})

    await config(DdnsConfig(health_check_enabled=False))
    summary = await run_health_checks(
        resolve=resolver, scope=DdnsScope.for_user_id(a["user_id"])
    )
    assert summary.enabled is False
    assert resolver.calls == [], "a disabled health check still resolved"

    await config(DdnsConfig(health_check_enabled=True))
    summary = await run_health_checks(
        resolve=resolver, scope=DdnsScope.for_user_id(a["user_id"])
    )
    assert summary.enabled is True
    assert (name, "A") in resolver.calls


async def test_the_staleness_window_is_what_makes_a_hostname_due(config, tenants):
    """The interval is a *staleness* threshold read per tick, not a
    trigger period baked in at registration — which is why the legacy
    ``reschedule_health_checker`` has no counterpart here."""
    a = tenants["a"]
    name = f"due.a-jobs-{W}.example.invalid"
    await _add_hostname(a["domain_id"], name, last_ip_v4="192.0.2.70")
    resolver = FakeResolver({(name, "A"): ["192.0.2.70"]})
    scope = DdnsScope.for_user_id(a["user_id"])
    enabled = DdnsConfig(health_check_enabled=True, health_check_interval_minutes=15)

    first = await run_health_checks(resolve=resolver, scope=scope, config=enabled)
    assert first.hostnames_checked == 1

    # Immediately again: nothing is stale, so nothing is due.
    second = await run_health_checks(resolve=resolver, scope=scope, config=enabled)
    assert second.hostnames_checked == 0

    # Same rows, a tighter window read at run time -> due again.
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            sa.text(
                "UPDATE ddns_hostname SET dns_checked_at = :t WHERE name = :n"
            ),
            {"t": _now() - timedelta(minutes=5), "n": name},
        )
        await s.commit()
    third = await run_health_checks(
        resolve=resolver,
        scope=scope,
        config=DdnsConfig(health_check_enabled=True, health_check_interval_minutes=1),
    )
    assert third.hostnames_checked == 1


async def test_the_health_check_batch_ceiling_reports_itself(config, tenants):
    a = tenants["a"]
    zone = f"a-jobs-{W}.example.invalid"
    names = [f"b{i}.{zone}" for i in range(3)]
    for name in names:
        await _add_hostname(a["domain_id"], name, last_ip_v4="192.0.2.80")
    resolver = FakeResolver({(n, "A"): ["192.0.2.80"] for n in names})
    summary = await run_health_checks(
        resolve=resolver,
        scope=DdnsScope.for_user_id(a["user_id"]),
        config=DdnsConfig(health_check_enabled=True, health_check_batch_size=2),
    )
    assert summary.hostnames_checked == 2
    assert summary.truncated is True
    assert summary.hostnames_considered == 3


async def test_the_never_written_slice_is_excluded_by_the_query_not_just_the_loop(
    config, tenants
):
    """A mutation of this module's own survived, so here is the guard.

    Deleting the ``last_ip IS NOT NULL`` clause from the due-set query
    changes **no** summary count and fails **no** other test in this
    file: ``_check`` independently skips a record whose expected
    address is ``NULL``, so the hostname is loaded, contributes
    nothing, and the accounting still balances. Found by mutating the
    query, not by reading it.

    It is not harmless. Without the clause, hostnames that can never be
    checked occupy slots in a batch-limited sweep, and with enough of
    them the ones that *can* be checked are never reached — a health
    check that reports zero problems because it never looked. So the
    property is asserted where it actually lives: in the statement that
    reached MySQL.
    """
    a = tenants["a"]
    name = f"due-shape.a-jobs-{W}.example.invalid"
    await _add_hostname(a["domain_id"], name, last_ip_v4="192.0.2.95")
    await _add_hostname(a["domain_id"], f"nowrite.a-jobs-{W}.example.invalid")

    with capture_sql() as seen:
        summary = await run_health_checks(
            resolve=FakeResolver({(name, "A"): ["192.0.2.95"]}),
            scope=DdnsScope.for_user_id(a["user_id"]),
            config=DdnsConfig(health_check_enabled=True),
        )

    assert summary.hostnames_never_written == 1
    due = [
        s
        for s in seen
        if "FROM ddns_hostname" in s and "LIMIT" in s.upper() and "COUNT(" not in s.upper()
    ]
    assert len(due) == 1, f"expected one due-set SELECT, got {len(due)}"
    flat = due[0].replace("\n", " ")
    assert "last_ip_v4 IS NOT NULL" in flat, (
        f"the due-set query does not exclude hostnames with nothing to compare "
        f"against; they will occupy batch slots: {flat}"
    )
    assert "dns_checked_at IS NULL" in flat, flat


async def test_the_health_check_does_not_reach_outside_its_scope(config, tenants):
    """Tenant B's scope over tenant A's rows. A statement that skipped
    the scope would stamp A's ``dns_checked_at``."""
    a, b = tenants["a"], tenants["b"]
    name = f"leak.a-jobs-{W}.example.invalid"
    hid = await _add_hostname(a["domain_id"], name, last_ip_v4="192.0.2.90")
    resolver = FakeResolver({(name, "A"): ["192.0.2.90"]})

    summary = await run_health_checks(
        resolve=resolver,
        scope=DdnsScope.for_user_id(b["user_id"]),
        config=DdnsConfig(health_check_enabled=True),
    )
    assert summary.hostnames_checked == 0
    assert resolver.calls == []
    factory = get_session_factory()
    async with factory() as s:
        row = (
            await s.execute(
                sa.text("SELECT dns_checked_at FROM ddns_hostname WHERE id = :i"),
                {"i": hid},
            )
        ).one()
    assert row.dns_checked_at is None


def test_classify_record_keeps_measured_absence_apart_from_a_failed_measurement():
    """The unit-level statement of the same rule, on the pure function."""
    absent, stored, detail = classify_record("192.0.2.1", [], None)
    assert (absent, stored, detail) == (DnsCheckStatus.MISSING, None, None)

    failed, stored, detail = classify_record("192.0.2.1", None, "resolver timed out")
    assert failed is DnsCheckStatus.ERROR
    assert stored is None
    assert detail == "resolver timed out"

    assert absent is not failed
    # …and a resolver that returns neither is not silently read as absence.
    weird, _, detail = classify_record("192.0.2.1", None, None)
    assert weird is DnsCheckStatus.ERROR
    assert detail


def test_a_failed_record_outranks_a_good_one_when_a_hostname_has_both():
    assert worst([DnsCheckStatus.OK, DnsCheckStatus.ERROR]) is DnsCheckStatus.ERROR
    assert worst([DnsCheckStatus.OK, DnsCheckStatus.MISMATCH]) is DnsCheckStatus.MISMATCH
    assert worst([DnsCheckStatus.OK, DnsCheckStatus.MISSING]) is DnsCheckStatus.MISSING
    assert worst([DnsCheckStatus.OK]) is DnsCheckStatus.OK
    assert worst([]) is DnsCheckStatus.NEVER_CHECKED


# ===================================================================== #
# 5. Device liveness — n/a, error, and a measured zero
# ===================================================================== #


async def test_device_liveness_renders_three_states_as_three_things(config, tenants):
    """Plan §3.4, literally: a dash, an error, and a zero.

    The assertion that matters is the last one — only the *measured*
    zero renders as ``"0"``. An implementation that formats
    ``updates_in_window`` directly gives ``"None"`` or ``"0"`` for the
    other two, and the board then says a device that has never been
    heard from is merely quiet.
    """
    a = tenants["a"]
    factory = get_session_factory()
    now = _now()

    async with factory() as s:
        never = m.Device(
            user_id=a["user_id"],
            username=f"never-{W}",
            password_hash="x",
            name="never-called",
        )
        failed = m.Device(
            user_id=a["user_id"],
            username=f"failed-{W}",
            password_hash="x",
            name="last-call-failed",
            last_seen_at=now - timedelta(hours=2),
        )
        idle = m.Device(
            user_id=a["user_id"],
            username=f"idle-{W}",
            password_hash="x",
            name="quiet",
            last_seen_at=now - timedelta(days=30),
        )
        active = m.Device(
            user_id=a["user_id"],
            username=f"active-{W}",
            password_hash="x",
            name="busy",
            last_seen_at=now - timedelta(minutes=5),
        )
        s.add_all([never, failed, idle, active])
        await s.flush()
        ids = {
            "never": never.id,
            "failed": failed.id,
            "idle": idle.id,
            "active": active.id,
        }
        await s.commit()

    await _add_events(
        [
            # The failed device's most recent event is a refusal.
            dict(
                user_id=a["user_id"],
                device_id=ids["failed"],
                user_email=a["email"],
                event_type="update",
                response_code="good",
                created_at=now - timedelta(hours=3),
            ),
            dict(
                user_id=a["user_id"],
                device_id=ids["failed"],
                user_email=a["email"],
                event_type="update",
                response_code="badauth",
                created_at=now - timedelta(hours=2),
            ),
            # The idle device succeeded, but outside the window.
            dict(
                user_id=a["user_id"],
                device_id=ids["idle"],
                user_email=a["email"],
                event_type="update",
                response_code="good",
                created_at=now - timedelta(days=30),
            ),
            # The active device, twice inside it.
            dict(
                user_id=a["user_id"],
                device_id=ids["active"],
                user_email=a["email"],
                event_type="update",
                response_code="good",
                created_at=now - timedelta(hours=1),
            ),
            dict(
                user_id=a["user_id"],
                device_id=ids["active"],
                user_email=a["email"],
                event_type="update",
                response_code="nochg",
                created_at=now - timedelta(minutes=5),
            ),
        ]
    )

    async with factory() as s:
        statuses = {
            st.device_id: st
            for st in await device_statuses(
                s, scope=DdnsScope.for_user_id(a["user_id"]), window_days=7
            )
        }

    got = {key: statuses[hid] for key, hid in ids.items()}
    assert got["never"].liveness is Liveness.NEVER_SEEN
    assert got["failed"].liveness is Liveness.LAST_CALL_FAILED
    assert got["idle"].liveness is Liveness.IDLE
    assert got["active"].liveness is Liveness.ACTIVE

    rendered = {key: st.render_updates() for key, st in got.items()}
    assert rendered["never"] == "—"
    assert rendered["failed"] == "error"
    assert rendered["idle"] == "0"
    assert rendered["active"] == "2"
    assert len(set(rendered.values())) == 4, rendered
    # The whole acceptance criterion in one line: exactly one of them is
    # the digit zero, and it is the one that was measured as zero.
    assert [k for k, v in rendered.items() if v == "0"] == ["idle"]

    # `n/a` is not `0` in the data either, not merely in the rendering.
    assert got["never"].updates_in_window is None
    assert got["idle"].updates_in_window == 0
    # The denominator travels with the numerator.
    assert {st.window_days for st in got.values()} == {7}


async def test_a_device_with_no_events_at_all_is_not_reported_as_failed(config, tenants):
    """``last_response_code is None`` means *no event on record*, which
    is neither success nor failure. Folding it into either is how a
    fresh device shows up red or green before it has done anything."""
    a = tenants["a"]
    factory = get_session_factory()
    async with factory() as s:
        device = m.Device(
            user_id=a["user_id"],
            username=f"fresh-{W}",
            password_hash="x",
            name="fresh",
            last_seen_at=_now(),
        )
        s.add(device)
        await s.flush()
        did = device.id
        await s.commit()
    async with factory() as s:
        statuses = {
            st.device_id: st
            for st in await device_statuses(
                s, scope=DdnsScope.for_user_id(a["user_id"]), window_days=7
            )
        }
    assert statuses[did].last_response_code is None
    assert statuses[did].liveness is Liveness.IDLE
    assert statuses[did].render_updates() == "0"


async def test_device_statuses_do_not_cross_tenants(config, tenants):
    a, b = tenants["a"], tenants["b"]
    factory = get_session_factory()
    async with factory() as s:
        statuses = await device_statuses(
            s, scope=DdnsScope.for_user_id(a["user_id"]), window_days=7
        )
    assert statuses, "vacuous — tenant A has no devices"
    assert {st.user_id for st in statuses} == {a["user_id"]}
    assert b["device_id"] not in {st.device_id for st in statuses}


def test_success_response_codes_are_the_allow_list_not_a_deny_list():
    """A response code invented by a later issue must default to
    *failure*: that surfaces on a board, where the other direction
    hides."""
    assert wj.SUCCESS_RESPONSE_CODES == frozenset({"good", "nochg"})
    for code in ("nohost", "badauth", "notfqdn", "abuse", "dnserr", "911", "brand-new"):
        assert code not in wj.SUCCESS_RESPONSE_CODES


# ===================================================================== #
# 6. A job that raises does not stop the scheduler
# ===================================================================== #


async def test_a_raising_job_does_not_stop_the_scheduler():
    """A real ``AsyncIOScheduler``, two jobs, one of them a thrower.

    Not "the wrapper catches" — that is a statement about the wrapper.
    This runs the actual scheduler class atrium hands hosts, on the
    actual event loop, and asserts the *other* job kept firing after
    the first one raised repeatedly. The counters give the failure
    count a denominator: ``0 failures`` over ``0`` runs is what a job
    that never fired also reports.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    wj.reset_counters()
    healthy_runs = 0

    async def thrower() -> None:
        raise RuntimeError("deliberate: the job body failed")

    async def healthy() -> None:
        nonlocal healthy_runs
        healthy_runs += 1

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        guarded("probe-thrower", thrower),
        "interval",
        seconds=0.05,
        id="probe-thrower",
        max_instances=1,
    )
    scheduler.add_job(
        guarded("probe-healthy", healthy),
        "interval",
        seconds=0.05,
        id="probe-healthy",
        max_instances=1,
    )
    scheduler.start()
    try:
        await asyncio.sleep(0.6)
        assert scheduler.running is True
        thrower_runs = wj.JOB_RUNS.get("probe-thrower", 0)
        thrower_fails = wj.JOB_FAILURES.get("probe-thrower", 0)

        assert thrower_runs >= 3, f"the thrower only fired {thrower_runs} times"
        assert thrower_fails == thrower_runs, (
            f"{thrower_fails} failures over {thrower_runs} runs — the guard "
            f"lost one"
        )
        assert healthy_runs >= 3, (
            f"the healthy job fired {healthy_runs} times while the thrower "
            f"raised {thrower_fails} times: the scheduler stopped"
        )
        assert wj.JOB_FAILURES.get("probe-healthy", 0) == 0
        # …and it is still scheduling: both jobs remain registered.
        assert {j.id for j in scheduler.get_jobs()} == {"probe-thrower", "probe-healthy"}
    finally:
        scheduler.shutdown(wait=False)


async def test_the_scheduler_survives_an_unguarded_raiser_too():
    """Measured, and it corrects the issue's implicit premise.

    #17's acceptance says "a job that raises does not kill the
    scheduler thread. Prove it." Proved — and the honest reading is
    that **APScheduler's executor already catches it**, so the guard is
    not what keeps the scheduler alive. What
    :func:`~atrium_ddns.worker_jobs.guarded` buys is that the failure
    is *ours*: a structured ``atrium_ddns.job.failed`` line naming the
    job and carrying the traceback, plus a counter, instead of
    APScheduler's own ``Job "…" raised an exception`` on the
    ``apscheduler.executors`` logger — which this estate's structlog
    configuration does not surface.

    Reporting the stronger claim ("the guard is what saves the
    scheduler") would have been easy and wrong. The guard earns its
    place on observability, and this test is the evidence for the
    distinction rather than against the guard.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    survivor_runs = 0

    async def thrower() -> None:
        raise RuntimeError("deliberate: unguarded")

    async def survivor() -> None:
        nonlocal survivor_runs
        survivor_runs += 1

    scheduler = AsyncIOScheduler()
    scheduler.add_job(thrower, "interval", seconds=0.05, id="raw-thrower")
    scheduler.add_job(survivor, "interval", seconds=0.05, id="raw-survivor")
    scheduler.start()
    try:
        await asyncio.sleep(0.4)
        assert scheduler.running is True
        assert survivor_runs >= 3, (
            f"the survivor fired {survivor_runs} times — APScheduler did not "
            f"absorb the unguarded exception after all, and the guard is "
            f"load-bearing for liveness as well as for logging"
        )
    finally:
        scheduler.shutdown(wait=False)


async def test_the_guard_counts_runs_as_well_as_failures():
    """A failure count with no run count is a number with no denominator."""
    wj.reset_counters()
    calls = {"n": 0}

    async def sometimes() -> None:
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise ValueError("every other one")

    job = guarded("probe-mixed", sometimes)
    for _ in range(4):
        await job()
    assert wj.JOB_RUNS["probe-mixed"] == 4
    assert wj.JOB_FAILURES["probe-mixed"] == 2


async def test_the_guard_lets_cancellation_through():
    """Swallowing ``CancelledError`` makes worker shutdown hang — a
    guard that turns a cancellation into a logged failure has broken
    the thing it was protecting."""
    wj.reset_counters()

    async def cancelled() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await guarded("probe-cancel", cancelled)()
    assert wj.JOB_FAILURES.get("probe-cancel", 0) == 0


async def test_init_worker_registers_every_job_in_the_table():
    """Derived from :data:`JOBS`, not from a hand-kept list — a job
    removed from the table takes its registration and this assertion
    with it."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from app.host_sdk.worker import HostWorkerCtx

    from atrium_ddns.bootstrap import init_worker

    scheduler = AsyncIOScheduler()
    init_worker(HostWorkerCtx(scheduler=scheduler))
    registered = {job.id for job in scheduler.get_jobs()}
    assert registered == set(wj.JOBS)
    assert registered, "vacuous — the job table is empty"
    for job in scheduler.get_jobs():
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.trigger.interval.total_seconds() == wj.JOBS[job.id][1]


def test_init_worker_does_no_io():
    """``app/worker.py`` calls ``init_worker`` outside any try/except
    and before ``scheduler.start()``. Anything that raises there takes
    the worker down at startup with nothing to retry it — so the
    registration path must not touch the database.

    Asserted structurally: ``register_jobs`` contains no ``await`` and
    no session construction.
    """
    tree = ast.parse(inspect.getsource(register_jobs))
    awaits = [n for n in ast.walk(tree) if isinstance(n, ast.Await)]
    assert awaits == []
    calls = {
        getattr(n.func, "attr", getattr(n.func, "id", ""))
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    for forbidden in ("get_session_factory", "load_config", "get_namespace"):
        assert forbidden not in calls, (
            f"register_jobs calls {forbidden}() — init_worker runs before the "
            f"scheduler starts and outside any except clause"
        )


def test_the_scheduled_jobs_take_no_arguments():
    """The scheduler binds the zero-argument form, so the ``config=``
    test seam cannot become a way for production to skip the
    namespace."""
    for job_id, (body, _seconds) in wj.JOBS.items():
        signature = inspect.signature(body)
        assert all(
            p.default is not inspect.Parameter.empty for p in signature.parameters.values()
        ), f"{job_id} has a required argument the scheduler cannot supply"


# ===================================================================== #
# 7. Guards on the module itself
# ===================================================================== #


def _module_source() -> str:
    return pathlib.Path(inspect.getsourcefile(wj)).read_text()


def test_no_hand_written_tenancy_filter_survives_in_the_worker():
    """The worker has no request and no principal, which is exactly
    where a hand-written ``user_id`` filter looks reasonable and is
    wrong. Every statement goes through ``DdnsScope``; this catches the
    shape, and the behavioural tests above catch the effect."""
    source = _module_source()
    tree = ast.parse(source)
    offences = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute):
            if node.left.attr in {"user_id", "device_id"} and isinstance(
                node.left.value, ast.Name
            ):
                # `Model.user_id == …` on a mapped class is the shape;
                # a local variable's attribute is not.
                if node.left.value.id in {"Device", "DnsEvent", "Domain", "Hostname"}:
                    offences.append(ast.unparse(node))
    assert offences == [], (
        f"hand-written tenancy predicates in worker_jobs.py: {offences}"
    )


def test_the_blocking_resolver_is_not_used_on_the_event_loop():
    """``dns.resolver.resolve`` blocks. Atrium's scheduler is an
    ``AsyncIOScheduler`` sharing the worker's only event loop with the
    heartbeat and the ``scheduled_jobs`` drain, so a blocking resolve
    stalls all of them and the first symptom is ``/health`` reporting a
    dead worker."""
    tree = ast.parse(_module_source())
    called = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    # Read off the call graph, not off the text: the module docstring
    # *names* the blocking call in the paragraph explaining why it is
    # not used, and a substring search reports that as a violation.
    assert "dns.asyncresolver.Resolver" in called
    assert "dns.resolver.resolve" not in called, (
        "worker_jobs.py calls the blocking resolver on the scheduler's loop, "
        "which is shared with the worker heartbeat and the scheduled_jobs drain"
    )


def test_dnspython_is_present_and_is_a_declared_dependency():
    """It reaches the image transitively, via ``email-validator``.

    A transitive dependency is one upstream refactor away from absent,
    and the failure mode would be a health check that errors on every
    hostname for ever — a confident, uniform ``error`` column that
    looks like a DNS outage. Two readings: the import works, and the
    package is still declared by something atrium depends on directly.
    """
    import importlib.metadata

    import dns.asyncresolver

    assert hasattr(dns.asyncresolver, "Resolver")
    requires = importlib.metadata.requires("email-validator") or []
    assert any(r.split()[0].split(">")[0].split("=")[0] == "dnspython" for r in requires), (
        f"dnspython is no longer a declared dependency of email-validator "
        f"({requires}); it is reaching the image by luck"
    )


def test_the_sweep_scope_states_its_reason():
    scope = wj._sweep_scope()
    assert scope.cross_tenant_reason
    assert "#17" in scope.cross_tenant_reason
    assert scope.user_id is None
    assert scope.permissions == frozenset()


def test_device_status_is_frozen_so_a_renderer_cannot_patch_the_state():
    status = DeviceStatus(
        device_id=1,
        user_id=1,
        device_name="x",
        liveness=Liveness.NEVER_SEEN,
        last_seen_at=None,
        last_response_code=None,
        updates_in_window=None,
        window_days=7,
    )
    with pytest.raises(Exception):
        status.updates_in_window = 0  # type: ignore[misc]
    assert status.render_updates() == "—"
