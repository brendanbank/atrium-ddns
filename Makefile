# Atrium Ddns — top-level Make targets.
#
# Convenience wrappers around the docker-compose stack. Most of these
# assume you ran `make dev-bootstrap` first to bring everything up.

COMPOSE := docker compose

# Where the Dockerfile's `dev` stage puts the repo-root `tests/` tree. Mirrors
# the repo layout, so `/opt/compat_tests/compat/...` is `tests/compat/...`.
COMPAT_TESTS := /opt/compat_tests

# Where the Dockerfile COPYs `backend/`. `make test-backend` runs
# `$(HOST_APP)/tests`, and `make migrate` runs `$(HOST_APP)/alembic.ini`.
HOST_APP := /opt/host_app

# The host package as the *interpreter* names it. NOT a path: `backend/` is
# pip-installed as well as copied, `$(HOST_APP)/src` is not on sys.path, and
# what `import atrium_ddns` resolves to is site-packages. See
# check-host-pkg-fresh for why the distinction is the whole issue.
HOST_PKG := atrium_ddns

# Content digest of a directory tree: every file's path and bytes, in sorted
# order. Run against the worktree and against the container (see the
# check-*-fresh targets) — one script, so the two readings cannot differ by
# method. Exported rather than inlined because the recipe would otherwise be
# unreadable through two layers of quoting.
#
# The argument is a path, or `pkg:<name>` to resolve an importable package by
# the import system rather than by a path someone typed. Both spellings feed
# the same digest, so a path reading and an import reading are comparable.
#
# Prints three tab-separated fields — digest, file count, resolved root — so a
# refusal names the values and where they were read from. "no match found" is
# the shape of guard that gets deleted rather than investigated; and for
# `pkg:` the root is not knowable in advance, so printing it is the only way
# the reader learns which copy was measured.
define TREE_DIGEST_PY
import hashlib, importlib.util, pathlib, sys

arg = sys.argv[1]
if arg.startswith("pkg:"):
    # find_spec, not a path: this is the same machinery the tests' own
    # `import atrium_ddns` goes through, so it cannot resolve to a copy the
    # tests do not use. find_spec does not execute the module.
    name = arg[4:]
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.origin:
        sys.exit("cannot resolve package " + name + " on this interpreter")
    root = pathlib.Path(spec.origin).parent
else:
    root = pathlib.Path(arg)

digest = hashlib.sha256()
def tracked(path):
    # Byte-compiled output and pytest's own cache appear on one side and not
    # the other, and a digest that counts them reports drift that is not there.
    parts = path.relative_to(root).parts
    return not any(part == "__pycache__" or part.startswith(".") for part in parts)

files = [p for p in sorted(root.rglob("*")) if p.is_file() and tracked(p)]
if not files:
    sys.exit("no files under " + str(root))
for path in files:
    digest.update(path.relative_to(root).as_posix().encode())
    digest.update(b"\0")
    digest.update(path.read_bytes())
print(digest.hexdigest(), len(files), root, sep="\t")
endef
export TREE_DIGEST_PY

# One freshness comparison. $(1) target name for the message, $(2) worktree
# path, $(3) container path or `pkg:<name>`, $(4) what to call the tree.
#
# No commas in any argument — $(call) splits on them.
define CHECK_FRESH
	@host=$$(python3 -c "$$TREE_DIGEST_PY" '$(2)' | tr -d '\r'); \
	img=$$($(COMPOSE) exec -T api /opt/venv/bin/python -c "$$TREE_DIGEST_PY" '$(3)' | tr -d '\r'); \
	hd=$$(printf '%s\n' "$$host" | cut -f1); \
	id=$$(printf '%s\n' "$$img" | cut -f1); \
	if [ -z "$$hd" ] || [ -z "$$id" ]; then \
		echo "$(1): could not read both digests (worktree='$$host' container='$$img')"; \
		exit 1; \
	fi; \
	if [ "$$hd" != "$$id" ]; then \
		echo "$(1): the api container is running a STALE copy of $(4)."; \
		echo "  worktree  $$host"; \
		echo "  container $$img"; \
		echo "  (digest / files / root)"; \
		echo "  $(4) is baked into the image at build time and"; \
		echo "  \`make up\` does not rebuild. Run: make build && make up"; \
		exit 1; \
	fi; \
	echo "$(1): container matches worktree ($$hd)"
endef

