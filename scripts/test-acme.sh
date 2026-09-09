#!/usr/bin/env bash
#
# Gate test for automatic certificate issuance and renewal.
#
#   scripts/test-acme.sh                # hermetic; no network
#   scripts/test-acme.sh --live-staging # adds one real exercise against
#                                       # Let's Encrypt STAGING
#
# ---------------------------------------------------------------------------
# What this proves, and what it deliberately does not touch
# ---------------------------------------------------------------------------
#
# It stands the **shipped** proxy service up — `compose.yaml`, through
# `docker compose`, with only the variables that exist for the purpose
# overridden — against ACME stores built here from `openssl`. It never contacts
# Let's Encrypt production and never binds 80 or 443.
#
# Everything it asserts about Traefik's behaviour has a control beside it, so
# no assertion can pass by being unreachable:
#
#   | phase | asserts                              | control that makes it bite |
#   |-------|--------------------------------------|----------------------------|
#   | 0     | a misconfigured stack is refused     | the same check with good
#   |       |                                      | values, which passes       |
#   | 1     | the rendered service is the ACME one | —                          |
#   | A     | an empty store issues, unprompted    | B, where it does not       |
#   | B     | a defaultCertificate SUPPRESSES that | A, where the same stack
#   |       | — the regression, kept measured      | does call the CA           |
#   | C     | renewal fires from the store's timer | A, a different trigger     |
#   | D     | issuance completes, store digest     | A/B/C, where the digest
#   |       | moves, the new cert reaches the wire | never moves                |
#   | E     | this configuration reaches the real  | the unroutable CA, which
#   |       | Let's Encrypt                        | fails at the socket        |
#
# Phase B is the important one to keep. The previous design shipped a
# `defaultCertificate` fallback and paid for it with silently suppressed
# issuance; this design drops the fallback for that reason. An assertion that
# the file simply lacks a block (phase 1) is a grep and would survive someone
# re-adding it with a good rationale. Phase B measures the *consequence*, so
# the cost of re-adding it is visible in the same run.
#
# Phase D runs against `pebble`, Let's Encrypt's own test CA, on a private
# docker network — no external endpoint at all. Phase E is the only one that
# leaves the machine, it goes to **staging**, and it is off unless
# `--live-staging` is passed.
#
set -uo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LIVE_STAGING=0
[ "${1:-}" = "--live-staging" ] && LIVE_STAGING=1

# A documentation domain (RFC 2606 reserves .invalid). Nothing here resolves
# and nothing here is a real name.
DOMAIN=acme.example.invalid
EMAIL=ops@example.invalid
# Let's Encrypt validates the *contact address* before it looks at anything
# else, and it rejects `.invalid` outright ("contact email has invalid domain:
# Domain name does not end with a valid public suffix"). Measured — the first
# version of the live phase used the address above and never got as far as
# registering. RFC 2606 reserves example.com for exactly this: a name with a
# real TLD that can never belong to anyone.
STAGING_EMAIL=ops@example.com
# Unroutable by RFC 1122; the connection is refused immediately, so "did
# Traefik try to talk to a CA" becomes a question the log answers in under a
# second rather than after a 2-minute client timeout.
BLACKHOLE_CA=https://127.0.0.1:1/directory
STAGING_CA=https://acme-staging-v02.api.letsencrypt.org/directory
PROD_CA=https://acme-v02.api.letsencrypt.org/directory
# Let's Encrypt's own test CA, run locally. Pinned to `latest` deliberately:
# it is a disposable CA on a private network for the length of one phase, it
# issues nothing anything trusts, and a stale pin here would be a maintenance
# burden with no security value. Nothing in the repository depends on it.
PEBBLE_IMAGE=ghcr.io/letsencrypt/pebble:latest

PROJECT="acme-test-$$"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/acme-test.XXXXXX") || exit 1

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
# Every check names both readings. "no match found" is a check that gets
# deleted rather than investigated the next time it fires.
cmp_eq() { # cmp_eq <label> <actual> <expected>
  if [ "$2" = "$3" ]; then ok "$1 — $2"; else bad "$1: actual=$2 expected=$3"; fi
}
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

