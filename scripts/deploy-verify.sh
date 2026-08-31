#!/usr/bin/env bash
# Ship a branch to the deploy host and verify the deploy BY CONTENT.
#
#   deploy-verify.sh <branch> [--no-deploy]
#   deploy-verify.sh --self-test        # prove the identity check can FAIL
#
# The verification is the point. `docker compose up` exiting 0 says the deploy
# ran, not that it carries your merge — and a deploy that silently does not
# carry your merge is the failure this script exists to make impossible.
#
# Two things are asserted after the deploy:
#   1. ancestry  — the sha you shipped is an ancestor of the host's HEAD
#   2. identity  — the file the running application IMPORTS hashes the same as
#                  the blob in that commit
#
# And one thing is asserted BEFORE it: the host's current migration version is
# contained in the branch being deployed. A branch carrying no migration at all
# took an estate down for four minutes purely by being one behind.
#
# Config: .overnight.conf or OVERNIGHT_* in the environment.
#   OVERNIGHT_DEPLOY_HOST      ssh alias (required)
#   OVERNIGHT_DEPLOY_PATH      checkout path on the host (required)
#   OVERNIGHT_DEPLOY_KEY       dedicated key, if not in ssh config
#   OVERNIGHT_DEPLOY_BRANCH    branch name used on the host (default: testdrive)
#   OVERNIGHT_COMPOSE_SERVICE  service to exec into for the identity check
#   OVERNIGHT_IDENTITY_FILE    path inside the container, and repo-relative path
#                              if they differ, as "container:repo"
#   OVERNIGHT_IDENTITY_MODULE  importable module the app loads (optional, see
#                              below). When set, the container path is RESOLVED
#                              rather than trusted.
#   OVERNIGHT_IDENTITY_PYTHON  interpreter used for that resolution — must be
#                              the one the service runs (default: python)
#   OVERNIGHT_IDENTITY_RESOLVE_STRICT
#                              1 (default): a configured path that is not the
#                              imported one fails the run after reporting the
#                              comparison. 0: warn only.
#   OVERNIGHT_MIGRATION_CMD    command on the host printing the applied version
#   OVERNIGHT_MIGRATION_GREP   how to map that version to a repo path/pattern
#
# --- naming the artefact the process loads (#38) --------------------------
#
# The identity check names ONE file, and nothing used to check that the file it
# named was one the application actually loads. That is not hypothetical here:
# this image holds TWO copies of the host source — `COPY backend /opt/host_app`
# and then `pip install /opt/host_app` — and only the installed copy is on
# sys.path. A check pointed at /opt/host_app is true, checkable, green, and
# about a tree nothing imports. Found by atrium-ddns #36 one level down, where
# the gate digested /opt/host_app while the tests imported site-packages.
#
# The failure mode is quiet by construction: the two copies are equal today
# because one chained build produces both, so the check passes for as long as
# the packaging happens to keep them equal, and goes on passing on the day it
# stops. Three changes, all of them about that:
#
#   1. RESOLVE, DO NOT TRUST. With OVERNIGHT_IDENTITY_MODULE set, the container
#      path is resolved inside the container by the same machinery the import
#      uses (`importlib.util.find_spec`), run with the interpreter the service
#      runs. It therefore cannot name a copy the app does not load. The literal
#      path in OVERNIGHT_IDENTITY_FILE stays configured as a SECOND INSTRUMENT:
#      when the two disagree the resolved path is what gets verified, the
#      comparison is reported either way, and then the run goes RED (exit 5)
#      because the config is stale. Resolving is not licence to stop breaking
#      loudly — the python version is in this path, so an atrium image bump
#      moves it, and that used to fail the deploy. It still does; it now fails
#      having also told you whether the deploy itself was good.
#
#      Which interpreter is not a detail. Resolving with a python that is not
#      the service's is the same defect one level further down: an answer about
#      an environment nobody serves from.
#
#   2. ABSENCE IS A FAILURE, and says which kind. The old code folded an
#      unreadable path into the digest-mismatch branch and blamed the image for
#      not rebuilding, which is the wrong diagnosis for a path that is not
#      there. It stays FAILED — an image bump that moves the python version out
#      from under the configured path SHOULD break loudly rather than quietly
#      compare something else — but it now says so in its own words.
#
#   3. A CONFIGURED CHECK THAT DID NOT RUN IS A FAILURE. `SKIP identity check`
#      used to be followed, unconditionally, by `deploy VERIFIED BY CONTENT`.
#      A typo in the repo-side path was therefore a pass. If an identity check
#      is configured and cannot be made, nothing is verified by content.
#
# `--self-test` demonstrates all of it against stubbed ssh: a deliberate
# mismatch, a missing path, a path the app does not import, and an
# unresolvable module — each one shown FAILING, and the matching case shown
# passing. It touches no host and no container.

