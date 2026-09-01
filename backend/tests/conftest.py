"""One shared teardown for the host suite, and the lock that stops the
workers deadlocking each other.

Background, because the fix is not the one the issue asked for
-------------------------------------------------------------
``_purge`` was copy-pasted into **seven** modules, and #65 was opened on
the theory that those copies deadlocked each other: an unindexed
``DELETE FROM ddns_event WHERE user_email IN (…)`` plus a cascading
``DELETE FROM users``, taking scan-wide locks in two different orders.

That theory is wrong, and the evidence is MySQL's own. With
``innodb_print_all_deadlocks=ON``, every deadlock the suite produced was
logged by the server, and **five of five** had the same two statements
in it:

    *** (1) TRANSACTION ... INSERT INTO ddns_domain (user_id, name) ...
    *** (2) TRANSACTION ... INSERT INTO users (email, ...) ...

Not one involved a ``DELETE``. The contended resources were the two
UNIQUE secondary indexes ``users.ix_users_email`` and
``ddns_domain.uq_ddns_domain_name``, and the shape was identical every
time: each transaction **holds** an S next-key lock on one index and
**waits** for an ``insert intention`` X gap lock on the other.

The mechanism is a property of InnoDB rather than of this code. Insert a
row into a unique index and the duplicate-key check leaves a shared
next-key lock covering the gap before that row; another session
inserting a key into the same gap needs an exclusive insert-intention
lock, which the shared lock blocks. Two workers whose keys interleave in
*two* indexes in opposite order therefore cycle. Emails and domain names
are namespaced per worker with a **suffix** (``…-gw4``, ``…-gw5``), so
in index order the workers' rows are shuffled together and adjacency
across workers is the normal case, not the exception.

Three things follow, and each kills one of the fixes the issue proposed:

* **Deleting by primary key does not help.** The deadlock is between
  ``INSERT``s in fixture *setup*. #46 had already implemented the
  by-primary-key purge in ``test_router_events.py`` and measured that it
  moved nothing — that measurement was right, and this is why.
* **Ordering the statements consistently does not help.** Both
  transactions already insert users before domains. The lock order that
  cycles is over *index gaps*, and which gap a row needs is decided by
  its key's collation order, not by the order the code issues
  statements.
* **READ COMMITTED does not help, and is already on.**
  ``app.db.get_engine()`` sets it (atrium #152). The manual is explicit
  that duplicate-key checks take gap locks *regardless* of isolation
  level, and the logged deadlocks are the demonstration.

What is actually done here
--------------------------
Fixture writes are made mutually exclusive across workers with a MySQL
named lock. While one worker is inside :func:`fixture_writes`, no other
worker has an open fixture transaction, so there is no second
transaction to cycle with — mutual exclusion, not a lower probability.

The cost of serialising that region is small because the region is
small: measured on this box, fixture setup is ~58 ms per DB-using test
and ~44 ms of it is two argon2 hashes, which happen in Python before the
lock is taken. :func:`unusable_password_hash` then removes most of even
that by hashing the one placeholder password once per process instead of
once per user per test — the seven copies of ``_password_hash`` all fed
the same constant string to argon2.

Why not the alternatives
------------------------
*A database per worker* removes the contention outright and is the only
option that needs no ongoing discipline, but every worker's schema has
to be created and migrated. Doing that per run costs more than the
suite; doing it once and caching it in the volume adds a "is worker 7's
schema stale?" question that ``make check-fresh`` cannot answer, which
is the defect family this repo keeps being bitten by.

*Prefixing the namespace instead of suffixing it* — so each worker's
keys form a contiguous block — is much closer to working than it looks,
and still does not. A worker inserting the largest key in its own block
takes its S lock on the **next** block's smallest record, and when a
block is empty the successor is shared by every worker at once. Making
it airtight needs a permanent sentinel row per worker in every unique
index the tests touch, and there are six of those
(``ix_users_email``, ``uq_ddns_domain_name``, ``uq_ddns_device_username``,
``uq_ddns_device_user_name``, ``uq_ddns_hostname_name``,
``uq_ddns_domain_backend_type``) — two of them compound keys led by an
autoincrement id, which no naming convention can namespace.

*Retrying on 1213* is what the MySQL manual recommends for application
code and it would make the suite green, but it leaves the contention in
place: the server would still log a deadlock on every run, and #65 asks
for zero rather than for zero-that-were-retried.

The rows no teardown could name — #87
-------------------------------------
``purge_tenants`` names a tenant by email or by id, and ``IN (…)`` never
matches NULL. ``router_nic`` writes one ``ddns_event`` row with
``user_id`` *and* ``user_email`` NULL every time a credential fails to
resolve — the pre-auth ``badauth`` — so those rows survived every
teardown the suite had and accumulated, monotonically, for ever.

**The population moved twice while this was being fixed, so the figure
is written with its date and its tip rather than as a fact.** #87's
headline is *~27 rows per suite run*. Measured on a fresh database at
``4d10e37`` it was **17**, from three modules — ``test_router_nic.py``
(14), ``test_import_legacy.py`` (2), ``test_router_tenant.py`` (1). #64
then merged, and ``record_event`` learned to attribute a ``badauth``
whose username *resolved to a device*; re-measured at ``9744071`` the
same sweep reads **10**, from two — ``test_router_nic.py`` (9) and
``test_import_legacy.py`` (1). ``test_router_tenant.py`` drove its
refusal with a real username and a wrong secret, so #64 attributed it
out of the population entirely.

What survives all three readings is the shape: a refusal that resolved
**nothing** has no tenant to name, by construction, and 10 of them are
still written and still leaked per run. Against 322 event rows written
in the same run, so the tenant teardowns work and these are the residue.

The fix is deliberately **not** wired per module.
:func:`record_unattributed_events` registers a mapper-level
``after_insert`` listener on :class:`~atrium_ddns.models.DnsEvent` that
records the id of every unattributed row *this process* writes, and
:func:`_sweep_unattributed_events` — session-scoped and autouse — hands
that list to ``purge_tenants(event_ids=…)`` at the end of the worker's
session. Two properties follow, and both are the reason for the shape:

* **It cannot miss a module, and it cannot go stale.** #87's own
  per-module table named two of the three modules that were leaking when
  it was written, and a fix hand-wired from it would have left
  ``test_router_tenant.py`` leaking and looked complete. A week later
  that same table would have been wrong in the *other* direction —
  ``test_router_tenant.py`` stopped leaking without anybody touching it,
  because #64 changed the writer. The listener is on the writer, so both
  moves are absorbed: no list to be short, none to be stale. It is the
  lesson #78 taught ``EXPECTED_PARTICIPANTS``, one level down.
* **It cannot report success having matched nothing.** The sweep
  re-counts the ids it handed over and raises when any survive. Deleting
  zero *because there were none* and deleting zero *because the
  predicate could not match* are different states, and rendering them
  identically is how this survived in the first place.

The mechanism's own failure mode is that ``record_event`` stops going
through the ORM — a Core ``insert()`` fires no mapper event, the sink
stays empty, and the sweep reports a clean nothing. That is checked
against the real writer in
``test_harness_guards.test_the_recorder_sees_the_routers_own_write``,
not asserted here.

``unattributed_emails`` stays on ``purge_tenants`` and keeps its one
caller (``test_tenant_isolation.py``), which is about a row written
*with* an email whose user is then deleted — a different state from the
one above, and the only one an email-shaped scan can reach.
"""
from __future__ import annotations

