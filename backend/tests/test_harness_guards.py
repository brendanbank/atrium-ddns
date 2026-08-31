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
import re
from collections.abc import Iterable, Mapping

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent

#: The shared teardown, and the host's ORM models. Both read from disk
#: rather than imported, for the reason the module docstring gives.
#: ``MODELS`` resolves inside the api container too — the image does
#: ``COPY backend /opt/host_app``, so ``tests/`` and ``src/`` keep the
#: same relationship there as in the worktree.
CONFTEST = TESTS_DIR / "conftest.py"
MODELS = TESTS_DIR.parent / "src" / "atrium_ddns" / "models.py"

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
        # #74. Named here because #78's derivation guard named it first:
        # its `world` and `pre_migration_hostname` fixtures both write
        # rows, so the derived set contained it and this list did not.
        # That is the hole #78 closed doing its job on the very next
        # branch to add a DB-heavy module.
        "test_router_hostname_backends.py",
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


#: ``DELETE FROM <table>`` in any casing, across a line break, with the
#: whitespace SQL allows. Stricter than the ``"DELETE FROM USERS" in
#: upper`` it replaces in one direction — ``\w+`` then compared against
#: a known table, so ``DELETE FROM users_archive`` is not a hit for
#: ``users`` — and looser in another, since a statement split over two
#: source lines now matches.
_DELETE = re.compile(r"DELETE\s+FROM\s+(\w+)", re.IGNORECASE)

#: A floor on the derived set below, not a substitute for it. The
#: failure this module exists to refuse is a scan that matches nothing
#: and reports clean; a derivation that quietly returned the empty set
#: would make :func:`test_no_fixture_deletes_rows_itself` vacuous, and
#: it would still be green. These two are the tables #65 and #78 were
#: about, so their absence is a broken derivation rather than a change
#: of policy.
TABLES_THE_GUARD_MUST_COVER = frozenset({"users", "ddns_event"})


def _docstrings(node: ast.AST) -> set[int]:
    """``id()`` of every docstring constant anywhere in a subtree.

    By identity, not by value. Two functions may carry the same
    docstring, and a fixture may hold a nested helper with one of its
    own; excluding by *text* would also drop a genuine code string that
    happened to read the same.
    """
    found: set[int] = set()
    for child in ast.walk(node):
        if not isinstance(
            child, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = child.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


def _code_strings(node: ast.AST) -> list[str]:
    """Every string literal in a subtree's *code*, docstrings excluded.

    Comments are excluded for free — they are not in the tree at all.
    f-strings are included for free, the other way: an ``ast.JoinedStr``
    holds its literal chunks as ``ast.Constant``, so
    ``f"DELETE FROM users WHERE email = {e}"`` yields its SQL here,
    while a statement assembled from a *variable* still does not.
    """
    skip = _docstrings(node)
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and id(child) not in skip
    ]


def _tables_deleted_in(strings: Iterable[str], tables: Iterable[str]) -> set[str]:
    """Which of ``tables`` these strings issue a ``DELETE FROM`` against."""
    watched = {t.lower() for t in tables}
    return {t.lower() for text in strings for t in _DELETE.findall(text)} & watched


def _tables_the_shared_teardown_owns() -> frozenset[str]:
    """The tables ``conftest.purge_tenants`` deletes from, from its body.

    Derived rather than typed, because the guard's whole claim is that
    a fixture must not do centrally-owned teardown itself: the set it
    forbids in a fixture is *by definition* the set the shared helper
    issues. A table added to ``purge_tenants`` is covered here without
    anyone remembering to add it, which is the failure mode
    ``EXPECTED_PARTICIPANTS`` had before #78.

    Read with the same docstring-excluding walk as the guard, so
    ``purge_tenants``'s own prose about ``DELETE FROM users`` is not the
    thing that puts ``users`` in the set.
    """
    tree = ast.parse(CONFTEST.read_text(encoding="utf-8"), filename="conftest.py")
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "purge_tenants"
        ):
            return frozenset(
                t.lower() for s in _code_strings(node) for t in _DELETE.findall(s)
            )
    return frozenset()