set -uo pipefail

conf=""; dir=$PWD
while [ "$dir" != "/" ]; do
  [ -f "$dir/.overnight.conf" ] && { conf="$dir/.overnight.conf"; break; }
  dir=$(dirname "$dir")
done
# `.overnight.conf` documents that "environment variables of the same name
# win", and a bare `set -a; . conf` does the exact opposite: it clobbers them.
# Nothing could override a configured value from the environment, which is also
# what made this script untestable without editing the committed config.
_env_before=$(env | grep '^OVERNIGHT_' || true)
# shellcheck disable=SC1090
[ -n "$conf" ] && set -a && . "$conf" && set +a
if [ -n "$_env_before" ]; then
  while IFS= read -r _kv; do [ -n "$_kv" ] && export "${_kv?}"; done <<<"$_env_before"
fi
unset _env_before _kv

: "${OVERNIGHT_DEPLOY_BRANCH:=testdrive}"
: "${OVERNIGHT_DEPLOY_KEY:=}"
: "${OVERNIGHT_COMPOSE_SERVICE:=}"
: "${OVERNIGHT_IDENTITY_FILE:=}"
: "${OVERNIGHT_IDENTITY_MODULE:=}"
: "${OVERNIGHT_IDENTITY_PYTHON:=python}"
: "${OVERNIGHT_IDENTITY_RESOLVE_STRICT:=1}"
: "${OVERNIGHT_MIGRATION_CMD:=}"

# sha256 of stdin, on whichever of the two spellings this box has.
local_sha256() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 | cut -d' ' -f1
  else sha256sum | cut -d' ' -f1; fi
}

# --- --self-test: show the identity check FAILING -------------------------
#
# A verifier that has only ever been seen passing proves nothing about itself:
# a check hardcoded to `exit 0` looks exactly like a check that works. This
# drives the real code path — the same file, re-invoked — with `ssh` stubbed,
# so it needs no host, no container and no network, and it asserts the exit
# code and the wording of each failure.
#
# The repo-side blob comes from HEAD and the configured OVERNIGHT_IDENTITY_FILE,
# so the self-test also incidentally asserts that the configured repo path
# exists in the commit under test.
self_test() {
  local tmp self ifile cpath rpath sha good bad pass=0 fail=0
  self=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")

  ifile=${OVERNIGHT_IDENTITY_FILE:-}
  [ -n "$ifile" ] || { echo "--self-test needs OVERNIGHT_IDENTITY_FILE configured"; return 1; }
  cpath=${ifile%%:*}; rpath=${ifile##*:}
  [ "$cpath" = "$rpath" ] && rpath=$ifile
  sha=$(git rev-parse HEAD) || return 1
  good=$(git cat-file blob "$sha:$rpath" 2>/dev/null | local_sha256)
  [ -n "$good" ] || { echo "--self-test: $rpath is not in HEAD"; return 1; }
  bad=$(printf 'one byte different' | local_sha256)

  tmp=$(mktemp -d -t deploy-verify-selftest-XXXX) || return 1
  cat >"$tmp/ssh" <<'STUB'
#!/usr/bin/env bash
# Stubbed ssh for `deploy-verify.sh --self-test`. The remote command is the
# last argument; it is answered from the STUB_* environment, never from a host.
cmd=${!#}
case "$cmd" in
  *"merge-base --is-ancestor"*) exit 0 ;;
  *find_spec*)
    [ -n "${STUB_RESOLVED:-}" ] || exit 1
    printf '%s\n' "$STUB_RESOLVED"; exit 0 ;;
  *sha256sum*)
    [ "${STUB_MISSING:-0}" = 1 ] && { echo "sha256sum: No such file or directory" >&2; exit 1; }
    printf '%s  -\n' "${STUB_DIGEST:-}"; exit 0 ;;
