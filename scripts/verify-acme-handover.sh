#!/usr/bin/env bash
#
# Did the ACME hand-over actually take?
#
#   verify-acme-handover.sh --store <acme.json> --host <name> \
#                           [--connect <addr>] [--port <n>] [--fallback <cert.pem>]
#
# ---------------------------------------------------------------------------
# Why "TLS answers 200" is not the check
# ---------------------------------------------------------------------------
#
# After the hand-over the proxy has two places it can get a certificate from:
# the ACME store copied from the incumbent (runbook step 5.4b), and the
# extracted `cert.pem` kept as `defaultCertificate`. If the copy never
# happened — wrong path, wrong permissions, forgotten — the fallback answers
# instead, and everything an operator would normally look at is green:
# the handshake succeeds, the chain validates, `curl` returns 200,
# `make tls-verify` prints a valid date.
#
# Worse, Traefik will not correct it. Measured on traefik:3.7: a domain that
# already has a certificate from the dynamic configuration is a domain Traefik
# does not request one for. With the fallback in place and the store empty, no
# ACME call is ever made, and the first symptom is the extracted copy expiring
# weeks later with nothing having renewed it.
#
# So the question is not "does TLS work" but "**which** certificate is on the
# wire". This compares the served certificate against the one inside the
# store, by fingerprint. Two instruments of different shape: one decodes a
# file on disk, the other completes a handshake.
#
set -uo pipefail

STORE=""; HOST=""; CONNECT=""; PORT=443; FALLBACK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --store)    STORE=${2:?};    shift 2 ;;
    --host)     HOST=${2:?};     shift 2 ;;
    --connect)  CONNECT=${2:?};  shift 2 ;;
    --port)     PORT=${2:?};     shift 2 ;;
    --fallback) FALLBACK=${2:?}; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$STORE" ] && [ -n "$HOST" ] || {
  echo "usage: verify-acme-handover.sh --store <acme.json> --host <name>" >&2
  echo "                               [--connect <addr>] [--port <n>] [--fallback <cert.pem>]" >&2
  exit 2
}
[ -r "$STORE" ] || { echo "cannot read the store: $STORE" >&2; exit 2; }
CONNECT=${CONNECT:-$HOST}

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/acme-verify.XXXXXX") || exit 2
trap 'rm -rf "$TMP"' EXIT

fp() { openssl x509 -in "$1" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2; }

# The store is decoded by the same script that produced the fallback, so a
# bug in the decoder cannot make the two sides agree by agreeing with itself.
if ! "$HERE/extract-acme-cert.sh" "$STORE" "$TMP" >"$TMP/extract.log" 2>&1; then
  echo "REFUSED: could not decode a certificate out of the store." >&2
  sed 's/^/  /' "$TMP/extract.log" >&2
  echo "  An empty store here is the failure this script exists to catch:" >&2
  echo "  the proxy will serve the fallback and never issue anything." >&2
  exit 1
fi
STORE_FP=$(fp "$TMP/cert.pem")
STORE_END=$(openssl x509 -in "$TMP/cert.pem" -noout -enddate | cut -d= -f2)

WIRE_PEM="$TMP/wire.pem"
if ! echo | openssl s_client -connect "$CONNECT:$PORT" -servername "$HOST" 2>/dev/null \
      | openssl x509 -out "$WIRE_PEM" 2>/dev/null; then
  echo "REFUSED: no certificate came back from $CONNECT:$PORT (SNI $HOST)." >&2
  echo "  Not a mismatch — a measurement that did not happen. Fix the reach first." >&2
  exit 1
fi
WIRE_FP=$(fp "$WIRE_PEM")
WIRE_END=$(openssl x509 -in "$WIRE_PEM" -noout -enddate | cut -d= -f2)

echo "  store  : $STORE_FP  (expires $STORE_END)"
echo "  wire   : $WIRE_FP  (expires $WIRE_END)"
if [ -n "$FALLBACK" ] && [ -r "$FALLBACK" ]; then
  echo "  fallback: $(fp "$FALLBACK")  (expires $(openssl x509 -in "$FALLBACK" -noout -enddate | cut -d= -f2))"
fi

if [ "$STORE_FP" = "$WIRE_FP" ]; then
  echo "  MATCH — the certificate on the wire is the one in this stack's own ACME"
  echo "  store. The hand-over took: renewal will run from that store."
  exit 0
fi

echo "  MISMATCH — the wire is NOT serving this stack's ACME store." >&2
if [ -n "$FALLBACK" ] && [ -r "$FALLBACK" ] && [ "$(fp "$FALLBACK")" = "$WIRE_FP" ]; then
  echo "  It is serving the extracted FALLBACK. That is the 'store was never" >&2
  echo "  copied' failure: TLS looks perfect and nothing will ever renew." >&2
  echo "  Re-do runbook step 5.4(b) and restart the proxy." >&2
fi
exit 1
