#!/usr/bin/env bash
#
# Gate test for the ACME hand-over (cutover runbook § 2.2 / § 5.4).
#
#   scripts/test-acme-handover.sh                # hermetic; no network
#   scripts/test-acme-handover.sh --live-staging # adds one real exercise
#                                                # against Let's Encrypt STAGING
#
# ---------------------------------------------------------------------------
# What this proves, and what it deliberately does not touch
# ---------------------------------------------------------------------------
#
# It stands the **shipped** proxy service up — `compose.yaml` plus the
# `compose.acme.yaml` overlay, through `docker compose`, with only the
# variables that exist for the purpose overridden — against a **synthetic**
# ACME store built here from `openssl`. It never reads the incumbent's store,
# never contacts Let's Encrypt production, and never binds 80 or 443.
#
# Everything it asserts about Traefik's behaviour has a control beside it, so
# no assertion can pass by being unreachable:
#
#   | phase | asserts                              | control that makes it bite |
#   |-------|--------------------------------------|----------------------------|
#   | A     | no ACME call at hand-over            | D and E, where the same
#   |       |                                      | log instrument does fire   |
#   | B     | fallback terminates TLS              | A, where a different
#   |       |                                      | certificate is presented   |
#   | C     | the fallback suppresses issuance     | the same stack with the
#   |       |                                      | fallback removed           |
#   | D     | renewal is NOT suppressed            | A, where it does not fire  |
#   | E     | a renewal completes, and the store   | A/B/D, where the digest
#   |       | digest moves when ACME acts          | does not move              |
#   | F     | this configuration reaches the real  | the unroutable CA, which
#   |       | Let's Encrypt                        | fails at the socket        |
#
# Phase E runs against `pebble`, Let's Encrypt's own test CA, on a private
# docker network — no external endpoint at all. Phase F is the only one that
# leaves the machine, it goes to **staging**, and it is off unless
# `--live-staging` is passed.
#
# ---------------------------------------------------------------------------
# One deliberate departure from production, and why it is not artificial
# ---------------------------------------------------------------------------
#
# In production the extracted `cert.pem` and the certificate inside the copied
# `acme.json` are the same bytes — `scripts/extract-acme-cert.sh` produced one
# from the other. Two identical certificates cannot tell you *which* of them
# terminated a handshake, so a test built that way would assert "TLS works"
# and call it "the store was loaded".
#
# So the incumbent here **renews between the extraction and the copy**: the
# fallback is certificate A, the copied store holds certificate B. That is not
# a contrivance; it is the runbook's own "cut over between 24 Aug and 23 Sep"
# branch (§ 4), and it makes "served from the store" and "served from the
# fallback" two different fingerprints.
#
set -uo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LIVE_STAGING=0
[ "${1:-}" = "--live-staging" ] && LIVE_STAGING=1

# A documentation domain (RFC 2606 reserves .invalid). Nothing here resolves
# and nothing here is a real name.
DOMAIN=handover.example.invalid
EMAIL=ops@example.invalid
# Let's Encrypt validates the *contact address* before it looks at anything
# else, and it rejects `.invalid` outright ("contact email has invalid domain:
# Domain name does not end with a valid public suffix"). Measured — the first
# version of phase E used the address above and never got as far as
# registering, so the store never moved and the phase reported a failure that
# was its own. RFC 2606 reserves example.com for exactly this: a name with a
# real TLD that can never belong to anyone.
STAGING_EMAIL=ops@example.com
# Unroutable by RFC 1122; the connection is refused immediately, so "did
# Traefik try to talk to a CA" becomes a question the log answers in under a
# second rather than after a 2-minute client timeout.
BLACKHOLE_CA=https://127.0.0.1:1/directory
STAGING_CA=https://acme-staging-v02.api.letsencrypt.org/directory
# Let's Encrypt's own test CA, run locally. Pinned to `latest` deliberately:
# it is a disposable CA on a private network for the length of one phase, it
# issues nothing anything trusts, and a stale pin here would be a maintenance
# burden with no security value. Nothing in the repository depends on it.
PEBBLE_IMAGE=ghcr.io/letsencrypt/pebble:latest