cleanup() {
  compose down --remove-orphans >/dev/null 2>&1
  docker rm -f "$PROJECT-control" "$PROJECT-issue" "$PROJECT-ca" >/dev/null 2>&1
  docker network rm "$PROJECT-net" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

sha256() { python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }
mtime()  { python3 -c 'import os,sys;print(os.stat(sys.argv[1]).st_mtime_ns)' "$1"; }
fp()     { openssl x509 -in "$1" -noout -fingerprint -sha256 | cut -d= -f2; }

# --------------------------------------------------------------------------
# A self-signed certificate on disk, for the phase B control only. Nothing the
# shipped configuration reads.
# --------------------------------------------------------------------------
make_cert() { # make_cert <dir> <days>
  mkdir -p "$1"
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$1/key.pem" -out "$1/cert.pem" \
    -days "$2" -subj "/CN=$DOMAIN" -addext "subjectAltName=DNS:$DOMAIN" \
    -addext "basicConstraints=critical,CA:FALSE" >/dev/null 2>&1
}

# --------------------------------------------------------------------------
# A Traefik ACME store, built from scratch. `--no-certs` makes the store an
# account with no certificate in it — which is what a stack that has never
# issued looks like, and the starting state for phases A and D.
# --------------------------------------------------------------------------
make_store() { # make_store <path> <days> [--no-certs]
  python3 - "$1" "$DOMAIN" "$2" "${3:-}" <<'PY'
import base64, json, os, pathlib, subprocess, sys, tempfile

out, domain, days, flag = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
out.parent.mkdir(parents=True, exist_ok=True)
tmp = pathlib.Path(tempfile.mkdtemp())
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", tmp / "k.pem", "-out", tmp / "c.pem", "-days", days,
                "-subj", f"/CN={domain}",
                "-addext", f"subjectAltName=DNS:{domain}",
                # CA:FALSE is load-bearing, not tidiness. `openssl req -x509`
                # marks its output a CA by default, and lego refuses to renew
                # a stored certificate that is one — "certificate bundle
                # starts with a CA certificate". A store built without this
                # line makes the renewal phase fail for a reason that has
                # nothing to do with the configuration under test.
                "-addext", "basicConstraints=critical,CA:FALSE"],
               check=True, capture_output=True)
subprocess.run(["openssl", "genrsa", "-out", tmp / "acct.pem", "2048"],
               check=True, capture_output=True)

certs = []
if flag != "--no-certs":
    certs = [{"domain": {"main": domain},
              "certificate": base64.b64encode((tmp / "c.pem").read_bytes()).decode(),
              "key": base64.b64encode((tmp / "k.pem").read_bytes()).decode(),
              "Store": "default"}]

out.write_text(json.dumps({"letsencrypt": {
    "Account": {
        "Email": "ops@example.invalid",
        # A registration URI pointing at a CA this test never uses. Traefik
        # notices ("Account URI does not match the current CAServer") and
        # resets the account, which is what any pre-existing store looks like
        # the first time a differently-configured Traefik opens it.
        "Registration": {"body": {"status": "valid"},
                         "uri": "https://acme.example.invalid/acct/0"},
        "PrivateKey": base64.b64encode((tmp / "acct.pem").read_bytes()).decode(),
        "KeyType": "4096"},
    "Certificates": certs}}))
os.chmod(out, 0o600)
# The certificate this store holds, so the caller can compare fingerprints.
if certs:
    print((tmp / "c.pem").read_text(), end="")
PY
}

compose() {
  TRAEFIK_HOSTNAME="$DOMAIN" \
  LETSENCRYPT_EMAIL="${ACME_EMAIL:-$EMAIL}" \
  LETSENCRYPT_CASERVER="${CASERVER:-$BLACKHOLE_CA}" \
  ACME_HTTP_PUBLISH=127.0.0.1:0 \
  ACME_HTTPS_PUBLISH=127.0.0.1:0 \
  ACME_STORE_DIR="$WORK/letsencrypt" \
  docker compose -p "$PROJECT" -f "$REPO/compose.yaml" --profile tls "$@"
}

start_proxy() { # start_proxy — brings the SHIPPED service up and waits for :443
  compose up -d --no-deps proxy >/dev/null 2>&1 || return 1
  HTTPS_PORT=""
  for _ in $(seq 1 40); do
    HTTPS_PORT=$(docker port "$PROJECT-proxy-1" 443/tcp 2>/dev/null | head -1 | sed 's/.*://')
    [ -n "$HTTPS_PORT" ] && \
      echo | openssl s_client -connect "127.0.0.1:$HTTPS_PORT" -servername "$DOMAIN" \
        >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  return 1
}

