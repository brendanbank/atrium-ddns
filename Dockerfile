# Atrium Ddns host extension image. Two stages:
#
#   1. frontend-builder — node + pnpm, builds the host SPA bundle
#      (a single main.js dynamic-imported by atrium at runtime).
#   2. runtime — FROM the published atrium image, pip-installs the
#      host backend package into atrium's venv, and copies the host
#      bundle into /opt/atrium/static/host so atrium serves it at
#      /host/main.js (same origin as the SPA, no CORS).

ARG ATRIUM_IMAGE=ghcr.io/brendanbank/atrium:0.28

# ---- frontend-builder ----
FROM node:25-alpine AS frontend-builder
WORKDIR /app
RUN npm install -g pnpm@10.33.1

COPY frontend/package.json frontend/pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile 2>/dev/null || pnpm install
COPY frontend/ ./

ARG VITE_API_BASE_URL="/api"
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN pnpm build

# ---- runtime ----
FROM ${ATRIUM_IMAGE} AS runtime

USER root
COPY backend /opt/host_app
# atrium's runtime image uses uv to build the venv but doesn't install
# pip into it. ensurepip bootstraps pip so we can install the host
# package — slightly slower than uv but avoids adding uv to the
# runtime image just for this one install.
RUN /opt/venv/bin/python -m ensurepip --upgrade \
 && /opt/venv/bin/python -m pip install --no-cache-dir /opt/host_app

# Bake the host bundle into atrium's static dir at /host/main.js.
COPY --from=frontend-builder /app/dist /opt/atrium/static/host

USER app

# Re-declare HEALTHCHECK on the derived image. The base atrium image
# already ships one (curl /api/healthz on :8000), but image scanners
# flag absence at the leaf Dockerfile because they don't trace
# inheritance.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/healthz || exit 1

# ---- dev ----
# Test tooling, baked rather than pip-installed per run.
#
# The atrium runtime image strips pytest/pytest-asyncio/httpx to keep prod
# small, so the scaffolded `make test-backend` reinstalled them on every
# invocation: ~1.5s of a 2.7s run spent proving three packages were already
# there. Baking them costs nothing in prod — this stage is never the default
# target, so `docker compose build` without `target: dev` still produces the
# stripped runtime image.
#
# pytest-xdist is the point of the exercise: `-n auto --dist=loadfile` is the
# default invocation (see backend/pyproject.toml), matching the recipe that
# gets atrium-pa to ~72 tests/sec.
FROM runtime AS dev
USER root
RUN /opt/venv/bin/python -m pip install --no-cache-dir \
      pytest pytest-asyncio pytest-xdist httpx \
 && mkdir -p /tmp/pytest-cache && chown app /tmp/pytest-cache
# The host package is installed read-only under /opt/host_app and the app
# user cannot write a cache there — pytest warns on every run. Point it
# somewhere writable instead of silencing the warning.
ENV PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest-cache"
USER app