PROJECT="acme-handover-test-$$"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/acme-handover.XXXXXX") || exit 1

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
  docker rm -f "$PROJECT-control" "$PROJECT-renew" "$PROJECT-ca" >/dev/null 2>&1
  docker network rm "$PROJECT-net" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

sha256() { python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }
mtime()  { python3 -c 'import os,sys;print(os.stat(sys.argv[1]).st_mtime_ns)' "$1"; }
fp()     { openssl x509 -in "$1" -noout -fingerprint -sha256 | cut -d= -f2; }

# --------------------------------------------------------------------------
# A Traefik ACME store, built from scratch. The shape matters: this is what
# scripts/extract-acme-cert.sh reads and what step 5.4(b) copies.
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
        # resets the account, which is exactly what a copied store looks like
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
  TLS_CERT_DIR="$WORK/certs" \
  ACME_STORE_DIR="$WORK/letsencrypt" \
  docker compose -p "$PROJECT" \
    -f "$REPO/compose.yaml" -f "$REPO/compose.acme.yaml" --profile tls "$@"
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

proxy_log() { docker logs "$PROJECT-proxy-1" 2>&1; }
count_in_log() { proxy_log | grep -c "$1"; }

# ==========================================================================
echo "ACME hand-over gate test"
echo "  repo:          $REPO"
echo "  work dir:      $WORK"
echo "  compose files: compose.yaml + compose.acme.yaml (the shipped pair)"
echo "  hermetic CA:   $BLACKHOLE_CA (unroutable — refuses instantly)"
echo "  phase E:       $PEBBLE_IMAGE on a private network"
if [ "$LIVE_STAGING" = 1 ]; then
  echo "  phase F:       ENABLED, against $STAGING_CA"
else
  echo "  phase F:       skipped (pass --live-staging to run it)"
fi

# --------------------------------------------------------------------------
head_ "0. The configuration refuses to load rather than defaulting"
# --------------------------------------------------------------------------
# The failure this guards against is silent by construction: the incumbent's
# compose defaults LETSENCRYPT_CASERVER to *staging*, Traefik's own default is
# *production*, and with the extracted copy still terminating TLS as the
# fallback, a stack that got the wrong one looks perfectly healthy until the
# fallback is gone. Compose's `${VAR:?}` turns that into an error at config
# time.
for v in TRAEFIK_HOSTNAME LETSENCRYPT_EMAIL LETSENCRYPT_CASERVER; do
  out=$( ( unset "$v"
           export TRAEFIK_HOSTNAME="$DOMAIN" LETSENCRYPT_EMAIL="$EMAIL"
           export LETSENCRYPT_CASERVER="$BLACKHOLE_CA"
           unset "$v"
           docker compose -f "$REPO/compose.yaml" -f "$REPO/compose.acme.yaml" \
             config >/dev/null ) 2>&1 )
  rc=$?
  if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "required variable $v"; then
    ok "unset $v — refused at config time, and the message names it"
  else
    bad "unset $v — rc=$rc, message: $(printf '%s' "$out" | tail -1)"
  fi
done

# The same file must still load for everybody who is NOT doing a hand-over.
if docker compose -f "$REPO/compose.yaml" config >/dev/null 2>&1; then
  ok "compose.yaml alone still loads with none of the three set"
else
  bad "compose.yaml alone no longer loads — the guard leaked into the base file"
fi

# --------------------------------------------------------------------------
head_ "1. The rendered service is the hand-over, not the borrowed arrangement"
# --------------------------------------------------------------------------
CFG=$(CASERVER="$BLACKHOLE_CA" compose config --format json 2>/dev/null)
render() { printf '%s' "$CFG" | python3 -c "$1" 2>/dev/null; }

