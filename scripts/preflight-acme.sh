#!/usr/bin/env bash
#
# Refuse to start the TLS terminator with a configuration that would fail
# silently, and say which variable is wrong.
#
#   scripts/preflight-acme.sh            # check, print a summary, exit non-zero
#   scripts/preflight-acme.sh --quiet    # same, but only speak up on failure
#
# ---------------------------------------------------------------------------
# Why this is a script and not `${VAR:?}` in compose.yaml
# ---------------------------------------------------------------------------
#
# The hand-over overlay this replaced could use Compose's own `${VAR:?...}`,
# which refuses to load the file at all when a variable is missing. The base
# `compose.yaml` cannot: Compose interpolates the whole document at load time,
# INCLUDING services whose profile is not active — measured, `docker compose
# config` on a file with an inactive-profile `${X:?}` fails — so a `:?` on the
# proxy would break `make up` for every developer who never runs the proxy.
#
# So the guard moved here, and `make tls-up` runs it first. It is strictly more
# than Compose could say anyway: Compose can only see that a variable is empty,
# while this can see that the CA server is the staging one, that the hostname
# is still the shipped placeholder, or that something else already holds the
# port the HTTP-01 challenge needs.
#
# It is not the only line of defence. An empty TRAEFIK_HOSTNAME renders the
# router rule as Host(``), which Traefik rejects outright — an operator who
# runs `docker compose` by hand and skips this still does not get a stack that
# quietly serves nothing.
set -uo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

PROD_CA=https://acme-v02.api.letsencrypt.org/directory
STAGING_CA=https://acme-staging-v02.api.letsencrypt.org/directory
# The values .env.example ships. They are RFC 2606 reserved names, so they can
# never be anybody's, and a stack still carrying them was never configured.
PLACEHOLDER_HOST=ddns.example.invalid
PLACEHOLDER_MAIL=ops@example.invalid

FAIL=0
say()  { [ "$QUIET" = 1 ] || printf '%s\n' "$1"; }
ok()   { say "  ok    $1"; }
warn() { say "  warn  $1"; }
# One call is one problem, however many lines it takes to explain it: the
# summary counts problems, and a count that tracked lines would inflate with
# every sentence added to an explanation.
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1" >&2; shift
         for l in "$@"; do printf '        %s\n' "$l" >&2; done; }

# --------------------------------------------------------------------------
# Read .env the way Compose does — as data. `source` would execute it, and a
# file that holds a database password should never be a file this runs.
# --------------------------------------------------------------------------
# ENV_FILE exists for the gate test, which has to ask "what happens when this
# variable is absent" without a developer's real .env answering on its behalf.
ENV_FILE=${ENV_FILE:-$REPO/.env}

envfile_get() { # envfile_get <KEY>
  [ -f "$ENV_FILE" ] || return 1
  python3 - "$ENV_FILE" "$1" <<'PY'
import sys
key = sys.argv[2]
for raw in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    if k.strip() != key:
        continue
    v = v.strip()
    # Compose strips one layer of matching quotes and nothing else.
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    print(v)
    break
PY
}

# The environment wins over .env, exactly as Compose resolves it.
resolve() { # resolve <KEY>
  local key=$1 val
  val=${!key-}
  if [ -z "$val" ]; then val=$(envfile_get "$key" 2>/dev/null); fi
  printf '%s' "$val"
}

HOSTNAME_V=$(resolve TRAEFIK_HOSTNAME)
EMAIL_V=$(resolve LETSENCRYPT_EMAIL)
CASERVER_V=$(resolve LETSENCRYPT_CASERVER)
STORE_V=$(resolve ACME_STORE_DIR); STORE_V=${STORE_V:-./letsencrypt}
HTTP_V=$(resolve ACME_HTTP_PUBLISH);  HTTP_V=${HTTP_V:-80}
HTTPS_V=$(resolve ACME_HTTPS_PUBLISH); HTTPS_V=${HTTPS_V:-443}

say "ACME preflight"

# --- the hostname ---------------------------------------------------------
if [ -z "$HOSTNAME_V" ]; then
  bad "TRAEFIK_HOSTNAME is not set. It becomes Traefik's router rule and is" \
      "where ACME finds the domain to ask for. Unset, the rule renders as" \
      "Host(\`\`), the router is rejected, and nothing is served. Set it in .env."
