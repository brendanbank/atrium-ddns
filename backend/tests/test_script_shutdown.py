"""#129 — a one-shot script must not end a successful run in a traceback.

``app.db`` caches a **process-global** engine, so any script shaped

    asyncio.run(coro())          # coro() opened a session

leaves a pooled ``aiomysql`` connection alive when ``asyncio.run``
closes the loop. The connection's ``__del__`` runs later, during
garbage collection, and calls ``loop.call_soon`` on a loop that is
gone::

    Exception ignored in: <function Connection.__del__ at 0x...>
    ...
    RuntimeError: Event loop is closed

Python swallows anything raised in a finaliser, so **the exit code
stays 0**. The step reports success, prints a traceback, and nothing
downstream notices — which is the whole cost: it trains a reader to
scroll past a traceback in the one place where a real failure looks
identical.

Two instruments, deliberately different in shape
------------------------------------------------
:func:`test_every_one_shot_script_disposes_the_engine_in_a_finally`
reads the **source** of every module under ``atrium_ddns.scripts``,
derived from the package directory rather than listed here. It is the
sweep: a script added tomorrow without the ``finally`` fails it without
anyone remembering this file exists.

:func:`test_a_one_shot_script_exits_with_clean_stderr` runs one for
real and reads its **stderr**. A source read cannot see a traceback
that garbage collection produces after ``main`` returns; a subprocess
cannot see the four modules it did not run. Neither is redundant with
the other, and the static one alone would have been the
probe-that-cannot-fail this repo keeps cataloguing.
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

import atrium_ddns.scripts

SCRIPTS_DIR = pathlib.Path(atrium_ddns.scripts.__file__).resolve().parent

#: The read-only subject of the live half. ``--show`` writes nothing:
#: ``app_settings`` is a singleton row per compose project and
#: ``conftest`` owns every write to it (``test_harness_guards.py``'s
#: #117 census), so seeding here would make this file the second writer
#: of a row with exactly one owner — and it would do it through a
#: subprocess, where that census cannot see it.
LIVE_SUBJECT = "atrium_ddns.scripts.seed_host_bundle"

#: What a finaliser traceback looks like on stderr. Three independent
#: strings rather than one, because the same fault has worn different
#: wording across library versions and a guard keyed on a single
#: message drifts silently.
TRACEBACK_MARKERS = (
    "Traceback (most recent call last)",
    "Exception ignored in",
    "Event loop is closed",
)


def _script_modules() -> list[pathlib.Path]:
    """Every one-shot module, derived from the package directory.

    Not a literal list. A hardcoded roster is the same defect one
    release later: the script this issue was opened about would have
    been on it, and the sixth script nobody adds to it would not.
    """
    return sorted(p for p in SCRIPTS_DIR.glob("*.py") if p.name != "__init__.py")


def _is_engine_dispose(node: ast.AST) -> bool:
    """``get_engine().dispose()`` — the shape that closes the pool."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "dispose"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "get_engine"
    )


def _dispose_helpers(tree: ast.Module) -> set[str]:
    """Module-level functions that are themselves a dispose.

    ``seed_brand`` and ``seed_host_bundle`` both route through a
    ``_dispose()`` helper so the reason can be written down once. A
    matcher that only knew the direct spelling would report those two
    as defective, which is worse than reporting nothing.
    """
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and any(_is_engine_dispose(inner) for inner in ast.walk(node))
    }


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def _asyncio_run_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
    ]


def shutdown_defects(source: str, filename: str) -> list[str]:
    """``[]`` if every ``asyncio.run`` entrypoint here closes the pool.

    An entrypoint is a module that calls ``asyncio.run``. The pool must
    be closed from a ``finally`` — not merely somewhere in the file —
    because a dispose on the happy path alone leaves the traceback on
    exactly the runs where a reader most needs clean output.

    Parsed, not grepped, so the paragraphs in this package that
    *describe* ``get_engine().dispose()`` are not counted as calls to
    it. Takes source rather than a path so the matcher can be pointed
    at a synthetic snippet — which is how it is shown to bite.

    Returns strings naming the module and the line, so a failure says
    which file rather than "no match found".
    """
    tree = ast.parse(source, filename=filename)
    runs = _asyncio_run_calls(tree)
    if not runs:
        return []

    helpers = _dispose_helpers(tree)
    in_finally = [
        inner
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        for stmt in node.finalbody
        for inner in ast.walk(stmt)
        if _is_engine_dispose(inner)
        or (isinstance(inner, ast.Call) and _called_name(inner) in helpers)
    ]
    if in_finally:
        return []
    return [
        f"{filename}: calls asyncio.run at line(s) "
        f"{', '.join(str(n) for n in sorted(r.lineno for r in runs))} but never awaits "
        f"get_engine().dispose() from a finally — a successful run will "
        f"print aiomysql's 'Event loop is closed' traceback and still exit 0"
    ]


# --------------------------------------------------------------------- #
# Instrument 1 — the sweep, over source
# --------------------------------------------------------------------- #


