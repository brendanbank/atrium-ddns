"""Guards on the test harness itself.

#65's acceptance criterion has a structural half — "all seven copies
gone; a guard fails if an eighth appears" — and a measured half, twenty
consecutive parallel runs with zero deadlocks. This file is the
structural half. The measured half cannot be a test: a suite cannot run
itself twenty times, and a test that *could* would be measuring the run
it is inside.

Everything here reads the tree from disk rather than by importing, so a
module that fails to import still gets counted. Every guard carries a
vacuity check, because the failure mode of a scan-the-source guard is
scanning nothing and reporting clean — this repository's own most
frequent defect, aimed at itself.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent

#: Every module whose fixtures build rows in the shared database.
#: Named rather than globbed so that a file *disappearing* is a failure
#: here instead of silently shrinking the population.
#:
#: It started as the eight modules #65 is about — seven with a
#: ``_purge`` and ``test_user_scope_secrets.py``, which had none and
#: contended anyway — and #78 found that a hand-kept list is a snapshot
#: of one branch. #65 was authored on V1M3 and the list was correct
#: there; ``test_import_legacy.py`` and ``test_rehearse_migration.py``
#: were being written on V1M4 at the same hour, and arrived by merge
#: into a tree whose accounting had already been taken. Two of the
#: three guards below scan every module and duly went red on that
#: merge; :func:`test_the_shared_teardown_is_the_one_everyone_calls`
#: reads this list, and stayed green about four modules it had never
#: heard of. ``test_router_hostnames.py`` and
#: ``test_router_health_checks.py`` are the other two — neither had
#: done anything wrong, and neither was being checked.
#:
#: So the list is no longer maintained by hand alone:
#: :func:`test_the_population_is_exactly_the_modules_that_touch_the_db`
#: derives it and requires the two to agree.
EXPECTED_PARTICIPANTS = frozenset(
    {
        "test_host_models.py",
        "test_import_legacy.py",
        "test_rehearse_migration.py",
        "test_router_board.py",
        "test_router_events.py",
        "test_router_health_checks.py",
        "test_router_hostnames.py",
        "test_router_nic.py",
        "test_router_tenant.py",
        "test_tenant_isolation.py",
        "test_user_scope_secrets.py",
        "test_worker_jobs.py",
    }
)

#: The database entry points a fixture has to go through to write a
#: row: ``conftest``'s two helpers, and the two ``app.db`` factories a
#: fixture reaches for when it is not using them.
DB_ENTRY_POINTS = frozenset(
    {"fixture_writes", "get_engine", "get_session_factory", "purge_tenants"}
)


def _test_modules() -> list[pathlib.Path]:
    return sorted(p for p in TESTS_DIR.glob("test_*.py"))


def test_the_scan_can_see_the_tree_it_scans() -> None:
    """Vacuity, first, and against a named list rather than a count.

    A guard that globs an empty directory passes every assertion below.
    The check here is that every named participant is still a file on
    disk; whether the *list* still names the right files is
    :func:`test_the_population_is_exactly_the_modules_that_touch_the_db`,
    which #78 added after four DB-heavy modules turned out to have
    joined the tree without joining the list.
    """
    found = {p.name for p in _test_modules()}
    assert found, f"no test modules found under {TESTS_DIR} — the scan is vacuous"
    missing = EXPECTED_PARTICIPANTS - found
    assert not missing, f"expected test modules are gone: {sorted(missing)}"
    assert (TESTS_DIR / "conftest.py").is_file(), "the shared teardown is missing"


def test_no_module_defines_its_own_purge() -> None:
    """The eighth copy fails here.

    Parsed rather than grepped, so a copy that spans lines, carries a
    decorator or hides inside a class is still seen — and so a mention
    of ``_purge`` in a docstring is not miscounted as a definition.

    The message names the file and the line, because a guard that says
    "no match found" gets deleted rather than investigated.
    """
    offenders: list[str] = []
    for path in _test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name == "_purge" or node.name.startswith("_purge_")
            ):
                if path.name == "test_router_events.py" and node.name == (
                    "_purge_this_module"
                ):
                    # Allowed by name: a three-line wrapper that hands
                    # its own recorded event ids to the shared teardown.
                    # It holds no DELETE of its own — asserted below.
                    continue
                offenders.append(f"{path.name}:{node.lineno} defines {node.name}()")
    assert not offenders, (
        "teardown belongs in conftest.purge_tenants, not in a per-module copy:\n  "
        + "\n  ".join(offenders)
    )


def _fixtures(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function decorated with a pytest fixture, by decorator text."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if "fixture" in ast.unparse(decorator):
                found.append(node)
                break
    return found


def test_no_fixture_deletes_rows_itself() -> None:
    """Fixtures call the shared teardown; they do not write their own.

    Scoped to **fixtures**, not to the whole module, and the distinction
    is load-bearing rather than a convenience. A *test body* may
    legitimately issue ``DELETE FROM users`` — that is exactly what
    ``test_host_models.py``'s cascade test does, and banning it there
    would delete a real assertion to satisfy a guard. What #65 is about
    is teardown: eight modules each owning a copy of it.

    Vacuity: the scan must find fixtures at all, in modules known to
    have them. Otherwise a decorator spelling this walk does not
    recognise turns the guard into a pass.
    """
    offenders: list[str] = []
    seen_fixtures = 0
    for path in _test_modules():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in _fixtures(tree):
            seen_fixtures += 1
            body = ast.get_source_segment(source, node) or ""
            upper = body.upper()
            for statement in ("DELETE FROM USERS", "DELETE FROM DDNS_EVENT"):
                if statement in upper:
                    offenders.append(
                        f"{path.name}:{node.lineno} fixture {node.name}() "
                        f"contains {statement}"
                    )
    assert seen_fixtures >= len(EXPECTED_PARTICIPANTS), (
        f"only {seen_fixtures} fixtures found across {len(_test_modules())} "
        "modules — the decorator walk is not seeing them"
    )
    assert not offenders, (
        "teardown belongs in conftest.purge_tenants:\n  " + "\n  ".join(offenders)
    )


def _names_referenced(node: ast.AST) -> set[str]:
    """Every identifier a subtree *refers to*, string literals excluded.

    By binding rather than by text, and the contrast with
    :func:`test_no_fixture_deletes_rows_itself` above is deliberate.
    That guard matches SQL as text over
    ``ast.get_source_segment(...)``, so it cannot tell a statement from
    a sentence describing one — a fixture whose docstring explains
    which teardown it no longer owns fails it, and a ``DELETE`` a
    fixture assembles from a variable passes. Both directions are
    wrong; it is still the right guard for what it catches, because a
    teardown written any other way is a much rarer thing than a
    teardown written literally. Filed rather than changed here — see
    the note in #78. Here the distinction costs nothing:
    ``ast.Name``/``ast.Attribute`` never look inside a string.
    """
    used: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            used.add(child.id)
        elif isinstance(child, ast.Attribute):
            used.add(child.attr)
    return used


def _modules_whose_fixtures_touch_the_db() -> dict[str, str]:
    """``module -> the first fixture in it that reaches the database``."""
    found: dict[str, str] = {}
    for path in _test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for node in _fixtures(tree):
            reached = sorted(DB_ENTRY_POINTS & _names_referenced(node))
            if reached:
                found[path.name] = (
                    f"{path.name}:{node.lineno} fixture {node.name}() "
                    f"uses {', '.join(reached)}"
                )
                break
    return found


def test_the_population_is_exactly_the_modules_that_touch_the_db() -> None:
    """``EXPECTED_PARTICIPANTS`` is derived, not just declared.

    This is #78's guard rather than #65's, and it exists because #65's
    list was a hand-taken census that a merge could invalidate without
    touching a line of it. Four modules had DB-writing fixtures and
    were not in the list; the guard that reads the list was green about
    all four.

    Asserted as an **equality**, in both directions and separately, so
    the message says which mistake was made:

    * a module missing from the list is one nobody is checking reaches
      the shared teardown — the hole #78 closes;
    * a module in the list whose fixtures no longer touch the database
      inflates the vacuity floor below and dilutes what the list means.

    The second direction is also this guard's own vacuity check. If the
    walk stopped recognising fixtures, or ``DB_ENTRY_POINTS`` stopped
    matching anything, the derived set would empty and every module in
    the list would be reported — loudly, rather than as a clean pass.
    """
    detected = _modules_whose_fixtures_touch_the_db()
    uncounted = sorted(set(detected) - EXPECTED_PARTICIPANTS)
    stale = sorted(EXPECTED_PARTICIPANTS - set(detected))
    assert not uncounted, (
        "these modules build rows in fixtures and are not in "
        "EXPECTED_PARTICIPANTS, so nothing checks that they use the shared "
        "teardown — add them:\n  "
        + "\n  ".join(detected[name] for name in uncounted)
    )
    assert not stale, (
        "these are in EXPECTED_PARTICIPANTS but no fixture in them reaches "
        f"{sorted(DB_ENTRY_POINTS)} any more. Either they stopped touching "
        "the database and should be dropped from the list, or this scan has "
        f"stopped seeing what it is looking for: {stale}"
    )


def test_the_shared_teardown_is_the_one_everyone_calls() -> None:
    """Every participant reaches the shared helpers, by import.

    Asserted from the *modules*, not from ``conftest``: a helper can be
    perfectly correct and called by nobody, which is the
    writer-nothing-calls half of this repo's favourite defect family.
    """
    silent: list[str] = []
    for name in sorted(EXPECTED_PARTICIPANTS):
        source = (TESTS_DIR / name).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=name)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "conftest":
                imported.update(alias.name for alias in node.names)
        if not {"purge_tenants", "fixture_writes"} & imported:
            silent.append(name)
    assert not silent, (
        "these build rows without the shared teardown or the fixture lock: "
        f"{silent}"
    )


def test_the_fixture_lock_is_not_a_no_op() -> None:
    """``fixture_writes`` refuses rather than proceeding unguarded.

    The lock's whole value is that ``GET_LOCK`` returning 0 (timed out)
    or NULL (error) stops the run. A version that logged and carried on
    would still be green almost always — the shape of guard this repo
    keeps finding in its own code — so the refusal is asserted here
    rather than assumed.
    """
    source = (TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="conftest.py")
    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
    ]
    assert len(guarded) >= 2, (
        "conftest.fixture_writes must raise on a failed GET_LOCK and on "
        f"re-entry; found {len(guarded)} raise statements"
    )
    assert "GET_LOCK" in source and "RELEASE_LOCK" in source


@pytest.mark.asyncio
async def test_the_lock_actually_excludes() -> None:
    """Show the lock biting, rather than trusting that it does.

    A second acquisition from an independent connection must **not**
    succeed while the first is held. Without this, every claim in
    ``conftest`` rests on ``GET_LOCK`` behaving the way its
    documentation says, on this server, at this isolation level — which
    is exactly the class of assumption that produced #65's wrong
    diagnosis in the first place.
    """
    import sqlalchemy as sa
    from app.db import get_engine
    from conftest import FIXTURE_LOCK, fixture_writes

    async with fixture_writes("test_harness_guards.lock-bites"):
        async with get_engine().connect() as rival:
            # 0 = "held by someone else, gave up after the timeout". The
            # timeout is deliberately short: this is the *contended*
            # path and it is meant to fail fast.
            answer = (
                await rival.execute(
                    sa.text("SELECT GET_LOCK(:n, 0)"), {"n": FIXTURE_LOCK}
                )
            ).scalar()
            assert answer == 0, (
                f"a rival connection acquired {FIXTURE_LOCK!r} while it was "
                f"held (GET_LOCK returned {answer!r}) — the lock excludes nothing"
            )

    # And the control: once released, the same rival gets it. Without
    # this half, a GET_LOCK that always returned 0 would pass the above.
    async with get_engine().connect() as rival:
        answer = (
            await rival.execute(
                sa.text("SELECT GET_LOCK(:n, 5)"), {"n": FIXTURE_LOCK}
            )
        ).scalar()
        assert answer == 1, f"the lock was not released (GET_LOCK returned {answer!r})"
        await rival.execute(sa.text("SELECT RELEASE_LOCK(:n)"), {"n": FIXTURE_LOCK})