served_fp() { # the SHA-256 fingerprint of the certificate the proxy presents
  echo | openssl s_client -connect "127.0.0.1:$HTTPS_PORT" -servername "$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2
}
served_issuer() {
  echo | openssl s_client -connect "127.0.0.1:$HTTPS_PORT" -servername "$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -issuer 2>/dev/null
}

proxy_log() { docker logs "$PROJECT-proxy-1" 2>&1; }
count_in_log() { proxy_log | grep -c "$1"; }

# The command line the shipped service renders, read out of `docker compose
# config` rather than retyped, so a control container differs from the real one
# only in the things it is meant to differ in.
shipped_args() { printf '%s' "$CFG" | python3 -c 'import json,sys;print("\n".join(json.load(sys.stdin)["services"]["proxy"]["command"]))'; }

# ==========================================================================
echo "ACME gate test"
echo "  repo:          $REPO"
echo "  work dir:      $WORK"
echo "  compose files: compose.yaml (the shipped service, no overlay)"
echo "  hermetic CA:   $BLACKHOLE_CA (unroutable — refuses instantly)"
echo "  phase D:       $PEBBLE_IMAGE on a private network"
if [ "$LIVE_STAGING" = 1 ]; then
  echo "  phase E:       ENABLED, against $STAGING_CA"
else
  echo "  phase E:       skipped (pass --live-staging to run it)"
fi

# --------------------------------------------------------------------------
head_ "0. A misconfigured stack is refused, and the message names the variable"
# --------------------------------------------------------------------------
# The guard cannot be Compose's own `${VAR:?}`. Compose interpolates the whole
# document at load time, INCLUDING services whose profile is not active, so a
# `:?` on the proxy would break `make up` for every developer who never runs
# it. That is asserted below, because it is the reason the guard is a script.
PRE="$REPO/scripts/preflight-acme.sh"
for v in TRAEFIK_HOSTNAME LETSENCRYPT_EMAIL; do
  # ENV_FILE=/dev/null so a developer's real .env cannot supply the variable
  # this case is about removing — without it the test passes or fails
  # depending on whose machine it runs on.
  out=$( ( export TRAEFIK_HOSTNAME="$DOMAIN" LETSENCRYPT_EMAIL="$EMAIL"
           unset "$v"
           ENV_FILE=/dev/null "$PRE" ) 2>&1 )
  if printf '%s' "$out" | grep -q "FAIL.*$v"; then
    ok "unset $v — refused, and the message names it"
  else
    bad "unset $v — not refused: $(printf '%s' "$out" | tail -1)"
  fi
done
# The placeholders .env.example ships are a configuration nobody chose.
out=$(ENV_FILE=/dev/null TRAEFIK_HOSTNAME=ddns.example.invalid LETSENCRYPT_EMAIL=ops@example.invalid "$PRE" 2>&1)
if printf '%s' "$out" | grep -q "FAIL.*placeholder"; then
  ok "the shipped .env.example placeholders are refused as placeholders"
else
  bad "the .env.example placeholders were accepted"
fi
# The control: the same check, good values, must pass. Without this the two
# assertions above would also pass against a script that refused everything.
if ENV_FILE=/dev/null TRAEFIK_HOSTNAME=ddns.example.com LETSENCRYPT_EMAIL=ops@example.com "$PRE" >/dev/null 2>&1; then
  ok "CONTROL: good values pass, so the refusals above are selective"
else
  bad "CONTROL: the preflight refuses even a good configuration"
fi
# Staging is a warning, not a refusal — rehearsals point there on purpose.
out=$(ENV_FILE=/dev/null TRAEFIK_HOSTNAME=ddns.example.com LETSENCRYPT_EMAIL=ops@example.com \
      LETSENCRYPT_CASERVER="$STAGING_CA" "$PRE" 2>&1)
if [ $? -eq 0 ] && printf '%s' "$out" | grep -q "warn.*STAGING"; then
  ok "staging is warned about and allowed"
else
  bad "staging handling is wrong: $(printf '%s' "$out" | grep -i staging | head -1)"
fi

