#!/usr/bin/env bash
# Deploy a dev-release bundle to a staging host.
#
# Takes the bundle `dev-release.sh` produced, ships it over ssh, checks
# out the tag, rebuilds and restarts, migrates both alembic chains, and
# verifies **by content** that the running stack is serving what the tag
# contains.
#
# The verification is the part worth keeping. A deploy that reports the
# git revision proves the checkout moved, not that the containers did:
# `up -d --build` has been observed building a new image, tagging it, and
# leaving the containers on the old one. So this compares the served
# bundle against the image it was built from, byte for byte.
#
# Usage:
#   scripts/deploy-dev.sh <ssh-host> dist/v0.1.0-dev.foo.1.bundle
#   STAGING_DIR=/srv/atrium-ddns scripts/deploy-dev.sh staging dist/….bundle
set -euo pipefail

HOST="${1:?usage: deploy-dev.sh <ssh-host> <bundle>}"
BUNDLE="${2:?usage: deploy-dev.sh <ssh-host> <bundle>}"
REMOTE_DIR="${STAGING_DIR:-/usr/local/atrium-ddns}"
PROFILE="${COMPOSE_PROFILE:-tls}"

[ -f "$BUNDLE" ] || { echo "no such bundle: $BUNDLE" >&2; exit 1; }

# The ref inside the bundle. Read from the bundle rather than parsed out
# of the filename: a renamed file would otherwise check out the wrong
# thing and report success.
TAG="$(git bundle list-heads "$BUNDLE" | awk '/refs\/tags\//{sub(".*refs/tags/",""); print; exit}')"
[ -n "$TAG" ] || { echo "bundle carries no tag ref: $BUNDLE" >&2; exit 1; }

echo "  host   : $HOST"
echo "  dir    : $REMOTE_DIR"
echo "  tag    : $TAG"

scp -q -o BatchMode=yes "$BUNDLE" "$HOST:/tmp/dev-release.bundle"

ssh -o BatchMode=yes "$HOST" REMOTE_DIR="$REMOTE_DIR" TAG="$TAG" PROFILE="$PROFILE" 'bash -s' <<'REMOTE'
set -euo pipefail
cd "$REMOTE_DIR"
echo "  before : $(git rev-parse --short HEAD)"
git fetch --quiet /tmp/dev-release.bundle "refs/tags/$TAG:refs/tags/$TAG"
git checkout -q --detach "refs/tags/$TAG"
rm -f /tmp/dev-release.bundle
echo "  after  : $(git rev-parse --short HEAD)  ($TAG)"

docker compose --profile "$PROFILE" build >/dev/null
docker compose --profile "$PROFILE" up -d --force-recreate api worker >/dev/null

for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q api)" 2>/dev/null)" = "healthy" ] && break
  sleep 5
done

docker compose exec -T api alembic upgrade head >/dev/null 2>&1
docker compose exec -T api alembic -c /opt/host_app/alembic.ini upgrade head >/dev/null 2>&1
echo "  atrium : $(docker compose exec -T api alembic current 2>/dev/null | tail -1)"
echo "  host   : $(docker compose exec -T api alembic -c /opt/host_app/alembic.ini current 2>/dev/null | tail -1)"

# By content, not by revision — see the header.
IMG=$(docker compose exec -T api sh -c \
  'sha256sum /opt/atrium/static/host/main.js | cut -c1-16')
PORT=$(grep -E '^API_HOST_PORT' .env | cut -d= -f2)
SERVED=$(curl -fsS "http://127.0.0.1:${PORT}/host/main.js" | sha256sum | cut -c1-16)
if [ "$IMG" = "$SERVED" ]; then
  echo "  bundle : served matches image ($SERVED)"
else
  echo "  bundle : MISMATCH — image=$IMG served=$SERVED" >&2
  echo "           the containers are running an older image." >&2
  exit 1
fi
REMOTE
echo "  ✓ $TAG is live on $HOST"
