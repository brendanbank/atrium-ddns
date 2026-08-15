"""The runner's own command-line contract, asserted rather than asserted-about.

`--target` is required and has no default. That requirement used to be enforced
by raising from `pytest_configure`, which fires whenever `conftest.py` is
loaded — so it also refused `pytest tests/`, `pytest tests/compat/legacy_behaviour`
and the service-free guards in `test_protocol.py`, none of which speak to a
service at all. #23 narrowed the blast radius to collection of the wire cases.

Narrowing a rule is exactly where a rule quietly stops existing, so this file
runs pytest **as a subprocess** and reads its exit code and its output. That is
a second instrument: `conftest.py` asserting its own behaviour in-process shares
an author with the behaviour, and cannot see an exit code at all.

Four readings, and together they say *no wire case can execute without an
explicit `--target`*:

===========================================  ==========================
invocation                                   must
===========================================  ==========================
`--base-url` with no `--target`              refuse, exit 4
`--target` with no `--base-url`              refuse, exit 4
neither                                      collect, run the guards, run
                                             **zero** cases, and say so
both, `--collect-only`                       collect exactly the cases the
                                             table selects for that target
===========================================  ==========================

The expected counts are derived from the table through `select_for_target`,
never written down here: a hardcoded 98 is a number that keeps passing after
the table changes underneath it.

Only `test_protocol.py` is handed to the child, never this directory — a child
that collected this file would run these tests again, and again.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import NO_TARGET_SKIP_REASON, TARGETS, all_cases, select_for_target

HERE = Path(__file__).parent
WIRE_SUITE = HERE / "test_protocol.py"
TESTS_ROOT = HERE.parent

#: Never connected to. `.invalid` is reserved by RFC 2606 and resolves nowhere,
#: so a bug that let one of the refusing invocations through would fail on the
#: connection rather than passing quietly against something real.
UNREACHABLE_BASE_URL = "http://compat-runner-contract.invalid:1"

#: pytest's exit code for a usage error (`ExitCode.USAGE_ERROR`).
USAGE_ERROR = 4


def _run(*args: str, target: Path = WIRE_SUITE) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(target),
            # The image installs the package read-only, and a child writing a
            # cache into it warns on every run. It has nothing to cache anyway.
            "-p",
            "no:cacheprovider",
            *args,
        ],
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _output(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


def _fail(proc: subprocess.CompletedProcess[str], what: str) -> str:
    return f"{what}\nexit={proc.returncode}\n--- child output ---\n{_output(proc)}"


# ---------------------------------------------------------------------------
# It still refuses
# ---------------------------------------------------------------------------


def test_a_base_url_without_a_target_is_refused() -> None:
    """The mistake `--target` exists to prevent: a service, unnamed.

    This is the one invocation that still fails at `pytest_configure`, and it
    should: nothing was swept up here, someone pointed the runner at a URL
    without saying what is behind it.
    """
    proc = _run("--base-url", UNREACHABLE_BASE_URL, "--collect-only")
    assert proc.returncode == USAGE_ERROR, _fail(
        proc, "--base-url with no --target was accepted"
    )
    out = _output(proc)
    assert "--target" in out and "no default" in out, _fail(
        proc, "the refusal does not name --target or say it has no default"
    )


def test_a_target_without_a_base_url_is_refused() -> None:
    """Unchanged by #23, and asserted so it stays that way."""
    for target in TARGETS:
        proc = _run("--target", target)
        assert proc.returncode == USAGE_ERROR, _fail(
            proc, f"--target {target} with no --base-url was accepted"
        )
        assert "--base-url is required" in _output(proc), _fail(
            proc, "the refusal does not name --base-url"
        )