CMD=$(render 'import json,sys;print("\n".join(json.load(sys.stdin)["services"]["proxy"]["command"]))')
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
# `!override` rather than a merge: the borrowed 8443 entrypoint must be gone,
# not sitting beside the new ones.
if printf '%s\n' "$CMD" | grep -q "8443"; then
  bad "command still mentions 8443 — the override merged instead of replacing"
else
  ok "command no longer mentions 8443"
fi

PORTS=$(render 'import json,sys;print(" ".join(str(p["target"]) for p in json.load(sys.stdin)["services"]["proxy"]["ports"]))')
cmp_eq "published container ports" "$PORTS" "80 443"

VOLS=$(render 'import json,sys;print("\n".join("%s ro=%s" % (v["target"], v.get("read_only", False)) for v in json.load(sys.stdin)["services"]["proxy"]["volumes"]))')
if printf '%s\n' "$VOLS" | grep -qx "/letsencrypt ro=False"; then
  ok "/letsencrypt is mounted read-WRITE — this stack owns its own store"
else
  bad "/letsencrypt mount wrong: $(printf '%s' "$VOLS" | tr '\n' ' ')"
fi
if printf '%s\n' "$VOLS" | grep -qx "/certs ro=True"; then
  ok "/certs is mounted read-only — the extracted copy stays a copy"
else
  bad "/certs mount wrong: $(printf '%s' "$VOLS" | tr '\n' ' ')"
fi
SRCS=$(render 'import json,sys;print(" ".join(v["source"] for v in json.load(sys.stdin)["services"]["proxy"]["volumes"]))')
case "$SRCS" in
  *dynamic-acme.yml*) ok "the hand-over dynamic file is mounted" ;;
  *) bad "dynamic-acme.yml is not mounted: $SRCS" ;;
esac
# Both dynamic files declare a router called `host-api`. Mounting both is a
# coin toss, so the overlay must have replaced the volume list.
case "$SRCS" in
  *"/dynamic.yml"*) bad "the pre-hand-over dynamic.yml is ALSO mounted — two host-api routers" ;;
  *) ok "the pre-hand-over dynamic.yml is not mounted" ;;
esac

DYN="$REPO/infra/traefik/dynamic-acme.yml"
grep -q 'certResolver: letsencrypt' "$DYN" \
  && ok "dynamic-acme.yml: the router asks the resolver for a certificate" \
  || bad "dynamic-acme.yml: no certResolver on the router"
grep -q 'defaultCertificate' "$DYN" \
  && ok "dynamic-acme.yml: the extracted copy is kept as the fallback" \
  || bad "dynamic-acme.yml: the fallback certificate has been dropped"
grep -q '{{ env "TRAEFIK_HOSTNAME" }}' "$DYN" \
  && ok "dynamic-acme.yml: the hostname comes from the environment" \
  || bad "dynamic-acme.yml: the hostname is not templated"
# The one disclosure rule this repository has no slack on.
if grep -E '^\s*rule:' "$DYN" | grep -qE '[A-Za-z0-9-]+\.[A-Za-z]{2,}'; then
  bad "dynamic-acme.yml: the router rule contains a literal domain name"
else
  ok "dynamic-acme.yml: no literal domain name in the router rule"
fi

# --------------------------------------------------------------------------
head_ "2. Extraction reads the incumbent's store and does not touch it"
# --------------------------------------------------------------------------
mkdir -p "$WORK/incumbent" "$WORK/certs" "$WORK/letsencrypt"
CERT_A="$WORK/A.pem"; make_store "$WORK/incumbent/acme.json" 90 > "$CERT_A"
INC_SHA_BEFORE=$(sha256 "$WORK/incumbent/acme.json")
INC_MTIME_BEFORE=$(mtime "$WORK/incumbent/acme.json")

"$REPO/scripts/extract-acme-cert.sh" "$WORK/incumbent/acme.json" "$WORK/certs" >/dev/null 2>&1 \
  && ok "extract-acme-cert.sh produced cert.pem / key.pem" \
  || bad "extract-acme-cert.sh failed"

