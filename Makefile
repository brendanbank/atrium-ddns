# Atrium Ddns — top-level Make targets.
#
# Convenience wrappers around the docker-compose stack. Most of these
# assume you ran `make dev-bootstrap` first to bring everything up.

COMPOSE := docker compose

.PHONY: help dev-bootstrap up down build logs ps migrate \
	seed-admin seed-bundle test test-frontend test-backend typecheck

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

test-backend:  ## backend tests inside the api container (parallel)
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest /opt/host_app/tests $(PYTEST_ARGS)

test-backend-serial:  ## same, one worker — for bisecting a failing test
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest /opt/host_app/tests -n 0

test-backend-file:  ## one file, verbose — the way to diagnose a hang (FILE=tests/x.py)
	$(COMPOSE) exec -T api /opt/venv/bin/python -m pytest /opt/host_app/$(FILE) -n 0 -v

typecheck:  ## tsc --noEmit on the host bundle
	cd frontend && pnpm typecheck

smoke:  ## local smoke test against the running stack (PASS=... for login checks)
	./scripts/smoke.sh $(if $(PASS),--pass '$(PASS)',--no-login)