esac
exit 0
STUB
  chmod +x "$tmp/ssh"

  _case() {
    local name=$1 want_rc=$2 want_txt=$3; shift 3
    local out rc
    out=$(env "$@" PATH="$tmp:$PATH" OVERNIGHT_DEPLOY_HOST=stub OVERNIGHT_DEPLOY_PATH=/stub \
              OVERNIGHT_MIGRATION_CMD= OVERNIGHT_COMPOSE_SERVICE=api \
              bash "$self" HEAD --no-deploy 2>&1); rc=$?
    if [ "$rc" = "$want_rc" ] && printf '%s\n' "$out" | grep -q -- "$want_txt"; then
      pass=$((pass+1))
      printf '  PASS  %s\n          exit %s: %s\n' "$name" "$rc" \
        "$(printf '%s\n' "$out" | grep -m1 -- "$want_txt" | sed 's/^ *//')"
    else
      fail=$((fail+1))
      printf '  FAIL  %s\n          wanted exit %s and /%s/, got exit %s:\n%s\n' \
        "$name" "$want_rc" "$want_txt" "$rc" "$(printf '%s\n' "$out" | sed 's/^/          | /')"
    fi
  }

  echo "deploy-verify.sh --self-test  (ssh stubbed; no host, no container, no network)"
  echo "  identity file : $cpath"
  echo "  repo blob     : $rpath @ ${sha:0:9} = ${good:0:16}…"
  echo "  mismatch blob : ${bad:0:16}…"
  echo

  _case "deployed blob matches the commit" 0 "byte-identical" \
    OVERNIGHT_IDENTITY_FILE="$ifile" OVERNIGHT_IDENTITY_MODULE=atrium_ddns.router \
    STUB_RESOLVED="$cpath" STUB_DIGEST="$good"

  _case "DELIBERATE MISMATCH: deployed blob differs" 4 "differs — image" \
    OVERNIGHT_IDENTITY_FILE="$ifile" OVERNIGHT_IDENTITY_MODULE=atrium_ddns.router \
    STUB_RESOLVED="$cpath" STUB_DIGEST="$bad"

  # The #38 scenario itself: the config names /opt/host_app, the app imports
  # site-packages. Pre-#38 this passed while asserting about the wrong tree.
  _case "config names a path the app does not import" 5 "the config is stale" \
    OVERNIGHT_IDENTITY_FILE="/opt/host_app/src/atrium_ddns/router.py:$rpath" \
    OVERNIGHT_IDENTITY_MODULE=atrium_ddns.router \
    STUB_RESOLVED="$cpath" STUB_DIGEST="$good"

  _case "…the same drift, accepted deliberately (STRICT=0)" 0 "WARNING (stale config)" \
    OVERNIGHT_IDENTITY_FILE="/opt/host_app/src/atrium_ddns/router.py:$rpath" \
    OVERNIGHT_IDENTITY_MODULE=atrium_ddns.router OVERNIGHT_IDENTITY_RESOLVE_STRICT=0 \
    STUB_RESOLVED="$cpath" STUB_DIGEST="$good"

  _case "…and the imported tree has since diverged" 4 "differs — image" \
    OVERNIGHT_IDENTITY_FILE="/opt/host_app/src/atrium_ddns/router.py:$rpath" \
    OVERNIGHT_IDENTITY_MODULE=atrium_ddns.router \
    STUB_RESOLVED="$cpath" STUB_DIGEST="$bad"

  _case "identity path absent from the container" 4 "does not exist in the running container" \
    OVERNIGHT_IDENTITY_FILE="$ifile" OVERNIGHT_IDENTITY_MODULE=atrium_ddns.router \
    STUB_RESOLVED="$cpath" STUB_MISSING=1

  _case "module does not resolve in the container" 4 "does not resolve inside the running" \
    OVERNIGHT_IDENTITY_FILE="$ifile" OVERNIGHT_IDENTITY_MODULE=atrium_ddns.router \
    STUB_RESOLVED= STUB_DIGEST="$good"

  _case "repo-side path is not in the commit" 4 "cannot be made" \
    OVERNIGHT_IDENTITY_FILE="$cpath:backend/src/atrium_ddns/no_such_file.py" \
    OVERNIGHT_IDENTITY_MODULE=atrium_ddns.router \
    STUB_RESOLVED="$cpath" STUB_DIGEST="$good"

  _case "no module configured: trusted, not resolved" 0 "trusted, not resolved" \
    OVERNIGHT_IDENTITY_FILE="$ifile" OVERNIGHT_IDENTITY_MODULE= \
    STUB_DIGEST="$good"

  rm -rf "$tmp"
  echo
  echo "  $pass passed, $fail failed"
  [ "$fail" -eq 0 ]
}