elif [ "$HOSTNAME_V" = "$PLACEHOLDER_HOST" ]; then
  bad "TRAEFIK_HOSTNAME is still the .env.example placeholder ($PLACEHOLDER_HOST)." \
      ".invalid can never resolve, so HTTP-01 can never complete."
elif ! printf '%s' "$HOSTNAME_V" | grep -qE '^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$'; then
  bad "TRAEFIK_HOSTNAME=$HOSTNAME_V is not a hostname. Name one host; no scheme," \
      "no port, no path, no wildcard — HTTP-01 cannot answer for a wildcard."
else
  ok "TRAEFIK_HOSTNAME  $HOSTNAME_V"
fi

# --- the contact ----------------------------------------------------------
if [ -z "$EMAIL_V" ]; then
  bad "LETSENCRYPT_EMAIL is not set. Let's Encrypt validates the contact address" \
      "before it looks at the order, so an empty one fails registration."
elif [ "$EMAIL_V" = "$PLACEHOLDER_MAIL" ]; then
  bad "LETSENCRYPT_EMAIL is still the .env.example placeholder ($PLACEHOLDER_MAIL)." \
      "Let's Encrypt refuses .invalid: \"Domain name does not end with a" \
      "valid public suffix\". Registration never happens."
elif ! printf '%s' "$EMAIL_V" | grep -qE '^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{2,}$'; then
  bad "LETSENCRYPT_EMAIL=$EMAIL_V is not an address Let's Encrypt will accept."
else
  ok "LETSENCRYPT_EMAIL $EMAIL_V"
fi

# --- the directory, and the one that used to be silent --------------------
case "$CASERVER_V" in
  "")            ok "LETSENCRYPT_CASERVER unset — defaulting to PRODUCTION ($PROD_CA)" ;;
  "$PROD_CA")    ok "LETSENCRYPT_CASERVER  PRODUCTION" ;;
  "$STAGING_CA")
    # Not a failure: rehearsals are supposed to point here. But with no
    # fallback certificate underneath any more, a staging certificate is the
    # only thing on the wire, and every browser will reject it.
    warn "LETSENCRYPT_CASERVER  STAGING — certificates from this directory are"
    warn "untrusted by every client. Correct for a rehearsal, wrong for a"
    warn "stack that serves anyone." ;;
  *)             warn "LETSENCRYPT_CASERVER  $CASERVER_V (neither Let's Encrypt directory)" ;;
esac

# --- the store ------------------------------------------------------------
# Traefik creates acme.json itself, so a missing file is normal and means "this
# stack has not issued yet". A missing *directory* is normal too — Docker makes
# it. What is not normal is a store that is not ours to write.
store_abs=$STORE_V
case "$store_abs" in /*) ;; *) store_abs="$REPO/${store_abs#./}" ;; esac
if [ -e "$store_abs/acme.json" ]; then
  if [ -w "$store_abs/acme.json" ]; then
    ok "ACME store        $STORE_V/acme.json (exists, writable)"
  else
    bad "$STORE_V/acme.json is not writable. Traefik cannot record an issuance" \
        "it has already paid a rate limit for."
  fi
elif [ -d "$store_abs" ] && [ ! -w "$store_abs" ]; then
  bad "$STORE_V is not writable, so no store can be created in it."
else
  ok "ACME store        $STORE_V/acme.json (will be created on first issuance)"
fi

# --- the port the challenge is answered on --------------------------------
# HTTP-01 is answered on the `web` entrypoint, published as $HTTP_V. If
# something else already holds it, Traefik will not start and the failure looks
# like a Docker error rather than a TLS one.
holder=$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null \
         | grep -E "(^|[^0-9]):$HTTP_V->" | awk '{print $1}' | grep -v -- '-proxy-1$')
if [ -n "$holder" ]; then
  bad "port $HTTP_V is published by another container: $(printf '%s' "$holder" | tr '\n' ' ')" \
      "HTTP-01 is answered there. Stop it, or set ACME_HTTP_PUBLISH."
else
  ok "port $HTTP_V         free for the HTTP-01 challenge"
  ok "port $HTTPS_V        for TLS"
fi

if [ "$FAIL" -eq 0 ]; then
  say "  — nothing blocking; the proxy will issue and renew on its own."
  exit 0
fi
printf '\nRefusing to start the TLS terminator: %d problem(s) above.\n' "$FAIL" >&2
printf 'See the ACME section of .env.example.\n' >&2
exit 1
