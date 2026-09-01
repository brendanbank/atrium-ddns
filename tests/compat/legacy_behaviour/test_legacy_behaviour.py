"""Guards on the two data files in this directory.

There is no host code to test yet -- `backend/src/atrium_ddns/models.py` is
still the scaffold's demo table -- so these guards do not assert model
behaviour. They assert that the *record* of that behaviour cannot decay
silently, which is the failure mode this issue exists to prevent:

- the dispositions must partition the legacy suite exactly, and the total must
  equal an independently measured count of it;
- every ported test must name a model case that exists, and every model case
  claiming a legacy source must be named by that source;
- a drop reason must come from the closed vocabulary;
- and no model case may assert wire behaviour, which is `../protocol_cases.yaml`'s.

`test_the_legacy_suite_still_collects_147_tests` is the second instrument. It
re-derives the count from the legacy checkout by walking its AST -- a different
shape from both the frozen number and from `pytest --collect-only` -- and skips
with a reason when that checkout is absent rather than passing vacuously.

Run:  python -m pytest tests/compat/legacy_behaviour -q
"""
from __future__ import annotations

import ast
import os
import pathlib
import re

import pytest
import yaml

HERE = pathlib.Path(__file__).parent
CASES_PATH = HERE / "model_cases.yaml"
INVENTORY_PATH = HERE / "legacy_inventory.yaml"
PROTOCOL_PATH = HERE.parent / "protocol_cases.yaml"

LEGACY_ROOT = pathlib.Path(
    os.environ.get("DYNDNS_LEGACY_ROOT", "/Users/brendan/src/dyndns-route53")
)

DROP_REASONS = {"atrium-owns", "flask-internal", "superseded-by-table"}
DISPOSITIONS = {"preserve", "fix", "change"}
CALIBRATIONS = {"agree", "derived", "not-replayable"}

# Every status token the DynDNS wire protocol can carry. A model case whose
# `then` is one of these on its own is asserting the wire, which belongs to
# protocol_cases.yaml.
WIRE_TOKENS = {"good", "nochg", "nohost", "notfqdn", "badauth", "abuse", "911", "dnserr"}


@pytest.fixture(scope="module")
def cases():
    return yaml.safe_load(CASES_PATH.read_text())


@pytest.fixture(scope="module")
def inventory():
    return yaml.safe_load(INVENTORY_PATH.read_text())


@pytest.fixture(scope="module")
def protocol():
    return yaml.safe_load(PROTOCOL_PATH.read_text())


# ---------------------------------------------------------------------------
# The partition
# ---------------------------------------------------------------------------

def test_every_legacy_test_has_exactly_one_disposition(inventory):
    ids = [t["id"] for t in inventory["tests"]]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"listed more than once: {dupes}"
    for t in inventory["tests"]:
        assert t["disposition"] in ("ported", "dropped"), t
        if t["disposition"] == "dropped":
            assert t.get("reason") in DROP_REASONS, (
                f"{t['id']}: reason {t.get('reason')!r} is not one of "
                f"{sorted(DROP_REASONS)}"
            )
            assert "cases" not in t, f"{t['id']} is dropped but names cases"
        else:
            assert t.get("cases"), f"{t['id']} is ported but names no case"
            assert "reason" not in t, f"{t['id']} is ported but carries a drop reason"


def test_ported_plus_dropped_equals_the_declared_total(inventory):
    tests = inventory["tests"]
    ported = [t for t in tests if t["disposition"] == "ported"]
    dropped = [t for t in tests if t["disposition"] == "dropped"]
    by_reason = {r: sum(1 for t in dropped if t["reason"] == r) for r in DROP_REASONS}

    declared = inventory["counts"]
    assert len(ported) == declared["ported"], (
        f"{len(ported)} ported rows vs counts.ported {declared['ported']}"
    )
    assert by_reason == declared["dropped"], f"{by_reason} vs {declared['dropped']}"
    assert len(dropped) == declared["dropped_total"]
    assert len(ported) + len(dropped) == declared["total"] == len(tests)