# The base file must still load for everybody who never runs the proxy.
if ( unset TRAEFIK_HOSTNAME LETSENCRYPT_EMAIL LETSENCRYPT_CASERVER
     docker compose -f "$REPO/compose.yaml" config >/dev/null 2>&1 ); then
  ok "compose.yaml loads with none of the ACME variables set (dev is unaffected)"
else
  bad "compose.yaml no longer loads unconfigured — a \${VAR:?} leaked into it"
fi

# --------------------------------------------------------------------------
head_ "1. The rendered service is the ACME arrangement"
# --------------------------------------------------------------------------
CFG=$(CASERVER="$BLACKHOLE_CA" compose config --format json 2>/dev/null)
render() { printf '%s' "$CFG" | python3 -c "$1" 2>/dev/null; }

CMD=$(shipped_args)
for arg in \
  "--entrypoints.web.address=:80" \
  "--entrypoints.websecure.address=:443" \
  "--entrypoints.web.http.redirections.entrypoint.to=websecure" \
  "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json" \
  "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web" \
  "--certificatesresolvers.letsencrypt.acme.email=$EMAIL" \
  "--certificatesresolvers.letsencrypt.acme.caserver=$BLACKHOLE_CA" \
  "--accesslog.fields.queryparameters.defaultmode=drop" ; do
  if printf '%s\n' "$CMD" | grep -qxF -- "$arg"; then ok "command carries $arg"
  else bad "command is missing $arg"; fi
done
if printf '%s\n' "$CMD" | grep -q "8443"; then
  bad "command still mentions 8443 — the borrowed-certificate entrypoint is back"
else
  ok "command no longer mentions 8443"
fi

# Unset, the CA server must default to PRODUCTION. This is the one default
# that used to be a silent coin toss between two directories.
DEF_CFG=$( unset LETSENCRYPT_CASERVER
           TRAEFIK_HOSTNAME="$DOMAIN" LETSENCRYPT_EMAIL="$EMAIL" \
           ACME_STORE_DIR="$WORK/letsencrypt" \
           docker compose -p "$PROJECT" -f "$REPO/compose.yaml" --profile tls \
             config --format json 2>/dev/null )
if printf '%s' "$DEF_CFG" | python3 -c 'import json,sys;print("\n".join(json.load(sys.stdin)["services"]["proxy"]["command"]))' 2>/dev/null \
     | grep -qxF -- "--certificatesresolvers.letsencrypt.acme.caserver=$PROD_CA"; then
  ok "an unset LETSENCRYPT_CASERVER renders the PRODUCTION directory"
else
  bad "an unset LETSENCRYPT_CASERVER does not render production"
fi

PORTS=$(render 'import json,sys;print(" ".join(str(p["target"]) for p in json.load(sys.stdin)["services"]["proxy"]["ports"]))')
cmp_eq "published container ports" "$PORTS" "80 443"

VOLS=$(render 'import json,sys;print("\n".join("%s ro=%s" % (v["target"], v.get("read_only", False)) for v in json.load(sys.stdin)["services"]["proxy"]["volumes"]))')
if printf '%s\n' "$VOLS" | grep -qx "/letsencrypt ro=False"; then
  ok "/letsencrypt is mounted read-WRITE — this stack owns its own store"
else
  bad "/letsencrypt mount wrong: $(printf '%s' "$VOLS" | tr '\n' ' ')"
fi
if printf '%s\n' "$VOLS" | grep -q "^/certs "; then
  bad "/certs is still mounted — the borrowed certificate is back"
else
  ok "no /certs mount: there is no second copy of the certificate anywhere"
fi

DYN="$REPO/infra/traefik/dynamic.yml"
# Strip comments before asserting on content. dynamic.yml explains at length
# why it has no defaultCertificate, so a naive grep for the word matches the
# explanation and reports the opposite of the truth. (It did, once.)
DYN_CODE="$WORK/dynamic.code.yml"
grep -vE '^[[:space:]]*#' "$DYN" > "$DYN_CODE"
SRCS=$(render 'import json,sys;print(" ".join(v["source"] for v in json.load(sys.stdin)["services"]["proxy"]["volumes"]))')
case "$SRCS" in
  *"/dynamic.yml"*) ok "the dynamic file is mounted" ;;
  *) bad "dynamic.yml is not mounted: $SRCS" ;;