def _models_for(tables: Iterable[str]) -> dict[str, str]:
    """``class name -> table``, for the guarded tables this repo declares.

    ``users`` and ``user_secret_keys`` are atrium's tables, not this
    host's, so no class in this tree maps to them and the ORM half
    below **cannot reach them**. The string half does, and that split is
    named here rather than left implicit: a guard that covers half of
    what it looks like it covers is worse than one that says which half.
    """
    tree = ast.parse(MODELS.read_text(encoding="utf-8"), filename=MODELS.name)
    watched = {t.lower() for t in tables}
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                names = [stmt.target.id]
            else:
                continue
            value = stmt.value
            if (
                "__tablename__" in names
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value.lower() in watched
            ):
                found[node.name] = value.value.lower()
    return found


def _orm_deletes(node: ast.AST, models: Mapping[str, str]) -> set[str]:
    """Guarded tables reached by ``sa.delete(Model)`` rather than by SQL.

    Only the *construct* form, and only for a model one of the guarded
    tables maps to. Counted rather than assumed: one fixture in this
    suite calls ``delete`` at all — ``test_worker_jobs.py``'s
    ``config()``, with ``sa.delete(AppSetting)``, restoring a config row
    it moved and doing exactly the right thing. An unscoped rule would
    flag it and be reverted within the week.

    The per-instance form ``session.delete(obj)`` is deliberately **not**
    matched: ``client.delete(url)`` is the identical shape, and there
    are ten of those in this suite. All ten sit in test bodies today,
    which this guard does not scan — but a fixture that drives the HTTP
    client is an ordinary thing to write, and keying on the attribute
    name alone would flag it. No fixture here uses the per-instance
    form; if one ever does, this is the hole it goes through, and it is
    a hole with a name rather than a silent pass.
    """
    hit: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or not child.args:
            continue
        func = child.func
        called = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        )
        if called != "delete":
            continue
        first = child.args[0]
        model = (
            first.attr if isinstance(first, ast.Attribute) else getattr(first, "id", "")
        )
        if model in models:
            hit.add(models[model])
    return hit


def _deletes_by_ast(
    source: str,
    node: ast.AST,
    tables: Iterable[str],
    models: Mapping[str, str],
) -> set[str]:
    """Instrument A: what the fixture's **code** deletes.

    Strings the parser says are code, plus the ORM construct. Prose —
    docstring or comment — is not code and is not seen.

    ``source`` is unused here and ``models`` is unused in instrument B.
    Both are in both signatures on purpose: the two are called in the
    same loop, over the same population, and a caller that had to
    remember which one takes what would eventually pass the wrong one.
    """
    return _tables_deleted_in(_code_strings(node), tables) | _orm_deletes(node, models)


def _deletes_by_text(
    source: str,
    node: ast.AST,
    tables: Iterable[str],
    models: Mapping[str, str] | None = None,
) -> set[str]:
    """Instrument B: what the fixture's **source segment** says.

    This is the matcher the guard used before #85, kept rather than
    deleted, because the two disagree in exactly the cases that decide
    whether the guard is worth having —
    :func:`test_the_two_matchers_disagree_only_over_prose_and_orm` is
    that comparison, run as a test rather than asserted in a comment.
    """
    return _tables_deleted_in([ast.get_source_segment(source, node) or ""], tables)