[ "${1:-}" = "--self-test" ] && { self_test; exit $?; }

branch=${1:?usage: deploy-verify.sh <branch> [--no-deploy]}
do_deploy=1
[ "${2:-}" = "--no-deploy" ] && do_deploy=0

: "${OVERNIGHT_DEPLOY_HOST:?set OVERNIGHT_DEPLOY_HOST}"
: "${OVERNIGHT_DEPLOY_PATH:?set OVERNIGHT_DEPLOY_PATH}"

ssh_opts=(-o BatchMode=yes)
[ -n "$OVERNIGHT_DEPLOY_KEY" ] && ssh_opts+=(-i "$OVERNIGHT_DEPLOY_KEY" -o IdentitiesOnly=yes)
remote() { ssh "${ssh_opts[@]}" "$OVERNIGHT_DEPLOY_HOST" "cd $OVERNIGHT_DEPLOY_PATH && $*"; }

warnings=()

sha=$(git rev-parse "$branch") || exit 1
echo "deploying $branch @ ${sha:0:9} to \$OVERNIGHT_DEPLOY_HOST:$OVERNIGHT_DEPLOY_PATH"

# --- guard: does this branch contain what the host has already applied? ----
if [ -n "$OVERNIGHT_MIGRATION_CMD" ]; then
  applied=$(remote "$OVERNIGHT_MIGRATION_CMD" 2>/dev/null | tr -d '\r' | tail -1)
  echo "  host migration version: ${applied:-<none>}"
  if [ -n "$applied" ]; then
    token=$(echo "$applied" | grep -oE '[0-9a-z_]+' | head -1)
    if [ -n "$token" ] && ! git grep -q -- "$token" "$branch" 2>/dev/null; then
      echo "REFUSING: the host has '$token' applied and '$branch' does not contain it."
      echo "Deploying a branch behind the applied migration is how one service"
      echo "fails to start while every other container stays healthy."
      exit 2
    fi
    echo "  OK  branch contains the applied version"
  fi
fi