.PHONY: help dev-bootstrap up down build logs ps migrate \
	seed-admin seed-bundle seed-compat-fixture verify-compat-rehash \
	test test-frontend test-backend test-compat \
	check-fresh check-compat-fresh check-backend-fresh check-host-pkg-fresh \
	test-backend-serial test-backend-file typecheck smoke \
	e2e-up e2e-deps e2e-down test-e2e check-bundle-fresh

help:  ## show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-21s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

dev-bootstrap: build up migrate  ## build, start, migrate; run me first
	@echo
	@echo "Stack is up. Next: make seed-admin EMAIL=you@example.com PASSWORD=..."
	@echo "Then: make seed-bundle && open http://localhost:8000"

up:  ## start the stack
	$(COMPOSE) up -d

down:  ## stop the stack (keeps data volume)
	$(COMPOSE) down

build:  ## (re)build the image
	$(COMPOSE) build

logs:  ## tail logs from all services
	$(COMPOSE) logs -f

ps:  ## list services
	$(COMPOSE) ps

migrate:  ## run atrium + host alembic chains
	$(COMPOSE) exec -T api alembic upgrade head
	$(COMPOSE) exec -T api alembic -c /opt/host_app/alembic.ini upgrade head

seed-admin:  ## seed a super_admin (EMAIL=... PASSWORD=... [FULL_NAME=...])
	@if [ -z "$(EMAIL)" ] || [ -z "$(PASSWORD)" ]; then \
		echo "usage: make seed-admin EMAIL=you@example.com PASSWORD=secret [FULL_NAME='Your Name']"; \
		exit 2; \
	fi
	$(COMPOSE) exec -T api python -m app.scripts.seed_admin \
		--email "$(EMAIL)" --password "$(PASSWORD)" \
		--full-name "$${FULL_NAME:-Admin}" --super-admin

seed-bundle:  ## point system.host_bundle_url at /host/main.js
	$(COMPOSE) exec -T api python -m atrium_ddns.scripts.seed_host_bundle /host/main.js

# The other promotion step. `0001_init` seeds `app_settings['brand']` so a
# *fresh* database comes up named correctly, but an alembic revision runs
# once per database: after it is stamped, correcting the name means either
# editing an applied revision (which never re-runs) or an UPDATE typed at
# the deployed database. This target is re-runnable, so the declared value
# and the stored one can be reconciled at any point.
#
# BRAND_NAME / BRAND_PRIMARY_COLOR come from `.env` via compose, so the name
# is declared beside the ports and secrets rather than passed by hand. The
# recipe deliberately passes no `--name`: the container reads the
# environment, which is the thing a fresh deploy reproduces.
seed-brand:  ## write app_settings['brand'] from BRAND_NAME / BRAND_PRIMARY_COLOR
	$(COMPOSE) exec -T api python -m atrium_ddns.scripts.seed_brand

show-brand:  ## print the stored brand row without writing
	$(COMPOSE) exec -T api python -m atrium_ddns.scripts.seed_brand --show

# The frozen table's `fixture:` world, read out of protocol_cases.yaml
# itself rather than restated — see the script's docstring for the four
# schema mappings and why each is a decision.
#
# `tests/compat/conftest.py` builds no fixture and says so; without this
# target `make test-compat TARGET=host` answers `nohost` to every case
# that resolves a hostname, which reads exactly like a compatibility
# finding. Freshness-gated for the same reason `test-compat` is: seeding
# from a stale copy of the table produces a world the runner's own copy
# does not describe.
#
# Needs ATRIUM_DDNS_COMPAT_STUB=1 in the api container's environment
# (compose.yaml passes it through); the script refuses rather than
# seeding a world whose backends nothing can resolve.
seed-compat-fixture: check-compat-fresh  ## seed the frozen table's fixture world
	$(COMPOSE) exec -T api /opt/venv/bin/python -m atrium_ddns.scripts.seed_compat_fixture

verify-compat-rehash:  ## print each fixture device's stored hash SHAPE (never the hash)
	$(COMPOSE) exec -T api /opt/venv/bin/python \
	  -m atrium_ddns.scripts.seed_compat_fixture --verify-rehash

# Tests. Test tooling is baked into the Dockerfile's `dev` stage
# (compose builds api with `target: dev`), so there is no per-run pip
# install. Backend tests run parallel by default — see
# backend/pyproject.toml addopts.
test: test-frontend test-backend  ## run host tests (frontend + backend)