def test_no_fixture_deletes_rows_itself() -> None:
    """Fixtures call the shared teardown; they do not write their own.

    Scoped to **fixtures**, not to the whole module, and the distinction
    is load-bearing rather than a convenience. A *test body* may
    legitimately issue ``DELETE FROM users`` — that is exactly what
    ``test_host_models.py``'s cascade test does, and banning it there
    would delete a real assertion to satisfy a guard. What #65 is about
    is teardown: eight modules each owning a copy of it.

    **Matched against the parsed body, not the source segment** — #85.
    Until then this was the one guard in the file that grepped, over
    ``ast.get_source_segment``, which returns the docstring with the
    code. Both directions were wrong. A fixture whose docstring
    explained the teardown it had *stopped* owning failed, textually
    identically to not having done the work — #78 paid that tax, by
    writing two fixtures' docstrings around the literal SQL. And a
    teardown built out of an f-string went through untouched.

    What it still does not catch, said plainly rather than implied:
    a statement assembled from a variable (``"DELETE FROM " + table``),
    and any ORM delete of ``users`` or ``user_secret_keys``, which are
    atrium's tables and have no model class in this tree
    (:func:`_models_for`). It is a copy-paste detector with an ORM
    corner, not a teardown detector.

    Vacuity, three ways, because a guard of this shape fails by
    matching nothing: the scan must find fixtures at all; the statement
    set it forbids must be non-empty and must still contain the two
    tables the guard was opened about; and the ORM half must have
    resolved at least one model.
    """
    tables = _tables_the_shared_teardown_owns()
    assert TABLES_THE_GUARD_MUST_COVER <= tables, (
        "the forbidden statements are derived from conftest.purge_tenants "
        f"and came back as {sorted(tables)}, which does not cover "
        f"{sorted(TABLES_THE_GUARD_MUST_COVER)} — either the shared teardown "
        "stopped deleting them or this derivation has stopped reading it, "
        "and in the second case every assertion below is vacuous"
    )
    models = _models_for(tables)
    assert models, (
        f"no model in {MODELS.name} maps to any of {sorted(tables)}, so the "
        "sa.delete(...) half of this guard matches nothing at all"
    )

    offenders: list[str] = []
    seen_fixtures = 0
    for path in _test_modules():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in _fixtures(tree):
            seen_fixtures += 1
            for table in sorted(_deletes_by_ast(source, node, tables, models)):
                offenders.append(
                    f"{path.name}:{node.lineno} fixture {node.name}() "
                    f"contains DELETE FROM {table.upper()}"
                )
    assert seen_fixtures >= len(EXPECTED_PARTICIPANTS), (
        f"only {seen_fixtures} fixtures found across {len(_test_modules())} "
        "modules — the decorator walk is not seeing them"
    )
    assert not offenders, (
        "teardown belongs in conftest.purge_tenants:\n  " + "\n  ".join(offenders)
    )


#: Fixture-shaped sources, and the verdict each instrument owes on
#: them: ``(label, source, instrument A says, instrument B says)``.
#:
#: Written as a table rather than as six tests because the *pattern of
#: disagreement* is the claim, not any single row. Rows 1 and 2 are the
#: false positives #78 paid for; row 5 is the false negative #85 was
#: opened about; row 6 is the gap neither closes, kept visible on
#: purpose; row 7 is the case-table's own vacuity check — an instrument
#: that flagged everything would pass every other row here.
MATCHER_CASES: tuple[tuple[str, str, bool, bool], ...] = (
    (
        "a docstring naming the teardown it stopped owning",
        '''
@pytest.fixture
def world():
    """It used to hold its own DELETE FROM users; it now calls
    conftest.purge_tenants, which issues that statement itself."""
    yield 1
''',
        False,
        True,
    ),
    (
        "a comment naming the statement",
        """
@pytest.fixture
def world():
    # Teardown was a DELETE FROM ddns_event here until #65.
    yield 1
""",
        False,
        True,
    ),
    (
        "the statement itself, the way #78 found it",
        """
@pytest.fixture
async def imported():
    yield 1
    async with factory() as session:
        await session.execute(
            sa.text("DELETE FROM users WHERE email = :e"), {"e": EMAIL}
        )
""",
        True,
        True,
    ),
    (
        "the statement in an f-string",
        """
@pytest.fixture
async def world():
    yield 1
    await s.execute(sa.text(f"DELETE FROM ddns_event WHERE user_id = {uid}"))
""",
        True,
        True,
    ),
    (
        "the ORM construct, which is not text at all",
        """
@pytest.fixture
async def world():
    yield 1
    await s.execute(sa.delete(DnsEvent).where(DnsEvent.user_id == uid))
""",
        True,
        False,
    ),
    (
        "assembled from a variable — the gap neither instrument closes",
        """
@pytest.fixture
async def world():
    yield 1
    await s.execute(sa.text("DELETE FROM " + table + " WHERE id = 1"))
""",
        False,
        False,
    ),
    (
        "a fixture doing it properly",
        """
@pytest.fixture
async def world():
    await purge_tenants([EMAIL], owner=LOCK_OWNER)
    yield 1
    await purge_tenants([EMAIL], owner=LOCK_OWNER)
""",
        False,
        False,
    ),
)


