#!/usr/bin/env bash
# Run the gate's backend half against a MERGE COMMIT on the milestone
# branch, and say what a red one means.
#
# ## Why this exists (#84, and #78 before it)
#
# `v1m4-migration-cutover` merged `v1m3-host-ui` at 4ff2da4. #65's harness
# guard came from one side and #50's rehearsal fixture from the other;
# `git cat-file -e` on each parent shows neither parent held both files. Both
# PRs were correct against the tree they were reviewed on. The merge commit
# was the first tree in which the guard could see the fixture, and it was red
# the moment it existed. It stayed red across two further merges, because:
#
#   - CI does not run on milestone branches at all, by design. That decision
#     is stated with its rationale in `.github/workflows/ci.yml`'s header and
#     this script does not reverse it — it is the local answer that lets the
#     decision stand.
#   - the local gate is per-issue. It answers "is my branch green", never
#     "is the branch I merged into green".
#   - a merge with no textual conflict emits no signal. Nothing conflicted
#     here; the semantics did.
#
# So: same commands, different tree. No new test, no new quality bar.
#
# ## Why it does not just print "red"
#
# A red run is not a verdict in this repository, because the background rate
# is not zero. Recorded in this batch alone, on byte-identical trees:
#
#   - PR #111: five full e2e runs read 18/20, 19/20, 20/20, 20/20, 20/20.
#     `device-detail.spec.ts:101` twice, `resolution-strip.spec.ts:57` once,
#     against a tree whose diff from the milestone branch was empty.
#   - PR #110: `zone-provider.spec.ts:63` timed out once, passed alone.
#   - PR #112: `test_worker_jobs.py::test_an_ipv6_answer_spelled_differently_
#     is_not_a_mismatch` failed once in a full run and never again.
#
# A check that called any of those a regression would cry wolf often enough
# to be ignored inside a week, which is the same outcome as not having it.
# Three things follow, and they are the design:
#
#   1. **It runs the instrument with the low rate.** Backend only. e2e left
#      the per-issue gate by operator decision and is not reinstated here; its
#      measured rate — 3 failures in 100 spec-runs, 2 of 5 full runs red — is
#      too high for an unattended accusation. Measured for the backend half on
#      this box on 2026-08-31: 11 consecutive full runs on the V1M7 tip,
#      841 passed every time, zero non-reproducing failures.
#   2. **It confirms before it accuses.** The FULL suite is re-run
#      (--confirm N, default 2 total runs) and only the INTERSECTION of the
#      failing sets counts. Full suite rather than the failing node ids alone
#      on purpose: re-running a subset changes the parallel contention that
#      produced the failure, so a real xdist-contention bug would re-run green
#      and be filed as a flake. Cost of a re-run is ~14s.
#   3. **Its positive verdict is comparative, not repetitive.** Reproducibly
#      red on the merge AND not red on either parent. That is the one shape a
#      flake cannot fake: a flake would have to hit the same node on every run
#      of the merge tree and on no run of either parent.
#
# Anything it cannot settle is reported UNRESOLVED and names the merge and
# both parents, rather than being rounded to either verdict.
#
# ## The report names the merge, not the last PR
#
# #78's cost was that a red tip reads as "the last PR broke it", and the last
# PR had not touched the files. Every line of output names the merge commit
# and both of its parents.
#
# Usage:
#   scripts/merge-tip-check.sh                    # the milestone branch tip
#   scripts/merge-tip-check.sh --commit 4ff2da4   # any merge commit
#   scripts/merge-tip-check.sh --confirm 3        # three full runs, not two
#   scripts/merge-tip-check.sh --no-parents       # skip the parent replay
#
# Exit codes are the verdict; see VERDICTS below.
set -uo pipefail

# 0  GREEN               the tip is green (or the only failures did not reproduce)
# 3  MERGE-INDUCED       reproducibly red here, not red on either parent
# 4  INHERITED           reproducibly red here AND on a parent — it arrived broken
# 5  UNRESOLVED          reproducibly red, parent evidence inconclusive or skipped
# 6  INFRASTRUCTURE      docker, build, migrate or freshness failed — NOT a red suite
# 2  usage
EX_GREEN=0; EX_MERGE=3; EX_INHERITED=4; EX_UNRESOLVED=5; EX_INFRA=6; EX_USAGE=2

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit $EX_INFRA

COMMIT=""
CONFIRM=2
DO_PARENTS=1
DO_FETCH=1
KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --commit) COMMIT="${2:?--commit needs a value}"; shift 2 ;;
    --confirm) CONFIRM="${2:?--confirm needs a value}"; shift 2 ;;
    --no-parents) DO_PARENTS=0; shift ;;
    --no-fetch) DO_FETCH=0; shift ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,80p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit $EX_USAGE ;;
  esac
