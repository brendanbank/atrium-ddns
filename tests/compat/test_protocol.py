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

import inspect
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