test-frontend:  ## frontend unit tests via vitest
	cd frontend && pnpm install --frozen-lockfile 2>/dev/null || (cd frontend && pnpm install)
	cd frontend && pnpm test

# Two pytest sessions, deliberately. The host suite runs under
# backend/pyproject.toml (`-n auto`, asyncio auto-mode); the compat tree has no
# ini of its own and wants neither. Separate sessions also keep the two counts
# separate in the output — one merged "N passed" is the number that lets a
# suite stop running without anyone noticing.
#
# The compat half is the service-free half: the 18 model-behaviour guards and
# the wire runner's own arithmetic guards. It opens no socket and needs no
# `--target`, and the wire table is reported as NOT RUN rather than as nought
# failures. The table itself is `make test-compat`, which is not in the gate
# because it needs a live service named on the command line.
test-backend: check-fresh  ## backend tests + service-free compat guards, in the api container
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest $(HOST_APP)/tests $(PYTEST_ARGS)
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest $(COMPAT_TESTS) -ra $(PYTEST_ARGS)

# Everything the gate reads out of the image is this worktree's. Three trees,
# three named targets, because "which one is stale" is the first thing anyone
# asks and an aggregate that only says "stale" makes them go and find out.
check-fresh: check-compat-fresh check-backend-fresh check-host-pkg-fresh  ## all three freshness guards

# Each tree is baked into the image, and `make up` does not rebuild. So editing
# a file and re-running the gate reads the *old* copy and reports green for it
# — the same defect compose.yaml's `image:` comment records for a shared tag,
# one layer along.
#
# Two readings of one tree, and they must agree. `docker compose exec` is the
# only instrument that can see what the running container actually holds;
# anything derived from the Dockerfile or from a build log is a statement about
# what should be there.
check-compat-fresh:  ## fail if the container's copy of tests/ is not this worktree's
	$(call CHECK_FRESH,check-compat-fresh,tests,$(COMPAT_TESTS),tests/)

# `backend/` as a whole. This is the tree `make test-backend` collects its test
# files from ($(HOST_APP)/tests) and the tree `make migrate` reads the host
# alembic chain from ($(HOST_APP)/alembic). #36's reproduction lives here: a
# `def test_x(): assert False` written into backend/tests/ without a rebuild
# was never *collected*, and the run reported 358 passed.
check-backend-fresh:  ## fail if the container's copy of backend/ is not this worktree's
	$(call CHECK_FRESH,check-backend-fresh,backend,$(HOST_APP),backend/)

# The copy the tests actually IMPORT — and it is not the one above.
#
# The Dockerfile does `COPY backend /opt/host_app` and then
# `pip install /opt/host_app`, so there are two copies of the source in the
# image. `$(HOST_APP)/src` is not on sys.path; `import atrium_ddns` inside the
# container resolves to /opt/venv/lib/python3.13/site-packages/atrium_ddns.
# Guarding $(HOST_APP) alone is an assertion about a tree nothing imports —
# true, checkable, and not about the code under test.
#
# Today the two agree byte-for-byte (12 files, same digest) because the pip
# install is a build layer chained to the COPY. They stop agreeing the moment
# anything reaches $(HOST_APP) at run time rather than at build time — a bind
# mount of ./backend being the obvious one, and the obvious "fix" for the
# rebuild cost. Under that mount $(HOST_APP) matches the worktree *by
# construction*, on every run, forever: a guard that can only ever pass. The
# site-packages reading is the one that still bites.
#
# Resolved with importlib.find_spec rather than by a hardcoded
# site-packages path, so a python minor-version bump in the atrium base image
# does not turn this into a guard reading an empty directory.
check-host-pkg-fresh:  ## fail if the INSTALLED atrium_ddns is not this worktree's
	$(call CHECK_FRESH,check-host-pkg-fresh,backend/src/$(HOST_PKG),pkg:$(HOST_PKG),the installed $(HOST_PKG) package)

