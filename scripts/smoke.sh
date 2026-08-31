#!/usr/bin/env bash
#
# Local smoke test — prove the stack is actually running, not just that
# `docker compose up` exited 0.
#
# Every check here exists because "the container is healthy" and "the app
# works" are different claims. Atrium's own HEALTHCHECK curls /api/healthz
# inside the container: it stays green through a host bundle that never
# loaded, a host router that failed to mount, and an unrun host migration.
#
#   ./scripts/smoke.sh                       # http://localhost:$API_HOST_PORT
#   ./scripts/smoke.sh --base http://host:8443
#   ./scripts/smoke.sh --user admin@example.com --pass 'secret'
#
# Exit 0 = every check passed. Exit 1 = at least one failed; each failure
# prints what it got, not just that it failed.
set -uo pipefail

cd -P "$(dirname "$0")/.." || exit 1

# The compose project this repo's Makefile pins. Left to compose, the name
# comes from the directory you invoked *from* — so a run through a symlink
# to this worktree addresses a different project than the one the stack was
# created under, finds no containers, and reports that as a failure of the
# stack rather than of the lookup. `cd -P` above resolves the symlink; this
# honours an explicit COMPOSE_PROJECT when make exports one.
COMPOSE_PROJECT="${COMPOSE_PROJECT:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g')}"
compose() { docker compose -p "$COMPOSE_PROJECT" "$@"; }

# Deliberately NOT `. ./.env`. Compose tolerates unquoted values containing
# spaces (WEBAUTHN_RP_NAME=Atrium DDNS); the shell does not, and sourcing it
# executes the second word. Read only the key we need.
env_get() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n1; }
API_HOST_PORT="${API_HOST_PORT:-$(env_get API_HOST_PORT)}"

BASE="http://localhost:${API_HOST_PORT:-8053}"
BASE_EXPLICIT=0
USER_EMAIL="${SMOKE_USER:-admin@example.com}"
USER_PASS="${SMOKE_PASS:-}"
SKIP_LOGIN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="$2"; BASE_EXPLICIT=1; shift 2 ;;
    --user) USER_EMAIL="$2"; shift 2 ;;
    --pass) USER_PASS="$2"; shift 2 ;;
    --no-login) SKIP_LOGIN=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

PASS=0; FAIL=0
COOKIE_JAR="$(mktemp -t ddns-smoke.XXXXXX)"
trap 'rm -f "$COOKIE_JAR"' EXIT

ok()   { printf '  \033[32mOK\033[0m      %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m    %s\n' "$1"; FAIL=$((FAIL+1)); }
info() { printf '  --      %s\n' "$1"; }

# check <name> <expected-substring> <curl args...>
# Empty expectation means "any 2xx will do".
check() {
  local name="$1" expect="$2"; shift 2
  local body code
  body="$(curl -sS -o - -w '\n%{http_code}' "$@" 2>&1)"
  code="$(printf '%s' "$body" | tail -n1)"
  body="$(printf '%s' "$body" | sed '$d')"
  if [ "$code" != "200" ]; then
    bad "$name — HTTP $code"
    [ -n "$body" ] && info "$(printf '%s' "$body" | head -c 200)"
    return 1
  fi
  if [ -n "$expect" ] && ! printf '%s' "$body" | grep -q "$expect"; then
    bad "$name — 200 but body lacks '$expect'"
    info "got: $(printf '%s' "$body" | head -c 200)"
    return 1
  fi
  ok "$name"
}

echo "smoke: $BASE"

# Reachability first. Every check below renders a connection refusal as an
# HTTP 000 with a domain-specific hint ("run make seed-bundle"), which sends
# the reader after the wrong problem. Answer "is anything listening?" once.
if ! curl -sS -o /dev/null --max-time 5 "$BASE/api/healthz" 2>/dev/null; then
  bad "cannot reach $BASE — nothing is listening, or the port is wrong"
  info "local stack: make up   |   check API_HOST_PORT in .env"
  echo
  printf 'smoke: %d passed, %d failed\n' "$PASS" "$FAIL"
  exit 1
fi

# --- liveness ------------------------------------------------------------
# healthz is the process; readyz is the process *plus its database*. A stack
# whose MySQL never came up answers the first and not the second.
check "GET /api/healthz"  ""        "$BASE/api/healthz"
check "GET /api/readyz"   '"ready"' "$BASE/api/readyz"

# --- the SPA and the host bundle ----------------------------------------
# The bundle is the single most common silent failure: it is served from the
# image, but the SPA only loads it if system.host_bundle_url points at it.
# Both halves are checked — the file existing proves nothing about whether
# anything asks for it.
check "GET / (SPA shell)" "<div id=\"root\"" "$BASE/"

bundle_code="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/host/main.js")"
bundle_type="$(curl -sSI "$BASE/host/main.js" | tr -d '\r' | awk -F': ' 'tolower($1)=="content-type"{print $2}')"
if [ "$bundle_code" = "200" ] && printf '%s' "$bundle_type" | grep -qi 'javascript'; then
  ok "GET /host/main.js — 200, $bundle_type"
