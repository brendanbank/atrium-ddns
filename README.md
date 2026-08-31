# Atrium Ddns

A host extension on top of [atrium](https://github.com/brendanbank/atrium).
Atrium ships the platform layer (auth, RBAC, audit, email, jobs,
notifications, admin shell); this repo adds the domain-specific routes,
models, and UI.

## Quick start

```bash
cp .env.example .env
# Edit .env: set APP_SECRET_KEY, JWT_SECRET (openssl rand -hex 48 each),
# MYSQL_PASSWORD, MYSQL_ROOT_PASSWORD.

make dev-bootstrap
make seed-admin EMAIL=you@example.com PASSWORD='a-good-password'
make seed-bundle
open http://localhost:8053   # API_HOST_PORT in .env; MySQL is on MYSQL_HOST_PORT (13353)
```

Sign in with the seeded admin and the **Atrium Ddns** card appears on
the home page, with a sidebar link to `/atrium-ddns`, an admin tab,
and a profile-page card. Bump the counter to exercise the RBAC + audit
path end to end.

### A dev stack you sign in to as yourself

`make seed-admin` above takes the password on the command line, which puts
it in your shell history and in make's echo of the recipe — and it cannot
pre-enrol TOTP, so the account it makes is not the one you actually sign in
as. If you keep the login in 1Password, `make dev-up` raises the stack and
seeds from there instead:

```bash
# once — which item holds the login. The file is gitignored.
echo 'DDNS_OP_ITEM=<item uuid>' > .devstack.local.conf

make dev-up      # up + migrate + seed admin (with TOTP) + seed bundle
```

It reads `username`, `password` and the item's one-time password field, so
your authenticator's codes work against the stack. It refuses before
building anything if the CLI is locked or the item does not resolve.

Both targets pin the compose project name to the **resolved** directory, so
invoking make through a symlink to this worktree addresses the same stack as
invoking it through the real path. Compose's own default uses the path you
typed, which turned one directory into two projects — two sets of
containers, two volumes, both binding the ports out of one `.env`, and a
`dev-down` from one path that left the other stack running. Set
`COMPOSE_PROJECT=other` to run a second stack from one worktree, and give it
its own ports in `.env` first.

`make dev-down` is the counterpart, and is not a synonym for `make down`:
`down` stops the containers and keeps the database, `dev-down` removes the
volume too. That takes every zone, device, name and stored provider
credential with it — `SECRET_ENCRYPTION_KEY` in `.env` survives, the
ciphertext it decrypts does not — so it asks first. `FORCE=1` skips the
prompt; without a terminal the answer defaults to no.

Prefer the item's UUID over its title — `op item list --format json` has
them. The title is often a hostname, and this repository keeps real
hostnames out of tracked files (the same reason `.env.example` ships
`TRAEFIK_HOSTNAME=ddns.example.invalid`). `scripts/dev-admin.sh` has the
rest of the reasoning.

## Layout

```
atrium-ddns/
  Dockerfile           # frontend-builder + FROM atrium runtime
  compose.yaml         # api + worker + mysql
  .env.example         # secrets template (copy to .env)
  Makefile             # dev-bootstrap / dev-up / migrate / seed-* / test
  backend/             # host Python package (atrium_ddns)
    pyproject.toml
    alembic.ini
    alembic/           # alembic_version_app chain (separate from atrium)
    src/atrium_ddns/
      bootstrap.py     # init_app(app), init_worker(host)
      models.py        # HostBase + your tables
      router.py        # FastAPI routes
      scripts/seed_host_bundle.py
    tests/             # pytest smoke tests
  frontend/            # Vite library project (single main.js)
    package.json
    vite.config.ts     # uses @brendanbank/atrium-host-bundle-utils/vite
    src/
      main.tsx         # registry calls — ~10 lines
      api.ts
      queryClient.ts
      AtriumDdnsWidget.tsx     # home widget + admin tab + page demo
      AtriumDdnsPage.tsx
      AtriumDdnsAdminTab.tsx
      AtriumDdnsProfileItem.tsx
    src/test/          # vitest setup + worked example using @brendanbank/atrium-test-utils
  .github/workflows/   # CI (typecheck + tests + smoke)
```

## What atrium gives you (don't reimplement)

- Auth (password / TOTP / email OTP / WebAuthn) with role-mandatory 2FA
- RBAC: roles + permissions + super_admin + impersonation
- Account lifecycle: invite-only or self-serve signup, soft-delete + grace + hard-delete
- Audit log + retention pruning
- Email pipeline: templates per locale, durable outbox, retry/backoff
- In-app notifications + SSE bell
- Admin app-config (branding, system flags, translations, auth toggles)
- Maintenance mode (super_admin bypass)

If you find yourself writing one of these, stop — atrium has it. See
the atrium repo's `CLAUDE.md` for the contract.

## What lives in this repo

- Your domain models (on `HostBase`, never `app.db.Base`)
- Your routes (gated by `current_user` or `require_perm("...")`)
- Your migrations (on the `alembic_version_app` chain)
- Your frontend pages / widgets / admin tabs
- Your background jobs (`host.scheduler.add_job` for ticks,
  `host.register_job_handler` for durable queue work)

## Adding things

| You want to add               | Where it lives                                       | Wire it via                                                                |
|-------------------------------|------------------------------------------------------|----------------------------------------------------------------------------|
| HTTP endpoint                 | `atrium_ddns/router.py`                             | `app.include_router(router)` in `init_app`                                 |
| New permission                | A new alembic migration                              | `seed_permissions_sync(op.get_bind(), [...], grants={...})`                |
| Recurring tick                | `atrium_ddns/schedule.py` async function            | `host.scheduler.add_job(fn, "interval", seconds=N, ...)` in `init_worker`  |
| Durable async job             | A handler `(session, job, payload) -> None`          | `host.register_job_handler(kind="...", handler=..., description="...")`    |
| Admin-tunable flag            | A Pydantic `BaseModel` config class                  | `register_namespace("ns", Model, public=False)` in `init_app`              |
| Per-user notification         | Inside the txn that mutated the row                  | `from app.services.notifications import notify_user`                       |
| Outbound email (queued)       | A template row in `email_templates` + a callsite     | `from app.email.sender import enqueue_and_log`                             |
| Audit row                     | Inside the txn that mutated the row                  | `from app.services.audit import record`                                    |
| Home widget                   | A React component in `frontend/src/`                 | `reg.registerHomeWidget({ key, render })`                                  |
| Dedicated route               | A page component                                     | `reg.registerRoute({ key, path, element, layout? })`                       |
| Sidebar link                  | A label + path                                       | `reg.registerNavItem({ key, label, to, icon? })`                           |
| Sidebar nav group             | A collapsible top-level parent with nav-only children| `reg.registerNavGroup({ key, label, icon?, condition?, order?, children })` |
| Admin tab                     | A component, gated by a permission                   | `reg.registerAdminTab({ key, label, icon?, perm, element })`               |
| Profile-page card             | A component                                          | `reg.registerProfileItem({ key, slot?, render, condition? })`              |

For the full backend extension surface see
[atrium's `docs/new-project/README.md`](https://github.com/brendanbank/atrium/blob/master/docs/new-project/README.md).

## Tests

```bash
make test            # frontend unit tests + backend smoke tests
make typecheck       # tsc --noEmit on the host bundle
make smoke           # HTTP checks against a running stack
make test-e2e        # Playwright, in a real browser (raises its own stack)
```

`make test-e2e` is the only one of these that renders anything. It raises
the stack (with `ATRIUM_DDNS_COMPAT_STUB=1`, which the walk needs),
migrates it, seeds an admin and the host bundle, checks that the bundle the
stack *serves* is the one in the image it was built from
(`make check-bundle-fresh`), installs chromium and runs
`frontend/tests-e2e/`. Add `PLAYWRIGHT_ARGS='--grep nav'` to narrow it, and
`make e2e-down` to delete the stack and its database volume.

The strip screenshot the walk writes on every run is
`frontend/test-results/resolution-strip.png` (gitignored); the copy in
`docs/ops/img/` is the one that was checked by eye and committed.

## Pinning atrium

`compose.yaml` reads `ATRIUM_IMAGE` from `.env`, and the pinned default is
`ghcr.io/brendanbank/atrium:0.29.0`. Override it to try another release
without editing anything:

```bash
ATRIUM_IMAGE=ghcr.io/brendanbank/atrium:X.Y.Z make build up
```

Always a three-part tag, never a floating `X.Y`: the pin is what the
release watcher below compares against an upstream release tag, and it
reads exact versions.

The pin is not in one place. The same tag appears in `.env.example`
(canonical), `Dockerfile`, `compose.yaml`, `.github/workflows/ci.yml` and
this file, and the frontend SDK packages —
`@brendanbank/atrium-host-types`, `@brendanbank/atrium-host-bundle-utils`,
`@brendanbank/atrium-test-utils` — carry the matching version in
`frontend/package.json`. **They are not independently upgradable.** A host
running image X with SDK packages from Y fails at runtime, not at build
time: the served bundle calls endpoints the image does not serve. Anything
that moves one of them moves all of them, or refuses.

### The release watcher

`.github/workflows/atrium-watch.yml` polls atrium once a day and opens a
single PR moving every pin above together, with the upstream release notes
inlined. The editing rules are atrium's
`.github/actions/host-atrium-bump` — one implementation shared by every
host, rather than a copy per repo that drifts.

To bump ahead of the poll, or to adopt a version it skipped:

```bash
make atrium-bump                  # latest release
make atrium-bump version=0.29.0   # a specific one
```

It polls; atrium never pushes. Atrium holds no credential for this repo,
so adding a host costs it nothing and grants it nothing.

CI on the bump PR builds the smoke and e2e stacks against the *new* image,
which is the point of opening a PR rather than committing to `master`.
What CI cannot tell you is in the PR checklist: new alembic migrations, a
breaking `__ATRIUM_REGISTRY__` change, or a new **required** env var.
0.27.0 is the worked example of the last one — it made
`SECRET_ENCRYPTION_KEY` mandatory, and a stack that took the bump without
reading anything would come back up only as far as atrium's startup
validation.

Merging moves the pin in the source tree. It does not deploy: the running
stack keeps its old image until it is rebuilt.

### One thing to know before you set it up

The watcher needs `ATRIUM_BUMP_TOKEN` — a fine-grained PAT, resource owner
`brendanbank`, scoped to this repository alone, with **Contents**, **Pull
requests** and **Workflows** all at read-and-write:

```bash
gh secret set ATRIUM_BUMP_TOKEN
```

It cannot be `GITHUB_TOKEN`. A PR opened by `GITHUB_TOKEN` does not trigger
`pull_request` workflows, so CI would never exercise the new image. And
Workflows write is needed because `.github/workflows/ci.yml` is one of the
pinned files — GitHub rejects a PAT push touching a workflow file without
it, which is a failure that only shows up on the first real release.

## Merging the automated PRs

`.github/workflows/auto-merge.yml` squash-merges an `atrium-bump/X.Y.Z` PR,
and Dependabot's grouped minor, patch and security PRs, once **every**
check on the PR's head commit is green. Majors are excluded by
construction: `.github/dependabot.yml` splits them into their own
`*-major-updates` PR, and the gate's allow-list does not name it.

It is a `workflow_run` gate rather than `gh pr merge --auto` because this
repository has no branch protection available to gate on, and native
auto-merge armed on an unprotected branch merges immediately — before CI
starts. The gate reads the checks on the exact commit CI ran against, so a
push landing after a green run cannot inherit that run's success.

A red PR is left open rather than blocked. Nothing here prevents merging
one by hand.