# The wire table. NOT part of `make test` and NOT part of the gate: it replays
# 114 cases against a running service, and which service that is has to be
# stated, not guessed. Defaulting either option to make this target "work" is
# the bug #8 was written about.
#
# BASE_URL is resolved from inside the api container, because that is the one
# environment guaranteed to have pytest and PyYAML. `http://api:8000` is this
# stack; a legacy service on the dev box is `http://host.docker.internal:<port>`
# on Docker Desktop. Nothing infers any of that — the runner uses the URL
# verbatim and prints the reachability probe it got back, and the three
# host-only cases carry the legacy body so a mis-paired target is caught by the
# response rather than by the URL.
#
# Freshness-gated too, for the same reason the gate is: the 131-case table is
# read out of the image, and replaying a frozen table from a previous revision
# against a live service is a worse lie than not running it. NOT gated on the
# two backend guards — the target may legitimately be the legacy service,
# which has nothing to do with this worktree's backend/.
#
# Invoked inside the recipe rather than as a prerequisite so the usage message
# still prints without a running stack. A prerequisite would answer "cannot
# reach the api container" to someone who typed the target with no arguments.
test-compat:  ## wire table vs a live service (TARGET=legacy|host BASE_URL=http://...)
	@if [ -z "$(TARGET)" ] || [ -z "$(BASE_URL)" ]; then \
		echo "usage: make test-compat TARGET=legacy|host BASE_URL=http://host:port"; \
		echo; \
		echo "Neither has a default, and this target will not invent one."; \
		echo "  TARGET   which implementation is behind BASE_URL. It decides"; \
		echo "           which cases run; guessing it runs the wrong table"; \
		echo "           against the wrong service and reports green."; \
		echo "  BASE_URL used verbatim, resolved from inside the api container."; \
		echo; \
		echo "See tests/compat/README.md for worked invocations."; \
		exit 2; \
	fi
	@$(MAKE) --no-print-directory check-compat-fresh
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest $(COMPAT_TESTS)/compat \
		--target "$(TARGET)" --base-url "$(BASE_URL)" -ra $(PYTEST_ARGS)

# Gated: this reports the same "the backend suite passed" claim as
# test-backend, one worker at a time, and a stale image lies the same way.
test-backend-serial: check-fresh  ## same, one worker — for bisecting a failing test
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest $(HOST_APP)/tests -n 0

# Deliberately NOT gated. This is the hang diagnostic: you reach for it when
# something is wrong, sometimes with a container you rebuilt by hand, and a
# guard that refuses to run it is a guard standing in front of the diagnosis.
# It makes no suite-wide claim, so there is nothing for staleness to falsify.
test-backend-file:  ## one file, verbose — the way to diagnose a hang (FILE=tests/x.py)
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest $(HOST_APP)/$(FILE) -n 0 -v

typecheck:  ## tsc --noEmit on the host bundle
	cd frontend && pnpm typecheck

smoke:  ## local smoke test (PASS=... [EMAIL=...] for the login checks)
	./scripts/smoke.sh $(if $(EMAIL),--user '$(EMAIL)',) $(if $(PASS),--pass '$(PASS)',--no-login)

# --- e2e (Playwright, in a real browser) ----------------------------------
#
# `make smoke` proves the stack answers over HTTP. It stays green through a
# bundle that loads and renders nothing, a nav item atrium never registered,
# and a board that throws in React — every one of which is invisible to curl
# and obvious to a browser. Five defects came out of one operator session
# against a stack whose smoke test was passing; this is the instrument that
# was missing.
#
# Three specs, deliberately: nav renders, the ui-parity §3.3.1 walk through
# the UI ending in a rendered resolution strip, and one negative. See
# frontend/tests-e2e/.
#
# Playwright runs on the HOST, not in a container: the chromium build is
# glibc-only and the frontend builder stage is node:26-alpine (musl). Same
# arrangement as atrium's `make smoke`.
E2E_EMAIL := admin@example.com
E2E_PASSWORD := e2e-pw-12345
# A fixed TOTP secret so the runner can compute valid codes without an
# authenticator. NOT a credential: this admin exists only in the e2e
# database, which `make e2e-down` deletes.
E2E_TOTP_SECRET := JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP
# The port comes from `.env`, the same way scripts/smoke.sh reads it, so a
# worktree that moved off the default is followed rather than guessed. Not
# `. ./.env` — that file holds unquoted values with spaces.
E2E_API_HOST_PORT := $(shell sed -n 's/^API_HOST_PORT=//p' .env 2>/dev/null | tail -n1)
E2E_BASE_URL ?= http://localhost:$(or $(E2E_API_HOST_PORT),8053)
# The tag compose.yaml gives this project's image, resolved the same way
# compose resolves it. Needed by check-bundle-fresh, which has to read the
# image *as built* and not the container's copy of it.
E2E_PROJECT := $(shell sed -n 's/^COMPOSE_PROJECT_NAME=//p' .env 2>/dev/null | tail -n1)
E2E_IMAGE := $(or $(E2E_PROJECT),atrium-ddns):latest

