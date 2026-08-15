"""One test per case in `protocol_cases.yaml`, named after the case.

    pytest tests/compat --target legacy|host --base-url <url>

Both options are required and neither has a default; README.md carries worked
invocations, and deliberately carries them there rather than here, because a
runner with a URL in it is a runner one edit away from a URL it recognises.

A failure prints the request, the expected body and the actual body **verbatim
and in full**, plus both as `bytes` reprs. Not a diff summary: the differences
this table exists to catch are a trailing newline, a `\\r`, a charset spelling
and a normalised IPv6 address, and every one of them is invisible in a diff
rendered as text.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from conftest import (
    TARGETS,
    CompatClient,
    CompatRequest,
    CompatResponse,
    all_cases,
    build_request,
    deleted_case_ids,
    load_table,
    select_for_target,
    unmet_preconditions,
)


# ---------------------------------------------------------------------------
# The table, one test per case
# ---------------------------------------------------------------------------


def test_case(case: Mapping[str, Any], compat_client: CompatClient) -> None:
    unmet = unmet_preconditions(case)
    if unmet:
        pytest.skip("; ".join(unmet))

    request = build_request(case)
    response = compat_client.send(request)

    mismatches = _compare(case["expect"], response)
    if mismatches:
        pytest.fail(
            _report(case, request, response, mismatches, compat_client.base_url),
            pytrace=False,
        )


def _compare(expect: Mapping[str, Any], response: CompatResponse) -> list[str]:
    """Every mismatching field, not just the first."""
    mismatches: list[str] = []

    if "status" in expect and response.status != expect["status"]:
        mismatches.append(f"status: expected {expect['status']}, got {response.status}")

    if "content_type" in expect and response.content_type != expect["content_type"]:
        # Exact comparison, no normalisation. A whitespace or charset
        # difference is a finding about the service, not noise to absorb.
        mismatches.append(
            f"content_type: expected {expect['content_type']!r}, got {response.content_type!r}"
        )

    if "line_count" in expect:
        actual_lines = _line_count(response.body)
        if actual_lines != expect["line_count"]:
            mismatches.append(
                f"line_count: expected {expect['line_count']}, got {actual_lines}"
            )

    if "body_ends_with_newline" in expect:
        actual_trailing = response.body.endswith(b"\n")
        if actual_trailing != expect["body_ends_with_newline"]:
            mismatches.append(
                f"body_ends_with_newline: expected {expect['body_ends_with_newline']}, "
                f"got {actual_trailing}"
            )

    if "body" in expect:
        expected_body = expect["body"].encode("utf-8")
        if response.body != expected_body:
            mismatches.append(
                f"body: {len(expected_body)} expected bytes != {len(response.body)} actual bytes"
            )

    return mismatches


def _line_count(body: bytes) -> int:
    if not body:
        return 0
    return body.count(b"\n") + 1


def _report(
    case: Mapping[str, Any],
    request: CompatRequest,
    response: CompatResponse,
    mismatches: list[str],
    base_url: str,
) -> str:
    expected_body = case["expect"].get("body", "").encode("utf-8")
    block = [
        f"compat case: {case['id']}",
        f"targets:     {case['targets']}",
        "",
        "request",
        "-------",
        request.describe(base_url),
        "",
        "mismatches",
        "----------",
        *(f"  {m}" for m in mismatches),
        "",
        f"expected body ({len(expected_body)} bytes), verbatim",
        "-------------------------------------------",
        expected_body.decode("utf-8", errors="replace"),
        "-------------------------------------------",
        f"actual body ({len(response.body)} bytes), verbatim",
        "-------------------------------------------",
        response.body.decode("utf-8", errors="replace"),
        "-------------------------------------------",
        "",
        "expected bytes: " + repr(expected_body),
        "actual bytes:   " + repr(response.body),
        "",
        "response headers",
        "----------------",
        *(f"  {name}: {value}" for name, value in response.headers),
    ]

    if case.get("note"):
        block += ["", "case note", "---------", str(case["note"]).strip()]

    legacy_behaviour = case.get("legacy_behaviour")
    if legacy_behaviour is not None:
        block += ["", "documented legacy behaviour", "---------------------------", str(legacy_behaviour).strip()]
        if response.body.decode("utf-8", errors="replace") == str(legacy_behaviour).strip():
            # Derived from the response, never from the URL. This is the one
            # signal that catches "--target host pointed at the legacy
            # service" — the mistake that a localhost URL over an ssh tunnel
            # makes easy and that no amount of URL inspection can detect.
            block += [
                "",
                "!! the actual body is exactly this case's documented `legacy_behaviour`.",
                "!! That is what the LEGACY service answers here. If you passed",
                f"!! --target host, check that {base_url} is not the legacy service.",
            ]

    return "\n".join(block)


# ---------------------------------------------------------------------------
# Guards on the runner's own arithmetic.
#
# These need no service. They exist because the failure mode of a table-driven
# runner is not a wrong answer, it is a case that quietly stopped running.
# ---------------------------------------------------------------------------


def test_every_case_declares_known_targets() -> None:
    unknown = {
        case["id"]: [t for t in case["targets"] if t not in TARGETS]
        for case in all_cases()
        if any(t not in TARGETS for t in case["targets"])
    }
    assert unknown == {}, (
        f"cases naming a target this runner does not know: {unknown}. "
        f"Known targets: {list(TARGETS)}. A typo here removes the case from "
        "every run without removing it from the table."
    )


@pytest.mark.parametrize("target", TARGETS)
def test_target_selection_partitions_the_table(target: str) -> None:
    selected, excluded = select_for_target(target)
    total = len(all_cases())
    assert len(selected) + len(excluded) == total, (
        f"target={target}: {len(selected)} selected + {len(excluded)} excluded "
        f"!= {total} cases in the table"
    )
    assert not (set(c["id"] for c in selected) & set(c["id"] for c in excluded))


def test_deleted_cases_are_never_executed() -> None:
    """`deleted_cases` is not a case list; it carries no `expect`."""
    deleted = set(deleted_case_ids())
    assert deleted, "deleted_cases is empty — the audit trail for the removal is gone"

    executed_ids = {c["id"] for c in all_cases()}
    assert deleted & executed_ids == set(), (
        f"ids present in both `cases` and `deleted_cases`: {sorted(deleted & executed_ids)}"
    )

    raw_deleted = [
        entry["id"] for entry in load_table()["deleted_cases"] if "expect" in entry
    ]
    assert raw_deleted == [], (
        f"deleted_cases entries carrying an `expect` block: {raw_deleted}. "
        "A deleted case that grows an expectation is a case that came back "
        "without a design change to §1."
    )


def test_no_url_shape_inference() -> None:
    """The runner must not contain a loopback literal — anywhere.

    `scripts/smoke.sh` decided "this is the local stack" from the shape of a
    URL, and an ssh tunnel produces a loopback URL for a remote host: it read
    the wrong stack and reported green. The rule that follows is not "do not
    branch on the URL", it is "have nothing to branch on". Comments included:
    a literal in a comment today is a literal in an `if` next year.

    Reintroduce `if "localhost" in base_url:` and this fails.
    """
    import conftest

    source = Path(conftest.__file__).read_text(encoding="utf-8")
    found = {
        literal: [
            n for n, line in enumerate(source.splitlines(), 1) if literal in line
        ]
        for literal in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal")
    }
    found = {k: v for k, v in found.items() if v}
    assert found == {}, (
        f"loopback/host literals in {conftest.__file__}: {found}. The base URL "
        "is used verbatim; nothing in the runner may recognise a URL."
    )


def test_request_building_cannot_depend_on_the_target() -> None:
    """`build_request` takes no target, and it is the only way a request is made.

    Structural rather than behavioural on purpose: a behavioural check ("both
    targets build the same request") passes trivially today and keeps passing
    after someone threads a target through, because it would be written to pass
    the target it is given. This fails the moment the parameter appears.
    """
    params = list(inspect.signature(build_request).parameters)
    assert params == ["case"], (
        f"build_request{tuple(params)} — it must take the case and nothing else. "
        "A target parameter here is a per-target request, which is a "
        "compatibility break the runner would be hiding rather than reporting."
    )
    source = Path(inspect.getsourcefile(build_request)).read_text(encoding="utf-8")
    body = inspect.getsource(build_request)
    for target in TARGETS:
        assert f'"{target}"' not in body and f"'{target}'" not in body, (
            f"build_request mentions the target {target!r}"
        )
    assert source.count("def build_request") == 1


def test_every_case_builds_a_request() -> None:
    """Every case in the table — both targets — turns into a wire request.

    Runs over the whole table rather than the selected subset, so a case that
    only runs on the other target cannot rot unnoticed.
    """
    for case in all_cases():
        request = build_request(case)
        assert request.target_path.startswith(case["path"])
        assert request.method


def test_the_table_is_frozen_at_its_recorded_shape() -> None:
    """The freeze is a guard, not a comment (issue #11, closing V1M1).

    V1M2 is measured against this table, so a change to it has to be a
    deliberate act. `version: 1` was written by #7 against a 101-case table and
    was still reading 1 when the table reached 114 — a version field nothing
    checks is a version field nobody bumps, which is the whole argument for
    this being a test rather than a paragraph.

    Four readings, because a single count is the easiest thing to satisfy by
    accident: the two list lengths, the spec-row count, and a digest over the
    *parsed* data. The digest is what makes a changed expected byte a failure
    while a reflowed comment is not; the counts are what makes the failure
    legible when a whole case is added or removed.

    To change the table: change it, bump `frozen.version`, and put the four new
    readings here in the same PR. The failure message carries the values.
    """
    table = load_table()
    frozen = table.get("frozen")
    assert frozen, (
        "protocol_cases.yaml has no `frozen:` block. It was frozen by issue "
        "#11 to close V1M1; removing the block removes the only thing that "
        "makes a change to the table visible."
    )

    assert table["version"] == frozen["version"], (
        f"top-level `version: {table['version']}` disagrees with "
        f"`frozen.version: {frozen['version']}`. They are the same number "
        "written twice on purpose — bump both."
    )

    actual = {
        "cases": len(table["cases"]),
        "deleted_cases": len(table["deleted_cases"]),
        "spec_rows": len(table["spec_rows"]),
        "content_digest": _content_digest(table),
    }
    recorded = {key: frozen.get(key) for key in actual}

    drifted = {k: (recorded[k], actual[k]) for k in actual if recorded[k] != actual[k]}
    assert drifted == {}, (
        "the table has changed since it was frozen. Recorded vs actual:\n"
        + "\n".join(f"  {k}: recorded {r!r}, actual {a!r}" for k, (r, a) in drifted.items())
        + f"\n\nIf the change is intended: bump `frozen.version` (now "
        f"{frozen['version']}), write the actual values above into the "
        "`frozen:` block, and re-run the table against a throwaway legacy "
        "instance — baseline.md is where that reading goes. If it is not "
        "intended, this is the regression the freeze exists to catch."
    )


def test_the_version_advances_exactly_when_the_data_does() -> None:
    """The four readings above cannot see an un-bumped version. This can.

    Issue #29 mutated an expected byte, recomputed all four readings, left
    `version` alone — and the guard above passed, because "these numbers
    describe this table" stays true however often the table changes. That is
    the same defect one level up from the one #11 wrote the freeze for: a
    version field nothing checks is a version field nobody bumps, and updating
    the readings is exactly what an agent changing the table will do.

    `frozen.previous` is the preceding freeze, recorded as data rather than as
    the prose comment it was first written as — prose cannot be read by a test,
    which is why the first version of this record did not close the hole. Two
    directions, because either one alone can be satisfied by accident:

    * the digest moved, so the version must have advanced;
    * the version advanced, so the digest must have moved — a bump over an
      unchanged table is a freeze that claims a review nobody did.

    **What this does not catch, stated because it was measured and not
    assumed.** `previous` is the last recorded *freeze*, not the file's last
    *state*, so a change made at the current version — mutate a byte, recompute
    all four readings, leave `version` alone — still passes. #29 ran that
    mutation and reported it surviving rather than describing this guard as
    closing the hole. The evasion is available exactly once: the next version
    bump copies the current block into `previous`, and a digest that moved
    without a bump then fails here. The freeze is enforced at the boundary
    between versions; between edits within one version, the enforcement is the
    PR review, which is why changing this file is a PR.
    """
    table = load_table()
    frozen = table["frozen"]
    previous = frozen.get("previous")
    assert previous, (
        "`frozen.previous` is missing. It records the freeze this one "
        "supersedes, and without it nothing can tell a version bump from a "
        "version left alone — see this test's docstring for the mutation that "
        "survived when it was absent."
    )

    assert previous["content_digest"] != frozen["content_digest"], (
        f"`frozen.version: {frozen['version']}` claims a new freeze, but its "
        f"content_digest is identical to version {previous['version']}'s. The "
        "data did not move, so there is nothing to re-freeze: either the "
        "change was lost, or the bump was reflexive."
    )
    assert frozen["version"] == previous["version"] + 1, (
        f"`frozen.version` is {frozen['version']} and "
        f"`frozen.previous.version` is {previous['version']}; the digest has "
        f"moved, so this must be {previous['version'] + 1}. When re-freezing, "
        "copy the whole outgoing `frozen:` block into `frozen.previous` and "
        "then bump — the previous reading is what makes the bump checkable "
        "instead of asserted."
    )


def _content_digest(table: Mapping[str, Any]) -> str:
    """sha256 over the parsed `cases` and `deleted_cases`, canonicalised.

    Over the *parsed* data rather than the file's bytes, so reflowing a comment
    or a block scalar does not trip it and changing one expected byte does. A
    byte digest of the file would fail on every prose edit and would therefore
    be routinely regenerated without anyone reading what changed, which is a
    guard that has been trained not to bite.
    """
    payload = json.dumps(
        [table["cases"], table["deleted_cases"]],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
