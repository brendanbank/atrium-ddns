#!/usr/bin/env python3
# VENDORED into this repo on purpose — do not reach for a copy elsewhere.
#
# #47's agent was briefed with a bare `find_ready.py` rather than a path. It
# found a different copy on the same machine — atrium-pa's, which hardcodes
# OWNER = "Brendan-Bank" / REPO = "atrium-pa" — ran it, and posted a claim
# comment on an unrelated repository's issue #47. Deleted within ~90 seconds,
# nothing else mutated, but the cause was a brief that named a filename
# instead of an artefact.
#
# This copy reads .overnight.conf like every other script here, so it cannot
# address the wrong repository. Invoke it as ./scripts/find_ready.py.
"""Ready-set, claim and board helper for an unattended milestone run.

An issue is READY when every issue named in its `Depends on:` line is closed.
Readiness is mirrored into a `blocked` label, which this script maintains — it
is never set by hand.

Configuration is read from `.overnight.conf` (searched upward from the current
directory), overridden by `OVERNIGHT_*` environment variables, overridden by
flags. See `.overnight.conf.example`.

  --list             show ready issues
  --pick             show the single best next issue
  --graph            print the dependency edges and verify acyclicity
  --sync-labels      add/remove `blocked` to match the true dependency state
  --claim N          post a claim comment and move the board item to In Progress
  --verify N         assert the board item is In Progress (claim actually took)
  --release N        edit the claim comment to begin `RELEASED —`, board to Todo
  --done N           move the board item to Done
  --slug S           claim slug, forming the branch `<N>_<slug>` (default: agent)

Every write is idempotent enough to re-run, and every failure is loud. A claim
that comments but does not move the board is an invitation to a collision, so
`--claim` verifies its own board move before reporting success.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

CLAIM_PREFIX = "Claimed for branch"
RELEASED_PREFIX = "RELEASED"
DEPENDS_RE = re.compile(r"^Depends on:\s*(.+)$", re.MULTILINE)
ISSUE_RE = re.compile(r"#(\d+)")


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

DEFAULTS = {
    "OVERNIGHT_REPO": "",  # owner/name — required
    "OVERNIGHT_OWNER": "",  # project owner login; defaults to the repo owner
    "OVERNIGHT_PROJECT": "",  # project (board) number — required for board ops
    "OVERNIGHT_EPIC_LABEL": "epic",  # issues with this label are not work
    "OVERNIGHT_BLOCKED_LABEL": "blocked",
    "OVERNIGHT_TODO_COLUMN": "Todo",
    "OVERNIGHT_ACTIVE_COLUMN": "In Progress",
    "OVERNIGHT_DONE_COLUMN": "Done",
}


def parse_value(raw: str) -> str:
    """Parse one `KEY=` right-hand side the way `bash` reads it when sourcing.

    This file is read twice by every run: the four shell scripts `.` it, and
    this script parses it. Those two readings must agree, so this follows the
    shell's word rules rather than something simpler:

    - `#` starts a comment only at the start of a *word* — the start of the
      value, or after unquoted whitespace. `value#nospace` is literal, so a
      branch name or a command containing `#` survives.
    - `#` inside single or double quotes is literal.
    - Quotes are removed, and whitespace an unquoted comment left behind goes
      with them, so `'In Progress'   # note` is exactly `In Progress`.

    `$VAR` is *not* expanded — the shell would expand it and we would not, so
    keep every value literal. `bootstrap-github.sh --check` compares the two
    readings key by key and complains if they ever diverge.
    """
    raw = raw.lstrip()
    out: list[tuple[str, bool]] = []  # (char, came from quotes/escape)
    i, n = 0, len(raw)
    word_break = True  # a `#` here begins a comment
    while i < n:
        ch = raw[i]
        if ch == "#" and word_break:
            break
        if ch == "\\" and i + 1 < n:
            out.append((raw[i + 1], True))
            i += 2
        elif ch in "'\"":
            quote, i = ch, i + 1
            while i < n and raw[i] != quote:
                if quote == '"' and raw[i] == "\\" and i + 1 < n and raw[i + 1] in '\\"$`':
                    i += 1
                out.append((raw[i], True))
                i += 1
            i += 1  # past the closing quote; an unterminated quote just ends here
        else:
            out.append((ch, False))
            i += 1
        word_break = bool(out) and not out[-1][1] and out[-1][0].isspace()
    while out and not out[-1][1] and out[-1][0].isspace():
        out.pop()  # whitespace a stripped comment left behind
    return "".join(c for c, _ in out)


def parse_config_text(text: str) -> dict[str, str]:
    """Every `KEY=value` in a config file's text, with no defaults and no env."""
    cfg: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        cfg[key] = parse_value(value)
    return cfg