def test_the_two_matchers_disagree_only_over_prose_and_orm() -> None:
    """Two instruments over one population, and both readings reported.

    ``_deletes_by_ast`` reads the parsed body; ``_deletes_by_text``
    reads the source segment. They are not a check and a re-check —
    they are differently shaped, and #85 exists because the difference
    is where the guard was wrong in both directions at once.

    Asserted per row, so a regression names the case that moved rather
    than a count. The last two assertions are this table's own vacuity
    check: it must contain at least one row where each instrument is
    alone in flagging, or "they disagree" is being asserted against
    nothing.
    """
    tables = _tables_the_shared_teardown_owns()
    models = _models_for(tables)
    ast_only: list[str] = []
    text_only: list[str] = []
    for label, source, want_ast, want_text in MATCHER_CASES:
        tree = ast.parse(source)
        fixtures = _fixtures(tree)
        assert len(fixtures) == 1, f"{label}: the case source is not one fixture"
        node = fixtures[0]
        by_ast = bool(_deletes_by_ast(source, node, tables, models))
        by_text = bool(_deletes_by_text(source, node, tables, models))
        assert by_ast is want_ast, (
            f"{label}: the parsed-body matcher said {by_ast}, expected "
            f"{want_ast} (text matcher said {by_text})"
        )
        assert by_text is want_text, (
            f"{label}: the source-segment matcher said {by_text}, expected "
            f"{want_text} (parsed-body matcher said {by_ast})"
        )
        if by_ast and not by_text:
            ast_only.append(label)
        if by_text and not by_ast:
            text_only.append(label)
    assert ast_only, (
        "no case where only the parsed-body matcher fires — the false "
        "negative #85 was opened about is not being exercised"
    )
    assert text_only, (
        "no case where only the source-segment matcher fires — the false "
        "positive #78 paid for is not being exercised"
    )