def test_the_declared_total_equals_the_measured_legacy_suite(inventory):
    m = inventory["legacy"]["measured"]
    assert m["collected"] == m["grepped"], (
        "the two instruments disagree on the size of the legacy suite: "
        f"pytest collected {m['collected']}, grep found {m['grepped']}"
    )
    assert sum(m["by_file"].values()) == m["collected"]
    assert inventory["counts"]["total"] == m["collected"]


def test_every_inventory_id_names_a_declared_file(inventory):
    files = set(inventory["legacy"]["files"])
    for t in inventory["tests"]:
        path = t["id"].split("::", 1)[0]
        assert path in files, f"{t['id']} is not in {sorted(files)}"
    counted = {f: sum(1 for t in inventory["tests"] if t["id"].startswith(f + "::"))
               for f in files}
    assert counted == inventory["legacy"]["measured"]["by_file"], (
        f"rows per file {counted} vs measured {inventory['legacy']['measured']['by_file']}"
    )


# ---------------------------------------------------------------------------
# The second instrument
# ---------------------------------------------------------------------------

def _ast_count(path: pathlib.Path) -> int:
    """Count test functions by walking the module. Different shape from both
    the frozen number and from pytest's collector."""
    tree = ast.parse(path.read_text())
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name.startswith("test"):
            n += 1
    return n


def test_the_legacy_suite_still_collects_the_measured_number(inventory):
    if not LEGACY_ROOT.is_dir():
        pytest.skip(
            f"legacy checkout not present at {LEGACY_ROOT}; set DYNDNS_LEGACY_ROOT. "
            "The frozen count in legacy_inventory.yaml is the only reading here, "
            "and it has NOT been re-derived."
        )
    measured = inventory["legacy"]["measured"]["by_file"]
    derived = {}
    for rel in measured:
        p = LEGACY_ROOT / rel
        assert p.is_file(), f"{p} missing from the legacy checkout"
        derived[rel] = _ast_count(p)
    assert derived == measured, (
        "the legacy suite has changed since this inventory was taken: "
        f"AST walk found {derived}, the file records {measured}. "
        "Reclassify the difference rather than editing the number."
    )