cmp_eq "incumbent store sha256 after extraction" "$(sha256 "$WORK/incumbent/acme.json")" "$INC_SHA_BEFORE"
cmp_eq "incumbent store mtime after extraction"  "$(mtime  "$WORK/incumbent/acme.json")" "$INC_MTIME_BEFORE"
cmp_eq "the extracted copy IS the incumbent's certificate" "$(fp "$WORK/certs/cert.pem")" "$(fp "$CERT_A")"
FP_A=$(fp "$WORK/certs/cert.pem")

# --------------------------------------------------------------------------
head_ "A. The hand-over: the copied store terminates TLS, with no ACME call"
# --------------------------------------------------------------------------
# The incumbent renewed between the extraction and the copy (runbook § 4's
# middle branch), so the store now holds certificate B and the fallback is
# still A. Which one appears on the wire is now a measurable question.
CERT_B="$WORK/B.pem"; make_store "$WORK/letsencrypt/acme.json" 90 > "$CERT_B"
FP_B=$(fp "$CERT_B")
STORE_SHA_BEFORE=$(sha256 "$WORK/letsencrypt/acme.json")
STORE_MTIME_BEFORE=$(mtime "$WORK/letsencrypt/acme.json")

if start_proxy; then ok "the shipped proxy service came up and answers TLS"
else bad "the shipped proxy service did not answer TLS"; fi

cmp_eq "certificate on the wire is the COPIED STORE's" "$(served_fp)" "$FP_B"
if [ "$(served_fp)" = "$FP_A" ]; then
  bad "the wire shows the FALLBACK — the copied store was not loaded"
else
  ok "the wire is not showing the fallback"
fi
# Chain validation is a second instrument on the same handshake: a fingerprint
# match says which certificate, %{ssl_verify_result} says the chain verified.
# 502 is expected and correct — this throwaway stack has no `api` behind it.
VERIFY=$(curl -sS -o /dev/null -w '%{http_code}/%{ssl_verify_result}' \
  --cacert "$CERT_B" --resolve "$DOMAIN:$HTTPS_PORT:127.0.0.1" \
  "https://$DOMAIN:$HTTPS_PORT/api/healthz" 2>/dev/null)
cmp_eq "curl through TLS (code/verify; 502 = no backend, by design)" "$VERIFY" "502/0"

cmp_eq "ACME issuance failures in the log" "$(count_in_log 'Unable to obtain ACME certificate')" "0"
cmp_eq "ACME renewal failures in the log"  "$(count_in_log 'Error renewing ACME certificate')" "0"
cmp_eq "new stack's store sha256 unmoved"  "$(sha256 "$WORK/letsencrypt/acme.json")" "$STORE_SHA_BEFORE"
cmp_eq "new stack's store mtime unmoved"   "$(mtime  "$WORK/letsencrypt/acme.json")" "$STORE_MTIME_BEFORE"
cmp_eq "incumbent's store sha256 unmoved"  "$(sha256 "$WORK/incumbent/acme.json")" "$INC_SHA_BEFORE"

# The acceptance criterion names this target by name.
if make -C "$REPO" tls-verify HOST="$DOMAIN" CONNECT=127.0.0.1 PORT="$HTTPS_PORT" 2>&1 \
     | grep -q "chain valid and not expired"; then
  ok "make tls-verify HOST=<name> CONNECT=127.0.0.1 PORT=<p> is green"
else
  bad "make tls-verify is not green against the throwaway stack"
fi

# The operator's own check, run the way the runbook asks for it.
if "$REPO/scripts/verify-acme-handover.sh" --store "$WORK/letsencrypt/acme.json" \
     --host "$DOMAIN" --connect 127.0.0.1 --port "$HTTPS_PORT" >"$WORK/verify.out" 2>&1; then
  ok "verify-acme-handover.sh agrees the served certificate came from the store"
else
  bad "verify-acme-handover.sh: $(tail -2 "$WORK/verify.out" | tr '\n' ' ')"
fi