esac
grep -q 'certResolver: letsencrypt' "$DYN_CODE" \
  && ok "dynamic.yml: the router asks the resolver for a certificate" \
  || bad "dynamic.yml: no certResolver on the router"
# Phase B measures what this block would cost. This is the cheap check.
grep -q 'defaultCertificate' "$DYN_CODE" \
  && bad "dynamic.yml: a defaultCertificate is back — see phase B for what it costs" \
  || ok "dynamic.yml: no defaultCertificate, so issuance is not suppressed"
grep -qE '^\s*certFile:' "$DYN_CODE" \
  && bad "dynamic.yml: a certificate file is configured; ACME is not the only source" \
  || ok "dynamic.yml: no certificate file — ACME is the only source"
grep -q '{{ env "TRAEFIK_HOSTNAME" }}' "$DYN_CODE" \
  && ok "dynamic.yml: the hostname comes from the environment" \
  || bad "dynamic.yml: the hostname is not templated"
# The one disclosure rule this repository has no slack on.
if grep -E '^\s*rule:' "$DYN_CODE" | grep -qE '[A-Za-z0-9-]+\.[A-Za-z]{2,}'; then
  bad "dynamic.yml: the router rule contains a literal domain name"
else
  ok "dynamic.yml: no literal domain name in the router rule"
fi

# --------------------------------------------------------------------------
head_ "A. A fresh stack issues: an empty store calls the CA unprompted"
# --------------------------------------------------------------------------
# This is the whole point of the arrangement. No fallback, no copied store, no
# operator step — the stack comes up, finds it has no certificate for the
# domain in its router rule, and goes and asks for one.
mkdir -p "$WORK/letsencrypt"
make_store "$WORK/letsencrypt/acme.json" 90 --no-certs >/dev/null
STORE_SHA_BEFORE=$(sha256 "$WORK/letsencrypt/acme.json")

if start_proxy; then ok "the shipped proxy service came up and answers TLS"
else bad "the shipped proxy service did not answer TLS"; fi
sleep 8

ATTEMPTS=$(count_in_log 'Unable to obtain ACME certificate')
if [ "$ATTEMPTS" -ge 1 ]; then
  ok "the CA was contacted without being asked ($ATTEMPTS attempt(s), refused by the blackhole)"
else
  bad "no issuance attempt from an empty store — a fresh stack would never get a certificate"
fi
# "Building ACME client" is DEBUG-level; at the INFO level this stack ships,
# the provider announcing itself is the observable equivalent. Measured against
# traefik:3.7 rather than assumed — the first version of this check looked for
# the DEBUG string and failed against a working stack.
if proxy_log | grep -q 'Starting provider .acme.Provider'; then
  ok "the ACME provider started"
else
  bad "the ACME provider never started"
fi
# Until issuance succeeds Traefik serves its own self-signed default, so TLS
# terminates throughout. That is a degraded state, not an outage, and it is the
# cost of dropping the fallback.
case "$(served_issuer)" in
  *TRAEFIK*|*Traefik*) ok "before issuance the wire shows Traefik's self-signed default" ;;
  *) bad "unexpected issuer before issuance: $(served_issuer)" ;;
esac
# The two verification targets are NOT redundant, and this is where that shows:
# a self-signed default passes a chain-and-dates check and fails a
# came-from-our-store check.
if make -C "$REPO" tls-verify HOST="$DOMAIN" CONNECT=127.0.0.1 PORT="$HTTPS_PORT" 2>&1 \
     | grep -q "chain valid and not expired"; then
  echo "  note  make tls-verify is GREEN here, against a self-signed default and no"
  echo "        issued certificate at all. That is not a bug in it — it checks the"
  echo "        chain and the dates, which are fine. It is why acme-verify exists,"
  echo "        and why a deploy is not verified by tls-verify alone."
else
  echo "  note  make tls-verify is not green pre-issuance on this build"
fi
if "$REPO/scripts/verify-acme.sh" --store "$WORK/letsencrypt/acme.json" \
     --host "$DOMAIN" --connect 127.0.0.1 --port "$HTTPS_PORT" >"$WORK/verify1.out" 2>&1; then
  bad "verify-acme.sh passed against a store with no certificate in it"
else
  ok "verify-acme.sh refuses pre-issuance: $(grep -m1 'NOT YET' "$WORK/verify1.out")"