# ATRIUM_DDNS_COMPAT_STUB=1 is not optional here and its absence is quiet:
# without it `stub1` resolves to no adapter, every update answers 911, and
# the walk ends with a hostname and no strip — which reads like a UI bug.
# Passed in the environment rather than written to `.env`, because compose
# interpolation prefers the environment and this must not leak into a stack
# someone raises later with a plain `make up`.
# `--force-recreate` on api and worker is not belt-and-braces. Measured here
# on 2026-08-16: a plain `up -d --build` built an image carrying the new host
# bundle, tagged it, and left the containers running the previous one — six
# specs failed against a UI that had been merged an hour earlier, and the
# served bundle was 801,320 bytes where the freshly built image held 815,012.
# The containers are cheap to replace and the confusion is not.
e2e-up:  ## raise + migrate + seed the stack the e2e specs run against
	@if [ ! -f .env ]; then echo "creating .env from .env.example"; cp .env.example .env; fi
	ATRIUM_DDNS_COMPAT_STUB=1 $(COMPOSE) up -d --build
	ATRIUM_DDNS_COMPAT_STUB=1 $(COMPOSE) up -d --force-recreate --no-deps api worker
	@ready=0; for i in $$(seq 1 40); do \
		if curl -fsS $(E2E_BASE_URL)/api/readyz >/dev/null 2>&1; then ready=1; break; fi; \
		sleep 3; \
	done; \
	if [ "$$ready" != "1" ]; then \
		echo "the api at $(E2E_BASE_URL) did not become ready in 120s"; \
		$(COMPOSE) logs --tail 50 api; \
		exit 1; \
	fi
	$(MAKE) --no-print-directory migrate
	$(COMPOSE) exec -T api python -m app.scripts.seed_admin \
		--email "$(E2E_EMAIL)" --password "$(E2E_PASSWORD)" \
		--full-name 'E2E Admin' --super-admin --totp-secret "$(E2E_TOTP_SECRET)"
	$(MAKE) --no-print-directory seed-bundle
	$(MAKE) --no-print-directory check-bundle-fresh
	@echo "e2e stack ready at $(E2E_BASE_URL)"

# The frontend's `check-fresh`, and it is here for the same reason the three
# backend guards are: the bundle is baked into the image at build time, and a
# stack running a previous image serves a previous UI while every process
# reports healthy. It cost this repo one confusing run — six specs red
# against a merged, correct UI.
#
# Two readings of one artefact, by two instruments that cannot share a
# mistake: the file inside the image compose just built, hashed by the
# container's own interpreter, and the bytes a browser would actually be
# served, hashed on the host after crossing the published port. A mismatch
# names both digests rather than saying "stale".
#
# Deliberately NOT a comparison against a locally built `frontend/dist`:
# that would require a host-side `pnpm build` on every gate run, and it
# would be asserting about a tree the deployed stack never reads.
check-bundle-fresh:  ## fail if the SERVED host bundle is not the one in this project's image
	@img=$$(docker run --rm --entrypoint /opt/venv/bin/python $(E2E_IMAGE) -c \
		"import hashlib,pathlib;p=pathlib.Path('/opt/atrium/static/host/main.js');b=p.read_bytes();print(hashlib.sha256(b).hexdigest(), len(b))"); \
	served=$$(curl -fsS $(E2E_BASE_URL)/host/main.js | python3 -c \
		"import hashlib,sys;b=sys.stdin.buffer.read();print(hashlib.sha256(b).hexdigest(), len(b))"); \
	if [ -z "$$img" ] || [ -z "$$served" ]; then \
		echo "check-bundle-fresh: could not read both bundles (image='$$img' served='$$served')"; \
		exit 1; \
	fi; \
	if [ "$$img" != "$$served" ]; then \
		echo "check-bundle-fresh: the stack is SERVING a different host bundle than the image it was built from."; \
		echo "  image  ($(E2E_IMAGE))  $$img"; \
		echo "  served ($(E2E_BASE_URL)/host/main.js)  $$served"; \
		echo "  (sha256 / bytes)"; \
		echo "  The containers are running an older image. Run: make e2e-up"; \
		exit 1; \
	fi; \
	echo "check-bundle-fresh: served bundle matches the image ($$img)"

e2e-deps:  ## host-side Playwright deps (idempotent no-op once present)
	cd frontend && pnpm install --frozen-lockfile 2>/dev/null || (cd frontend && pnpm install)
	cd frontend && pnpm exec playwright install chromium