# --- ship: bundle, do not fetch -------------------------------------------
# `git fetch origin` on the host authenticates through the FORWARDED agent from
# the workstation, so the dependency the dedicated key was meant to remove is
# still there, one hop further along. It works right up until the agent locks,
# and an overnight run deploys many times. A bundle needs no credentials on the
# host at all.
#
# Bundle the WHOLE branch, not a range: an incremental bundle is rejected with
# "Repository lacks these prerequisite commits" whenever the host has not seen
# the base, which is most of the time.
if [ "$do_deploy" = 1 ]; then
  bundle=$(mktemp -t overnight-deploy-XXXX).bundle
  git bundle create "$bundle" "$branch" >/dev/null 2>&1 || { echo "bundle failed"; exit 1; }
  scp "${ssh_opts[@]}" -q "$bundle" "$OVERNIGHT_DEPLOY_HOST:/tmp/deploy.bundle" || exit 1
  rm -f "$bundle"

  # -f on the fetch: the host's copy of a re-pushed branch is otherwise a
  # non-fast-forward and is refused.
  remote "git fetch /tmp/deploy.bundle $branch:$branch -f" || exit 1
  remote "git checkout -B $OVERNIGHT_DEPLOY_BRANCH $branch" || exit 1

  # Never skip: catches interpolation before it becomes a crash-loop.
  remote "docker compose config >/dev/null" || { echo "compose config is invalid"; exit 1; }
  remote "docker compose up -d --build" || { echo "compose up reported failure"; exit 1; }
fi

# --- verify 1: ancestry ---------------------------------------------------
if remote "git merge-base --is-ancestor $sha HEAD"; then
  echo "  OK  ${sha:0:9} is an ancestor of the host HEAD"
else
  echo "FAILED: the host is not carrying ${sha:0:9}. The deploy may have exited 0 anyway."
  exit 3
fi