import functools
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest_asyncio
import sqlalchemy as sa
from app.db import get_engine, get_session_factory
from app.models.auth import User
from app.models.ops import AppSetting
from app.services.app_config import put_namespace
from atrium_ddns.models import DnsEvent
from atrium_ddns.worker_jobs import CONFIG_NAMESPACE, DdnsConfig
from sqlalchemy.ext.asyncio import AsyncSession

#: This worker's namespace. Ten workers share one MySQL, so anything a
#: test creates carries it — a hardcoded email or device name produces
#: collisions that read as flakiness.
# --- unit lane: in-memory SQLite ---
#
# Seeded at conftest IMPORT time, before anything calls `app.db.get_engine()`,
# because that function caches its engine in a module global and passes
# `isolation_level="READ COMMITTED"` — which SQLite rejects. Doing this in a
# fixture would run too late.
#
# Opt-in: nothing happens unless DATABASE_URL names sqlite, so `make
# test-backend` against MySQL is untouched.
if os.environ.get("DATABASE_URL", "").startswith("sqlite"):
    import app.db as _atrium_db
    from sqlalchemy import event as _event
    from sqlalchemy.ext.asyncio import (
        AsyncSession as _AS,
        async_sessionmaker as _asm,
        create_async_engine as _cae,
    )
    from sqlalchemy.pool import StaticPool as _StaticPool

    if _atrium_db._engine is None:
        _atrium_db._engine = _cae(
            "sqlite+aiosqlite:///:memory:",
            poolclass=_StaticPool,          # `:memory:` is per-connection
            connect_args={"check_same_thread": False},
        )
        _atrium_db._session_factory = _asm(
            _atrium_db._engine, class_=_AS, expire_on_commit=False
        )

        @_event.listens_for(_atrium_db._engine.sync_engine, "connect")
        def _sqlite_foreign_keys(dbapi_connection, _record):  # noqa: ANN001
            # SQLite ignores foreign keys unless asked. Off by default means
            # ON DELETE CASCADE silently does nothing — "no error, wrong
            # data", which is the worst way for this to present.
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()


WORKER = os.environ.get("PYTEST_XDIST_WORKER", "serial")

#: The advisory lock every fixture write takes. Server-wide, and the
#: server is this compose project's own container, so two agents running
#: side by side cannot see each other's lock.
FIXTURE_LOCK = "atrium_ddns_fixture_writes"

#: Seconds to wait for the lock. Long enough that a slow fixture on a
#: loaded box does not trip it, short enough that a re-entrancy bug
#: fails the run instead of looking like a hang.
FIXTURE_LOCK_TIMEOUT = 30

#: The advisory lock the shared ``atrium_ddns`` config row is owned
#: under. Deliberately **not** :data:`FIXTURE_LOCK`: tenant setup and
#: config ownership have different lifetimes (one statement versus one
#: whole test), and sharing a lock between them would serialise the
#: entire suite behind the config-taking tests.
DDNS_CONFIG_LOCK = "atrium_ddns_config_row"

#: Set while this process holds the lock. Taking it twice from one
#: worker would wait on a lock the same worker holds — on a *different*
#: connection, so MySQL's re-entrancy does not save us — and the symptom
#: would be a 30-second stall that reads as a hang. Refuse instead, and
#: name the caller.
_HELD_BY: str | None = None


#: The attribute name that carries membership of the harness-guard
#: population for things pytest will not let you mark. Same string as the
#: registered marker in ``backend/pyproject.toml``, deliberately: one
#: registry, one name.
GUARD_ATTRIBUTE = "harness_guard"