fi
cmp_eq "a failed issuance did not rewrite the store" "$(sha256 "$WORK/letsencrypt/acme.json")" "$STORE_SHA_BEFORE"

# --------------------------------------------------------------------------
head_ "B. CONTROL: a defaultCertificate suppresses all of that"
# --------------------------------------------------------------------------
# The regression this design exists to prevent, kept measured rather than
# described. Same shipped command line, same empty store, one block added to
# the dynamic file — and the stack stops asking for a certificate while looking
# perfectly healthy from outside.
compose down >/dev/null 2>&1
make_cert "$WORK/fallback" 90
FP_FALLBACK=$(fp "$WORK/fallback/cert.pem")
{
  cat <<'YAML'
tls:
  stores:
    default:
      defaultCertificate:
        certFile: /certs/cert.pem
        keyFile: /certs/key.pem
YAML
  cat "$DYN"
} > "$WORK/dynamic-with-fallback.yml"

make_store "$WORK/letsencrypt/acme.json" 90 --no-certs >/dev/null
CTRL_ARGS=()
while IFS= read -r line; do
  [ -n "$line" ] && CTRL_ARGS+=("$line")
done < <(shipped_args)
docker run -d --name "$PROJECT-control" \
  -e TRAEFIK_HOSTNAME="$DOMAIN" \
  -p 127.0.0.1:0:443 \
  -v "$WORK/dynamic-with-fallback.yml:/etc/traefik/dynamic/dynamic.yml:ro" \
  -v "$WORK/fallback:/certs:ro" \
  -v "$WORK/letsencrypt:/letsencrypt" \
  traefik:3.7 "${CTRL_ARGS[@]}" >/dev/null 2>&1
