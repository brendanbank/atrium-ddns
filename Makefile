# Atrium Ddns — top-level Make targets.
#
# Convenience wrappers around the docker-compose stack. Most of these
# assume you ran `make dev-bootstrap` first to bring everything up.

# Compose's project name decides the container names, the network, the
# volume, AND the tag of the image it builds. Left to compose, it is derived
# from the directory you invoked make *from* — the logical path, symlink and
# all.
#
# That is not hypothetical here. This worktree is reachable both as its real
# path and through a Conductor symlink pointing at it, so the *same
# directory* became two projects: two sets of containers, two volumes, and
# both binding API_HOST_PORT/MYSQL_HOST_PORT out of the same `.env`. The
# second stack to start dies with "port is already allocated", and — worse,
# because it is quiet — `make dev-down` from one path leaves the stack
# created from the other path running with its data intact.
#
# So the name is pinned, and derived from the **resolved** directory: a
# symlink and its target agree, while two genuinely separate worktrees still
# get separate stacks. Lowercased and punctuation-folded because compose
# rejects a project name that is not [a-z0-9_-].
#
# Override it (COMPOSE_PROJECT=other) to run a second stack from one
# worktree — but give it its own ports in `.env` first, or it collides for
# exactly the reason above.
COMPOSE_PROJECT ?= $(shell basename "$(realpath $(dir $(firstword $(MAKEFILE_LIST))))" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g')
COMPOSE := docker compose -p $(COMPOSE_PROJECT)

# The scripts shell out to `docker compose` themselves and would otherwise
# re-derive the name from *their* cwd, which is the bug this file just
# fixed. Exported rather than passed per-call so a script added later
# inherits it without having to know.
export COMPOSE_PROJECT

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

.PHONY: help dev-bootstrap dev-up dev-down up down build logs ps migrate gate \
	seed-admin seed-bundle seed-compat-fixture verify-compat-rehash \
	test test-frontend test-backend test-compat check-compat-executed \
	check-fresh check-compat-fresh check-backend-fresh check-host-pkg-fresh \
	test-backend-serial test-backend-file typecheck smoke \
	e2e-up e2e-deps e2e-down test-e2e check-bundle-fresh \
	atrium-bump

help:  ## show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-21s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

dev-bootstrap: build up migrate  ## build, start, migrate; run me first
	@echo
	@echo "Stack is up. Next: make seed-admin EMAIL=you@example.com PASSWORD=..."
	@echo "Then: make seed-bundle && open http://localhost:8000"

# --- .env, and refusing to start a stack that cannot live (#137) ------------
#
# `.env.example` ships `replace-me-with-openssl-rand-hex-32` for
# SECRET_ENCRYPTION_KEY. atrium checks that key's SHAPE in every environment,
# so a `.env` that is a straight copy of the example boots an api that raises
# ValidationError, is restarted by `restart: unless-stopped`, and raises it
# again — while `make up` prints "Started" and exits 0. Measured here on
# 2026-09-01: exit 0, RestartCount climbing 5→8, curl on the published port
# refused. The defect is the successful report over something already dead.
#
# Two answers, and they are not exclusive:
#
#   `make env`   mints a working `.env` — the targets that already created one
#                behind your back (dev-up, e2e-up) now create a live one
#                instead of a dead one.
#   `check-env`  refuses before compose is called, naming the key and the fix.
#
# The guard is the one that generalises, so it is a prerequisite of `up` rather
# than a step inside it: it runs on every path that reaches `up`, including
# `dev-bootstrap` and `make gate`'s own `$(MAKE) up migrate`.
#
# Note `up` does NOT create `.env`. It never did, and a target that starts a
# stack is the wrong place to learn that a file was written for you. It refuses
# and names `make env`.
env:  ## create .env from .env.example with freshly generated secrets
	@# ONE shell, deliberately. This was five separate recipe lines, and make
	@# runs each in its own shell — so the `exit $$?` that follows "already
	@# exists" ended only *that* shell, and make went straight on to
	@# `cp .env.example .env`. The target printed "left untouched" and
	@# overwrote the file in the same breath.
	@#
	@# It cost real data before it was found: a developer .env holding the
	@# credentials a running MySQL volume had been initialised with was
	@# replaced by the example's defaults, leaving a database nothing could
	@# authenticate to, and CI's e2e .env was replaced mid-run — which moved
	@# API_HOST_PORT from 8000 to 8053 while make had already expanded
	@# E2E_BASE_URL from the old value, so the readiness probe polled a port
	@# nothing was listening on and timed out after 120s naming neither cause.
	@#
	@# Guarded by `make check-env-idempotent`, which asserts the file is
	@# byte-identical after a second run.
	@set -e; \
	if [ -f .env ]; then \
		echo "make env: .env already exists — left untouched"; \
		exec ./scripts/check-env.sh; \
	fi; \
	cp .env.example .env; \
	for spec in "APP_SECRET_KEY 48" "JWT_SECRET 48" "SECRET_ENCRYPTION_KEY 32"; do \
		set -- $$spec; key=$$1; bytes=$$2; \
		val=$$(openssl rand -hex $$bytes); \
		tmp=$$(mktemp); \
		awk -v k="$$key" -v v="$$val" '$$0 ~ "^" k "=" && !seen { print k "=" v; seen=1; next } { print }' \
			.env > $$tmp && mv $$tmp .env; \
	done; \
	chmod 600 .env; \
	echo "make env: wrote .env from .env.example, with fresh values for"; \
	echo "          APP_SECRET_KEY, JWT_SECRET and SECRET_ENCRYPTION_KEY."; \
	echo "          Back SECRET_ENCRYPTION_KEY up: lose it and every stored"; \
	echo "          provider credential is unrecoverable."; \
	echo "          Ports are still the .env.example defaults — change"; \
	echo "          API_HOST_PORT / MYSQL_HOST_PORT if they collide."; \
	./scripts/check-env.sh

check-env-idempotent:  ## `make env` must never modify an existing .env
	@# Runs the REAL recipe in the REAL directory, against a valid .env, and
	@# compares the file's digest. The first version of this guard ran make
	@# from a temp dir with no scripts/, so check-env.sh was not found and
	@# `set -e` aborted before the cp — in BOTH the fixed and broken recipes.
	@# It printed PASS against a deliberately reintroduced defect: a probe
	@# that could not fail, written while guarding against that very family.
	@#
	@# Two conditions are load-bearing. The .env must be VALID, because the
	@# broken recipe only reached its `cp` when check-env.sh exited 0. And the
	@# assertion is on the file, not the output: the broken version printed
	@# "left untouched" and overwrote it in the same breath.
	@set -e; \
	had=0; bak=""; \
	if [ -f .env ]; then had=1; bak=$$(mktemp); cp .env $$bak; else $(MAKE) --no-print-directory env >/dev/null; fi; \
	before=$$(md5 -q .env 2>/dev/null || md5sum .env | cut -d" " -f1); \
	set +e; $(MAKE) --no-print-directory env >/dev/null 2>&1; set -e; \
	after=$$(md5 -q .env 2>/dev/null || md5sum .env | cut -d" " -f1); \
	if [ "$$had" = 1 ]; then cp $$bak .env; rm -f $$bak; else rm -f .env; fi; \
	if [ "$$before" != "$$after" ]; then \
		echo "check-env-idempotent: FAIL — make env modified an existing .env"; \
		echo "  before $$before"; \
		echo "  after  $$after"; \
		exit 1; \
	fi; \
	echo "check-env-idempotent: PASS — an existing .env is byte-identical after make env"

check-env:  ## refuse to start a stack whose .env cannot produce a living api
	@./scripts/check-env.sh

check-env-self-test:  ## show the .env guard refusing, against synthetic files
	@./scripts/check-env.sh --self-test

up: check-env  ## start the stack. NEVER builds — see below.
	@# `--no-build` is not a tidy-up. Each worktree is its own compose
	@# project, so its image tag is unique and a plain `up` builds a
	@# 301 MB image from scratch, silently, as a side effect of starting
	@# a stack. Twelve of those were built in one day before anyone
	@# noticed. Building is now `make build`, explicitly, by someone who
	@# meant it. If this refuses with "needs to be built", that is the
	@# guard working: run `make build` if you actually want to pay for it.
	$(COMPOSE) up -d --no-build

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

# --- A dev stack you sign in to yourself ------------------------------------
#
# `make dev-bootstrap` raises a stack, and `make seed-admin EMAIL=… PASSWORD=…`
# gives it an admin — but one typed on the command line, with no TOTP. So it
# is not the account you actually log in as, and the password ends up in
# your shell history and in make's own echo of the recipe.
#
# `make dev-up` is the same stack seeded from a 1Password login item:
# username, password *and* the one-time password secret, so your
# authenticator's codes work against it. scripts/dev-admin.sh holds the
# reasoning, including why the item is named in a gitignored file rather
# than here.
#
# The port is read from `.env` the way scripts/smoke.sh reads it, so a
# worktree that moved off the default is followed rather than guessed.
DEV_API_HOST_PORT := $(shell sed -n 's/^API_HOST_PORT=//p' .env 2>/dev/null | tail -n1)
DEV_MYSQL_HOST_PORT := $(shell sed -n 's/^MYSQL_HOST_PORT=//p' .env 2>/dev/null | tail -n1)
DEV_BASE_URL ?= http://localhost:$(or $(DEV_API_HOST_PORT),8053)

dev-up:  ## raise the stack + seed YOUR admin from 1Password (see scripts/dev-admin.sh)
	@# Was `cp .env.example .env`, which produced a file whose
	@# SECRET_ENCRYPTION_KEY atrium refuses — a dead api behind a green start.
	@# `make env` copies the same file and mints real values into it, and ends
	@# in the guard, so an existing hand-edited `.env` is checked rather than
	@# overwritten. See the block above `up` (#137).
	@$(MAKE) --no-print-directory env
	@# Refuse before building anything. A five-minute image build that ends
	@# in "the 1Password CLI is not signed in" is a worse way to learn it.
	./scripts/dev-admin.sh --check
	@# Ports, before the build. Compose reports a clash as "Bind for
	@# :::13353 failed: port is already allocated" from the daemon, after
	@# it has built an image and created half the stack — and it names
	@# neither what is holding the port nor that `.env` is where you
	@# change it. Checked against this project's own containers too, so
	@# "my own stack is already up" reads differently from "something
	@# else has that port".
	@for pair in "API_HOST_PORT $(or $(DEV_API_HOST_PORT),8053)" "MYSQL_HOST_PORT $(or $(DEV_MYSQL_HOST_PORT),13353)"; do \
		set -- $$pair; key=$$1; port=$$2; \
		holder=$$(docker ps --format '{{.Label "com.docker.compose.project"}}\t{{.Names}}\t{{.Ports}}' \
			| awk -v p=":$$port->" '$$0 ~ p {print $$1"/"$$2; exit}'); \
		if [ -n "$$holder" ]; then \
			case "$$holder" in \
				$(COMPOSE_PROJECT)/*) echo "note: $$holder already publishes $$port — it will be recreated" ;; \
				*) echo "port $$port ($$key) is taken by container $$holder"; \
				   echo "  stop it, or give this stack its own ports in .env"; \
				   exit 1 ;; \
			esac; \
		elif lsof -nP -iTCP:$$port -sTCP:LISTEN >/dev/null 2>&1; then \
			echo "port $$port ($$key) is in use by a process outside docker"; \
			echo "  lsof -nP -iTCP:$$port -sTCP:LISTEN   names it"; \
			echo "  or give this stack its own ports in .env"; \
			exit 1; \
		fi; \
	done
	$(COMPOSE) up -d --build
	@# Not belt-and-braces, and the same reason e2e-up does it: a plain
	@# `up -d --build` can build an image carrying a new host bundle, tag
	@# it, and leave the containers running the previous one. Measured in
	@# this repo on 2026-08-16.
	$(COMPOSE) up -d --force-recreate --no-deps api worker
	@ready=0; for i in $$(seq 1 40); do \
		if curl -fsS $(DEV_BASE_URL)/api/readyz >/dev/null 2>&1; then ready=1; break; fi; \
		sleep 3; \
	done; \
	if [ "$$ready" != "1" ]; then \
		echo "the api at $(DEV_BASE_URL) did not become ready in 120s"; \
		echo "  /api/readyz answered:"; \
		curl -sS -m 5 $(DEV_BASE_URL)/api/readyz 2>&1 | head -c 400 | sed 's/^/    /'; \
		echo; \
		$(COMPOSE) logs --tail 50 api; \
		exit 1; \
	fi
	$(MAKE) --no-print-directory migrate
	@./scripts/dev-admin.sh
	$(MAKE) --no-print-directory seed-bundle
	$(MAKE) --no-print-directory check-bundle-fresh
	@echo "dev stack ready at $(DEV_BASE_URL) — sign in with that 1Password item"
	@echo "  make down       stops it and keeps the database"
	@echo "  make dev-down   shreds it, database and all"
	@echo "  make ui-live / make ui   for UI edits without an image rebuild"

# The counterpart to dev-up, and deliberately not a synonym for `down`.
#
# `down` stops the containers and leaves the volume, so `make up` comes
# back to the same database. This removes the volume — which is the whole
# point of having a second target, and the reason it asks first.
#
# What goes with the volume is not just rows. It holds every zone, device
# and name you created by hand, and the **encrypted** provider
# credentials: `SECRET_ENCRYPTION_KEY` lives in `.env` and survives this,
# but the ciphertext it decrypts does not, so a Route 53 key entered
# through the UI is gone and has to be pasted in again.
#
# The image is left alone. It is build output rather than state, and
# keeping it is what makes the next `make dev-up` fast.
#
# FORCE=1 skips the prompt, for a script that already knows. Without a
# TTY the `read` gets nothing and the default answer is no, which is the
# right way round for a target that deletes a database.
dev-down:  ## shred the dev stack — containers, network AND the database volume
	@if [ "$(FORCE)" != "1" ]; then \
		echo "This removes the containers, the network, and this volume:"; \
		$(COMPOSE) config --volumes 2>/dev/null | sed 's/^/  - /'; \
		echo "Every zone, device, name and stored provider credential goes with it."; \
		echo "(\`make down\` stops the stack and keeps all of it.)"; \
		printf 'Shred it? [y/N] '; \
		read -r reply || reply=; \
		case "$$reply" in \
			[yY]|[yY][eE][sS]) ;; \
			*) echo "nothing was removed"; exit 1 ;; \
		esac; \
	fi
	$(COMPOSE) down -v --remove-orphans
	@echo "shredded — 'make dev-up' builds it again from scratch"

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
# the frozen table against a running service, and which service that is has to
# be stated, not guessed. Defaulting either option to make this target "work"
# is the bug #8 was written about.
#
# No case count in this sentence, deliberately. It read "114 cases" for three
# freezes after the table stopped having 114 of them, which is the inherited
# number `docs/ops/overnight-template.md` keeps telling agents not to write
# down. The runner prints its own accounting block on every run, derived from
# the table it actually loaded; that is the reading, and it cannot go stale.
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

# Did the table actually run, or did it merely exit 0?
#
# `test-compat` above answers "did anything fail". It does NOT answer "did
# anything run", and the two come apart in exactly the ways that matter:
#
#   $ make test-compat TARGET=host BASE_URL=... PYTEST_ARGS='-k checkip-default-is-html'
#   1 passed ... NOT RUN 126
#
# — exit 0, a green summary line, and one case of the table exercised. A CI job
# whose only instrument is `make test-compat`'s exit code reports that as a
# clean wire table. So CI captures the run and reads the runner's own
# accounting block back, and this target is what does the reading.
#
# Four ways it goes red, and none of them is "a case failed" (test-compat owns
# that): the freshness guard's PASS line is absent, so the table may have been
# replayed out of a stale image; the runner printed NOT RUN, in either of its
# two spellings — the whole table (no `--target`) or a shortfall (`-k`, or
# collection stopping early); the accounting block is unreadable, which is NOT
# zero and NOT a pass; or fewer cases passed than the block says were
# executable.
#
# Deliberately NOT folded into `test-compat`. That target is also the
# diagnostic — `PYTEST_ARGS='-k one-case'` is how anyone bisects a failing
# case, and a guard that refuses a filtered run is a guard standing in front of
# the diagnosis. The vacuity question belongs to the unattended caller, which
# is CI, and it is asked there.
#
# No expected count in here. The invariants are relational (`passed` equals
# `executable`, shortfall is nought), so freezing the table at a different size
# cannot make this target stale — which is the one property a hardcoded 124
# would not have had.
check-compat-executed:  ## assert a captured `make test-compat` log really ran the table (LOG=<file>)
	@set -u; \
	log="$(LOG)"; \
	if [ -z "$$log" ]; then \
		echo "usage: make check-compat-executed LOG=<file>"; \
		echo; \
		echo "  <file> is what a \`make test-compat ... 2>&1 | tee <file>\` run wrote."; \
		exit 2; \
	fi; \
	if [ ! -s "$$log" ]; then \
		echo "check-compat-executed: '$$log' is missing or empty."; \
		echo "  An absent log is not a clean run. Refusing."; \
		exit 1; \
	fi; \
	fail=0; \
	if ! grep -q 'check-compat-fresh: container matches worktree' "$$log"; then \
		echo "check-compat-executed: the freshness guard's PASS line is not in '$$log'."; \
		echo "  Either check-compat-fresh never ran, or the run routed around it."; \
		echo "  A table replayed from a stale image reports green for the wrong"; \
		echo "  cases, which is worse than not running it."; \
		fail=1; \
	fi; \
	if grep -q 'NOT RUN' "$$log"; then \
		echo "check-compat-executed: the runner reported cases NOT RUN:"; \
		grep 'NOT RUN' "$$log" | sed 's/^/    /'; \
		fail=1; \
	fi; \
	if ! grep -q '^  reachability probe ' "$$log"; then \
		echo "check-compat-executed: no reachability probe in '$$log' — the runner"; \
		echo "  never established that anything answered at --base-url."; \
		fail=1; \
	fi; \
	executable=`awk '$$1=="executable" && $$2 ~ /^[0-9]+$$/ {print $$2}' "$$log" | tail -1`; \
	passed=`awk '/^  ran this session /{for(i=1;i<=NF;i++) if($$i ~ /^passed/) print $$(i-1)}' "$$log" | tail -1`; \
	if [ -z "$$executable" ] || [ -z "$$passed" ]; then \
		echo "check-compat-executed: could not read the accounting block from '$$log'"; \
		echo "  (executable='$$executable' passed='$$passed')."; \
		echo "  An unreadable block is not nought and is not a pass. Refusing."; \
		fail=1; \
	elif [ "$$executable" -lt 1 ]; then \
		echo "check-compat-executed: executable=$$executable — the table selected"; \
		echo "  no case at all for this target. That is not a green wire table."; \
		fail=1; \
	elif [ "$$passed" -ne "$$executable" ]; then \
		echo "check-compat-executed: $$passed passed, but the accounting block says"; \
		echo "  $$executable were executable. Every executable case must be one of"; \
		echo "  them."; \
		fail=1; \
	fi; \
	if [ "$$fail" -eq 0 ]; then \
		echo "check-compat-executed: $$passed of $$executable executable cases passed,"; \
		echo "  0 selected-but-unexecuted, freshness guard PASSED, service probed."; \
	else \
		echo "check-compat-executed: REFUSING to call '$$log' a wire-table run."; \
	fi; \
	exit $$fail

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

# The whole gate, as one command that decides for itself what to run.
#
# It replaces a checklist an agent executed by hand. That checklist had to be
# conditional — a Playwright run cannot fail because of a `.gitignore` edit —
# and every place the condition lived was a place to get it wrong: the
# orchestrator's brief said "run the full gate" for an issue that touched no
# code, an agent improvised where the brief was silent, and a two-line change
# spent half an hour in the suite. The judgement was the defect, not the
# commands.
#
# So the judgement moves here, where it is read from the diff rather than from
# a person, and it prints what it ran AND what it skipped and why — the output
# is the PR-body evidence, so a skip is a visible decision instead of a silent
# absence.
#
# BASE is what "changed" is measured against — the milestone branch during a
# run, trunk otherwise.
# Read from `.overnight.conf` the way every other target here reads `.env` —
# one key, with sed, never sourced. The first version of this consulted only
# `$(OVERNIGHT_MILESTONE_BRANCH)`, which make sees only if it is *exported*;
# `.overnight.conf` is a file the shell scripts source, not something make
# inherits. So it silently fell through to `master` during a run, diffed the
# whole milestone branch against trunk, and reported every already-merged file
# as changed. Over-inclusive rather than unsafe, and invisible: a gate that
# runs too much still says PASS.
GATE_CONF_BRANCH := $(shell sed -n 's/^OVERNIGHT_MILESTONE_BRANCH=//p' .overnight.conf 2>/dev/null | tail -n1)
GATE_BASE ?= $(or $(OVERNIGHT_MILESTONE_BRANCH),$(GATE_CONF_BRANCH),$(OVERNIGHT_TRUNK),master)

# A third scope, added by #137: a guard nothing invokes is a guard that passes
# because it never ran. `Makefile`, `compose.yaml`, `.env.example` and the
# shell scripts reach no pytest and no vitest, so a diff confined to them used
# to print "this diff reaches no test" and that was the whole reading. The
# `.env` guard's own `--self-test` is the test they reach.
gate:  ## unit tests only. No docker, ever. Only what the diff can reach.
	@# TWO RULES, both operator decisions of 2026-09-01:
	@#
	@# 1. UNIT TESTS ONLY. `make test-backend` is NOT a unit suite — it is 933
	@#    tests against a real MySQL, ten xdist workers sharing one database.
	@#    That is a functional suite, and its shared state is what generates
	@#    the race conditions this project keeps chasing (#117, #149). It runs
	@#    deliberately, or at the milestone e2e, never here.
	@#
	@# 2. NO DOCKER. A unit test does not need a container. Both suites below
	@#    run on the host: vitest, and the service-free `pytest tests/` that
	@#    CI runs as `compat-guards` with nothing but pytest and pyyaml.
	@#
	@# And a change runs a suite only if it can change that suite's result.
	@# Reaching no test is a RESULT, not a skipped step.
	@set -e; \
	base="$(GATE_BASE)"; \
	git rev-parse --verify -q "origin/$$base" >/dev/null 2>&1 && base="origin/$$base"; \
	changed=$$( { git diff --name-only "$$base"...HEAD 2>/dev/null; git diff --name-only; git ls-files -o --exclude-standard; } | sort -u ); \
	if [ -z "$$changed" ]; then echo "gate: no changes against $$base — nothing to check"; exit 0; fi; \
	fe=$$(printf '%s\n' "$$changed" | grep -cE '^frontend/' || true); \
	be=$$(printf '%s\n' "$$changed" | grep -cE '^(backend/|tests/|scripts/.*\.py)' || true); \
	echo "gate: $$(printf '%s\n' "$$changed" | wc -l | tr -d ' ') file(s) changed against $$base"; \
	if [ "$$fe" -gt 0 ]; then \
		echo "gate: frontend ($$fe file(s)) -> typecheck + vitest"; \
		( cd frontend && pnpm install --frozen-lockfile >/dev/null && pnpm typecheck && pnpm test --run ); \
	else \
		echo "gate: SKIP frontend — nothing under frontend/ changed."; \
	fi; \
	if [ "$$be" -gt 0 ]; then \
		echo "gate: backend ($$be file(s)) -> service-free unit tests"; \
		[ -x .gate-venv/bin/python ] || { python3 -m venv .gate-venv && .gate-venv/bin/pip install -q --disable-pip-version-check pytest pyyaml; }; \
		.gate-venv/bin/python -m pytest tests/ -q; \
	else \
		echo "gate: SKIP backend — nothing under backend/, tests/ or scripts/*.py changed."; \
	fi; \
	if [ "$$fe" -eq 0 ] && [ "$$be" -eq 0 ]; then \
		echo "gate: this diff reaches no test, and that is the result rather than"; \
		echo "gate: a skipped step. Evidence is the diff plus a direct demonstration."; \
	fi; \
	echo "gate: no container was started."; \
	echo "gate: PASS"

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
# Ask compose which image it will actually run, rather than guessing the
# name from `.env`.
#
# `COMPOSE_PROJECT_NAME` is usually ABSENT from `.env` — compose defaults
# it to the directory name — so the old derivation fell through to a
# hardcoded `atrium-ddns:latest`. In a worktree called anything else that
# is a different image, often a stale one, and `check-bundle-fresh` then
# compared the served bundle against an artefact nobody had built today.
# It reported "the containers are running an older image" about an image
# the containers had never run.
#
# The guard is only worth having if it names its target the way the thing
# under test does. This asks compose.
E2E_IMAGE := $(shell $(COMPOSE) config --images 2>/dev/null | grep -v '^mysql' | head -n1)

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
	@# Same substitution as dev-up, and this is the line issue #137 was
	@# written from: it was the first thing a fresh worktree ran, and it wrote
	@# a `.env` whose SECRET_ENCRYPTION_KEY the api refuses.
	@$(MAKE) --no-print-directory env
	ATRIUM_DDNS_COMPAT_STUB=1 $(COMPOSE) up -d --build
	ATRIUM_DDNS_COMPAT_STUB=1 $(COMPOSE) up -d --force-recreate --no-deps api worker
	@ready=0; for i in $$(seq 1 40); do \
		if curl -fsS $(E2E_BASE_URL)/api/readyz >/dev/null 2>&1; then ready=1; break; fi; \
		sleep 3; \
	done; \
	if [ "$$ready" != "1" ]; then \
		echo "the api at $(E2E_BASE_URL) did not become ready in 120s"; \
		echo "  /api/readyz answered:"; \
		curl -sS -m 5 $(E2E_BASE_URL)/api/readyz 2>&1 | head -c 400 | sed 's/^/    /'; \
		echo; \
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
ui-live:  ## start the stack with frontend/dist bind-mounted (no image rebuild per UI change)
	cd frontend && pnpm build
	$(COMPOSE) -f compose.yaml -f compose.dev.yaml up -d api
	@echo
	@echo "  live UI mount is on. Loop:  edit -> make ui -> hard-reload the browser"
	@echo "  turn it off with:           make ui-static"

ui:  ## rebuild ONLY the host bundle (~1s). Needs `make ui-live` first.
	@cd frontend && pnpm build
	@if ! docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' $$($(COMPOSE) ps -q api) 2>/dev/null | grep -q /opt/atrium/static/host; then \
		echo "  the live mount is NOT active — this build will not reach the container."; \
		echo "  run: make ui-live"; \
		exit 1; \
	fi
	@echo "  bundle rebuilt and served. Hard-reload the browser (cmd-shift-R)."

ui-static:  ## drop the bind mount and go back to the image's own bundle
	$(COMPOSE) up -d --force-recreate api
	@echo "  live mount off; serving the bundle baked into the image."

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

# --- Atrium version bump ---------------------------------------------------
# The pinned atrium version lives in five files plus three npm packages, and
# they are not independently upgradable. Rather than keep a second copy of
# the editing rules here, this target dispatches the same workflow the daily
# poll runs, so the laptop path and the unattended path are one
# implementation. Reach for it to adopt a release ahead of the poll, or to
# re-run a bump the watcher skipped.
#
# It opens a PR; it does not merge one. .github/workflows/auto-merge.yml
# does that, and only once every check on the PR is green.
#
# `gh workflow run` dispatches the copy of the workflow on the default
# branch — GitHub offers no dispatch for a workflow that is not on master
# yet, so this target only works once atrium-watch.yml has been merged.
atrium-bump:  ## open the atrium bump PR now (version=X.Y.Z, blank = latest release)
	gh workflow run atrium-watch.yml $(if $(version),-f version=$(version),)
	@echo "Dispatched. Watch it with: gh run watch \$$(gh run list --workflow atrium-watch.yml -L1 --json databaseId --jq '.[0].databaseId')"
