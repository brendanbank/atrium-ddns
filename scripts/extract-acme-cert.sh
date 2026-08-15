#!/usr/bin/env bash
#
# Extract one certificate from a Traefik ACME store into cert.pem / key.pem.
#
#   extract-acme-cert.sh <acme.json> <output-dir>
#
# Why this exists rather than pointing a second Traefik at the same store:
# **the store has a live writer.** The old service's Traefik holds
# `--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json`
# and renews from it. Two Traefik instances sharing one acme.json corrupt it —
# the failure is a lost account key, not a merge conflict. This reads it and
# never writes, so the writer stays sole.
#
# CONSEQUENCE, and the reason this is a "for now" measure: the extracted files
# are a COPY. When the writer renews (~30 days before expiry) the copy goes
# stale and TLS starts serving an expired certificate. Re-run this and restart
# the proxy. `make tls-refresh` does both. Check the expiry it prints.
set -uo pipefail

SRC=${1:?usage: extract-acme-cert.sh <acme.json> <output-dir>}
OUT=${2:?usage: extract-acme-cert.sh <acme.json> <output-dir>}
RESOLVER=${ACME_RESOLVER:-letsencrypt}

[ -r "$SRC" ] || { echo "cannot read $SRC" >&2; exit 1; }
mkdir -p "$OUT" || exit 1

python3 - "$SRC" "$OUT" "$RESOLVER" <<'PY' || exit 1
import base64, json, os, sys, pathlib

src, out, resolver = sys.argv[1], sys.argv[2], sys.argv[3]
store = json.load(open(src))

if resolver not in store:
    sys.exit(f"resolver {resolver!r} not in {src}; found {list(store)}")
certs = store[resolver].get("Certificates") or []
if not certs:
    # An empty Certificates list is what an un-provisioned store looks like,
    # and it is indistinguishable from a healthy one until TLS fails. Refuse.
    sys.exit(f"resolver {resolver!r} holds no certificates")
if len(certs) > 1:
    sys.exit(f"{len(certs)} certificates present; this script assumes one. "
             "Pick deliberately rather than let it choose for you.")

c = certs[0]
for field in ("certificate", "key"):
    if not c.get(field):
        sys.exit(f"certificate is missing {field!r}")

chain = base64.b64decode(c["certificate"]).decode()
key = base64.b64decode(c["key"]).decode()
if "BEGIN CERTIFICATE" not in chain or "PRIVATE KEY" not in key:
    sys.exit("decoded material does not look like PEM")

o = pathlib.Path(out)
# Write via a temp file and rename: the proxy may be reading these, and a
# half-written key is a worse failure than a stale one.
for name, body, mode in (("cert.pem", chain, 0o644), ("key.pem", key, 0o600)):
    tmp = o / (name + ".tmp")
    tmp.write_text(body)
    os.chmod(tmp, mode)
    tmp.replace(o / name)

names = [c["domain"].get("main")] + (c["domain"].get("sans") or [])
print(f"  extracted {len([n for n in names if n])} name(s), "
      f"{chain.count('BEGIN CERTIFICATE')} chain block(s)")
PY

if command -v openssl >/dev/null 2>&1; then
  end=$(openssl x509 -in "$OUT/cert.pem" -noout -enddate 2>/dev/null | cut -d= -f2)
  echo "  expires: ${end:-unknown}"
  openssl x509 -in "$OUT/cert.pem" -noout -checkend $((14*86400)) >/dev/null 2>&1 \
    || echo "  WARNING: expires within 14 days — the writer has probably renewed; re-run after it does"
fi
