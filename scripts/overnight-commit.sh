#!/usr/bin/env bash
#
# Commit that survives a locked signing token.
#
#   overnight-commit.sh -m "subject" [extra git commit args...]
#   overnight-commit.sh -F path/to/message
#
# Why this exists
# ---------------
# `OVERNIGHT_GPG_BLOCKING=0` reads as "a locked token will not stop the run".
# It does not do that. It is consulted by `preflight.sh`, once, before any work
# — and nothing consults it again. At commit time git tries to sign, gpg
# answers `signing failed: No secret key`, and git exits with
# `fatal: failed to write commit object`.
#
# So an unattended run configured to tolerate a locked token still dies at its
# first commit, having done all the work and pushed none of it. Unpushed
# worktree state is the one thing a shutdown cannot recover.
#
# Verified, not assumed: with GNUPGHOME pointed at an empty directory,
# `git -c commit.gpgsign=true commit` fails as above, and the same commit with
# `commit.gpgsign=false` succeeds and produces an unsigned (`N`) commit.
#
# Behaviour
# ---------
#   1. Try a signed commit.
#   2. If that fails AND OVERNIGHT_GPG_BLOCKING=0, retry unsigned and say so
#      loudly on stdout — the PR body should carry it, because an unsigned
#      commit shows as unverified on GitHub permanently and rewriting history
#      after a merge is worse than the pause would have been.
#   3. If that fails AND OVERNIGHT_GPG_BLOCKING=1, stop. That is the setting
#      meaning what it says.
#
# It deliberately does NOT pre-check whether gpg works. A probe that asks "can
# I sign?" and a commit that actually signs are different questions, and only
# the second one matters — `printf t | gpg --clearsign` can succeed while the
# commit still fails on a different key.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1

# The environment wins over the file — that is the documented rule in
# .overnight.conf's own header ("Environment variables of the same name win").
# `set -a; . conf` does the opposite: it overwrites anything already exported.
# Getting this backwards meant OVERNIGHT_GPG_BLOCKING=1 in the environment was
# silently replaced by the file's 0, and the stop condition committed unsigned
# instead of stopping. Capture first, restore after.
_env_gpg_blocking=${OVERNIGHT_GPG_BLOCKING:-}

conf=""; dir=$PWD
while [ "$dir" != "/" ]; do
  [ -f "$dir/.overnight.conf" ] && { conf="$dir/.overnight.conf"; break; }
  dir=$(dirname "$dir")
done
# shellcheck disable=SC1090
[ -n "$conf" ] && set -a && . "$conf" && set +a

[ -n "$_env_gpg_blocking" ] && OVERNIGHT_GPG_BLOCKING=$_env_gpg_blocking
: "${OVERNIGHT_GPG_BLOCKING:=1}"

[ $# -gt 0 ] || { echo "usage: overnight-commit.sh -m <subject> | -F <file> [args...]" >&2; exit 2; }

if err=$(git -c commit.gpgsign=true commit "$@" 2>&1); then
  printf '%s\n' "$err"
  echo "commit signed: $(git log --format='%h %G?' -1)"
  exit 0
fi

printf '%s\n' "$err" >&2

if [ "$OVERNIGHT_GPG_BLOCKING" = "1" ]; then
  cat >&2 <<'STOP'

STOP CONDITION: could not sign, and OVERNIGHT_GPG_BLOCKING=1.
Unlock the signing token and re-run. Do not work around this by setting the
flag to 0 mid-run — that decision is the operator's, not the run's.
STOP
  exit 1
fi

echo "signing failed; OVERNIGHT_GPG_BLOCKING=0, retrying unsigned" >&2
if git -c commit.gpgsign=false commit "$@"; then
  cat <<NOTE
commit UNSIGNED: $(git log --format='%h %G?' -1)
  The signing token was unavailable and policy permits unsigned commits.
  Record this in the PR body — it shows as unverified on GitHub permanently,
  and history rewriting after a merge costs more than a re-sign now does.
NOTE
  exit 0
fi

echo "commit failed for a reason other than signing — see the error above" >&2
exit 1
