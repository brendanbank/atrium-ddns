#!/usr/bin/env bash
# Cut a development release: a signed tag plus a git bundle, for a
# staging host, without CI running.
#
# ## Why this exists
#
# `master` is the release boundary and CI is the gate on the way in
# (`.github/workflows/ci.yml`). A development release is the other
# thing: code you want running on a staging host *before* it is ready
# for that gate. It has to be identifiable, reproducible and deployable,
# and it must not queue a CI run per iteration.
#
# ## Why it ships a bundle rather than pushing
#
# The deploy hosts have no credentials for the remote — `git fetch`
# there fails with "could not read from remote repository". A bundle is
# a single file that `git fetch` accepts as a remote, so it needs
# nothing but ssh and scp. See `docs/ops/dev-releases.md`.
#
# Usage:
#   scripts/dev-release.sh                 # next dev number on this branch
#   scripts/dev-release.sh --number 7      # an explicit one
#   scripts/dev-release.sh --dry-run
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DRY_RUN=0
EXPLICIT_NUMBER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --number) EXPLICIT_NUMBER="${2:?--number needs a value}"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# --- Refusals, in the order that catches the most damage earliest ----

# 1. Never from master. A dev release is by definition not the release
#    boundary, and tagging master as `-dev` would say the opposite.
if [ "$BRANCH" = "master" ]; then
  echo "refusing: on master. A dev release is cut from a dev branch." >&2
  echo "  master is the release boundary; CI gates the way in." >&2
  exit 1
fi

# 2. The branch name must be one CI cannot fire on. This is checked
#    against the workflow rather than against a remembered rule,
#    because the rule lives in that file and can change without this
#    script noticing. A guard that trusts its own copy of the policy is
#    a guard that stops matching the day the policy moves.
CI_FILE=".github/workflows/ci.yml"
if [ ! -f "$CI_FILE" ]; then
  echo "refusing: $CI_FILE is missing — cannot prove CI will not fire." >&2
  exit 1
fi
if grep -qE '^\s*tags:' "$CI_FILE"; then
  echo "refusing: $CI_FILE has a tag trigger, so tagging would start a run." >&2
  echo "  Either drop it, or exclude dev tags explicitly, then re-run." >&2
  exit 1
fi
# EVERY branch list the workflow reacts to must be exactly `[master]`.
#
# Note the quantifier. An earlier version asked whether *any* line said
# `[master]` — which a widened allowlist passes, because the
# `pull_request` line still says it while `push` has grown `dev/**`.
# That version was written, tested against a mutation that silently
# failed to apply, and declared working. The mutation test now asserts
# it applied.
CI_BRANCHES="$(sed -n 's/^[[:space:]]*branches:[[:space:]]*//p' "$CI_FILE" | tr -d ' ')"
if [ -z "$CI_BRANCHES" ]; then
  echo "refusing: no branch triggers found in $CI_FILE — cannot prove CI is" >&2
  echo "  master-only. A workflow with no 'branches:' key fires on every push." >&2
  exit 1
fi
while IFS= read -r line; do
  if [ "$line" != "[master]" ]; then
    echo "refusing: CI is no longer master-only. Found trigger: $line" >&2
    echo "  A dev release assumes pushes outside master run nothing." >&2
    echo "  Read $CI_FILE and docs/ops/dev-releases.md before releasing." >&2
    exit 1
  fi
done <<< "$CI_BRANCHES"
case "$BRANCH" in
  dev/*|staging/*) ;;
  *)
    echo "refusing: branch '$BRANCH' is not dev/* or staging/*." >&2
    echo "  The convention is documented in docs/ops/dev-releases.md." >&2
    echo "  Rename with: git branch -m dev/<topic>" >&2
    exit 1
    ;;
esac

# 3. A dirty tree cannot be reproduced from its tag. The tag would name
#    a commit that is not what got deployed, which is worse than having
#    no tag at all.
if [ -n "$(git status --porcelain)" ]; then
  echo "refusing: working tree is dirty. A dev release must be reproducible" >&2
  echo "  from its tag; commit or stash first." >&2
  git status --short | sed 's/^/    /' >&2
  exit 1
fi

# --- The tag -------------------------------------------------------

VERSION="$(awk -F'"' '/^version = /{print $2; exit}' backend/pyproject.toml)"
SLUG="$(printf '%s' "${BRANCH#dev/}" | tr -c 'a-zA-Z0-9' '-' | sed 's/-\+/-/g;s/^-//;s/-$//')"

if [ -n "$EXPLICIT_NUMBER" ]; then
  N="$EXPLICIT_NUMBER"
else
  # Highest existing number for this branch, plus one. Per-branch so two
  # dev branches do not renumber each other.
  N=$(( $(git tag --list "v${VERSION}-dev.${SLUG}.*" \
        | sed "s/.*\.//" | sort -n | tail -1 | grep -E '^[0-9]+$' || echo 0) + 1 ))
fi

TAG="v${VERSION}-dev.${SLUG}.${N}"
SHA="$(git rev-parse --short HEAD)"
BUNDLE="dist/${TAG}.bundle"

echo "  branch : $BRANCH"
echo "  commit : $SHA"
echo "  tag    : $TAG"
echo "  bundle : $BUNDLE"

if [ "$DRY_RUN" = "1" ]; then
  echo "  (dry run — nothing written)"
  exit 0
fi

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "refusing: tag $TAG already exists. Pass --number to pick another." >&2
  exit 1
fi

mkdir -p dist
# Signed, like every other tag and commit in this repo. `-s` fails loudly
# if the hardware token is not available, which is the right outcome: an
# unsigned dev tag is indistinguishable from one anybody could have made.
git tag -s "$TAG" -m "Development release $TAG from $BRANCH at $SHA

Not a release. Cut for staging deployment; CI has not run on this
commit and master has not seen it. See docs/ops/dev-releases.md."

# `--all` plus the tag: the deploy host needs the objects AND a ref it
# can check out by name.
git bundle create "$BUNDLE" "$TAG" HEAD >/dev/null 2>&1
echo "  ✓ tagged and bundled"
echo
echo "  deploy with:"
echo "    scripts/deploy-dev.sh <ssh-host> $BUNDLE"
