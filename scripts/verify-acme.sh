#!/usr/bin/env bash
#
# Is the certificate on the wire the one this stack issued for itself?
#
#   verify-acme.sh --store <acme.json> --host <name> [--connect <addr>] [--port <n>]
#
# ---------------------------------------------------------------------------
# Why "TLS answers 200" is not the check
# ---------------------------------------------------------------------------
#
# A handshake succeeding proves a certificate exists, not where it came from,
# and the two used to come apart badly. While the proxy carried a
# `defaultCertificate` fallback — a copy of a predecessor's certificate — an
# empty ACME store produced a stack that looked flawless: the handshake
# succeeded, the chain validated, `curl` returned 200, `make tls-verify`
# printed a valid date, and no certificate was ever issued. Measured on
# traefik:3.7, a domain that already has a certificate from the dynamic
# configuration is a domain Traefik does not request one for, so nothing
# corrected it until the copy expired weeks later.
#
# That fallback is gone (infra/traefik/dynamic.yml says why), which removes the
# trap but not the reason to measure. Renewal happens in the store; clients see
# the wire. This is the one check that says those are the same object, so it is
# what proves a renewal will actually reach anybody. Run it after a deploy.
#
# Two instruments of different shape: one decodes a file on disk, the other
# completes a handshake. Neither can make the other agree with it.
#
set -uo pipefail

STORE=""; HOST=""; CONNECT=""; PORT=443
while [ $# -gt 0 ]; do
  case "$1" in
    --store)    STORE=${2:?};    shift 2 ;;
    --host)     HOST=${2:?};     shift 2 ;;
    --connect)  CONNECT=${2:?};  shift 2 ;;
    --port)     PORT=${2:?};     shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$STORE" ] && [ -n "$HOST" ] || {
  echo "usage: verify-acme.sh --store <acme.json> --host <name> [--connect <addr>] [--port <n>]" >&2
  exit 2
}
CONNECT=${CONNECT:-$HOST}

TMP=$(mktemp -d "${TMPDIR:-/tmp}/acme-verify.XXXXXX") || exit 2
trap 'rm -rf "$TMP"' EXIT

fp()  { openssl x509 -in "$1" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2; }
end_() { openssl x509 -in "$1" -noout -enddate 2>/dev/null | cut -d= -f2; }

# --------------------------------------------------------------------------
# Pull the leaf out of the store.
#
# Traefik writes acme.json with mode 0600 and the certificate base64-encoded as
# a full PEM chain; the leaf is the first block. Missing file, missing
# resolver, and present-but-empty `Certificates` are three different answers
# and are reported as three different messages — "no certificate" is the state
# a stack is in before its first issuance, and telling an operator that is not
# the same as telling them something is broken.
# --------------------------------------------------------------------------
if [ ! -e "$STORE" ]; then
  echo "NOT YET: $STORE does not exist." >&2
  echo "  Traefik creates it on the first issuance, so this is what a stack that" >&2
  echo "  has never obtained a certificate looks like. Check 'make tls-logs'." >&2
  exit 1
fi
[ -r "$STORE" ] || { echo "cannot read the store: $STORE (it is mode 0600 and owned by the proxy)" >&2; exit 2; }

if ! python3 - "$STORE" "$TMP/cert.pem" <<'PY' 2>"$TMP/decode.err"
import base64, json, sys

store, out = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(store))
except Exception as exc:
    sys.exit(f"the store is not parseable JSON: {exc}")
if not isinstance(data, dict) or not data:
    sys.exit("the store has no resolver in it")

for resolver, body in data.items():
    certs = (body or {}).get("Certificates") or []
    if not certs:
        continue
    pem = base64.b64decode(certs[0]["certificate"])
    marker = b"-----END CERTIFICATE-----"
    # The leaf only. A chain here would make the fingerprint depend on which
    # block openssl happened to read first.
    end = pem.find(marker)
    if end == -1:
        sys.exit(f"resolver {resolver!r}: the stored certificate is not PEM")
    open(out, "wb").write(pem[: end + len(marker)] + b"\n")
    print(f"resolver {resolver!r}: {len(certs)} certificate(s)")
    break
else:
    sys.exit("the store holds an account but no certificates")
PY
then
  echo "NOT YET: could not decode a certificate out of $STORE." >&2
  sed 's/^/  /' "$TMP/decode.err" >&2
  echo "  A store with no certificate in it means this stack has not issued one." >&2
  echo "  Check 'make tls-logs' for the challenge." >&2
  exit 1
fi

STORE_FP=$(fp "$TMP/cert.pem"); STORE_END=$(end_ "$TMP/cert.pem")

WIRE_PEM="$TMP/wire.pem"
if ! echo | openssl s_client -connect "$CONNECT:$PORT" -servername "$HOST" 2>/dev/null \
      | openssl x509 -out "$WIRE_PEM" 2>/dev/null; then
  echo "REFUSED: no certificate came back from $CONNECT:$PORT (SNI $HOST)." >&2
  echo "  Not a mismatch — a measurement that did not happen. Fix the reach first." >&2
  exit 1
fi
WIRE_FP=$(fp "$WIRE_PEM"); WIRE_END=$(end_ "$WIRE_PEM")

echo "  store : $STORE_FP  (expires $STORE_END)"
echo "  wire  : $WIRE_FP  (expires $WIRE_END)"

if [ "$STORE_FP" = "$WIRE_FP" ]; then
  echo "  MATCH — the certificate on the wire is the one in this stack's own ACME"
  echo "  store, so the renewal that store performs is the one clients will see."
  exit 0
fi

echo "  MISMATCH — the wire is NOT serving this stack's ACME store." >&2
ISSUER=$(openssl x509 -in "$WIRE_PEM" -noout -issuer 2>/dev/null)
SUBJ=$(openssl x509 -in "$WIRE_PEM" -noout -subject 2>/dev/null)
echo "  wire issuer : ${ISSUER#issuer=}" >&2
echo "  wire subject: ${SUBJ#subject=}" >&2
case "$ISSUER" in
  *TRAEFIK*|*Traefik*)
    echo "  That is Traefik's built-in self-signed certificate, which it serves when" >&2
    echo "  no router matched the SNI name. The router rule and TRAEFIK_HOSTNAME" >&2
    echo "  disagree with the name you asked for, or the certificate is not issued" >&2
    echo "  yet. 'make tls-preflight' checks the first; 'make tls-logs' the second." >&2 ;;
  *)
    echo "  Something in front of this stack is terminating TLS with its own" >&2
    echo "  certificate, or the store belongs to a different proxy than the one" >&2
    echo "  on that port." >&2 ;;
esac
exit 1
