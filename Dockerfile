# Atrium Ddns host extension image. Two stages:
#
#   1. frontend-builder — node + pnpm, builds the host SPA bundle
#      (a single main.js dynamic-imported by atrium at runtime).
#   2. runtime — FROM the published atrium image, pip-installs the
#      host backend package into atrium's venv, and copies the host
#      bundle into /opt/atrium/static/host so atrium serves it at
#      /host/main.js (same origin as the SPA, no CORS).

ARG ATRIUM_IMAGE=ghcr.io/brendanbank/atrium:0.30.1

# ---- frontend-builder ----
FROM node:26-alpine AS frontend-builder
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

# Build stamp. `GET /api/version` (atrium 0.30+) reads these and the SPA
# renders them at the top of the signed-in user menu, so "which build is
# this box running?" is answerable without exec-ing into the container.
#
# Baked at build time on purpose: the runtime image has no `.git` and no
# git binary, and `backend/pyproject.toml` names a release line, not the
# commit it was built from.
#
# `ATRIUM_VERSION` / `ATRIUM_COMMIT` describe the *atrium base* image and
# are NOT set here — Docker inherits ENV across `FROM`, so they ride along
# from ghcr.io/brendanbank/atrium and the menu reports both layers.
#
# `ATRIUM_APP_VERSION` is the release tag, verbatim (`v0.1.9`, not
# `0.1.9`): the string is meant to be pasted into `git checkout`.
# Untagged, it stays empty and the line renders as `<brand>: <short sha>`
# off the commit, so a build is always identifiable either way. Fed by
# `release.yml` from the tag it built, and by the Makefile from git for
# local builds; a bare `docker build` leaves both empty and the app half
# of `/api/version` reports nothing.
#
# ATRIUM_APP_NAME is deliberately left unset. Unset, atrium labels the
# line with the brand name from `/app-config` — which is admin-editable
# here (BRAND_NAME / `make seed-brand`), so a rebranded deployment says
# its own name instead of one frozen into the image at build time. Set it
# explicitly only if you want the image to override that.
#
# Last layers before USER: every expensive layer (pip install, bundle
# copy) is above this point, so a changed stamp does not rebuild them.
ARG ATRIUM_APP_VERSION=""
ARG ATRIUM_APP_COMMIT=""
ENV ATRIUM_APP_VERSION=${ATRIUM_APP_VERSION} \
    ATRIUM_APP_COMMIT=${ATRIUM_APP_COMMIT}

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

# The compatibility suites (`tests/` at the repo root, not `backend/tests`).
#
# Only `backend/` reached the image, so `make test-backend` — the gate's only
# backend command — could not see `tests/compat/` at all: a 101-case wire table,
# 81 model rules and 18 guards, none of them executed by anything. V1M1 was
# about to freeze artefacts with no writer.
#
# Copied into `dev` rather than `runtime` on purpose: `dev` is the test image
# (compose builds it by default, `--target runtime` is the prod one), so tests
# reach the gate without shipping to production. Last layer in the stage, so
# editing a case file rebuilds one COPY rather than a pip install — the
# mutation checks in #23's PR body each cost a ~1s rebuild because of it.
#
# `/opt/compat_tests/compat/...` mirrors the repo's own `tests/compat/...`, so
# a path in a failure report reads the same in both places. Deliberately NOT
# under /opt/host_app: pytest would then take backend/pyproject.toml as the
# config file and run these under `-n auto --dist=loadfile`, which buys nothing
# on an 0.1s data suite and hides its per-test output.
COPY tests /opt/compat_tests
# Whatever the developer's local pytest left behind is not part of the image.
# Without this the image content depends on whether someone ran the suite in
# their checkout, which makes `make check-compat-fresh` argue with itself.
RUN find /opt/compat_tests -type d -name __pycache__ -prune -exec rm -rf {} +
USER app
