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
	test-backend-serial test-backend-file typecheck smoke

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

tls-verify:  ## prove TLS actually serves a valid chain (HOST=<name> required)
	@test -n "$(HOST)" || { echo "usage: make tls-verify HOST=<name> [PORT=8443]"; exit 2; }
	@echo | openssl s_client -connect $(HOST):$(or $(PORT),8443) -servername $(HOST) 2>/dev/null \
	  | openssl x509 -noout -subject -dates -checkend 0 \
	  && echo "  chain valid and not expired"