def harness_guard(obj: object) -> object:
    """Tag a non-test callable as a guard on the harness itself — #108.

    ``@pytest.mark.harness_guard`` is the carrier for everything pytest
    collects. It is not available here: pytest refuses marks on fixtures
    outright ("Marks applied to fixtures have no effect"), so a guard
    that lives in a fixture cannot join the population the way a test
    does.

    That is not a hypothetical. :func:`_sweep_unattributed_events` is
    what turned #87's mutation M4 into an error instead of a silent
    pass — a guard by behaviour, in a fixture by necessity, and
    invisible to every enumeration this repo had. #108 exists because a
    population that silently excludes it is short, and a short
    population is a clause counting its own denominator.

    So the tag is an attribute rather than a mark, applied **above** the
    fixture decorator so it lands on whatever object ends up bound to
    the module name (pytest 8+ binds a ``FixtureFunctionDefinition``,
    not the function). ``test_harness_guards.harness_guards()`` reads
    both carriers and returns one population;
    ``test_every_autouse_session_fixture_in_conftest_is_tagged`` is what
    stops the next one being added without it.
    """
    setattr(obj, GUARD_ATTRIBUTE, True)
    return obj


@functools.cache
def unusable_password_hash() -> str:
    """The placeholder hash every tenant fixture used to compute itself.

    argon2 costs ~22 ms and the input is a constant, so seven modules
    were paying it once per user per test for a value no test reads —
    ~44 ms of the ~58 ms each DB-using test spent in setup. Cached for
    the process: the salt differs between runs but never within one, and
    nothing asserts on it.

    Deliberately **not** used by ``test_router_nic.py``'s login tests,
    which hash real passwords and must keep doing so.
    """
    from fastapi_users.password import PasswordHelper

    return PasswordHelper().hash("unusable-" + "x" * 24)


def _needs_advisory_lock() -> bool:
    """True only on MySQL.

    ``GET_LOCK`` exists here to serialise xdist workers that share one
    MySQL. On a per-worker in-memory SQLite there is nothing shared to
    serialise, and the function does not exist — so the guard is not
    "skipped for convenience", it is inapplicable. That distinction is
    why this is a dialect test and not a flag.
    """
    return get_engine().dialect.name == "mysql"


@asynccontextmanager
async def fixture_writes(owner: str = "?") -> AsyncIterator[AsyncSession]:
    """A session whose transaction no other xdist worker overlaps.

    The lock lives on its **own** connection, not on the session's. A
    session releases its connection to the pool at commit, and
    ``RELEASE_LOCK`` issued afterwards would land on whichever
    connection the pool handed back next — usually a different MySQL
    session, where the release is a silent no-op returning ``NULL`` and
    the lock stays held until the pool happens to recycle. That is a
    30-second stall for every other worker and it would look like the
    fix had made things worse.

    ``GET_LOCK`` returning 0 (timed out) or NULL (error) raises. A
    helper that shrugged and carried on would still produce a green
    suite most of the time, which is the shape of guard this repo keeps
    finding in its own code.
    """
    global _HELD_BY
    if _HELD_BY is not None:
        raise RuntimeError(
            f"fixture_writes({owner!r}) nested inside fixture_writes({_HELD_BY!r}). "
            "The lock is held on a second connection, so this would block for "
            f"{FIXTURE_LOCK_TIMEOUT}s and then fail — open one guarded region at a time."
        )

    engine = get_engine()
    if not _needs_advisory_lock():
        _HELD_BY = owner
        try:
            factory = get_session_factory()
            async with factory() as session:
                yield session
                await session.commit()
        finally:
            _HELD_BY = None
        return
    async with engine.connect() as guard:
        got = (
            await guard.execute(
                sa.text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": FIXTURE_LOCK, "timeout": FIXTURE_LOCK_TIMEOUT},
            )
        ).scalar()
        if got != 1:
            raise RuntimeError(
                f"GET_LOCK({FIXTURE_LOCK!r}, {FIXTURE_LOCK_TIMEOUT}) returned {got!r} "
                f"for {owner!r} — 0 is a timeout, NULL is an error. Not proceeding "
                "unguarded."
            )
        _HELD_BY = owner
        try:
            factory = get_session_factory()
            async with factory() as session:
                yield session
                await session.commit()
        finally:
            _HELD_BY = None
            await guard.execute(
                sa.text("SELECT RELEASE_LOCK(:name)"), {"name": FIXTURE_LOCK}
            )