sleep 8
CTRL_ATTEMPTS=$(docker logs "$PROJECT-control" 2>&1 | grep -c 'Unable to obtain ACME certificate')
CTRL_PORT=$(docker port "$PROJECT-control" 443/tcp 2>/dev/null | head -1 | sed 's/.*://')
CTRL_FP=$(echo | openssl s_client -connect "127.0.0.1:$CTRL_PORT" -servername "$DOMAIN" 2>/dev/null \
          | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
if [ "$CTRL_ATTEMPTS" -eq 0 ]; then
  ok "with a fallback present the same stack NEVER calls the CA (phase A: $ATTEMPTS)"
else
  bad "the fallback did not suppress issuance ($CTRL_ATTEMPTS attempts) — phase A proves less than it claims"
fi
cmp_eq "and it serves the fallback, looking entirely healthy" "$CTRL_FP" "$FP_FALLBACK"
if docker logs "$PROJECT-control" 2>&1 | grep -q 'No ACME certificate generation required'; then
  ok "Traefik says so in as many words: \"No ACME certificate generation required\""
else
  ok "suppression measured by attempt count rather than by log text"
fi
docker rm -f "$PROJECT-control" >/dev/null 2>&1

# --------------------------------------------------------------------------
head_ "C. Renewal fires from the store's own timer"
# --------------------------------------------------------------------------
# Traefik renews 720h (30 days) before expiry, checked at start-up and every
# 24h. Issuance and renewal are different triggers; phase A exercised one.
compose down >/dev/null 2>&1
CERT_C="$WORK/C.pem"; make_store "$WORK/letsencrypt/acme.json" 10 > "$CERT_C"
FP_C=$(fp "$CERT_C")
STORE_SHA_BEFORE=$(sha256 "$WORK/letsencrypt/acme.json")
if start_proxy; then ok "proxy came up with a near-expiry certificate in the store"
else bad "proxy did not come up with a near-expiry store"; fi
sleep 6

RENEWALS=$(count_in_log 'Error renewing ACME certificate')
if [ "$RENEWALS" -ge 1 ]; then
  ok "renewal was attempted and failed against the unroutable CA ($RENEWALS)"
else
  bad "no renewal attempt — a certificate in this stack would never renew"
fi
cmp_eq "TLS still terminates during the failed renewal" "$(served_fp)" "$FP_C"
cmp_eq "a failed renewal did not rewrite the store" "$(sha256 "$WORK/letsencrypt/acme.json")" "$STORE_SHA_BEFORE"
python3 -c 'import json,sys;json.load(open(sys.argv[1]))' "$WORK/letsencrypt/acme.json" 2>/dev/null \
  && ok "the store is still valid JSON after a failed renewal" \
  || bad "the store is no longer parseable"

# --------------------------------------------------------------------------
head_ "D. Issuance completes end to end, against a local CA"
# --------------------------------------------------------------------------
# Phases A–C assert that a call was made and that a digest did NOT move.
# Neither shows issuance *succeeding*, and a digest that can never move is not
# a measurement. This runs the whole path against `pebble` — Let's Encrypt's
# own test CA, on a private docker network — with challenge validation stubbed
# out, because what is under test is the configuration, not whether a
# `.invalid` name can pass HTTP-01.
#
# It starts from an EMPTY store, so what it measures is first issuance: the
# thing a brand-new deployment does.
D_STATUS=skipped
if docker image inspect "$PEBBLE_IMAGE" >/dev/null 2>&1 || docker pull "$PEBBLE_IMAGE" >/dev/null 2>&1; then
  compose down >/dev/null 2>&1
  docker network create "$PROJECT-net" >/dev/null 2>&1
  cid=$(docker create "$PEBBLE_IMAGE")
  docker cp "$cid:/test/certs/pebble.minica.pem" "$WORK/pebble.pem" >/dev/null 2>&1
  docker rm "$cid" >/dev/null 2>&1
fi
if [ -s "$WORK/pebble.pem" ]; then
  # `pebble` is not a decorative alias: its own serving certificate names
  # `localhost` and `pebble` and nothing else, so any other name fails
  # verification before a single ACME message is exchanged.
  docker run -d --name "$PROJECT-ca" --network "$PROJECT-net" --network-alias pebble \
    -e PEBBLE_VA_ALWAYS_VALID=1 -e PEBBLE_VA_NOSLEEP=1 -e PEBBLE_WFE_NONCEREJECT=0 \
    "$PEBBLE_IMAGE" >/dev/null 2>&1
  sleep 3

  rm -f "$WORK/letsencrypt/acme.json"
  make_store "$WORK/letsencrypt/acme.json" 90 --no-certs >/dev/null
  STORE_SHA_BEFORE=$(sha256 "$WORK/letsencrypt/acme.json")

  D_ARGS=()
  while IFS= read -r line; do
    case "$line" in
      --certificatesresolvers.letsencrypt.acme.caserver=*)
        line="--certificatesresolvers.letsencrypt.acme.caserver=https://pebble:14000/dir" ;;
    esac
    [ -n "$line" ] && D_ARGS+=("$line")
  done < <(shipped_args)
  docker run -d --name "$PROJECT-issue" --network "$PROJECT-net" \
    -e TRAEFIK_HOSTNAME="$DOMAIN" -e LEGO_CA_CERTIFICATES=/pebble.pem \
    -p 127.0.0.1:0:443 \
    -v "$WORK/pebble.pem:/pebble.pem:ro" \
    -v "$DYN:/etc/traefik/dynamic/dynamic.yml:ro" \
    -v "$WORK/letsencrypt:/letsencrypt" \
    traefik:3.7 "${D_ARGS[@]}" >/dev/null 2>&1
  sleep 15
  D_LOG=$(docker logs "$PROJECT-issue" 2>&1)
  HTTPS_PORT=$(docker port "$PROJECT-issue" 443/tcp 2>/dev/null | head -1 | sed 's/.*://')
  D_STATUS=ran

  printf '%s' "$D_LOG" | grep -q 'Server responded with a certificate' \
    && ok "the CA issued a certificate and Traefik took delivery" \
    || bad "no certificate was issued: $(printf '%s' "$D_LOG" | grep -m1 -i 'error' | tail -c 200)"
  if [ "$(sha256 "$WORK/letsencrypt/acme.json")" != "$STORE_SHA_BEFORE" ]; then
    ok "the store digest MOVED — the instrument phases A–C rely on is live"
  else
    bad "the store digest did not move even after issuance; A–C prove nothing"
  fi
  # A certificate that lands in the store and is never served is the
  # artefact-with-no-reader failure wearing a certificate.
  D_ISSUER=$(served_issuer)
  case "$D_ISSUER" in
    *Pebble*) ok "the ISSUED certificate is on the wire — ${D_ISSUER#issuer=}" ;;
    *) bad "the wire is not showing the issued certificate: $D_ISSUER" ;;
  esac
  if "$REPO/scripts/verify-acme.sh" --store "$WORK/letsencrypt/acme.json" \
       --host "$DOMAIN" --connect 127.0.0.1 --port "$HTTPS_PORT" >"$WORK/verify2.out" 2>&1; then
    ok "verify-acme.sh agrees the served certificate came from the store"
  else
    bad "verify-acme.sh: $(tail -3 "$WORK/verify2.out" | tr '\n' ' ')"
  fi
  docker rm -f "$PROJECT-issue" "$PROJECT-ca" >/dev/null 2>&1
  docker network rm "$PROJECT-net" >/dev/null 2>&1