def test_no_wire_case_executes_without_a_target() -> None:
    """The narrowing, measured: the suite runs, and the table does not.

    Exit 0 — the guards are allowed to run — but zero cases executed, reported
    as *not run* rather than as nought failures. The skip reason is imported
    from `conftest`, so rewording it there cannot leave this assertion passing
    against text nobody prints any more.
    """
    proc = _run("-ra", "-v")
    out = _output(proc)
    # 0 = everything passed, 1 = some test in the child failed. Both mean the
    # session ran; which of the child's own guards it failed is that guard's
    # business, and asserting 0 here made every unrelated red in
    # `test_protocol.py` report twice. 2, 3 and 4 (interrupted, internal error,
    # usage error) are the regression: the suite refused to run at all.
    assert proc.returncode in (0, 1), _fail(
        proc, "the wire suite refused to run without --target instead of skipping"
    )

    # ERROR counts as executed. A `--target` defaulted somewhere makes the
    # cases collect and then die in `compat_client` for want of a base URL,
    # which prints ERROR and not FAILED — and a check watching only for
    # failures would call that a clean session.
    executed = [
        line
        for line in out.splitlines()
        if "::test_case[" in line
        and any(outcome in line for outcome in (" PASSED", " FAILED", " ERROR"))
    ]
    assert executed == [], _fail(proc, f"wire cases executed with no --target: {executed}")

    assert "NO-TARGET-GIVEN-WIRE-TABLE-NOT-RUN" in out, _fail(
        proc, "the skipped case is not named in the output"
    )
    # A distinctive clause of the reason, not the whole paragraph: pytest wraps
    # it across lines in the short summary.
    assert "so NO wire case ran" in NO_TARGET_SKIP_REASON  # vacuity guard
    assert "so NO wire case ran" in out, _fail(
        proc, "the skip reason was not reported (is -ra reaching the summary?)"
    )
    assert "NOT RUN" in out, _fail(proc, "the accounting block does not say NOT RUN")
    assert f"0 of {len(all_cases())} cases executed" in out, _fail(
        proc, "the accounting does not print the nought beside the table size"
    )
    # And the guards did run — otherwise "0 cases executed" is true of a
    # session that collected nothing at all, which is the failure #23 exists to
    # fix wearing this test's clothes.
    passed_guards = [line for line in out.splitlines() if " PASSED" in line]
    assert len(passed_guards) >= 5, _fail(
        proc, f"expected the service-free guards to run; saw {len(passed_guards)} passes"
    )


# ---------------------------------------------------------------------------
# And it still selects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", TARGETS)
def test_a_target_collects_exactly_the_cases_the_table_selects(target: str) -> None:
    """`--target` decides collection — the thing the skip must not have broken.

    The expected number is re-derived from the table on every run. Freezing it
    here would make this test agree with itself rather than with the data.
    """
    expected = len(select_for_target(target)[0])
    proc = _run("--target", target, "--collect-only", "-q")
    assert proc.returncode == 0, _fail(proc, f"--collect-only --target {target} failed")
    out = _output(proc)
    collected = sum(1 for line in out.splitlines() if "::test_case[" in line)
    assert collected == expected, _fail(
        proc, f"target={target}: collected {collected} cases, the table selects {expected}"
    )
    assert "NO-TARGET-GIVEN-WIRE-TABLE-NOT-RUN" not in out, _fail(
        proc, "the no-target placeholder was collected despite a target being given"
    )


# ---------------------------------------------------------------------------
# The other half of #23: the tree collects with no flags at all
# ---------------------------------------------------------------------------


def test_the_whole_tests_tree_collects_with_no_options() -> None:
    """`pytest tests/` — the acceptance criterion, as a test rather than a note.

    `--collect-only`, because a child that *ran* the tree would run this file,
    which would start a grandchild. Collection is the half that was broken:
    `pytest_configure` raised before a single item existed.
    """
    proc = _run("--collect-only", "-q", target=TESTS_ROOT)
    assert proc.returncode == 0, _fail(proc, "pytest <tests root> --collect-only failed")
    out = _output(proc)
    for suite in ("test_protocol.py::", "test_legacy_behaviour.py::", "test_runner_contract.py::"):
        assert suite in out, _fail(proc, f"{suite} was not collected from the tree")