done
case "$CONFIRM" in ''|*[!0-9]*) echo "--confirm takes a number" >&2; exit $EX_USAGE ;; esac
[ "$CONFIRM" -lt 1 ] && CONFIRM=1

say() { printf '%s\n' "tip-check: $*"; }

# --- which commit ---------------------------------------------------------

# The milestone branch is read from .overnight.conf, the one file both the
# shell scripts and find_ready.py agree on. One key, with sed, never sourced
# — the same reading `make gate` does.
MILESTONE="$(sed -n 's/^OVERNIGHT_MILESTONE_BRANCH=//p' .overnight.conf 2>/dev/null | tail -n1)"
MILESTONE="${OVERNIGHT_MILESTONE_BRANCH:-$MILESTONE}"

if [ -z "$COMMIT" ]; then
  if [ -z "$MILESTONE" ]; then
    echo "refusing: no --commit and no OVERNIGHT_MILESTONE_BRANCH in .overnight.conf" >&2
    exit $EX_USAGE
  fi
  # "A stale fetch reads as a missing merge" — overnight-template.md. Fetch
  # through the path we are about to read, or say we did not.
  if [ "$DO_FETCH" = 1 ]; then
    say "fetching origin/$MILESTONE"
    git fetch --quiet origin "$MILESTONE" || { echo "refusing: fetch failed" >&2; exit $EX_INFRA; }
  else
    say "NOT fetching (--no-fetch) — the tip below may be stale"
  fi
  COMMIT="$(git rev-parse --verify -q "FETCH_HEAD" 2>/dev/null || git rev-parse --verify -q "origin/$MILESTONE")"
fi

SHA="$(git rev-parse --verify -q "$COMMIT^{commit}")" || {
  echo "refusing: '$COMMIT' is not a commit in this repository" >&2; exit $EX_USAGE; }
SHORT="$(git rev-parse --short "$SHA")"