def test_every_one_shot_script_disposes_the_engine_in_a_finally() -> None:
    """The negative result, kept rather than re-derived.

    #129 was opened about ``seed_host_bundle``. The sweep that answered
    it found the other four had the fix already, which is the half a
    "here are the two we found" report loses.
    """
    modules = _script_modules()
    assert modules, (
        f"the census found no modules under {SCRIPTS_DIR} — vacuous, not "
        f"clean. A guard whose only evidence is that it found nothing is "
        f"not a guard"
    )

    sources = {p.name: p.read_text(encoding="utf-8") for p in modules}
    entrypoints = {
        name: shutdown_defects(source, name)
        for name, source in sources.items()
        if _asyncio_run_calls(ast.parse(source, filename=name))
    }
    assert entrypoints, (
        f"none of the {len(modules)} modules under {SCRIPTS_DIR} matched as "
        f"an asyncio.run entrypoint; the matcher is broken, not the tree"
    )

    defects = [line for lines in entrypoints.values() for line in lines]
    assert not defects, (
        f"{len(defects)} of {len(entrypoints)} one-shot scripts end a "
        f"successful run in a finaliser traceback:\n  " + "\n  ".join(defects)
    )


@pytest.mark.parametrize(
    ("label", "source", "expect_defect"),
    [
        (
            "the naive spelling — no dispose at all",
            "import asyncio\n"
            "async def _seed():\n"
            "    pass\n"
            "def main():\n"
            "    asyncio.run(_seed())\n",
            True,
        ),
        (
            "disposed, but only on the happy path",
            "import asyncio\n"
            "from app.db import get_engine\n"
            "async def _seed():\n"
            "    await get_engine().dispose()\n"
            "def main():\n"
            "    asyncio.run(_seed())\n",
            True,
        ),
        (
            "a docstring that only talks about disposing",
            "import asyncio\n"
            "async def _seed():\n"
            '    """Remember to await get_engine().dispose() in a finally."""\n'
            "    try:\n"
            "        pass\n"
            "    finally:\n"
            "        pass\n"
            "def main():\n"
            "    asyncio.run(_seed())\n",
            True,
        ),
        (
            "disposed from a finally, directly",
            "import asyncio\n"
            "from app.db import get_engine\n"
            "async def _seed():\n"
            "    try:\n"
            "        pass\n"
            "    finally:\n"
            "        await get_engine().dispose()\n"
            "def main():\n"
            "    asyncio.run(_seed())\n",
            False,
        ),
        (
            "disposed from a finally, through a helper",
            "import asyncio\n"
            "from app.db import get_engine\n"
            "async def _dispose():\n"
            "    await get_engine().dispose()\n"
            "async def _seed():\n"
            "    try:\n"
            "        pass\n"
            "    finally:\n"
            "        await _dispose()\n"
            "def main():\n"
            "    asyncio.run(_seed())\n",
            False,
        ),
        (
            "not an entrypoint at all — nothing to say",
            "from app.db import get_engine\nasync def helper():\n    pass\n",
            False,
        ),
    ],
)
def test_the_matcher_bites(label: str, source: str, expect_defect: bool) -> None:
    """The six ways this matcher must be able to be wrong.

    Written against snippets rather than by breaking the real tree,
    because a guard demonstrated once by hand is a guard nobody can
    re-demonstrate. Both false-negative shapes (no dispose; dispose on
    the happy path only), the prose-versus-code case a ``grep`` would
    fail, and both true shapes (direct; through a helper) are here — so
    neither "always fires" nor "never fires" passes this table.
    """
    found = shutdown_defects(source, f"<{label}>")
    assert bool(found) is expect_defect, (
        f"{label}: expected defect={expect_defect}, matcher said {found}"
    )


# --------------------------------------------------------------------- #
# Instrument 2 — one script, run for real, read from stderr
# --------------------------------------------------------------------- #


@pytest.mark.functional  # spawns the real script as a subprocess; it gets no test engine
def test_a_one_shot_script_exits_with_clean_stderr() -> None:
    """The half a source read cannot do.

    The traceback is produced by the garbage collector after the
    interpreter has left ``main``. Nothing short of running the process
    and reading its stderr observes it.
    """
    proc = subprocess.run(
        [sys.executable, "-m", LIVE_SUBJECT, "--show"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    # Vacuity first: clean stderr is also what a script that never
    # reached the database prints.
    assert proc.returncode == 0, (
        f"{LIVE_SUBJECT} --show exited {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "host_bundle_url" in proc.stdout, (
        f"{LIVE_SUBJECT} --show printed nothing about the row, so it may not "
        f"have opened a connection at all and clean stderr would prove "
        f"nothing. stdout: {proc.stdout!r}"
    )

    hit = [marker for marker in TRACEBACK_MARKERS if marker in proc.stderr]
    assert not hit, (
        f"{LIVE_SUBJECT} succeeded (exit 0) and still printed {hit} on "
        f"stderr:\n{proc.stderr}"
    )