# --------------------------------------------------------------------------
head_ "B/C. The fallback terminates TLS — and suppresses first issuance"
compose down >/dev/null 2>&1
make_store "$WORK/letsencrypt/acme.json" 90 --no-certs >/dev/null
if start_proxy; then ok "proxy came up with an ACME store holding no certificate"
else bad "proxy did not come up with an empty store"; fi

cmp_eq "certificate on the wire is the EXTRACTED FALLBACK" "$(served_fp)" "$FP_A"
VERIFY=$(curl -sS -o /dev/null -w '%{http_code}/%{ssl_verify_result}' \
  --cacert "$WORK/certs/cert.pem" --resolve "$DOMAIN:$HTTPS_PORT:127.0.0.1" \
  "https://$DOMAIN:$HTTPS_PORT/api/healthz" 2>/dev/null)
cmp_eq "curl through the fallback (code/verify)" "$VERIFY" "502/0"

# THE FINDING. Traefik does not request a certificate for a domain it already
# has one for — and `defaultCertificate` counts. So a hand-over where step
# 5.4(b) was skipped looks flawless and never issues anything.
cmp_eq "issuance attempts with the fallback present" "$(count_in_log 'Unable to obtain ACME certificate')" "0"

# The control. Same store, same shipped command, one line of dynamic config
# removed. If ACME now fires, the suppression above is caused by the fallback
# and the instrument is not simply blind.
python3 - "$DYN" "$WORK/dynamic-nofallback.yml" <<'PY'
import pathlib, sys
src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
text = src.read_text()
_, sep, tail = text.partition("http:")
dst.write_text(sep + tail)          # everything from `http:` on — no tls block
PY
# The control runs the SAME command line the shipped service renders — read
# out of `docker compose config`, not retyped — so the only difference between
# it and phase C is the one block removed from the dynamic file.
CTRL_ARGS=()
while IFS= read -r line; do
  [ -n "$line" ] && CTRL_ARGS+=("$line")
done < <(printf '%s' "$CFG" | python3 -c 'import json,sys;print("\n".join(json.load(sys.stdin)["services"]["proxy"]["command"]))')
docker run -d --name "$PROJECT-control" \
  -e TRAEFIK_HOSTNAME="$DOMAIN" \
  -v "$WORK/dynamic-nofallback.yml:/etc/traefik/dynamic/dynamic-acme.yml:ro" \
  -v "$WORK/certs:/certs:ro" \
  -v "$WORK/letsencrypt:/letsencrypt" \
  traefik:3.7 "${CTRL_ARGS[@]}" >/dev/null 2>&1
sleep 8
CTRL_ATTEMPTS=$(docker logs "$PROJECT-control" 2>&1 | grep -c 'Unable to obtain ACME certificate')
if [ "$CTRL_ATTEMPTS" -ge 1 ]; then
  ok "CONTROL: with the fallback removed the same stack DOES call the CA ($CTRL_ATTEMPTS attempt(s))"
else
  bad "CONTROL: no ACME attempt even without the fallback — the log instrument proves nothing"
fi
docker rm -f "$PROJECT-control" >/dev/null 2>&1

# --------------------------------------------------------------------------
head_ "D. Renewal is NOT suppressed — the store's own timer still fires"
# --------------------------------------------------------------------------
# Traefik renews 720h (30 days) before expiry, checked at start-up and every
# 24h. A certificate inside that window in the copied store must produce a
# renewal attempt even though the fallback suppressed *issuance* in phase C.
# This is what makes the hand-over a hand-over rather than a freeze.
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
  bad "no renewal attempt — the hand-over would never renew"
fi
cmp_eq "TLS still terminates during the failed renewal" "$(served_fp)" "$FP_C"
cmp_eq "a failed renewal did not rewrite the store" "$(sha256 "$WORK/letsencrypt/acme.json")" "$STORE_SHA_BEFORE"
python3 -c 'import json,sys;json.load(open(sys.argv[1]))' "$WORK/letsencrypt/acme.json" 2>/dev/null \
  && ok "the store is still valid JSON after a failed renewal" \
  || bad "the store is no longer parseable"

