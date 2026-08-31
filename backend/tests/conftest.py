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
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager

import pytest_asyncio
import sqlalchemy as sa
from app.db import get_engine, get_session_factory
from app.models.auth import User
from atrium_ddns.models import DnsEvent
from sqlalchemy.ext.asyncio import AsyncSession

#: This worker's namespace. Ten workers share one MySQL, so anything a
#: test creates carries it — a hardcoded email or device name produces
#: collisions that read as flakiness.
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "serial")

#: The advisory lock every fixture write takes. Server-wide, and the
#: server is this compose project's own container, so two agents running
#: side by side cannot see each other's lock.
FIXTURE_LOCK = "atrium_ddns_fixture_writes"

#: Seconds to wait for the lock. Long enough that a slow fixture on a
#: loaded box does not trip it, short enough that a re-entrancy bug
#: fails the run instead of looking like a hang.
FIXTURE_LOCK_TIMEOUT = 30

#: Set while this process holds the lock. Taking it twice from one
#: worker would wait on a lock the same worker holds — on a *different*
#: connection, so MySQL's re-entrancy does not save us — and the symptom
#: would be a 30-second stall that reads as a hang. Refuse instead, and
#: name the caller.
_HELD_BY: str | None = None


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