# All parents, not just two. A merge into the milestone branch has two; a
# commit pushed straight to it has one — and this branch has had both today
# (0d14da7, 9744071 and f26235a are direct, 2e6fb13 and 83dc999 are merges).
# Refusing on a non-merge tip would have left the commonest red tip on this
# branch unchecked, which is the failure this is about. A one-parent commit
# gets the same treatment with one comparison instead of two, and the verdict
# is named differently because the diagnosis is different.
PARENTS=()
read -r -a _RL <<<"$(git rev-list --parents -n1 "$SHA")"
PARENTS=("${_RL[@]:1}")
NPAR=${#PARENTS[@]}
if [ "$NPAR" -eq 0 ]; then
  say "REFUSING: $SHORT is a root commit — there is nothing to compare it to."
  exit $EX_USAGE
fi
KIND="merge"; [ "$NPAR" -eq 1 ] && KIND="commit"

echo
echo "================================================================"
echo "  $KIND  $SHORT  $(git log -1 --format=%s "$SHA")"
for i in "${!PARENTS[@]}"; do
  printf '  parent-%d %s  %s\n' "$((i+1))" \
    "$(git rev-parse --short "${PARENTS[$i]}")" \
    "$(git log -1 --format=%s "${PARENTS[$i]}")"
done
echo "================================================================"
if [ "$NPAR" -ge 2 ]; then
  say "a red tip reads as 'the last PR broke it'. It is not necessarily."
  say "the tree under test is the MERGE above, not either parent."
fi
echo

# --- one tree, one stack, one suite ---------------------------------------

WORKDIRS=()
cleanup() {
  local d
  for d in "${WORKDIRS[@]:-}"; do
    [ -d "$d" ] || continue
    if [ "$KEEP" = 1 ]; then echo "tip-check: kept $d"; continue; fi
    ( cd "$d" && docker compose -p "$(basename "$d")" down -v --remove-orphans >/dev/null 2>&1 )
    rm -rf "$d"
  done
}
trap cleanup EXIT

# Two free TCP ports, asked of the kernel rather than guessed. Parallel agents
# each run their own stack on this box and a guessed pair collides; compose
# fails loudly on the bind, but a stall at 03:00 is still a stall.
free_ports() {
  python3 - <<'PY'
import socket
out = []
for _ in range(2):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    out.append(s.getsockname()[1])
    s.listen(1)
print(*out)
PY
}

# Materialise a commit's tree into its own directory. `git archive` rather
# than `git worktree add`: no index, no ref, nothing that can disturb the
# caller's branch, and it works from a worktree-isolated agent.
materialise() {
  local sha="$1" dir="$2" api mysql
  mkdir -p "$dir" || return 1
  git archive "$sha" | tar -x -C "$dir" || return 1
  read -r api mysql <<<"$(free_ports)"
  sed \
    -e "s/^API_HOST_PORT=.*/API_HOST_PORT=$api/" \
    -e "s/^MYSQL_HOST_PORT=.*/MYSQL_HOST_PORT=$mysql/" \
    -e "s|^APP_BASE_URL=.*|APP_BASE_URL=http://localhost:$api|" \
    -e "s/^APP_SECRET_KEY=.*/APP_SECRET_KEY=tip-check-not-a-real-key/" \
    -e "s/^JWT_SECRET=.*/JWT_SECRET=tip-check-not-a-real-key/" \
    -e "s/^SECRET_ENCRYPTION_KEY=.*/SECRET_ENCRYPTION_KEY=$(printf '0%.0s' $(seq 64))/" \
    "$dir/.env.example" > "$dir/.env" || return 1
  echo "$api/$mysql"
}

# Raise the stack for a materialised tree. Returns non-zero for an
# INFRASTRUCTURE problem, which is deliberately not the same thing as a red
# suite — #78's whole cost was a red that got attributed to the wrong cause.
raise() {
  local dir="$1" log="$2"
  { make -C "$dir" build && make -C "$dir" up; } >>"$log" 2>&1 || return 1
  # mysql's healthcheck gates api, but the host alembic chain still races the
  # first connection on a cold volume. Retry rather than fail the run on it.
  local i
  for i in $(seq 1 20); do
    make -C "$dir" migrate >>"$log" 2>&1 && return 0
    sleep 3
  done
  return 1
}

# One full `make test-backend` against a raised stack. Writes the failing
# pytest node ids, one per line, to stdout. Freshness (`check-fresh`) is a
# prerequisite of the target and stays that way: a stale container reports
# green about the wrong tree, which is the failure mode this whole script is
# aimed at, one layer down.
suite_run() {
  local dir="$1" log="$2" out
  out="$(make -C "$dir" test-backend 2>&1)"
  printf '%s\n' "$out" >>"$log"
  # This run's own summary lines, not the log's — the log accumulates across
  # runs and `tail` on it silently reports an earlier run's numbers under a
  # later run's heading.
  #
  # Written to a file rather than to a variable because this function is
  # called inside `$( )`: a subshell's assignment does not reach the caller,
  # and the first version of this line died with "unbound variable" for
  # exactly that reason.
  local sum
  sum="$(printf '%s\n' "$out" | grep -E '^=+ .*(passed|failed|error).* =+$' | sed 's/^/    /')"
  # #78's second observation: when the host session is red, `make` stops and
  # the compat session never runs. One summary line, not two, means the
  # accounting is partial — say so rather than letting a reader assume the
  # compat half was green.
  if [ "$(printf '%s\n' "$sum" | grep -c .)" -lt 2 ]; then
    sum="$sum
    (the compat session did not run — make stopped at the first red session)"
  fi
  printf '%s\n' "$sum" > "$log.summary"
  if printf '%s\n' "$out" | grep -qE 'STALE copy of|could not read both digests'; then
    echo "__INFRA__"
    return
  fi
  printf '%s\n' "$out" | sed -n 's/^FAILED \([^ ]*\).*/\1/p' | sort -u
}

summarise() { cat "$1.summary" 2>/dev/null; }

# A pytest node id names a path relative to the container's rootdir, not a
# repo path. `../opt/host_app/tests/x.py` is `backend/tests/x.py` here.
#
# Getting this wrong is not a cosmetic bug, and it was one in the first
# version of this script: an unmapped path made `git cat-file -e` miss on
# BOTH parents, which the cheap check below read as "absent from both" and
# turned into a MERGE-INDUCED verdict without replaying anything. Right
# answer, no evidence — the vacuous-guard shape this repository keeps
# finding, written into the guard against it. So: an unmappable path returns
# empty, and empty forces the replay rather than concluding.
repo_path() {
  local f="${1%%::*}"
  while [ "${f#../}" != "$f" ]; do f="${f#../}"; done
  f="${f#/}"
  case "$f" in
    opt/host_app/*)     f="backend/${f#opt/host_app/}" ;;
    opt/compat_tests/*) f="tests/${f#opt/compat_tests/}" ;;
  esac
  # Vacuity check on the mapping itself: whatever we resolved MUST exist on
  # the commit that is red. If it does not, the mapping is wrong and we know
  # nothing about the parents.
  git cat-file -e "$SHA:$f" 2>/dev/null && printf '%s' "$f"
}

# --- the tip --------------------------------------------------------------

docker info >/dev/null 2>&1 || { say "REFUSING: docker is not available. That is a stop condition, not a red tip."; exit $EX_INFRA; }

TIPDIR="$(mktemp -d "${TMPDIR:-/tmp}/tipchk-$SHORT-XXXXXX")"
WORKDIRS+=("$TIPDIR")
TIPLOG="$TIPDIR.log"
say "materialising $SHORT into $TIPDIR"
PORTS="$(materialise "$SHA" "$TIPDIR")" || { say "REFUSING: could not materialise $SHORT"; exit $EX_INFRA; }
say "stack: project $(basename "$TIPDIR"), ports $PORTS (its own; nothing else's)"
say "raising the stack (build + up + migrate) — log: $TIPLOG"
raise "$TIPDIR" "$TIPLOG" || { say "REFUSING: could not raise a stack for $SHORT. INFRASTRUCTURE, not a red tip."; tail -20 "$TIPLOG" | sed 's/^/    /'; exit $EX_INFRA; }

REPRO=""
for run in $(seq 1 "$CONFIRM"); do
  say "full backend suite on $SHORT — run $run of $CONFIRM"
  FAILED="$(suite_run "$TIPDIR" "$TIPLOG")"
  if [ "$FAILED" = "__INFRA__" ]; then
    say "REFUSING: the container is not running this tree (freshness guard). INFRASTRUCTURE."
    exit $EX_INFRA
  fi
  summarise "$TIPLOG"
  if [ -z "$FAILED" ]; then
    if [ "$run" = 1 ]; then
      say "VERDICT: GREEN — $SHORT is green on the first full run."
      say "  $KIND $SHORT, parent(s) $(for p in "${PARENTS[@]}"; do printf '%s ' "$(git rev-parse --short "$p")"; done)"
      exit $EX_GREEN
    fi
    # Red once, green now. That is the background rate, not a regression.
    say "VERDICT: GREEN, with a NON-REPRODUCING failure — report it, do not swallow it."
    say "  run 1 failed and run $run passed on a byte-identical tree:"
    printf '%s\n' "$REPRO" | sed 's/^/      /'
    say "  this repository has a measured background rate (see the header)."
    say "  Recorded here so the population of known flakes grows instead of the noise."
    exit $EX_GREEN
  fi
  printf '%s\n' "$FAILED" | sed 's/^/      FAILED /'
  if [ "$run" = 1 ]; then
    REPRO="$FAILED"
  else
    REPRO="$(comm -12 <(printf '%s\n' "$REPRO") <(printf '%s\n' "$FAILED"))"
  fi
  if [ -z "$REPRO" ]; then
    say "VERDICT: GREEN — every failure was a different one. Nothing reproduced across $run runs."
    exit $EX_GREEN
  fi
done

echo
say "reproducible across $CONFIRM full runs of $SHORT:"
printf '%s\n' "$REPRO" | sed 's/^/      /'
echo

if [ "$DO_PARENTS" = 0 ]; then
  say "VERDICT: UNRESOLVED — reproducibly red, parents not replayed (--no-parents)."
  say "  merge $SHORT is red. Whether the merge CAUSED it is unanswered."
  exit $EX_UNRESOLVED
fi

# --- the parents ----------------------------------------------------------
#
# The cheap half first, and it is the half that settles #78's shape. A test
# file that does not EXIST on a parent cannot have been red on that parent —
# `git cat-file -e` answers in milliseconds what a stack answers in two
# minutes, and it is the same command #78 and #84 both quote as the proof.

# One line per distinct file, not per failing node: two failures in the same
# module are one question about one path.
#
# Newline-separated and read with `while IFS= read -r`, never split on $IFS: a
# parametrised pytest node id legitimately contains spaces (`test_x[a b]`) and
# word-splitting would turn one node into two, both unmappable, which the
# check above then reports as "cannot map" and replays for. Safe, but slow and
# wrong for a reason nobody would find.
REPRO_FILES=""
UNMAPPABLE=""
while IFS= read -r node; do
  [ -n "$node" ] || continue
  rp="$(repo_path "$node")"
  if [ -z "$rp" ]; then
    UNMAPPABLE="$UNMAPPABLE $node"
  else
    REPRO_FILES="$REPRO_FILES$rp
"
  fi
done <<EOF
$REPRO
EOF
REPRO_FILES="$(printf '%s' "$REPRO_FILES" | sort -u)"

REDON=""
for p in "${PARENTS[@]}"; do
  pshort="$(git rev-parse --short "$p")"

  # Settled cheaply? Only if EVERY reproducibly-failing file both maps to a
  # repo path and is absent from this parent. One unmappable path, or one
  # file this parent holds, and this parent gets the full replay.
  settled=1
  if [ -n "$UNMAPPABLE" ]; then
    say "  $pshort: cannot map$UNMAPPABLE to a repo path — replaying rather than assuming"
    settled=0
  fi
  while IFS= read -r rp; do
    [ -n "$rp" ] || continue
    [ "$settled" = 1 ] || break
    if git cat-file -e "$p:$rp" 2>/dev/null; then
      settled=0
    else
      say "  $pshort does not contain $rp — that parent cannot have been red for it"
    fi
  done <<EOF
$REPRO_FILES
EOF
  if [ "$settled" = 1 ]; then
    say "  $pshort settled by \`git cat-file -e\` alone, no stack needed"
    continue
  fi

  PDIR="$(mktemp -d "${TMPDIR:-/tmp}/tipchk-$pshort-XXXXXX")"
  WORKDIRS+=("$PDIR")
  PLOG="$PDIR.log"
  say "replaying the full suite on parent $pshort — log: $PLOG"
  if ! materialise "$p" "$PDIR" >/dev/null; then
    say "  could not materialise $pshort"; REDON="$REDON ?$pshort"; continue
  fi
  if ! raise "$PDIR" "$PLOG"; then
    say "  could not raise a stack for $pshort (INFRASTRUCTURE on the parent, not a verdict)"
    tail -10 "$PLOG" | sed 's/^/      /'
    REDON="$REDON ?$pshort"; continue
  fi
  PF="$(suite_run "$PDIR" "$PLOG")"
  summarise "$PLOG"
  if [ "$PF" = "__INFRA__" ]; then REDON="$REDON ?$pshort"; continue; fi
  # Compared by test NAME, not by node id: the rootdir prefix is identical
  # across trees here, but a moved file would otherwise read as "green on the
  # parent" when it is the same test failing under a new path.
  HIT="$(comm -12 <(printf '%s\n' "$REPRO" | sed 's/.*\///') <(printf '%s\n' "$PF" | sed 's/.*\///'))"
  if [ -n "$HIT" ]; then
    say "  $pshort is ALSO red for:"
    printf '%s\n' "$HIT" | sed 's/^/        /'
    REDON="$REDON $pshort"
  else
    say "  $pshort is green for every reproducible failure above"
  fi
done

provenance() {
  echo "    $KIND  $SHORT  $(git log -1 --format=%s "$SHA")"
  for i in "${!PARENTS[@]}"; do
    printf '    parent-%d  %s  %s\n' "$((i+1))" \
      "$(git rev-parse --short "${PARENTS[$i]}")" \
      "$(git log -1 --format=%s "${PARENTS[$i]}")"
  done
}

echo
echo "================================================================"
case "$REDON" in
  "")
    if [ "$NPAR" -ge 2 ]; then
      echo "  VERDICT: MERGE-INDUCED"
      echo
      echo "  $SHORT is reproducibly red and NO parent is."
      provenance
      echo
      echo "  Both sides were green against the tree they were reviewed on."
      echo "  This commit is the first tree in which they coexist. The PR that"
      echo "  merged last did not necessarily touch these files — do not open"
      echo "  the issue against it. Open it against the MERGE, name both"
      echo "  parents, and say which file came from which side."
    else
      echo "  VERDICT: INTRODUCED HERE"
      echo
      echo "  $SHORT is reproducibly red and its only parent is not."
      provenance
      echo
      echo "  One parent, so there is no combination to blame: this commit's"
      echo "  own diff did it. \`git show $SHORT --stat\` is the whole suspect list."
    fi
    echo "================================================================"
    exit $EX_MERGE ;;
  *\?*)
    echo "  VERDICT: UNRESOLVED"
    echo "  $SHORT is reproducibly red. A parent could not be replayed:$REDON"
    echo "  Reported rather than rounded to a verdict."
    provenance
    echo "================================================================"
    exit $EX_UNRESOLVED ;;
  *)
    echo "  VERDICT: INHERITED"
    echo "  $SHORT is red, and so is:$REDON"
    echo "  It arrived broken. This commit did not cause it and neither did the"
    echo "  last PR — go to that parent's own history."
    provenance
    echo "================================================================"
    exit $EX_INHERITED ;;
esac