# --------------------------------------------------------------------------
head_ "E. The renewal actually completes — end to end, against a local CA"
# --------------------------------------------------------------------------
# Phases A–D assert that a digest did NOT move and that a renewal failed in a
# survivable way. Neither shows the renewal *succeeding*, and a digest that
# can never move is not a measurement. This phase runs the whole path against
# `pebble` — Let's Encrypt's own test CA, in-process on a private docker
# network — with challenge validation stubbed out, because what is under test
# is the hand-over, not whether a `.invalid` name can pass HTTP-01.
#
# It runs the command line the shipped service renders, read back out of
# `docker compose config`, with two mounts a shipped file could never carry:
# pebble's CA certificate, and the network that reaches it.
E_STATUS=skipped
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

  make_store "$WORK/letsencrypt/acme.json" 10 > "$WORK/E.pem"
  FP_E=$(fp "$WORK/E.pem")
  STORE_SHA_BEFORE=$(sha256 "$WORK/letsencrypt/acme.json")

  E_ARGS=()
  while IFS= read -r line; do
    case "$line" in
      --certificatesresolvers.letsencrypt.acme.caserver=*)
        line="--certificatesresolvers.letsencrypt.acme.caserver=https://pebble:14000/dir" ;;
    esac
    [ -n "$line" ] && E_ARGS+=("$line")
  done < <(printf '%s' "$CFG" | python3 -c 'import json,sys;print("\n".join(json.load(sys.stdin)["services"]["proxy"]["command"]))')
  docker run -d --name "$PROJECT-renew" --network "$PROJECT-net" \
    -e TRAEFIK_HOSTNAME="$DOMAIN" -e LEGO_CA_CERTIFICATES=/pebble.pem \
    -p 127.0.0.1:0:443 \
    -v "$WORK/pebble.pem:/pebble.pem:ro" \
    -v "$REPO/infra/traefik/dynamic-acme.yml:/etc/traefik/dynamic/dynamic-acme.yml:ro" \
    -v "$WORK/certs:/certs:ro" \
    -v "$WORK/letsencrypt:/letsencrypt" \
    traefik:3.7 "${E_ARGS[@]}" >/dev/null 2>&1
  sleep 15
  E_LOG=$(docker logs "$PROJECT-renew" 2>&1)
  HTTPS_PORT=$(docker port "$PROJECT-renew" 443/tcp 2>/dev/null | head -1 | sed 's/.*://')
  E_STATUS=ran

  printf '%s' "$E_LOG" | grep -q 'Server responded with a certificate' \
    && ok "the CA issued a certificate and Traefik took delivery" \
    || bad "no certificate was issued: $(printf '%s' "$E_LOG" | grep -m1 -i 'error' | tail -c 200)"
  if [ "$(sha256 "$WORK/letsencrypt/acme.json")" != "$STORE_SHA_BEFORE" ]; then
    ok "the store digest MOVED — the instrument phases A–D rely on is live"
  else
    bad "the store digest did not move even after issuance; A–D prove nothing"
  fi
  # The renewed certificate must actually reach the wire, replacing both the
  # near-expiry one it renewed and the fallback beneath it. A renewal that
  # lands in the store and is never served is the artefact-with-no-reader
  # failure wearing a certificate.
  SERVED=$(served_fp)
  if [ -n "$SERVED" ] && [ "$SERVED" != "$FP_E" ] && [ "$SERVED" != "$FP_A" ]; then
    ok "the RENEWED certificate is on the wire — $SERVED"
  else
    bad "the wire still shows a pre-renewal certificate: $SERVED"
  fi
  ISSUER=$(echo | openssl s_client -connect "127.0.0.1:$HTTPS_PORT" -servername "$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -issuer 2>/dev/null)
  case "$ISSUER" in
    *Pebble*) ok "issued by the test CA, not self-signed — ${ISSUER#issuer=}" ;;
    *) bad "unexpected issuer: $ISSUER" ;;
  esac
  if "$REPO/scripts/verify-acme-handover.sh" --store "$WORK/letsencrypt/acme.json" \
       --host "$DOMAIN" --connect 127.0.0.1 --port "$HTTPS_PORT" >"$WORK/verify2.out" 2>&1; then
    ok "verify-acme-handover.sh still agrees after the renewal"
  else
    bad "verify-acme-handover.sh: $(tail -2 "$WORK/verify2.out" | tr '\n' ' ')"
  fi
  cmp_eq "incumbent's store sha256 still unmoved" "$(sha256 "$WORK/incumbent/acme.json")" "$INC_SHA_BEFORE"
  docker rm -f "$PROJECT-renew" "$PROJECT-ca" >/dev/null 2>&1
  docker network rm "$PROJECT-net" >/dev/null 2>&1