# --- verify 2: identity of the file the app IMPORTS ------------------------
if [ -n "$OVERNIGHT_COMPOSE_SERVICE" ] && [ -n "$OVERNIGHT_IDENTITY_FILE" ]; then
  cpath=${OVERNIGHT_IDENTITY_FILE%%:*}
  rpath=${OVERNIGHT_IDENTITY_FILE##*:}
  [ "$cpath" = "$rpath" ] && rpath=${OVERNIGHT_IDENTITY_FILE}

  # Resolve the container path through the import machinery, not the operator's
  # memory. `find_spec` is what `import` itself uses, so its answer is the file
  # the process loads by definition — no convention in between to drift.
  if [ -n "$OVERNIGHT_IDENTITY_MODULE" ]; then
    resolver="import importlib.util as u,sys; s=u.find_spec(sys.argv[1]); print(s.origin if s else '')"
    resolved=$(remote "docker compose exec -T $OVERNIGHT_COMPOSE_SERVICE \
                 $OVERNIGHT_IDENTITY_PYTHON -c \"$resolver\" $OVERNIGHT_IDENTITY_MODULE" 2>/dev/null \
                 | tr -d '\r' | grep -v '^$' | tail -1)
    if [ -z "$resolved" ]; then
      echo "FAILED: '$OVERNIGHT_IDENTITY_MODULE' does not resolve inside the running"
      echo "        container ($OVERNIGHT_IDENTITY_PYTHON in service '$OVERNIGHT_COMPOSE_SERVICE')."
      echo "The app cannot import what it is supposed to serve, or the interpreter"
      echo "named by OVERNIGHT_IDENTITY_PYTHON is not the one the service runs."
      exit 4
    fi
    if [ "$resolved" != "$cpath" ]; then
      w="OVERNIGHT_IDENTITY_FILE names $cpath but the app imports $resolved — verified the latter"
      warnings+=("$w")
      stale_config=1
      echo "  WARNING (stale config): $w"
      echo "        Two copies of the same source in one image is how a check"
      echo "        stays green about a tree nothing loads (#38). Update the config."
      cpath=$resolved
    else
      echo "  OK  $cpath is what '$OVERNIGHT_IDENTITY_MODULE' resolves to inside the container"
    fi
  else
    w="no OVERNIGHT_IDENTITY_MODULE — $cpath is trusted, not resolved"
    warnings+=("$w")
    echo "  NOTE $w"
  fi

  # Ask git whether the blob is there, rather than inferring it from an empty
  # digest. The old spelling piped a failed `cat-file` straight into the
  # hasher, so a missing repo path produced e3b0c442… — the sha256 of empty
  # input — and never the empty string its own guard tested for. The `SKIP
  # identity check` branch could therefore not fire; what actually happened
  # was a digest mismatch against nothing, reported as "the image did not
  # rebuild". Right verdict, wrong reason, and impossible to act on.
  if ! git cat-file -e "$sha:$rpath" 2>/dev/null; then
    echo "FAILED: an identity check is configured but cannot be made — $rpath"
    echo "        is not in ${sha:0:9}."
    echo "A check that did not run proves nothing, and this branch used to be"
    echo "unreachable: the digest of a missing blob is not the empty string."
    exit 4
  fi
  want=$(git cat-file blob "$sha:$rpath" | local_sha256)

  got=$(remote "docker compose exec -T $OVERNIGHT_COMPOSE_SERVICE sha256sum $cpath" 2>/dev/null \
          | awk '{print $1}' | tr -d '\r')
  if [ -z "$got" ]; then
    echo "FAILED: $cpath does not exist in the running container (or is unreadable)."
    echo "An image bump that moves this path breaks here, loudly, which is the"
    echo "intended behaviour: a loud break beats a silent comparison of the"
    echo "wrong tree. Re-point OVERNIGHT_IDENTITY_FILE at the new path."
    exit 4
  elif [ "$want" = "$got" ]; then
    echo "  OK  $cpath in the running container is byte-identical to the blob in ${sha:0:9}"
  else
    echo "FAILED: $cpath differs — image ${got} vs commit ${want}"
    echo "The host is on the right commit and running the wrong code: the image did not rebuild."
    exit 4
  fi

  # Config drift is red by default, AFTER the comparison has been made and
  # reported. Deliberately not a warning-inside-a-green-run: the state where
  # the configured path and the imported path disagree is exactly the state
  # nobody has looked at, and it is also what an atrium image bump produces
  # (the python version is in the path). Before this script resolved anything
  # that bump made `sha256sum` fail and the deploy report FAILED — loudly, on
  # purpose. Resolving must not quietly buy that loudness back; it buys a
  # correct comparison AND keeps the run red until the config is corrected.
  # OVERNIGHT_IDENTITY_RESOLVE_STRICT=0 downgrades it to a warning.
  if [ "${stale_config:-0}" = 1 ] && [ "$OVERNIGHT_IDENTITY_RESOLVE_STRICT" = 1 ]; then
    echo "FAILED: the identity assertion is correct but the config is stale."
    echo "        configured: $(printf '%s' "$OVERNIGHT_IDENTITY_FILE" | cut -d: -f1)"
    echo "        imported  : $cpath"
    echo "The deploy itself looks good — that path IS byte-identical to the commit."
    echo "Point OVERNIGHT_IDENTITY_FILE at the imported path (an image bump moves"
    echo "it), or set OVERNIGHT_IDENTITY_RESOLVE_STRICT=0 to accept the drift."
    exit 5
  fi
else
  echo "  NOTE no identity check configured — ancestry alone does not prove the"
  echo "       running image was rebuilt. Set OVERNIGHT_COMPOSE_SERVICE and"
  echo "       OVERNIGHT_IDENTITY_FILE, or verify something behavioural by hand."
fi

if [ ${#warnings[@]} -gt 0 ]; then
  echo "deploy VERIFIED BY CONTENT: $branch @ ${sha:0:9} — with ${#warnings[@]} warning(s):"
  printf '  ! %s\n' "${warnings[@]}"
else
  echo "deploy VERIFIED BY CONTENT: $branch @ ${sha:0:9}"
fi