else
  echo "  SKIPPED — $PEBBLE_IMAGE could not be obtained."
  echo "  NOT MEASURED, which is not the same as passed: without it nothing in"
  echo "  this run shows an issuance completing or the store digest moving."
fi

# --------------------------------------------------------------------------
if [ "$LIVE_STAGING" = 1 ]; then
head_ "E. LIVE: the shipped configuration reaches Let's Encrypt STAGING"
# --------------------------------------------------------------------------
# Pebble proves the protocol; it cannot prove that this configuration, from
# this machine, reaches the real thing. This does — against the **staging**
# directory, never production.
#
# It stops at the account contact, and deliberately so. Let's Encrypt validates
# the contact address before anything else and refuses `.invalid` ("Domain name
# does not end with a valid public suffix") and `example.com` ("contact email
# has forbidden domain"). Every address that would get past that check is a
# real one, and this repository is not allowed to contain a real address. So
# what is proved here is reach and protocol — a 400 from staging's `new-acct`
# endpoint is a full TLS handshake and a signed JWS round trip — and what is
# NOT proved is registration and issuance against Let's Encrypt itself. Phase D
# covers those against a CA that will take our word for the contact.
  compose down >/dev/null 2>&1
  rm -f "$WORK/letsencrypt/acme.json"
  make_store "$WORK/letsencrypt/acme.json" 90 --no-certs >/dev/null
  echo "  endpoint: $STAGING_CA"
  echo "  contact:  $STAGING_EMAIL"
  if CASERVER="$STAGING_CA" ACME_EMAIL="$STAGING_EMAIL" start_proxy; then
    ok "the shipped service came up pointed at staging"
  else bad "the shipped service did not come up against staging"; fi
  sleep 20

  if [ "$(count_in_log 'Unable to obtain ACME certificate')" -ge 1 ]; then
    ok "the request reached staging and staging answered"
    proxy_log | grep -m1 'Unable to obtain ACME certificate' \
      | sed -e 's/.*error=/      staging said: /' -e 's/\x1b\[[0-9;]*m//g' | cut -c1-220
  else
    bad "no issuance attempt against staging"
  fi
  # If this had failed at the socket it would prove exactly what the
  # unroutable CA already proved, and nothing more.
  if proxy_log | grep -q 'connection refused'; then
    bad "transport-level failure — staging was not actually reached"
  else
    ok "the refusal came from the CA, not from the socket"
  fi
  if proxy_log | grep -q 'acme-staging-v02.api.letsencrypt.org'; then
    ok "the endpoint in the log is the STAGING host"
  else
    bad "the log does not name the staging endpoint"
  fi
  if proxy_log | grep -q 'acme-v02.api.letsencrypt.org/'; then
    bad "PRODUCTION appears in the log — this run contacted the wrong endpoint"
  else
    ok "no production Let's Encrypt endpoint appears anywhere in the log"
  fi
fi

# --------------------------------------------------------------------------
head_ "Result"
echo "  $PASS passed, $FAIL failed"
echo
echo "  ACME endpoints, per phase:"
echo "    0-C  $BLACKHOLE_CA — unroutable, nothing was contacted"
case "$D_STATUS" in
  ran)     echo "    D    pebble, on a private docker network — a real ACME protocol run" ;;
  skipped) echo "    D    NOT RUN — pebble unavailable. No issuance was observed." ;;
esac
if [ "$LIVE_STAGING" = 1 ]; then
  echo "    E    $STAGING_CA"
  echo "         reach and protocol only; registration was refused on the contact address."
else
  echo "    E    NOT RUN — pass --live-staging. Let's Encrypt was not contacted at all."
fi
echo
echo "  Let's Encrypt PRODUCTION was not contacted in any phase."
[ "$FAIL" -eq 0 ]