async def purge_tenants(
    emails: Sequence[str],
    *,
    event_ids: Sequence[int] | None = None,
    unattributed_emails: Sequence[str] | None = None,
    owner: str = "purge",
) -> None:
    """The one teardown. Replaces seven copies of ``_purge``.

    Removes the named users and everything hanging off them. Called from
    both ends of every tenant fixture, because MySQL DDL and a killed
    worker do not cooperate and teardown is not guaranteed to have run.

    Three things it does that the copies did not, all of them from #46's
    write-up of the one copy it owned:

    **Deletes events by ``user_id``, not by ``user_email``.**
    ``user_email`` carries no index, so the old form scanned
    ``ddns_event`` whole. It is not what deadlocked the suite — see the
    module docstring — but a full scan per fixture teardown is still a
    full scan per fixture teardown.

    **Deletes explicitly recorded event ids too.** ``router_nic`` writes
    a row with ``user_id`` *and* ``user_email`` NULL when a credential
    fails, and ``IN (…)`` never matches NULL, so those rows survived
    teardown and accumulated one per run forever. A caller that writes
    such a row passes its ids in.

    **Events before users.** By the time ``DELETE FROM users`` runs,
    ``ddns_event.user_id``'s ``ON DELETE SET NULL`` has nothing left to
    set, so the cascade touches no rows and takes no locks.

    ``unattributed_emails`` is the one place a scan is still permitted,
    and it is a parameter rather than the default so that using it is a
    decision. ``ddns_event.user_email`` carries no index, so
    ``WHERE user_email IN (…)`` reads the table; the only rows that need
    it are those with ``user_id IS NULL`` left behind by a *previous*
    run, which no id from this process can name and no index can find.
    Callers that wrote such a row in this process pass ``event_ids``
    instead.

    ``user_secret_keys`` is deleted explicitly rather than left to its
    ``ON DELETE CASCADE``, so a caller that created a key does not
    depend on cascade ordering. #14 removed the *orphan sweep* of that
    table — ``WHERE user_id NOT IN (SELECT id FROM users)``, which could
    never match a row and took a scan-wide lock to prove it. This is the
    opposite: an equality on the primary key of a set we already have.
    """
    async with fixture_writes(owner=owner) as s:
        if event_ids:
            await s.execute(
                sa.text("DELETE FROM ddns_event WHERE id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True)
                ),
                {"ids": list(event_ids)},
            )
        if unattributed_emails:
            await s.execute(
                sa.text(
                    "DELETE FROM ddns_event WHERE user_id IS NULL AND user_email IN :e"
                ).bindparams(sa.bindparam("e", expanding=True)),
                {"e": list(unattributed_emails)},
            )
        # An empty ``emails`` is a legitimate call, not a mistake:
        # :func:`purge_unattributed_events` has ids and no tenant. The
        # guard is here rather than at the call site because
        # ``Column.in_([])`` renders an always-false expression *and* a
        # SQLAlchemy warning, and a warning nobody reads on a statement
        # that cannot match is two bad things rather than one.
        user_ids = (
            list(
                (
                    await s.execute(
                        sa.select(User.id).where(User.email.in_(list(emails)))
                    )
                ).scalars()
            )
            if emails
            else []
        )
        if user_ids:
            await s.execute(
                sa.text("DELETE FROM ddns_event WHERE user_id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True)
                ),
                {"ids": user_ids},
            )
            # The host tables, explicitly, before the users they hang off.
            #
            # `DELETE FROM users` cleans these up on a *migrated* schema,
            # because alembic emits the `ON DELETE CASCADE` that
            # `HostForeignKey` only records as a marker — it registers no
            # ForeignKey against the metadata, so anything built by
            # `create_all` has no constraint and no cascade at all.
            #
            # Relying on the cascade made this cleanup silently dependent on
            # DDL that only one of the two schemas has. It presented as ~196
            # unrelated-looking UNIQUE failures the first time the suite was
            # pointed at SQLite: the user went, its devices and domains
            # stayed, and the next test collided.
            #
            # EVERY delete below is scoped to `user_ids`, including the
            # tables that have no `user_id` of their own — they are reached
            # through the device or domain that does. An unscoped
            # `DELETE FROM ddns_hostname` would pass on SQLite, where each
            # worker owns its database, and delete nine other workers' rows
            # on the shared MySQL. That is the #117 / #149 failure exactly,
            # reintroduced by a cleanup written to fix it.
            await s.execute(
                sa.text(
                    "DELETE FROM ddns_hostname_backend WHERE hostname_id IN ("
                    "  SELECT id FROM ddns_hostname WHERE device_id IN ("
                    "    SELECT id FROM ddns_device WHERE user_id IN :ids)"
                    "  OR domain_id IN (SELECT id FROM ddns_domain WHERE user_id IN :ids))"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": user_ids},
            )
            await s.execute(
                sa.text(
                    "DELETE FROM ddns_hostname WHERE device_id IN ("
                    "  SELECT id FROM ddns_device WHERE user_id IN :ids)"
                    " OR domain_id IN (SELECT id FROM ddns_domain WHERE user_id IN :ids)"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": user_ids},
            )
            await s.execute(
                sa.text(
                    "DELETE FROM ddns_domain_backend WHERE domain_id IN ("
                    "  SELECT id FROM ddns_domain WHERE user_id IN :ids)"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": user_ids},
            )
            await s.execute(
                sa.text(
                    "DELETE FROM ddns_rate_limit_event WHERE device_id IN ("
                    "  SELECT id FROM ddns_device WHERE user_id IN :ids)"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": user_ids},
            )
            # atrium's audit rows too. `audit_log.actor_user_id` is a real
            # FK on the migrated schema, so MySQL cascades them away with the
            # user; `create_all` has no constraint, the rows survive, and the
            # next test's "exactly one audit row" assertion counts the last
            # test's as well. Passes alone, fails in a full run — the shape
            # that reads as flakiness.
            await s.execute(
                sa.text(
                    "DELETE FROM audit_log WHERE actor_user_id IN :ids"
                    " OR impersonator_user_id IN :ids"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": user_ids},
            )
            for _t in ("ddns_domain", "ddns_device"):
                await s.execute(
                    sa.text(f"DELETE FROM {_t} WHERE user_id IN :ids").bindparams(
                        sa.bindparam("ids", expanding=True)
                    ),
                    {"ids": user_ids},
                )
            await s.execute(
                sa.text(
                    "DELETE FROM user_secret_keys WHERE user_id IN :ids"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": user_ids},
            )
            await s.execute(
                sa.text("DELETE FROM users WHERE id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True)
                ),
                {"ids": user_ids},
            )


# ===================================================================== #
# #87 — the rows no email can name
# ===================================================================== #

#: Every open sink, innermost last. A list of lists rather than one
#: list, so :func:`record_unattributed_events` nests: the session-wide
#: sweep is always open, and a guard that wants to watch one statement
#: opens a second one inside it and gets its own ids without stealing
#: them from the sweep.
_UNATTRIBUTED_SINKS: list[list[int]] = []


def _collect_unattributed(mapper: object, connection: object, target: DnsEvent) -> None:
    """``after_insert`` on :class:`DnsEvent`. Records the unnameable ones.

    A module-level function rather than a closure so that
    ``sa.event.contains(DnsEvent, "after_insert", _collect_unattributed)``
    is answerable — which is how a guard tells *the listener is
    installed in this process* from *the listener is defined in this
    file*. The AST-vs-process distinction is one this repo has already
    paid for.

    ``target.id`` is populated by the time this fires: the ORM has
    executed the INSERT and fetched the generated key. Verified rather
    than assumed — a listener that recorded ``None`` would produce a
    sweep whose ``IN (…)`` matched nothing while looking busy, which is
    the same defect one level up.

    The condition mirrors ``router_nic.record_event``'s ``auth`` being
    ``None``: it sets ``user_id`` and ``user_email`` from the same
    object, so a row is either fully attributed or fully anonymous.
    Testing both columns rather than trusting that pairing is cheap and
    keeps this correct if the writer ever sets one without the other.
    """
    if target.user_id is None and target.user_email is None and target.id is not None:
        for sink in _UNATTRIBUTED_SINKS:
            sink.append(target.id)


@contextmanager
def record_unattributed_events() -> Iterator[list[int]]:
    """Collect the ids of unattributed ``ddns_event`` rows written in here.

    Yields the list itself, which fills as rows are written, so the
    caller hands it to ``purge_tenants(event_ids=…)`` on the way out.

    Registered against the **mapper**, not against a session, so it sees
    every write in the process — including the ones the app under test
    makes through its own ``get_session`` dependency, which is where
    every one of #87's rows comes from and which no fixture holds a
    handle to.

    The listener is attached once for the outermost sink and removed
    with it, so a run that opens no recorder pays nothing.
    """
    sink: list[int] = []
    if not _UNATTRIBUTED_SINKS:
        sa.event.listen(DnsEvent, "after_insert", _collect_unattributed)
    _UNATTRIBUTED_SINKS.append(sink)
    try:
        yield sink
    finally:
        _UNATTRIBUTED_SINKS.remove(sink)
        if not _UNATTRIBUTED_SINKS:
            sa.event.remove(DnsEvent, "after_insert", _collect_unattributed)


async def purge_unattributed_events(
    event_ids: Sequence[int], *, owner: str = "purge-unattributed"
) -> None:
    """Hand recorded ids to :func:`purge_tenants`, then prove they went.

    The re-count is the whole point and is not ceremony. ``IN (…)``
    never matching NULL is #87's entire mechanism, and a teardown that
    issues the statement, gets ``rowcount`` 0 and returns happily is
    indistinguishable from one that had nothing to do. The two states
    are separated here by asking the *table* rather than the delete:

    * nothing recorded — return without opening a connection, which is
      what a worker that ran only ``test_providers.py`` does;
    * recorded and gone — the normal path;
    * recorded and **still there** — raise, naming both counts and the
      surviving ids.

    Asserted on the rows, not on the delete's report of itself.
    ``rowcount`` is the statement describing its own work, and this repo
    has been wrong more than once about an instrument's own account.
    """
    ids = sorted(set(event_ids))
    if not ids:
        return
    await purge_tenants((), event_ids=ids, owner=owner)
    factory = get_session_factory()
    async with factory() as s:
        survivors = list(
            (
                await s.execute(
                    sa.text("SELECT id FROM ddns_event WHERE id IN :ids").bindparams(
                        sa.bindparam("ids", expanding=True)
                    ),
                    {"ids": ids},
                )
            ).scalars()
        )
    if survivors:
        raise AssertionError(
            f"{len(survivors)} of {len(ids)} recorded unattributed ddns_event "
            f"rows survived purge_tenants(event_ids=…) [{owner}]. The ids were "
            "recorded at insert time, so this is NOT 'there was nothing to "
            "delete' — it is 'the delete did not match'. Surviving ids: "
            f"{survivors[:20]}{'…' if len(survivors) > 20 else ''}"
        )


@harness_guard
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _sweep_unattributed_events() -> AsyncIterator[None]:
    """The one place #87's rows are cleaned up. Autouse, session-scoped.

    Autouse because the alternative is a list of participating modules,
    and #78 already established what a hand-kept list of test modules is
    worth: #87's own per-module table named two of the three modules
    that leak, and a fix wired from it would have shipped looking
    complete.

    Session-scoped because the loop is
    (``asyncio_default_*_loop_scope = "session"``) and because there is
    nothing to gain from cleaning between modules — the rows are inert
    while the run is in progress, and the defect is that they outlive
    it.

    ``--dist=loadfile`` gives each xdist worker its own session and its
    own sink, and the sink only ever holds ids this process wrote. That
    matters: ``test_router_nic.test_badauth_is_recorded`` counts rows by
    address around a single request, and it was already observed flaking
    once in 65 runs because a *sibling module* deleted a same-shaped row
    inside its window. A sweep that scanned for unattributed rows
    instead of naming its own would reintroduce that across all ten
    workers.
    """
    with record_unattributed_events() as ids:
        yield
        await purge_unattributed_events(ids, owner=f"conftest.sweep[{WORKER}]")


# ===================================================================== #
# #117 — the one row nothing could namespace
# ===================================================================== #
#
# Everything else this suite creates carries ``PYTEST_XDIST_WORKER``.
# ``app_settings['atrium_ddns']`` cannot: it is **one row per compose
# project**, the key is a production constant, and the readers include
# two processes pytest does not own — the api container serving the
# tests' own HTTP calls, and the worker container running the health
# check and the retention prune on a 60 s / 3600 s tick.
#
# So it cannot be namespaced and it cannot be monkeypatched (an
# in-process patch is invisible to the other two containers). What is
# left is to give it exactly one owner, a state it is always in, and a
# lock for the moments it is not.
#
# What was measured before this existed, on this box, over one full
# ``make test-backend`` (853 tests, 10.6 s):
#
# * the row was **ABSENT for 91.8 % of the run** (458 of 499 samples,
#   taken by the database server itself at 20 ms). Absent means
#   ``get_namespace`` falls back to ``DdnsConfig()``, where
#   ``health_check_enabled`` is **True** — so the worker container was
#   armed to sweep cross-tenant for nine seconds in ten;
# * 16 distinct MySQL connections **read** the row 277 times; 2 of them
#   — both on the single xdist worker running ``test_worker_jobs.py`` —
#   **wrote** it 62 times (36 upserts, 26 deletes). Counted from the
#   server's own general log, not from the code;
# * the readers are not the three files that spell ``load_config``.
#   Nine HTTP endpoints call it (``/board``, ``/devices``, ``/events``,
#   ``/hostnames/{id}/update``, ``/health-checks/run``, …) plus
#   ``router_nic._admit``, and **98 test functions across 10 files**
#   drive one of them without naming the config at all. A name-based
#   sweep sees three files; the population is ten.
#
# The teardown that produced the 91.8 % was correct on its own terms:
# it restored what it read on entry, and on a fresh database it read
# nothing. **"Faithful to what was found" and "safe" are different
# properties here, and #117 asks which one to pick.** Absence is the
# faithful answer and it is the one state in which a second process
# behaves differently from every in-process reader, so it is the wrong
# thing to restore to. The baseline below is present, explicit, and
# identical to ``DdnsConfig()`` except for the single field that arms
# another container.
#
# What this does NOT claim. The armed sweep was measured writing into
# the suite's own rows — with the worker tick amplified from 60 s to
# 1 s, 51 substantive sweeps and 277 ``health_check.transition`` lines
# across 15 full runs — and the suite stayed green 15/15, with
# ``test_router_health_checks.py`` 25/25. So this closes a live race
# and a real cross-container write; it is **not** an explanation of
# ``test_worker_jobs.py``'s two non-reproducing failures, which remain
# unexplained. See #117.

#: Set while this process owns the shared config row, for the same
#: reason :data:`_HELD_BY` exists: the lock is held on its own
#: connection, so a second acquisition from the same worker would wait
#: on a lock that worker already holds and read as a hang.
_CONFIG_HELD_BY: str | None = None


def ddns_config_baseline() -> DdnsConfig:
    """The state the shared row is in whenever no test owns it.

    ``DdnsConfig()``'s defaults with ``health_check_enabled=False`` and
    nothing else moved — so every value an endpoint reads through
    ``load_config`` is the model default a reader would expect, and the
    one field that makes another *container* act is off for the whole
    session rather than for 8 % of it.
    """
    return DdnsConfig(health_check_enabled=False)


async def read_ddns_config_row() -> dict[str, Any] | None:
    """The row as MySQL holds it, or ``None`` when it does not exist.

    ``None`` and ``{}`` are different states and this returns the
    difference: absent is what arms the worker container, and folding it
    into an empty config would hide exactly the condition this section
    exists to prevent.
    """
    factory = get_session_factory()
    async with factory() as s:
        return (
            await s.execute(
                sa.select(AppSetting.value).where(AppSetting.key == CONFIG_NAMESPACE)
            )
        ).scalar_one_or_none()


async def write_ddns_config(cfg: DdnsConfig) -> None:
    """Write the namespace through the path the app itself writes it.

    ``put_namespace`` rather than a hand-built ``INSERT … ON DUPLICATE
    KEY``: it validates against the registered model, so a test pinning
    a field the model dropped fails here instead of storing a value
    nothing will ever read back.
    """
    factory = get_session_factory()
    async with factory() as s:
        if not _needs_advisory_lock():
            # atrium's `put_namespace` builds a MySQL `INSERT … ON DUPLICATE
            # KEY UPDATE`, which SQLite cannot compile. The upsert exists
            # because ten workers share one row; on a per-worker in-memory
            # database the row cannot already exist, so a plain write is the
            # same operation without the dialect.
            #
            # atrium-pa reaches the same place from the other side: its
            # `_stub_app_config` replaces get_namespace/put_namespace with an
            # in-memory cell for its unit lane. Neither repo changes atrium.
            #
            # The cost, stated: `put_namespace` validates against the
            # registered model, and this does not. A test pinning a field the
            # model dropped would store it here instead of failing. The MySQL
            # lane still runs the validating path.
            row = await s.get(AppSetting, CONFIG_NAMESPACE)
            if row is None:
                s.add(AppSetting(key=CONFIG_NAMESPACE, value=cfg.model_dump(mode="json")))
            else:
                row.value = cfg.model_dump(mode="json")
            await s.commit()
            return
        await put_namespace(s, CONFIG_NAMESPACE, cfg.model_dump(mode="json"))


async def remove_ddns_config_row() -> None:
    """Delete the row, for the one test whose subject is its absence.

    ``test_get_namespace_answers_with_the_defaults_when_nothing_is_seeded``
    has to see the row gone. Routed through here rather than spelled in
    that module so the census in ``test_harness_guards`` can still say
    *exactly one file writes this row*; the caller holds
    :data:`DDNS_CONFIG_LOCK` for the duration and :func:`ddns_config`
    puts the baseline back.
    """
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(sa.delete(AppSetting).where(AppSetting.key == CONFIG_NAMESPACE))
        await s.commit()


@asynccontextmanager
async def ddns_config_lock(owner: str = "?") -> AsyncIterator[None]:
    """Exclusive ownership of the shared row, across every xdist worker.

    Same mechanism as :func:`fixture_writes` and for a different reason:
    that one prevents a deadlock between concurrent INSERTs, this one
    prevents a *reader on another worker* seeing a value a writer is
    about to take away. #108's agent measured that race —
    ``test_worker_jobs.py`` writes ``event_retention_days=90``,
    ``test_router_events.py`` reads it back through the API, and
    ``--dist=loadfile`` puts them on different workers — as
    ``assert 30 == 90``, one gate run in two.

    **Lock order is config-then-fixture, never the reverse.** Nothing in
    :func:`fixture_writes` or :func:`purge_tenants` touches this row, so
    there is no cycle today; the second refusal below is what keeps it
    that way, because a cycle between two 30-second named locks presents
    as a hung suite with no failing test to name.
    """
    global _CONFIG_HELD_BY
    if _CONFIG_HELD_BY is not None:
        raise RuntimeError(
            f"ddns_config_lock({owner!r}) nested inside "
            f"ddns_config_lock({_CONFIG_HELD_BY!r}). The lock is held on a "
            f"second connection, so this would block for "
            f"{FIXTURE_LOCK_TIMEOUT}s and then fail."
        )
    if _HELD_BY is not None:
        raise RuntimeError(
            f"ddns_config_lock({owner!r}) taken inside "
            f"fixture_writes({_HELD_BY!r}). Lock order is config-then-fixture; "
            "the reverse order on any worker makes a deadlock between the two "
            "named locks reachable, and it would present as a hang."
        )

    engine = get_engine()
    if not _needs_advisory_lock():
        _CONFIG_HELD_BY = owner
        try:
            yield
        finally:
            _CONFIG_HELD_BY = None
        return
    async with engine.connect() as guard:
        got = (
            await guard.execute(
                sa.text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": DDNS_CONFIG_LOCK, "timeout": FIXTURE_LOCK_TIMEOUT},
            )
        ).scalar()
        if got != 1:
            raise RuntimeError(
                f"GET_LOCK({DDNS_CONFIG_LOCK!r}, {FIXTURE_LOCK_TIMEOUT}) returned "
                f"{got!r} for {owner!r} — 0 is a timeout, NULL is an error. Not "
                "proceeding unguarded: an unguarded write to this row is visible "
                "to every other worker and to the api and worker containers."
            )
        _CONFIG_HELD_BY = owner
        try:
            yield
        finally:
            _CONFIG_HELD_BY = None
            await guard.execute(
                sa.text("SELECT RELEASE_LOCK(:name)"), {"name": DDNS_CONFIG_LOCK}
            )


@harness_guard
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _sqlite_schema() -> AsyncIterator[None]:
    """Create the schema on a non-MySQL engine.

    On MySQL the suite runs against a database alembic has already
    migrated, so this does nothing. On a per-worker in-memory SQLite there
    is no migration to run — the chain is written in MySQL's dialect — so
    the schema comes from the metadata instead.

    That is a real difference and worth naming: the MySQL lane tests the
    schema the migrations produce, this one tests the schema the models
    declare. They are supposed to agree; only the MySQL lane can prove it.
    """
    if _needs_advisory_lock():
        yield
        return
    from app.db import Base as _AtriumBase
    import app.models  # noqa: F401  — registers atrium's tables
    from atrium_ddns.models import HostBase as _HostBase

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(_AtriumBase.metadata.create_all)
        await conn.run_sync(_HostBase.metadata.create_all)
        # The three system roles. `create_all` builds tables, not rows, and
        # atrium seeds these in its init migration — which this lane never
        # runs, because the chain is written in MySQL's dialect. Without them
        # `assign_role` raises NoResultFound and anything that creates a user
        # with a role fails for a reason that looks nothing like the cause.
        #
        # Copied from atrium's `0001_atrium_init`. If that list ever grows,
        # this is where it diverges — the MySQL lane is what would catch it,
        # which is one of the things that lane is for.
        await conn.execute(
            sa.text(
                "INSERT INTO roles (code, name, is_system) VALUES"
                " ('super_admin', 'Super admin', 1),"
                " ('admin', 'Admin', 1),"
                " ('user', 'User', 1)"
            )
        )
    yield


@harness_guard
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _pin_ddns_config(_sqlite_schema: None) -> AsyncIterator[None]:
    """Put the shared row on its baseline, and refuse a session that
    ends with it anywhere else.

    Autouse and session-scoped because the property is about the whole
    run: between tests, during tests that never heard of the config, and
    while a container three processes away is on its own clock, the row
    must be present and the sweep must be off. Every worker seeds the
    same value under the lock, so ten sessions starting at once is ten
    idempotent writes rather than a lost update.

    **The exit check is the part that fails loudly.** A pin that
    silently stops being applied reads exactly like a pin that is
    working, and that is how the previous arrangement survived: its
    teardown deleted the row and the suite went on passing. This one
    takes the lock — so no other worker can be mid-test — and compares
    the row against the baseline, naming both sides. A fixture that
    starts restoring to absence again fails here, on the worker that did
    it, at the end of the session.
    """
    baseline = ddns_config_baseline()
    expected = baseline.model_dump(mode="json")

    async with ddns_config_lock(owner=f"conftest.baseline[{WORKER}]"):
        await write_ddns_config(baseline)
        seeded = await read_ddns_config_row()
        if seeded != expected:
            raise AssertionError(
                f"the {CONFIG_NAMESPACE} baseline did not stick on {WORKER}. "
                f"wrote {expected!r}, read back {seeded!r}. Absent means the "
                "worker container falls back to health_check_enabled=True and "
                "sweeps cross-tenant for the length of the run."
            )

    yield

    async with ddns_config_lock(owner=f"conftest.baseline-exit[{WORKER}]"):
        final = await read_ddns_config_row()
    if final != expected:
        raise AssertionError(
            f"the shared {CONFIG_NAMESPACE} row was left off its baseline at "
            f"the end of {WORKER}'s session.\n"
            f"  expected {expected!r}\n"
            f"  found    {final!r}\n"
            "Something wrote this row without going through the ddns_config "
            "fixture, or restored it to absence. Absence is the state that "
            "arms the worker container's cross-tenant sweep — see #117."
        )


@pytest_asyncio.fixture
async def ddns_config(
    request: Any,
) -> AsyncIterator[Callable[[DdnsConfig], Awaitable[None]]]:
    """Own the shared ``atrium_ddns`` row for one test, then put it back.

    Yields the writer, so a test that needs a particular value spells
    it: ``await ddns_config(DdnsConfig(event_retention_days=7))``. A
    test that only needs the row to hold still takes the fixture and
    calls nothing.

    Three properties, and the middle one is the fix:

    * **entry is checked, not assumed.** The row must already be the
      baseline. If it is not, an earlier test left it moved and every
      reader since has been reading somebody else's value — the failure
      this fixture exists to make impossible, so it is raised here
      rather than absorbed;
    * **the lock is held for the whole test**, not for the write. The
      race is between a writer on one worker and a *reader* on another,
      and a lock released at the write would still let the reader land
      between the write and the restore;
    * **teardown restores the baseline, not the entry state.** Restoring
      "what was there" is what deleted the row for 92 % of every run.
    """
    baseline = ddns_config_baseline()
    expected = baseline.model_dump(mode="json")
    owner = f"ddns_config[{getattr(request.node, 'nodeid', '?')}]"

    async with ddns_config_lock(owner=owner):
        on_entry = await read_ddns_config_row()
        if on_entry != expected:
            raise AssertionError(
                f"{CONFIG_NAMESPACE} was not on its baseline when {owner} took "
                f"the lock.\n  expected {expected!r}\n  found    {on_entry!r}\n"
                "The row has one owner (this fixture); a value here that is not "
                "the baseline means something else wrote it."
            )
        try:
            yield write_ddns_config
        finally:
            await write_ddns_config(baseline)


# --------------------------------------------------------------------- #
# The second surface that cannot be namespaced — #149
# --------------------------------------------------------------------- #
#
# The section above is about one shared *row*. This is about shared
# *rows*, and it is a different mechanism over a different table that
# happens to have the same answer.
#
# ``run_health_checks(scope=<cross-tenant>, due_only=False)`` — what
# ``POST /api/atrium_ddns/health-checks/run`` calls when the caller
# holds ``atrium_ddns.admin``, and deliberately the scheduled job's own
# function object rather than a second implementation (#75) — selects
# **every** hostname in the installation carrying a published address,
# orders ``dns_checked_at IS NULL`` first, caps at
# ``health_check_batch_size`` (default 200) and writes
# ``dns_checked_at``, ``dns_ip_v4``, ``dns_ip_v6`` and
# ``dns_check_error`` on every row it reaches. ``due_only=False`` is the
# part that makes the population the whole database: it drops the
# staleness clause, so a row checked a millisecond ago is still
# eligible.
#
# There is nothing to namespace. A hostname's tenancy is exactly what a
# cross-tenant scope is defined to ignore, so ten xdist workers writing
# ``PYTEST_XDIST_WORKER`` into every name they create does not help —
# the sweep does not select on the name.
#
# **Why #117's fix cannot cover this, stated because the two look
# alike.** #117 pinned ``app_settings['atrium_ddns']`` to
# ``health_check_enabled=False`` for the whole session, which stops the
# *worker container's* 60 s tick, and it was right to. This sweep never
# reads that row: ``test_router_health_checks.pinned_config`` is autouse
# and monkeypatches ``load_config`` in **both** ``router`` and
# ``worker_jobs`` to return ``DdnsConfig()``, whose default is
# ``health_check_enabled=True``. The baseline is structurally invisible
# to it. Two surfaces, two mechanisms, one answer.
#
# What was measured before this existed, on this box, over five full
# ``make test-backend`` runs (933 tests, ``-n auto --dist=loadfile``),
# with the sweep instrumented to report its own population:
#
#     considered  30  23  17   4  21
#     checked     18  11   7   2  11
#     its own      2   2   2   2   2
#
# — so 16, 9, 5, 0 and 9 rows belonging to *other files* had their
# health-check columns rewritten, on four runs in five. Extended to name
# them, one run caught ``b0.a-jobs-gw5.example.invalid`` in the eligible
# set: a row ``test_worker_jobs.py``'s
# ``test_the_health_check_batch_ceiling_reports_itself`` had created
# three statements earlier, on another worker. That test is #149.
#
# The failure is deterministic once the interleaving is forced. The same
# call injected between that test's seeding and its assertion gives
# ``assert 0 == 2``, with ``hostnames_due=0`` and ``hostnames_not_due=3``
# on the summary — the sweep had stamped all three rows non-due.
#
# **Deliberately :data:`DDNS_CONFIG_LOCK` and not a third named lock.**
# Two named locks with one documented order (config-then-fixture) is a
# thing a reader can hold in their head and a thing
# :func:`ddns_config_lock` refuses to violate; three named locks is a
# cycle waiting to be introduced, and a cycle between 30-second
# ``GET_LOCK``s presents as a hung suite with no failing test to name —
# the failure that second refusal exists to prevent. The two uses are
# also the same claim from two directions: the config row is the switch
# that arms an installation-wide sweep, and this *is* an
# installation-wide sweep. Nothing needs both at once, and the nesting
# refusal makes an attempt fail loudly rather than block.


@pytest_asyncio.fixture
async def installation_wide_sweep(request: Any) -> AsyncIterator[None]:
    """Exclusive right to run a sweep that reaches every tenant's rows.

    Taken by the one test that performs an installation-wide
    ``ddns_hostname`` write, and by every test whose assertions are
    about the ``dns_*`` columns of rows it owns. Both halves are
    required: a lock only one side takes excludes nothing, which is the
    shape of guard this repository keeps finding in its own code.

    Held for the **length of the test**, not for the write, for
    :func:`ddns_config`'s reason — the race is between a writer on one
    worker and a *reader* on another, and a reader that lands between
    the write and the end of the test sees the same damage.

    The population on both sides is a census rather than a habit:
    ``test_harness_guards.test_the_cross_tenant_sweep_has_one_writer``
    derives it from the tests' own source and fails when a second writer
    appears.
    """
    owner = f"installation_wide_sweep[{getattr(request.node, 'nodeid', '?')}]"
    async with ddns_config_lock(owner=owner):
        yield