else
  bad "GET /host/main.js — HTTP $bundle_code, type '${bundle_type:-none}'"
  info "run: make seed-bundle   (and check the bundle landed in the image)"
fi

# Is the SPA actually pointed at it? app-config is public pre-login.
if curl -sS "$BASE/api/app-config" | grep -q 'host_bundle_url'; then
  ok "app-config advertises host_bundle_url"
else
  bad "app-config has no host_bundle_url — the bundle exists but nothing loads it"
  info "run: make seed-bundle"
fi

# --- the host router is mounted and gated -------------------------------
# 401 here is the PASS. A 404 means init_app never mounted the router; a 200
# means an authenticated-only endpoint is answering anonymously, which is
# worse than either.
anon_code="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/atrium_ddns/state")"
case "$anon_code" in
  401) ok  "GET /api/atrium_ddns/state — 401 anonymous (router mounted, auth enforced)" ;;
  404) bad "GET /api/atrium_ddns/state — 404: host router not mounted (check ATRIUM_HOST_MODULE and init_app)" ;;
  200) bad "GET /api/atrium_ddns/state — 200 anonymous: AUTH NOT ENFORCED" ;;
  *)   bad "GET /api/atrium_ddns/state — unexpected HTTP $anon_code" ;;
esac

# --- authenticated round trip -------------------------------------------
if [ "$SKIP_LOGIN" = "1" ] || [ -z "$USER_PASS" ]; then
  info "login checks skipped (pass --pass, or --no-login to silence)"
else
  login_code="$(curl -sS -o /dev/null -w '%{http_code}' -c "$COOKIE_JAR" \
    -X POST "$BASE/api/auth/jwt/login" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode "username=$USER_EMAIL" \
    --data-urlencode "password=$USER_PASS")"
  # 204/200 = in. 202-ish or a 2FA redirect means the account has TOTP on,
  # which is a legitimate state, not a failure of the stack.
  if [ "$login_code" = "204" ] || [ "$login_code" = "200" ]; then
    ok "POST /api/auth/jwt/login — $login_code"
    check "GET /api/users/me/context (cookie)" "$USER_EMAIL" \
      -b "$COOKIE_JAR" "$BASE/api/users/me/context"
    check "GET /api/atrium_ddns/state (cookie)" '"counter"' \
      -b "$COOKIE_JAR" "$BASE/api/atrium_ddns/state"
  else
    bad "POST /api/auth/jwt/login — HTTP $login_code"
    info "2FA-enabled accounts do not complete here; use --no-login"
  fi
fi

# --- migrations ----------------------------------------------------------
# Both chains, read from the database rather than from a migration tool's
# exit code. An unrun host chain is invisible until the first query fails.
# These read the LOCAL compose stack via `docker compose exec`, so they are
# only meaningful when --base points at that same stack.
#
# Gating on the URL *looking* local is not enough, and the first version of
# this did exactly that. Smoke-testing a deployed host through an ssh tunnel
# (`-L 18443:localhost:8443`) gives a base of http://localhost:18443, which
# matched the "is it local" test — so the script read the local stack's
# alembic revisions and printed them under a run aimed at the remote. Both
# happened to be on the same revisions, so it rendered as two green checks
# confirming a deployment it had never looked at.
#
# So: any explicit --base disables them. The checks only run in the default
# no-argument case, which is the one situation where the compose stack and
# the base URL are provably the same thing.
if [ "$BASE_EXPLICIT" = "1" ]; then
  info "migration checks skipped — --base was given, so this may not be the local stack"
  info "on the target: docker compose exec -T api alembic current"
elif command -v docker >/dev/null 2>&1 && compose ps -q api >/dev/null 2>&1; then
  for chain in alembic_version alembic_version_app; do
    rev="$(compose exec -T api /opt/venv/bin/python -c "
import asyncio, sqlalchemy as sa
from app.db import get_session_factory
async def main():
    async with get_session_factory()() as s:
        r = await s.execute(sa.text('SELECT version_num FROM $chain'))
        print(r.scalar_one_or_none() or 'EMPTY')
asyncio.run(main())
" 2>/dev/null | tr -d '\r' | tail -n1)"
    if [ -n "$rev" ] && [ "$rev" != "EMPTY" ]; then
      ok "$chain at $rev"
    else
      bad "$chain is empty or unreadable — run: make migrate"
    fi
  done
else
  info "migration checks skipped (no local compose stack)"
fi

# --- TODO: /nic/* -------------------------------------------------------
# The DynDNS endpoints do not exist yet. When they land, the protocol
# conformance table (tests/compat/) is the real check; this file only needs
# to prove /nic/checkip answers, so a deploy that mounted no DDNS router
# fails here rather than in a router's logs three days later.

echo
printf 'smoke: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
