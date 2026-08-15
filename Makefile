# Atrium Ddns — top-level Make targets.
#
# Convenience wrappers around the docker-compose stack. Most of these
# assume you ran `make dev-bootstrap` first to bring everything up.

COMPOSE := docker compose

# Where the Dockerfile's `dev` stage puts the repo-root `tests/` tree. Mirrors
# the repo layout, so `/opt/compat_tests/compat/...` is `tests/compat/...`.
COMPAT_TESTS := /opt/compat_tests

# Content digest of a directory tree: every file's path and bytes, in sorted
# order. Run against the worktree and against the container (see
# check-compat-fresh) — one script, so the two readings cannot differ by
# method. Exported rather than inlined because the recipe would otherwise be
# unreadable through two layers of quoting.
define COMPAT_DIGEST_PY
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
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
print(digest.hexdigest())
endef
export COMPAT_DIGEST_PY

.PHONY: help dev-bootstrap up down build logs ps migrate \
	seed-admin seed-bundle test test-frontend test-backend test-compat \
	check-compat-fresh \
	test-backend-serial test-backend-file typecheck smoke

help:  ## show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

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
test-backend: check-compat-fresh  ## backend tests + service-free compat guards, in the api container
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest /opt/host_app/tests $(PYTEST_ARGS)
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest $(COMPAT_TESTS) -ra $(PYTEST_ARGS)

# `tests/` is baked into the image, and `make up` does not rebuild. So editing
# a compat file and re-running the gate reads the *old* copy and reports green
# for it — the same defect compose.yaml's `image:` comment records for a shared
# tag, one layer along, and the one this whole issue is about.
#
# Two readings of one tree, and they must agree. `docker compose exec` is the
# only instrument that can see what the running container actually holds;
# anything derived from the Dockerfile or from a build log is a statement about
# what should be there.
check-compat-fresh:  ## fail if the container's copy of tests/ is not this worktree's
	@host=$$(python3 -c "$$COMPAT_DIGEST_PY" tests | tr -d '\r'); \
	img=$$($(COMPOSE) exec -T api /opt/venv/bin/python -c "$$COMPAT_DIGEST_PY" \
		$(COMPAT_TESTS) | tr -d '\r'); \
	if [ -z "$$host" ] || [ -z "$$img" ]; then \
		echo "check-compat-fresh: could not read both digests (worktree=$$host container=$$img)"; \
		exit 1; \
	fi; \
	if [ "$$host" != "$$img" ]; then \
		echo "check-compat-fresh: the api container is running a STALE copy of tests/."; \
		echo "  worktree  $$host"; \
		echo "  container $$img"; \
		echo "  tests/ is COPYed into the image (Dockerfile, dev stage) and"; \
		echo "  \`make up\` does not rebuild. Run: make build && make up"; \
		exit 1; \
	fi; \
	echo "check-compat-fresh: container matches worktree ($$host)"

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
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest $(COMPAT_TESTS)/compat \
		--target "$(TARGET)" --base-url "$(BASE_URL)" -ra $(PYTEST_ARGS)

test-backend-serial:  ## same, one worker — for bisecting a failing test
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest /opt/host_app/tests -n 0

test-backend-file:  ## one file, verbose — the way to diagnose a hang (FILE=tests/x.py)
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest /opt/host_app/$(FILE) -n 0 -v

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