def load_config() -> dict[str, str]:
    """`.overnight.conf` < environment. Missing file is not an error."""
    cfg = dict(DEFAULTS)
    for parent in [Path.cwd(), *Path.cwd().parents]:
        candidate = parent / ".overnight.conf"
        if candidate.is_file():
            cfg.update(parse_config_text(candidate.read_text()))
            break
    for key in list(cfg):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    if not cfg["OVERNIGHT_OWNER"] and "/" in cfg["OVERNIGHT_REPO"]:
        cfg["OVERNIGHT_OWNER"] = cfg["OVERNIGHT_REPO"].split("/", 1)[0]
    return cfg


CFG = load_config()


def need(key: str, why: str) -> str:
    value = CFG.get(key, "")
    if not value:
        raise SystemExit(f"{key} is not set — {why}. Put it in .overnight.conf or the environment.")
    return value


# --------------------------------------------------------------------------
# gh plumbing
# --------------------------------------------------------------------------


def gh(*args: str, check: bool = True) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        if not check:
            return ""
        raise SystemExit(f"gh {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r.stdout


_PROJECT_FIELDS = """
  id
  field(name: "Status") {
    ... on ProjectV2SingleSelectField { id options { id name } }
  }
"""


def project_meta() -> tuple[str, str, dict[str, str]]:
    """Return (project_id, status_field_id, {column_name: option_id}).

    Resolved at call time rather than hardcoded: the option IDs are opaque and
    change if the board's Status column is edited, and a stale constant fails
    as a confusing GraphQL error rather than a clear one.

    Tries the owner as an organization, then as a user — a portable script
    cannot know which, and guessing wrong produces an unhelpful null.
    """
    owner = need("OVERNIGHT_OWNER", "board operations need a project owner")
    number = need("OVERNIGHT_PROJECT", "board operations need a project number")
    for kind in ("organization", "user"):
        query = f"query($owner: String!, $number: Int!) {{ {kind}(login: $owner) {{ projectV2(number: $number) {{ {_PROJECT_FIELDS} }} }} }}"
        out = gh(
            "api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F",
            f"number={number}", check=False,
        )
        if not out:
            continue
        node = (json.loads(out).get("data") or {}).get(kind)
        proj = (node or {}).get("projectV2")
        if proj and proj.get("field"):
            field = proj["field"]
            return proj["id"], field["id"], {o["name"]: o["id"] for o in field["options"]}
    raise SystemExit(
        f"could not read project {number} for owner {owner} as an organization or a user — "
        "check OVERNIGHT_OWNER/OVERNIGHT_PROJECT and that `gh auth status` has project scope"
    )


def board_item_id(issue: int) -> str | None:
    out = gh(
        "project", "item-list", need("OVERNIGHT_PROJECT", "board lookup"),
        "--owner", need("OVERNIGHT_OWNER", "board lookup"),
        "--format", "json", "--limit", "500",
    )
    for item in json.loads(out)["items"]:
        if (item.get("content") or {}).get("number") == issue:
            return item["id"]
    return None


def board_column(issue: int) -> str | None:
    out = gh(
        "project", "item-list", need("OVERNIGHT_PROJECT", "board lookup"),
        "--owner", need("OVERNIGHT_OWNER", "board lookup"),
        "--format", "json", "--limit", "500",
    )
    for item in json.loads(out)["items"]:
        if (item.get("content") or {}).get("number") == issue:
            return item.get("status")
    return None


def set_status(issue: int, column: str) -> None:
    item = board_item_id(issue)
    if item is None:
        raise SystemExit(f"#{issue} is not on the board — add it before moving it")
    project_id, field_id, options = project_meta()
    if column not in options:
        raise SystemExit(f"board has no '{column}' column — found {sorted(options)}")
    gh(
        "project", "item-edit", "--project-id", project_id, "--id", item,
        "--field-id", field_id, "--single-select-option-id", options[column],
    )


# --------------------------------------------------------------------------
# dependency graph
# --------------------------------------------------------------------------


def issues() -> list[dict]:
    out = gh(
        "issue", "list", "--repo", need("OVERNIGHT_REPO", "issue queries need a repo"),
        "--state", "all", "--limit", "500",
        "--json", "number,title,body,state,labels,milestone",
    )
    return json.loads(out)


def deps_of(body: str | None) -> set[int]:
    if not body:
        return set()
    m = DEPENDS_RE.search(body)
    if not m or "none" in m.group(1).lower():
        return set()
    return {int(n) for n in ISSUE_RE.findall(m.group(1))}


def build() -> tuple[dict[int, dict], dict[int, set[int]]]:
    by_num, edges = {}, {}
    for i in issues():
        by_num[i["number"]] = i
        edges[i["number"]] = deps_of(i.get("body"))
    # drop references to issues that do not exist rather than crashing
    for n, d in edges.items():
        edges[n] = {x for x in d if x in by_num}
    return by_num, edges


def check_acyclic(edges: dict[int, set[int]]) -> None:
    seen: set[int] = set()
    stack: set[int] = set()

    def dfs(n: int) -> None:
        if n in stack:
            raise SystemExit(f"CYCLE detected involving #{n}")
        if n in seen:
            return
        stack.add(n)
        for m in edges.get(n, ()):
            dfs(m)
        stack.discard(n)
        seen.add(n)

    for n in edges:
        dfs(n)


def ready(by_num: dict[int, dict], edges: dict[int, set[int]]) -> list[int]:
    epic = CFG["OVERNIGHT_EPIC_LABEL"]
    out = []
    for n, i in by_num.items():
        if i["state"] != "OPEN":
            continue
        if any(lb["name"] == epic for lb in i["labels"]):  # epics are not work
            continue
        if all(by_num[d]["state"] == "CLOSED" for d in edges[n]):
            out.append(n)
    return sorted(out)


# --------------------------------------------------------------------------
# claims
# --------------------------------------------------------------------------


def claim_comments(issue: int) -> list[dict]:
    """Comments that are themselves claims, oldest first.

    Only a comment that *is* the claim counts. A separate comment saying a
    claim looks stale is not a release, however confidently it is worded —
    that rule exists because it was broken, and two agents implemented the
    same issue twice.
    """
    repo = need("OVERNIGHT_REPO", "claim lookup")
    out = gh("api", f"repos/{repo}/issues/{issue}/comments", "--paginate")
    found = []
    for c in json.loads(out) if out.strip().startswith("[") else []:
        body = (c.get("body") or "").lstrip()
        if body.startswith(CLAIM_PREFIX) or body.startswith(RELEASED_PREFIX):
            found.append(c)
    return found


def live_claim(issue: int) -> dict | None:
    for c in claim_comments(issue):
        if (c.get("body") or "").lstrip().startswith(CLAIM_PREFIX):
            return c
    return None


def do_claim(issue: int, slug: str, r: list[int], edges, by_num) -> None:
    if issue not in r:
        raise SystemExit(
            f"#{issue} is not ready — open deps: "
            f"{sorted(d for d in edges[issue] if by_num[d]['state'] == 'OPEN')}"
        )
    existing = live_claim(issue)
    if existing:
        raise SystemExit(
            f"#{issue} is already claimed — stopping rather than racing.\n"
            f"  {existing['html_url']}\n"
            f"  Release it with --release {issue} only if you own it."
        )

    # Board membership is a *precondition*, not a side effect, so it is checked
    # before anything is written. It used to be checked inside the status move,
    # which runs after the claim comment — so an issue not on the board got a
    # comment and then an abort, and the comment is the race guard the next
    # claim reads. The issue became permanently unclaimable by its own failed
    # run, recoverable only by moving the board item by hand. Hit twice for
    # real.
    if board_item_id(issue) is None:
        raise SystemExit(f"#{issue} is not on the board — add it before claiming")

    branch = f"{issue}_{slug}"
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Comment first: it is the race guard, and the check above reads it. If the
    # board move then fails we stop loudly with the claim already recorded,
    # which is recoverable. The reverse order could move the board for a claim
    # that never lands.
    gh(
        "issue", "comment", str(issue), "--repo", need("OVERNIGHT_REPO", "claim"),
        "--body", f"{CLAIM_PREFIX} `{branch}` at {ts}.",
    )
    set_status(issue, CFG["OVERNIGHT_ACTIVE_COLUMN"])

    # Verify rather than assume. A claim that comments but does not move the
    # board reads as free to the next agent.
    column = board_column(issue)
    if column != CFG["OVERNIGHT_ACTIVE_COLUMN"]:
        raise SystemExit(
            f"claim comment posted but the board reads '{column}', not "
            f"'{CFG['OVERNIGHT_ACTIVE_COLUMN']}' — fix this before writing any code"
        )
    print(f"  claimed #{issue} -> branch {branch} (board: {column})")


def do_release(issue: int) -> None:
    c = live_claim(issue)
    if c is None:
        raise SystemExit(f"#{issue} has no live claim to release")
    repo = need("OVERNIGHT_REPO", "release")
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = f"{RELEASED_PREFIX} — at {ts}. Original claim:\n\n> " + (c["body"] or "").replace(
        "\n", "\n> "
    )
    gh("api", "-X", "PATCH", f"repos/{repo}/issues/comments/{c['id']}", "-f", f"body={body}")
    set_status(issue, CFG["OVERNIGHT_TODO_COLUMN"])
    print(f"  released #{issue} (board: {CFG['OVERNIGHT_TODO_COLUMN']})")


# --------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true")
    p.add_argument("--pick", action="store_true")
    p.add_argument("--graph", action="store_true")
    p.add_argument("--sync-labels", action="store_true")
    p.add_argument("--claim", type=int)
    p.add_argument("--verify", type=int)
    p.add_argument("--release", type=int)
    p.add_argument("--done", type=int)
    p.add_argument("--slug", default="agent")
    a = p.parse_args()

    if a.verify:
        column = board_column(a.verify)
        expected = CFG["OVERNIGHT_ACTIVE_COLUMN"]
        print(f"  #{a.verify} board column: {column!r}")
        raise SystemExit(0 if column == expected else f"expected {expected!r}")

    if a.done:
        set_status(a.done, CFG["OVERNIGHT_DONE_COLUMN"])
        print(f"  #{a.done} -> {CFG['OVERNIGHT_DONE_COLUMN']}")
        return

    if a.release:
        do_release(a.release)
        return

    by_num, edges = build()
    check_acyclic(edges)

    if a.graph:
        for n in sorted(edges):
            if edges[n]:
                print(f"  #{n} <- {sorted(edges[n])}")
        print(f"  edges: {sum(len(v) for v in edges.values())}")
        print("  acyclic: PASS")
        return

    r = ready(by_num, edges)

    if a.sync_labels:
        blocked_label = CFG["OVERNIGHT_BLOCKED_LABEL"]
        repo = need("OVERNIGHT_REPO", "label sync")
        for n, i in by_num.items():
            if i["state"] != "OPEN":
                continue
            blocked = bool(edges[n]) and any(by_num[d]["state"] == "OPEN" for d in edges[n])
            has = any(lb["name"] == blocked_label for lb in i["labels"])
            if blocked and not has:
                gh("issue", "edit", str(n), "--repo", repo, "--add-label", blocked_label)
                print(f"  +{blocked_label} #{n}")
            elif not blocked and has:
                gh("issue", "edit", str(n), "--repo", repo, "--remove-label", blocked_label)
                print(f"  -{blocked_label} #{n}")
        return

    if a.claim:
        do_claim(a.claim, a.slug, r, edges, by_num)
        return

    if a.pick:
        print(r[0] if r else "none ready")
        return

    for n in r:
        i = by_num[n]
        ms = (i.get("milestone") or {}).get("title", "-")
        labels = ",".join(lb["name"] for lb in i["labels"])
        print(f"  #{n:<4} [{ms}] {i['title'][:58]:<58} {labels}")
    if not r:
        print("  none ready")


if __name__ == "__main__":
    sys.exit(main())