# The stack has to be up for these, and raising it is part of the target on
# purpose: "runs from a clean checkout with no manual steps" is the
# acceptance criterion, and a target that assumes a stack is one more manual
# step written down somewhere else.
test-e2e: e2e-up e2e-deps  ## Playwright specs against a real browser
	cd frontend && E2E_BASE_URL=$(E2E_BASE_URL) \
		E2E_ADMIN_EMAIL=$(E2E_EMAIL) E2E_ADMIN_PASSWORD=$(E2E_PASSWORD) \
		E2E_ADMIN_TOTP_SECRET=$(E2E_TOTP_SECRET) \
		pnpm exec playwright test $(PLAYWRIGHT_ARGS)

e2e-down:  ## stop the e2e stack AND delete its database volume
	$(COMPOSE) down -v

# --- TLS (prod, "for now" measure) ----------------------------------------
# The certificate is a COPY of one in the old service's ACME store. That store
# has a live writer; when it renews, this copy is stale and TLS serves an
# expired certificate. Re-run tls-refresh after a renewal.
ACME_JSON ?= /usr/local/dyndns-route53/letsencrypt/acme.json
TLS_CERT_DIR ?= ./certs

tls-extract:  ## copy cert+key out of the old service's ACME store
	./scripts/extract-acme-cert.sh $(ACME_JSON) $(TLS_CERT_DIR)

tls-up: tls-extract  ## start the TLS terminator in front of api
	$(COMPOSE) --profile tls up -d proxy

tls-refresh: tls-extract  ## re-extract after a renewal and reload the proxy
	$(COMPOSE) --profile tls restart proxy
	@echo "reloaded; verify with: make tls-verify HOST=<name>"

# CONNECT exists so this can be pointed at a stack that does not resolve —
# a throwaway one on loopback, or the deploy host before DNS moves. HOST stays
# the SNI name either way: a check that reaches the right socket with the
# wrong SNI is a check of the default certificate, not of the router's.
tls-verify:  ## prove TLS actually serves a valid chain (HOST=<name> required)
	@test -n "$(HOST)" || { echo "usage: make tls-verify HOST=<name> [PORT=8443] [CONNECT=<addr>]"; exit 2; }
	@echo | openssl s_client -connect $(or $(CONNECT),$(HOST)):$(or $(PORT),8443) -servername $(HOST) 2>/dev/null \
	  | openssl x509 -noout -subject -dates -checkend 0 \
	  && echo "  chain valid and not expired"

# --- ACME hand-over (cutover; docs/ops/cutover.md § 2.2 and § 5.4) ---------
# Everything above serves a COPY of the incumbent's certificate. Everything
# below is the arrangement that stops copying: this stack's own ACME store,
# its own resolver, 80/443. It lives in `compose.acme.yaml` — a file the
# operator names on purpose — so that a routine `docker compose up -d` can
# never perform the hand-over by accident.
COMPOSE_ACME := $(COMPOSE) -f compose.yaml -f compose.acme.yaml

acme-config:  ## render the hand-over configuration without starting anything
	$(COMPOSE_ACME) --profile tls config

# Deliberately NOT `up -d` on the whole stack: this replaces the proxy and
# nothing else. Runbook step 5.4(c).
acme-up:  ## start the TLS terminator with ACME on 80/443 (THE HAND-OVER)
	$(COMPOSE_ACME) --profile tls up -d proxy

acme-down:  ## stop it again — runbook rollback for step 5.4(c)
	$(COMPOSE_ACME) --profile tls stop proxy

# The check that distinguishes "TLS works" from "the hand-over took".
acme-verify:  ## which certificate is on the wire (HOST=<name> required)
	@test -n "$(HOST)" || { echo "usage: make acme-verify HOST=<name> [PORT=443] [CONNECT=<addr>]"; exit 2; }
	./scripts/verify-acme-handover.sh \
	  --store $(or $(ACME_STORE_DIR),./letsencrypt)/acme.json \
	  --host $(HOST) --port $(or $(PORT),443) \
	  $(if $(CONNECT),--connect $(CONNECT),) \
	  --fallback $(TLS_CERT_DIR)/cert.pem

test-acme-handover:  ## gate-test the hand-over on a throwaway stack (LIVE=1 adds LE staging)
	./scripts/test-acme-handover.sh $(if $(LIVE),--live-staging,)