else
  echo "  SKIPPED — $PEBBLE_IMAGE could not be obtained."
  echo "  NOT MEASURED, which is not the same as passed: without it nothing in"
  echo "  this run shows a renewal completing or the store digest moving."
fi

# --------------------------------------------------------------------------
if [ "$LIVE_STAGING" = 1 ]; then
head_ "F. LIVE: the shipped configuration reaches Let's Encrypt STAGING"
# --------------------------------------------------------------------------
# Pebble proves the protocol; it cannot prove that this configuration, from
# this machine, reaches the real thing. This does — against the **staging**
# directory, never production.
#
# It stops at the account contact, and deliberately so. Let's Encrypt
# validates the contact address before anything else and refuses `.invalid`
# ("Domain name does not end with a valid public suffix") and `example.com`
# ("contact email has forbidden domain"). Every address that would get past
# that check is a real one, and this repository is not allowed to contain a
# real address. So what is proved here is reach and protocol — a 400 from
# staging's `new-acct` endpoint is a full TLS handshake and a signed JWS round
# trip — and what is NOT proved is registration and issuance against Let's
# Encrypt itself. Phase E covers those against a CA that will take our word
# for the contact.
  compose down >/dev/null 2>&1
  make_store "$WORK/letsencrypt/acme.json" 10 > "$WORK/F.pem"
  FP_F=$(fp "$WORK/F.pem")
  echo "  endpoint: $STAGING_CA"
  echo "  contact:  $STAGING_EMAIL"
  if CASERVER="$STAGING_CA" ACME_EMAIL="$STAGING_EMAIL" start_proxy; then
    ok "the shipped service came up pointed at staging"
  else bad "the shipped service did not come up against staging"; fi
  sleep 20

  if [ "$(count_in_log 'Error renewing ACME certificate')" -ge 1 ]; then
    ok "the renewal reached staging and staging answered"
    proxy_log | grep -m1 'Error renewing ACME certificate' \
      | sed -e 's/.*error=/      staging said: /' -e 's/\x1b\[[0-9;]*m//g' | cut -c1-220
  else
    bad "no renewal attempt against staging"
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
  cmp_eq "TLS still terminates throughout" "$(served_fp)" "$FP_F"
  cmp_eq "incumbent's store sha256 still unmoved" "$(sha256 "$WORK/incumbent/acme.json")" "$INC_SHA_BEFORE"
fi

# --------------------------------------------------------------------------
head_ "Result"
echo "  $PASS passed, $FAIL failed"
echo
echo "  ACME endpoints, per phase:"
echo "    0-D  $BLACKHOLE_CA — unroutable, nothing was contacted"
case "$E_STATUS" in
  ran)     echo "    E    pebble, on a private docker network — a real ACME protocol run" ;;
  skipped) echo "    E    NOT RUN — pebble unavailable. No issuance was observed." ;;
esac
if [ "$LIVE_STAGING" = 1 ]; then
  echo "    F    $STAGING_CA"
  echo "         reach and protocol only; registration was refused on the contact address."
else
  echo "    F    NOT RUN — pass --live-staging. Let's Encrypt was not contacted at all."
fi
echo
echo "  Let's Encrypt PRODUCTION was not contacted in any phase."
echo "  The incumbent's store was never read: every store in this run was built"
echo "  by openssl under $WORK."
[ "$FAIL" -eq 0 ]