def harness_guards() -> tuple[str, ...]:
    """Every guard this module defines, read off its own source.

    Here for #108, which owns the sweep of *all* the harness guards to a
    negative result and needs a population it did not type. This covers
    one module — the one the guards live in — and is derived from the
    file on disk, so a guard deleted takes itself out of the list and a
    guard added joins it without a second edit.

    A ``pytest.mark`` would be the more general mechanism and is
    deliberately not used: ``--strict-markers`` is on and no ``markers``
    list is registered, so introducing one means editing
    ``backend/pyproject.toml``, which is outside #85's declared scope.
    """
    tree = ast.parse(
        pathlib.Path(__file__).read_text(encoding="utf-8"), filename=__file__
    )
    return tuple(
        sorted(
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
    )


def test_the_guard_list_is_derived_and_matches_what_was_imported() -> None:
    """Two readings of this module's own population, and they must agree.

    The list on disk against the names the interpreter actually bound.
    They come apart when a guard is defined inside a ``class``, under an
    ``if``, or shadowed by a later definition — all of which are ways to
    have a guard that :func:`harness_guards` reports and pytest does not
    run, or the reverse. Either way #108's sweep would be counting
    something other than what executes.
    """
    on_disk = set(harness_guards())
    imported = {
        name
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    }
    assert on_disk, (
        "no guards found in this module's own source — the walk is vacuous"
    )
    assert on_disk == imported, (
        "the guards read off this file and the guards this module bound "
        f"disagree: only on disk {sorted(on_disk - imported)}, only imported "
        f"{sorted(imported - on_disk)}"
    )


def _names_referenced(node: ast.AST) -> set[str]:
    """Every identifier a subtree *refers to*, string literals excluded.

    By binding rather than by text. #78 added this walk and recorded
    the contrast with :func:`test_no_fixture_deletes_rows_itself`, which
    at the time matched SQL as text over ``ast.get_source_segment(...)``
    and so could not tell a statement from a sentence describing one.
    **#85 closed that**: the guard above now reads the parsed body, and
    keeps the text matcher only as the second instrument it is compared
    against. Here the distinction costs nothing either way —
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


# ===================================================================== #
# #87 — the teardown that could not name what it deleted
# ===================================================================== #
#
# Clause 3 of V1M7 asks that every guard the suite relies on is shown
# failing when the thing it guards is broken. The four below are about
# `conftest`'s unattributed-event sweep, and they are deliberately
# *runtime* guards rather than source scans: the sweep's failure mode is
# that its listener stops being reached, which an AST walk cannot see.
# `test_the_recorder_sees_the_routers_own_write` drives the production
# writer, and `test_the_sweep_refuses_a_delete_that_matched_nothing`
# breaks the teardown on purpose and requires the noise.


def _guard_ip() -> str:
    """A TEST-NET-2 address unique to this xdist worker.

    An unattributed row carries no user and no email — that is the state
    under test — so the address is the only handle, exactly as
    ``test_router_nic.BADAUTH_IP`` records. **TEST-NET-2 rather than
    TEST-NET-3**, so that nothing here can be confused with the
    ``203.0.113.0/24`` addresses ``test_router_nic.py`` counts on: two
    guards sharing an address space is how the sibling-module
    cancellation in that file's docstring happened.
    """
    import zlib

    from conftest import WORKER

    return "198.51.100.%d" % (1 + zlib.crc32(WORKER.encode()) % 250)


async def _write_unattributed_row(client_ip: str) -> int:
    """One ``badauth``-shaped row with no tenant on it. Returns its id.

    Written through ``router_nic.record_event`` with ``auth=None`` —
    the production writer, called the way the production caller calls
    it — rather than by constructing a ``DnsEvent`` here. A guard that
    builds its own subject asserts about a row shaped like the one that
    leaks; this asserts about the row that leaks.
    """
    from app.db import get_session_factory
    from atrium_ddns import router_nic
    from atrium_ddns.models import DnsEvent

    factory = get_session_factory()
    async with factory() as session:
        router_nic.record_event(
            session,
            event_type=router_nic.EVENT_AUTH,
            response_code=router_nic.STATUS_BADAUTH,
            auth=None,
            client_ip=client_ip,
        )
        # Taken from ``session.new`` *before* the flush, which is the
        # only moment the pending object is reachable by identity. The
        # writer returns nothing, and re-finding the row afterwards by
        # ``client_ip`` would be a scan asserting on a shape rather than
        # on the row this call produced.
        pending = [obj for obj in session.new if isinstance(obj, DnsEvent)]
        assert len(pending) == 1, (
            f"router_nic.record_event queued {len(pending)} DnsEvent rows, "
            "expected exactly 1 — this helper can no longer name what it wrote"
        )
        row = pending[0]
        assert row.user_id is None and row.user_email is None, (
            "record_event(auth=None) produced an attributed row "
            f"(user_id={row.user_id!r}, user_email={row.user_email!r}) — the "
            "state #87 is about is no longer reachable through this writer"
        )
        await session.flush()
        row_id = row.id
        await session.commit()
    assert row_id is not None, (
        "the writer produced no primary key — every assertion below would be "
        "about the id None"
    )
    return int(row_id)


async def _event_exists(row_id: int) -> bool:
    """Read the row back by primary key, on its own connection."""
    import sqlalchemy as sa
    from app.db import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        found = (
            await session.execute(
                sa.text("SELECT COUNT(*) FROM ddns_event WHERE id = :i"), {"i": row_id}
            )
        ).scalar_one()
    return bool(found)


@pytest.mark.asyncio
async def test_an_unattributed_row_survives_every_email_shaped_teardown() -> None:
    """#87's mechanism, demonstrated rather than described.

    ``purge_tenants`` names tenants two ways and both are email-shaped:
    ``emails`` resolves to ``users.id`` and deletes by ``user_id``, and
    ``unattributed_emails`` matches ``user_email`` directly. The row
    ``router_nic`` writes when a credential does not resolve has **both
    columns NULL**, and ``IN (…)`` never matches NULL — so the teardown
    runs, reports nothing wrong, and leaves the row behind.

    Both halves are asserted, because only the pair is a measurement:
    the row is confirmed *present* before the teardown (otherwise
    "still present after" is vacuous) and confirmed *still present*
    after. A version of this guard that only checked the second half
    would pass against a table that never got the row at all.

    The email handed in is this worker's own and belongs to nobody, so
    the call is a real teardown with a real, non-empty predicate. That
    is the point: it is not failing to delete because it was given
    nothing to do.
    """
    from conftest import WORKER, purge_tenants

    nobody = f"harness-guard-{WORKER}@nobody.invalid"
    row_id = await _write_unattributed_row(_guard_ip())
    try:
        assert await _event_exists(row_id), (
            f"the unattributed row {row_id} was not written — the assertion "
            "that it survives teardown would be vacuous"
        )
        await purge_tenants(
            [nobody],
            unattributed_emails=[nobody],
            owner="test_harness_guards.email-shaped",
        )
        assert await _event_exists(row_id), (
            f"row {row_id} was removed by an email-shaped teardown. That is "
            "not the behaviour #87 is about, and the sweep in conftest is "
            "now solving a problem that no longer exists — check whether "
            "router_nic has started attributing badauth rows"
        )
    finally:
        await purge_tenants(
            (), event_ids=[row_id], owner="test_harness_guards.cleanup"
        )
    assert not await _event_exists(row_id), (
        f"row {row_id} survived purge_tenants(event_ids=…) as well, so the "
        "id-shaped teardown does not work either and #87 has no fix"
    )


@pytest.mark.asyncio
async def test_the_recorder_sees_the_routers_own_write() -> None:
    """The listener is on the *production* path, not on a stand-in.

    ``conftest.record_unattributed_events`` hangs a mapper-level
    ``after_insert`` on ``DnsEvent``. That mechanism is invisible to a
    Core ``insert()``, so the day ``router_nic.record_event`` stops
    going through ``session.add`` the sink stays empty, the sweep
    deletes nothing, reports nothing, and #87 comes back silently. This
    is the guard that goes red on that day, and it is the reason the
    helper above calls the router rather than constructing a row.

    ``len(ids) == 1`` rather than ``ids`` — an equality, because "the
    sink is non-empty" is also true of a listener that fires twice per
    insert, and a double-recorded id would make the sweep's re-count
    lie about its denominator.
    """
    from conftest import purge_unattributed_events, record_unattributed_events

    with record_unattributed_events() as ids:
        row_id = await _write_unattributed_row(_guard_ip())

    assert ids == [row_id], (
        f"the recorder captured {ids!r} for a row written at id {row_id}. An "
        "empty list means the mapper listener is not reached by "
        "router_nic.record_event any more — which is exactly the state in "
        "which the sweep reports a clean nothing while rows accumulate"
    )
    assert await _event_exists(row_id), (
        f"row {row_id} is not in the table, so the delete below cannot "
        "distinguish working from matching nothing"
    )

    await purge_unattributed_events(ids, owner="test_harness_guards.recorder")

    assert not await _event_exists(row_id), (
        f"purge_unattributed_events returned without raising and row {row_id} "
        "is still there — the helper's own re-count is not looking at what it "
        "deleted"
    )


@pytest.mark.asyncio
async def test_the_sweep_refuses_a_delete_that_matched_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleted-zero-because-none and deleted-zero-because-unmatched, apart.

    The whole of #87 is a teardown that ran, matched no rows, and
    reported success. ``purge_unattributed_events`` is supposed to be
    unable to do that — so here the teardown under it is replaced with
    one that does nothing at all, which is the strongest form of "the
    predicate could not match", and the helper is required to notice.

    Three states, asserted as three, because rendering them in one type
    is the defect this repo keeps finding:

    * **nothing recorded** — ``[]`` returns quietly and touches no
      connection;
    * **recorded and removed** — the previous guard;
    * **recorded and still there** — raises, and the message says which
      ids survived.

    The mutation is reverted by ``monkeypatch`` at the end of the test,
    and the row is removed with the real teardown in ``finally`` — a
    guard that leaves its own subject behind would be adding to the
    accumulation it exists to stop.
    """
    import conftest

    # State 1: nothing recorded. Must not raise, and must not pretend to
    # have done work.
    await conftest.purge_unattributed_events([], owner="test_harness_guards.empty")

    row_id = await _write_unattributed_row(_guard_ip())
    try:
        async def _no_op_teardown(*args: object, **kwargs: object) -> None:
            return None

        monkeypatch.setattr(conftest, "purge_tenants", _no_op_teardown)

        # State 3: recorded, and the teardown beneath cannot match it.
        with pytest.raises(AssertionError) as caught:
            await conftest.purge_unattributed_events(
                [row_id], owner="test_harness_guards.broken"
            )
        message = str(caught.value)
        assert str(row_id) in message, (
            "the failure does not name the surviving id, and a guard that "
            f"fails uninformatively gets deleted rather than fixed: {message}"
        )
        assert "did not match" in message, (
            "the failure does not say which of the two zeroes this is: "
            f"{message}"
        )
    finally:
        monkeypatch.undo()
        await conftest.purge_tenants(
            (), event_ids=[row_id], owner="test_harness_guards.cleanup"
        )
    assert not await _event_exists(row_id)


def test_the_unattributed_sweep_is_installed_in_this_process() -> None:
    """The autouse fixture ran — not merely that it is written down.

    ``conftest._sweep_unattributed_events`` is session-scoped and
    autouse, and the failure that costs nothing to make is for it to
    stop being collected — a rename, a move, a ``conftest.py`` that
    pytest stops treating as one. Every other guard in this section
    opens its own recorder and would keep passing through that; the
    suite would go back to leaking 17 rows a run with a green board.

    So this asks the *process*: is the listener attached to the mapper
    right now, and is there an open sink for it to write into. Reachable
    in the AST and unreachable in the process is a distinction this repo
    has a name for.
    """
    import sqlalchemy as sa
    from atrium_ddns.models import DnsEvent

    import conftest

    assert sa.event.contains(
        DnsEvent, "after_insert", conftest._collect_unattributed
    ), (
        "the after_insert listener is not attached to DnsEvent in this "
        "process, so nothing is recording the rows no email can name. The "
        "usual cause is that the session-scoped autouse fixture "
        "_sweep_unattributed_events is no longer being collected"
    )
    assert conftest._UNATTRIBUTED_SINKS, (
        "the listener is attached but no sink is open, so every id it "
        "records is discarded — a recorder that cannot fail to look clean"
    )