def test_every_inventory_id_exists_in_the_legacy_checkout(inventory):
    if not LEGACY_ROOT.is_dir():
        pytest.skip(f"legacy checkout not present at {LEGACY_ROOT}")
    present: set[str] = set()
    for rel in inventory["legacy"]["measured"]["by_file"]:
        tree = ast.parse((LEGACY_ROOT / rel).read_text())
        for node in tree.body:
            # pytest's own rule: Test* classes, test* functions. Helpers such as
            # `_login_admin` and `_basic_auth_header` are not tests, and counting
            # them would inflate the denominator this whole file is measured against.
            if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and sub.name.startswith("test"):
                        present.add(f"{rel}::{node.name}::{sub.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name.startswith("test"):
                present.add(f"{rel}::{node.name}")
    listed = {t["id"] for t in inventory["tests"]}
    assert not (listed - present), f"named but gone: {sorted(listed - present)}"
    assert not (present - listed), f"exists but unclassified: {sorted(present - listed)}"


# ---------------------------------------------------------------------------
# Cross-references
# ---------------------------------------------------------------------------

def test_every_case_is_well_formed(cases):
    ids = [c["id"] for c in cases["cases"]]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate case ids: {dupes}"
    for c in cases["cases"]:
        assert c["subject"] in cases["subjects"], c["id"]
        assert c["disposition"] in DISPOSITIONS, c["id"]
        assert c["calibrated"] in CALIBRATIONS, c["id"]
        for field in ("given", "then", "evidence"):
            assert c.get(field), f"{c['id']} has no {field}"
        assert isinstance(c.get("source"), list), f"{c['id']} has no source list"
        if c["disposition"] in ("fix", "change"):
            assert c.get("legacy") and c.get("host"), (
                f"{c['id']} is a {c['disposition']} case and must record both "
                "the legacy behaviour and what replaces it"
            )
        else:
            assert "legacy" not in c and "host" not in c, (
                f"{c['id']} is `preserve` but carries legacy/host"
            )


def test_ported_tests_name_cases_that_exist(cases, inventory):
    known = {c["id"] for c in cases["cases"]}
    for t in inventory["tests"]:
        if t["disposition"] != "ported":
            continue
        unknown = set(t["cases"]) - known
        assert not unknown, f"{t['id']} names cases that do not exist: {sorted(unknown)}"


def test_case_sources_agree_with_the_inventory(cases, inventory):
    """Bidirectional. A case citing a legacy test, and that test not citing the
    case back, is the two files having drifted apart."""
    by_test = {t["id"]: set(t.get("cases", [])) for t in inventory["tests"]}
    problems = []
    for c in cases["cases"]:
        for src in c["source"]:
            if src not in by_test:
                problems.append(f"{c['id']} cites {src}, which is not in the inventory")
            elif c["id"] not in by_test[src]:
                problems.append(
                    f"{c['id']} cites {src}, but that test's inventory row does not "
                    f"name it (it names {sorted(by_test[src]) or 'nothing'})"
                )
    assert not problems, "\n".join(problems)


def test_every_ported_test_is_cited_back_by_at_least_one_of_its_cases(cases, inventory):
    """The other direction of the same edge. Without this, a case could quietly
    drop its `source` list and the inventory would still read as if the test
    had been ported -- the port becoming a claim rather than a link.

    A ported test may additionally name cases that do *not* cite it back (a
    boundary case its own probe never reached, say), which is why this asks for
    at least one rather than all.
    """
    by_case = {c["id"]: set(c["source"]) for c in cases["cases"]}
    orphans = [
        t["id"] for t in inventory["tests"]
        if t["disposition"] == "ported"
        and not any(t["id"] in by_case.get(c, set()) for c in t["cases"])
    ]
    assert not orphans, (
        "ported, but no case they name lists them as a source:\n  "
        + "\n  ".join(orphans)
    )


def test_no_ported_case_is_orphaned(cases, inventory):
    cited = {c for t in inventory["tests"] for c in t.get("cases", [])}
    known = {c["id"] for c in cases["cases"]}
    assert not (cited - known)
    # The reverse is allowed and expected: cases found by reading the code carry
    # `source: []`. Assert that those and only those are the uncited ones, so a
    # case that lost its source silently shows up here.
    uncited = known - cited
    by_id = {c["id"]: c for c in cases["cases"]}
    wrong = sorted(i for i in uncited if by_id[i]["source"])
    assert not wrong, (
        f"these cases claim a legacy source but no inventory row ports to them: {wrong}"
    )


# ---------------------------------------------------------------------------
# The boundary with the wire table
# ---------------------------------------------------------------------------

def test_no_model_case_id_collides_with_a_protocol_case_id(cases, protocol):
    ours = {c["id"] for c in cases["cases"]}
    theirs = {c["id"] for c in protocol["cases"]}
    theirs |= {c["id"] for c in protocol["deleted_cases"]}
    assert not (ours & theirs), f"id collision with the wire table: {sorted(ours & theirs)}"


def test_no_model_case_asserts_the_wire(cases):
    """`then` is what the host must satisfy. If it reduces to a DynDNS status
    token, or names a response body or an HTTP status, the case is a second copy
    of protocol_cases.yaml -- which is the specific thing this file must not be."""
    problems = []
    for c in cases["cases"]:
        then = c["then"]
        # a bare status token, or "the response is `good`" style
        for tok in WIRE_TOKENS:
            if re.search(rf"\bthe (response|body|reply) is [`'\"]?{tok}\b", then):
                problems.append(f"{c['id']}: `then` asserts the wire status {tok!r}")
        if re.search(r"\bHTTP \d{3}\b|\bstatus_code\b|\bresponse body\b", then):
            problems.append(f"{c['id']}: `then` asserts an HTTP-level fact")
    assert not problems, "\n".join(problems)


def test_no_model_case_carries_a_wire_expectation_block(cases):
    """Schema-level: the fields protocol_cases.yaml uses to assert bytes are
    forbidden here, so the two files cannot converge by accident."""
    forbidden = {"expect", "query", "path", "auth", "client_ip", "headers", "targets"}
    for c in cases["cases"]:
        overlap = forbidden & set(c)
        assert not overlap, f"{c['id']} carries wire-table fields: {sorted(overlap)}"


def test_superseded_drops_are_backed_by_a_wire_table_that_covers_them(inventory, protocol):
    """`superseded-by-table` is only honest if the table is non-empty and covers
    the three endpoints those tests exercised. A drop reason pointing at an
    empty table would be the strongest possible silent drop."""
    n = len(protocol["cases"])
    assert n > 0, "the wire table is empty; superseded-by-table means nothing"
    paths = {c["path"] for c in protocol["cases"]}
    assert paths == {"/nic/update", "/nic/delete", "/nic/checkip"}, paths
    superseded = [t for t in inventory["tests"]
                  if t.get("reason") == "superseded-by-table"]
    assert superseded, "no test was dropped as superseded; check the classification"


def test_wire_half_references_resolve(inventory, protocol):
    known = {c["id"] for c in protocol["cases"]}
    for t in inventory["tests"]:
        wh = t.get("wire_half")
        if wh is not None:
            assert wh in known, f"{t['id']} names wire_half {wh!r}, absent from the table"


# ---------------------------------------------------------------------------
# Coverage of the subjects the issue named
# ---------------------------------------------------------------------------

def test_every_subject_the_issue_named_has_cases(cases):
    required = {"ttl", "backends", "rate_limit", "event_log"}
    have = {c["subject"] for c in cases["cases"]}
    assert required <= have, f"missing: {sorted(required - have)}"
    per = {s: sum(1 for c in cases["cases"] if c["subject"] == s) for s in have}
    thin = sorted(s for s in required if per[s] < 3)
    assert not thin, f"subjects with fewer than three cases: {thin} (counts {per})"


def test_the_family_sensitive_subjects_do_not_treat_ipv4_as_the_only_case(cases):
    """Production is IPv6-first: **305 IPv6 against 143 IPv4** across 448 events
    (plan §3.3.1, measured 2026-08-15 on `events.ip_address`). Every subject
    whose behaviour differs by address family must therefore say so somewhere,
    or it has been written against the less common path.

    The figure this docstring used to cite — *10 of 11 hostnames track a v6
    address* — was **retracted**, and the retraction is the more useful half.
    It was read from `hostnames.last_ip_v6`, which `dyndns.py` seeds from
    `dns.resolver.resolve()` at boot for any hostname with no tracked IP. So it
    counted hostnames with an AAAA record *in the zone*, including ones no
    client has ever touched — a different population from the one the claim was
    about, rendered in the same type. `events.ip_address` is the traffic.

    The conclusion is unchanged and slightly stronger, which is why this is a
    citation fix rather than a test change. It is fixed at all because a
    retracted number in a docstring is worse than one in prose: a docstring
    reads as the reason the test exists, and this one names a plan section as
    its authority — so the next reader to check finds the plan and the
    docstring disagreeing, and has to work out which is current."""
    family_sensitive = {"provider", "health_check", "ip_tracking", "event_log"}
    v6 = re.compile(r"\bAAAA\b|\bIPv6\b|\bv6\b|2001:", re.I)
    silent = []
    for subject in sorted(family_sensitive):
        texts = [
            " ".join(str(c.get(f, "")) for f in
                     ("given", "then", "evidence", "note", "legacy", "host"))
            for c in cases["cases"] if c["subject"] == subject
        ]
        assert texts, f"subject {subject!r} has no cases at all"
        hits = sum(1 for t in texts if v6.search(t))
        if hits == 0:
            silent.append(f"{subject}: 0 of {len(texts)} cases mention v6")
    assert not silent, (
        "family-sensitive subjects that never mention IPv6:\n  " + "\n  ".join(silent)
    )
