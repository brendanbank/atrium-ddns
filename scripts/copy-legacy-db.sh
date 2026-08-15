#!/usr/bin/env bash
#
# Take a WAL-safe COPY of the live legacy dyndns-route53 SQLite database
# and verify the transfer by content on both sides.
#
#   scripts/copy-legacy-db.sh /tmp/legacy-copy.db
#   scripts/copy-legacy-db.sh /tmp/legacy-copy.db --host atrium-ddns-deploy
#
# Why this exists as a script rather than as a paragraph in a runbook
# ------------------------------------------------------------------
#
# **Opening a WAL-mode SQLite database creates files beside it — even
# with `mode=ro`.** Read-only WAL access needs the shared-memory file,
# and SQLite creates `<db>-shm` (32 KB) and `<db>-wal` (0 bytes)
# whenever the directory is writable. Measured, not reasoned about:
#
#     before:              src.db
#     after mode=ro open:  src.db  src.db-shm  src.db-wal
#
# Pointed at the running service's data directory that is a **write into
# production** from an operation everyone would call read-only. #49 hit
# it for real: its first `--dry-run` left the sidecars behind and its
# second run refused because of them.
#
# So nothing here ever opens the live file from outside. `sqlite3
# ".backup"` runs *inside the running container* — the legacy repo's own
# `scripts/backup.sh` recipe — which is the one reader that is already
# attached to that database and already holds its `-shm`. The bytes are
# streamed off over the existing ssh channel and the temporary file is
# removed from the container afterwards.
#
# `immutable=1` would also stop the sidecars being created and is
# **rejected**: it tells SQLite to ignore the WAL entirely. On this
# production database that is 65 536 bytes of main file beside a 4 MB
# write-ahead log, so a run would read a stale prefix and report a
# plausible, wrong population instead of refusing. A guard that turns a
# loud failure into a quiet one is worse than none.
#
# What it verifies, and what a bare `docker cp` would not
# -------------------------------------------------------
#
# * **sha256 on both sides.** The digest is taken in the container,
#   before the bytes move, and again on this workstation after they
#   land. A truncated stream is a different digest, and a pipeline that
#   only checks its exit status cannot see one.
# * **`PRAGMA integrity_check`** on the local copy — that a file is
#   whole is a different claim from that it is a valid database.
# * **the copy has no `-wal` sibling**, which is what tells you the
#   `.backup` checkpointed rather than handing you a torn main file.
#
# The copy carries real bcrypt hashes and Fernet ciphertext. It is
# written 0600 into a directory of your choosing; delete it when done.
set -euo pipefail

DEST=""
HOST="${OVERNIGHT_DEPLOY_HOST:-atrium-ddns-deploy}"
CONTAINER="dyndns-route53-web-1"
SOURCE_DB="/app/instance/dyndns.db"

usage() {
    cat <<'EOF'
usage: scripts/copy-legacy-db.sh <destination.db> [options]

  --host NAME        ssh alias of the deploy host
                     (default: $OVERNIGHT_DEPLOY_HOST or atrium-ddns-deploy)
  --container NAME   legacy web container (default: dyndns-route53-web-1)
  --source PATH      database inside that container
                     (default: /app/instance/dyndns.db)

Takes a WAL-safe copy with `sqlite3 ".backup"` INSIDE the running
container, streams it here, and verifies sha256 on both sides. The live
file is never opened from outside the container and nothing is created
beside it.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)      HOST="$2"; shift 2 ;;
        --container) CONTAINER="$2"; shift 2 ;;
        --source)    SOURCE_DB="$2"; shift 2 ;;
        -h|--help)   usage; exit 0 ;;
        -*)          echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)           DEST="$1"; shift ;;
    esac
done

if [[ -z "$DEST" ]]; then
    usage >&2
    exit 2
fi

# Refuse rather than overwrite: two rehearsals from "a fresh copy" that
# silently share one file are one rehearsal reported twice.
if [[ -e "$DEST" ]]; then
    echo "REFUSED: $DEST already exists." >&2
    echo "  A rehearsal starts from a FRESH copy. Delete it, or name another path." >&2
    exit 2
fi
for sidecar in "$DEST-wal" "$DEST-shm"; do
    if [[ -e "$sidecar" ]]; then
        echo "REFUSED: $sidecar exists beside the destination." >&2
        echo "  Something has this path open. Pick another." >&2
        exit 2
    fi
done

REMOTE_TMP="/tmp/legacy-copy-$$-$(date +%s).db"
SHA_FILE="$(mktemp -t legacy-copy-sha)"
trap 'rm -f "$SHA_FILE"' EXIT

umask 077

echo "copying ${CONTAINER}:${SOURCE_DB} from ${HOST}"
echo "  recipe: sqlite3 \".backup\" inside the running container (WAL-safe)"

# The remote half prints the digest on stderr and the bytes on stdout,
# so the transfer needs no intermediate file on this side either.
# shellcheck disable=SC2029  # remote expansion is the point
ssh -o BatchMode=yes "$HOST" \
    "docker exec $CONTAINER sh -c 'set -e; \
        sqlite3 $SOURCE_DB \".backup $REMOTE_TMP\"; \
        sha256sum $REMOTE_TMP | cut -d\" \" -f1 >&2; \
        cat $REMOTE_TMP; \
        rm -f $REMOTE_TMP'" \
    >"$DEST" 2>"$SHA_FILE"

REMOTE_SHA="$(tr -d '[:space:]' <"$SHA_FILE")"
if [[ -z "$REMOTE_SHA" ]]; then
    echo "REFUSED: the container reported no digest." >&2
    echo "  An empty digest and a matching digest are not the same result;" >&2
    echo "  this refuses rather than comparing nothing to nothing." >&2
    rm -f "$DEST"
    exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
    LOCAL_SHA="$(sha256sum "$DEST" | cut -d' ' -f1)"
else
    LOCAL_SHA="$(shasum -a 256 "$DEST" | cut -d' ' -f1)"
fi

echo "  sha256 in container : $REMOTE_SHA"
echo "  sha256 here         : $LOCAL_SHA"

if [[ "$REMOTE_SHA" != "$LOCAL_SHA" ]]; then
    echo "REFUSED: the copy does not match the bytes the container hashed." >&2
    rm -f "$DEST"
    exit 1
fi
echo "  transfer verified by content, not by exit code."

if [[ -e "$DEST-wal" || -e "$DEST-shm" ]]; then
    echo "REFUSED: a -wal/-shm sibling appeared beside the copy." >&2
    echo "  The .backup should have checkpointed. Nothing may open this path." >&2
    exit 1
fi

# `PRAGMA integrity_check` opens the copy — which is exactly the
# operation this file is about — so it runs on the COPY, in the
# directory the caller chose, and never on the source. sqlite3 may leave
# its own sidecars here; they are removed so the file the importer sees
# is the one that was verified.
INTEGRITY="$(sqlite3 "$DEST" 'PRAGMA integrity_check;' 2>/dev/null || echo 'UNREADABLE')"
rm -f "$DEST-wal" "$DEST-shm"
echo "  PRAGMA integrity_check: $INTEGRITY"
if [[ "$INTEGRITY" != "ok" ]]; then
    echo "REFUSED: the copy is not a valid SQLite database." >&2
    exit 1
fi

chmod 600 "$DEST"
BYTES="$(wc -c <"$DEST" | tr -d '[:space:]')"
echo
echo "copy: $DEST ($BYTES bytes, mode 600)"
echo "  It carries real bcrypt hashes and Fernet ciphertext. Delete it when done."
