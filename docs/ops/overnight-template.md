# Overnight task template — portable

A contract for running a milestone unattended: **one agent per issue, each in
its own worktree; a per-issue PR into the milestone branch; a local gate as the
only quality bar; deployed evidence before an issue closes; one report at the
end.**

This file is repo-independent. Everything project-specific lives in the
**Project card** below — fill that in, append your own traps to the last
section, and change nothing else. If a rule here looks like it does not apply
to your project, read the incident under it before deleting it; almost all of
them were written after the failure, not before.

---

## Project card — fill this in

Copy this file to `docs/ops/overnight-template.md` in the target repo and
replace every `<…>`. A blank cell is a stall waiting to happen at 03:00.

Filled in for **`brendanbank/atrium-ddns`** — the atrium-based rewrite of
`brendanbank/dyndns-route53`. Cells marked **OPEN** are not yet answerable and
are listed again at the bottom of this card.

| | |
|---|---|
| `<MILESTONE_BRANCH>` | milestone title lowercased, dashes for spaces — e.g. `v1m1-compat-suite`. Cut from `master` on first use and pushed. |
| `<TRUNK>` | `master` |
| `<BOARD>` | Projects v2 board **2**, *atrium-ddns delivery* (`OVERNIGHT_PROJECT=2`), `Status` = Todo / In Progress / Done. Claim with `find_ready.py --claim <issue> --slug overnight`, then `find_ready.py --verify <issue>` — the claim is not a claim until the board moved. `bootstrap-github.sh --check`: setup COMPLETE. |
| `<READY_CHECK>` | `find_ready.py --list` / `--graph`. Reads `Depends on:` from each issue body; every issue this repo opens must carry one (`Depends on: none` if free). |
| `<GATE>` | see *Gate* below — four commands, measured 2026-08-15 on the scaffold. |
| `<DEPLOY>` | `git bundle` over the deploy key → `docker compose up -d --build` on the host → verified by content with `scripts/deploy-verify.sh`. Identity file is `backend/src/atrium_ddns/router.py`, compared byte-for-byte against the **installed** package at `/opt/venv/.../site-packages/atrium_ddns/router.py`. **Not `/opt/host_app`** — the image copies `backend/` there *and* pip-installs it, and only the installed copy is on `sys.path`, so comparing `/opt/host_app` asserts about a tree the app never imports (found by #36). `compose up` exiting 0 proves nothing, and neither does hashing the wrong file. |
| `<DEPLOY_HOST>` | ssh alias **`atrium-ddns-deploy`** — one host, serial, one deployer (the orchestrator). The alias resolves in `~/.ssh/config`; the hostname is never in the repo, so `OVERNIGHT_DEPLOY_HOST` is safe to commit. Ubuntu 24.04, docker 29.1.3, 3 containers already running (the old service among them), 31 G free, 2.9 G RAM available, **port 8443 free**. The new stack goes up **beside** the old one on 8443 (`API_HOST_PORT=8443`, a distinct `MYSQL_HOST_PORT`, explicit `COMPOSE_PROJECT_NAME`). See plan § 5b — 8443 implies TLS and atrium does not terminate it. |
| `<PROMOTION>` | **yes, this repo has one.** Atrium reads branding, feature flags, PAT policy and `system.host_bundle_url` from the `app_settings` KV table, not from the merged file. A merged bundle that nothing points at does not run: `make seed-bundle` (or an equivalent write to `system.host_bundle_url`) is the promotion step. Same for any `register_namespace` default a migration seeds. |
| `<SMOKE>` | `scripts/smoke.sh` (`make smoke`) — 11 local checks against a running stack, exits non-zero on any failure. Later joined by `tests/smoke_test_dns.sh`, ported from `dyndns-route53`. `smoke_test_dns.sh` performs **real DNS writes** against whatever zone it is pointed at — allow-list is a dedicated test zone, never a zone carrying live records. Widening it is the operator's call. |
| `<LIVE_DATA>` | the old service's `events` / `hostnames` tables (SQLite, on the deploy host) are the only production telemetry, and they are the migration source of truth. Read-only copies via `scripts/backup.sh` from the old repo. Credentials live outside the checkout. |
| `<CREDENTIALS>` | **Signing:** GPG subkey `F60F2EAA7F5ACC52` on a hardware token via `pinentry-mac`. `OVERNIGHT_GPG_BLOCKING=0` for the development phase — a locked token no longer halts the run (see *Standing decisions*). **Transport:** GitHub over HTTPS via the `gh` credential helper; an empty `ssh-add -l` is routine and pre-flight confirms `origin` through the path it actually chose. **Deploy:** `~/.ssh/atrium-ddns-deploy`, ed25519, passphraseless, `IdentitiesOnly yes`. **This is the agent-independent path** — macOS points `SSH_AUTH_SOCK` at the launchd agent, which holds no identities, and 1Password's agent lives at a different socket and may be locked. Verified reaching the host with `SSH_AUTH_SOCK` unset entirely. Refusal is a stop condition. **Registry:** `ghcr.io/brendanbank/atrium:0.28` — public, no token. |
| `<STATE_FILE>` | `.context/<milestone>-orchestration.md` (the workspace `.context/` is gitignored by Conductor) |
| `<EPIC>` | the milestone's `epic`-labelled issue. Not yet created — opens with the milestone. |
| `<MIGRATION_SLOT>` | **two chains, both single-head, both scarce.** `alembic_version_app` — the host chain at `backend/alembic/versions/`, currently `0003_ddns_event_backend_type`; one agent at a time may add a revision, so schedule it. Atrium's own `alembic_version` chain is upstream's and must never be written to — but it *moves* on an image bump (0.28 brought `0012_user_secret_keys`), so an atrium uptake is an image bump **and** `alembic upgrade head`, as one step. |

### Gate

Measured on this workspace, 2026-08-15, on the freshly scaffolded tree. Every
agent **measures its own baseline** — do not inherit these numbers.

```bash
cd frontend && pnpm install --frozen-lockfile
pnpm typecheck                     # tsc --noEmit — 0 errors
pnpm test                          # vitest — 2 passed (2)
cd .. && make up && make migrate    # both alembic chains to head
make test-backend                  # host tests 532 passed + compat 31 passed, 3 skipped
make smoke PASS=<pw> EMAIL=<addr>  # scripts/smoke.sh — 11 passed (11)
make test-e2e                      # Playwright — 16 passed (5 spec files), ~35s once the stack is up
```

**`make test-e2e` is in the gate from #91, and it is the only instrument
here that renders anything.** Everything above it is HTTP: the endpoints,
the bundle's bytes, the registration keys. All of it stayed green through
the five defects an operator found in one session (#88, #89, #90), because
a curl cannot see a nav item that never mounted or a board that throws in
React. It raises its own stack — `make e2e-up` builds, migrates, seeds the
admin *and* the bundle — so it is a superset of `make up && make migrate`,
and it needs `ATRIUM_DDNS_COMPAT_STUB=1`, which the target sets in the
environment rather than in `.env` so a later plain `make up` does not
inherit a fixture stack. `make e2e-down` deletes the volume.

**One interaction to know before reading a red gate.** `make e2e-up` seeds
its admin *with TOTP enrolled* (the runner needs to compute codes), so on an
e2e stack `make smoke PASS=… EMAIL=…` reports **9 passed, 2 failed** —
`/api/users/me/context` and `/api/atrium_ddns/state` answer `403
{"code":"totp_required"}` on a cookie that has not passed the challenge.
That is the account being 2FA-enabled, not the stack being broken;
`scripts/smoke.sh` says so in its own failure hint. Run `make smoke` with no
arguments (8 passed, 0 failed) against an e2e stack, or use a stack seeded
without `--totp-secret` for the credentialed checks.

Two traps it has already paid for, recorded here rather than rediscovered:
the chromium binary is glibc-only, so Playwright runs on the **host** and
never in the alpine builder stage; and `pnpm` v10 does not run dependency
build scripts, so installing `@playwright/test` downloads **no** browser —
`pnpm exec playwright install chromium` is the explicit step, and it is
what `make e2e-deps` is for.

**And a third, which is `check-fresh`'s missing fourth guard.** `make e2e-up`
now ends in `make check-bundle-fresh`, because the host bundle is baked into
the image exactly like `tests/`, `backend/` and the installed package are —
and it had no guard. Measured on 2026-08-16: a plain `docker compose up -d
--build` built an image carrying a newly merged UI, tagged it, and left the
containers on the previous one. Six specs failed against a UI that was
correct and merged, the served bundle read 801,320 bytes where the image
held 815,012, and the only reason it was caught in minutes rather than
argued about for an hour is that the specs were exercising the new markup.
**Had they not been, the run would have been green about the wrong bundle.**
The guard hashes the file inside the image with the container's interpreter
and the bytes served over the published port with the host's, and names both
digests when they differ; `e2e-up` also `--force-recreate`s api and worker,
which is the prevention to the guard's detection.

`make test-backend` grew a second pytest session (#23): the compat suites now
reach the image via `COPY tests /opt/compat_tests` in the Dockerfile's **`dev`
stage only**, so they run in the gate without shipping to production. It is
gated on `make check-fresh`, which digests **three** trees — `tests/`,
`backend/`, and the *installed* `atrium_ddns` package — in the worktree and in
the running container, and **refuses when any differs** — `make up` does not
rebuild, so without it the gate silently reads a stale copy and reports green.
Verified here: editing a case file without rebuilding stops the gate with *"the
api container is running a STALE copy of tests/"*; rebuilding with the same
edit fails 3 guards; reverting returns 29 passed.

The 131-case wire table is **not** in the gate and must not be — it needs a live
service and an explicit pair (`make test-compat TARGET=… BASE_URL=…`). A run
without `--target` prints *"the wire table was NOT RUN (0 of 131 cases
executed)"* rather than a zeroed accounting block that reads like a pass.

CI gained a Docker-free `compat-guards` job running literally `pytest tests/`
from the checkout — the invocation that used to die before collecting anything.
Two instruments: green there and red in the image means the image is stale, not
the tests wrong.

The gate needs Docker: `OVERNIGHT_NEED_DOCKER=1`, and `docker info` failing is a
stop condition, not a routine.

**Backend tests run parallel by default** — `-n auto --dist=loadfile`, the
recipe atrium-pa uses to run 6197 tests in 86s. Two things follow for anyone
adding tests:

- **Namespace anything you create in the database per worker.** Ten workers
  share one MySQL. `test_user_scope_secrets.py` derives its table name and its
  user emails from `PYTEST_XDIST_WORKER`; a test that hardcodes them produces
  collisions that read as flakiness.
- **Serial escape hatch is `-n 0`, not `-p no:xdist`.** The latter unloads the
  plugin while `addopts` still passes `-n`, and pytest dies with "unrecognized
  arguments". `make test-backend-serial` does it correctly. For a suspected
  *hang*, use `make test-backend-file FILE=…` — xdist absorbs per-test output,
  so a hung run and a slow one look identical until the names print.

Measured on this box, 10 workers: a synthetic 800-test suite at realistic
per-test cost runs **11.13s serial → 3.58s parallel (3.1×)**. At 8 real tests
the run is *slower* in parallel — ten worker processes each import the app —
which is the honest shape of the trade and the reason the number is recorded
against 800 rather than against today's suite.

**Parallel agents need distinct host ports.** Compose isolation now covers the
image tag and the volume, but `API_HOST_PORT` / `MYSQL_HOST_PORT` come from
`.env`, which each worktree copies from the same example. Two agents on the
defaults collide. Docker fails loudly on the bind (`port is already allocated`),
so this is a stall rather than a silent corruption — but pick your own pair and
set `COMPOSE_PROJECT_NAME` too. If you find a stack you did not create, stop and
report it; do not reuse it and do not tear it down.

**CI does not run on milestone branches — by design, and the gate is why.**
The workflow fires only for `master` (PRs into it, and pushes to it). A
per-issue PR into the milestone branch gets **no** GitHub CI at all, so the
local gate is not a first opinion ahead of a second one: it is the only one.
That is the contract's own rule (*a local gate as the only quality bar*) made
literal, and it is safe because the gate is a strict superset of the workflow —
same typecheck, same vitest suite, same backend tests, same Playwright specs
(CI's `e2e` job, added by #91), plus a smoke test against a stack it stood up
itself, which CI's `smoke` job also does but only after a full image build.

Two consequences an agent must not get wrong:

- **Running the gate is not optional and not delegable to CI.** There is no
  green tick coming later to catch what you skipped. An issue whose PR was
  opened without a full local gate run has been merged unverified.
- **Report the gate's real numbers in the PR body**, measured in your own
  worktree. They are the only record that it ran.

**The gate as scaffolded was not green.** `pnpm test` failed to collect every
suite — `@brendanbank/atrium-host-bundle-utils@0.27.0` ships extensionless
relative ESM imports, which Vite's bundler resolution tolerates (so `pnpm build`
passes) and Node's ESM loader does not (so vitest, which externalises
node_modules, does not). `frontend/vitest.config.ts` inlines both SDK packages
to route them through Vite's resolver. Two instruments, one green and one red,
on the same tree — which is the whole argument for having two.

### Running unattended while 1Password is locked

The question to ask of every credential is not "is it available now" but "is it
available at 03:00 with the laptop locked and 1Password sealed". Audited
empirically, not by reading config:

| dependency | needs 1Password? | how it was checked |
|---|---|---|
| ssh to the deploy host | **no** | dedicated passphraseless key + `IdentitiesOnly yes`. Authenticates with `SSH_AUTH_SOCK` unset, pointed at a nonexistent socket, and pointed at the empty launchd agent |
| `git push` / `gh` / PR / merge | **no** | token lives in the **macOS keychain** (`gh auth status` → `keyring`, `credential.helper=osxkeychain`), unlocked for the whole login session. `git ls-remote` succeeds with no ssh agent |
| reading a secret mid-run | **no** | the deploy host's `.env` is already provisioned, so nothing needs minting or fetching. `grep` for `op`/1Password across `scripts/`, `Makefile`, CI, and the overnight-run scripts returns nothing |
| **GPG signing** | **effectively yes** | hardware token via `pinentry-mac`. This is the one that breaks — see below |

**`OVERNIGHT_GPG_BLOCKING=0` does not do what it reads like it does.** It is
consulted by `preflight.sh`, once, before any work — and nothing consults it
again. At commit time git tries to sign regardless, gpg answers `signing
failed: No secret key`, and git exits `fatal: failed to write commit object`.
So a run configured to *tolerate* a locked token still dies at its first
commit, having done the work and pushed none of it. Unpushed worktree state is
the one thing a shutdown cannot recover.

Verified by pointing `GNUPGHOME` at an empty directory: the signed commit fails
exactly as above, and the same commit with `commit.gpgsign=false` succeeds and
produces an unsigned (`N`) commit.

**Use `scripts/overnight-commit.sh` instead of `git commit`.** It tries signed,
falls back to unsigned *only* when `OVERNIGHT_GPG_BLOCKING=0`, and stops when
it is `1` — the setting finally meaning what it says at the moment it matters.
An unsigned commit prints a notice that belongs in the PR body: it shows as
unverified on GitHub permanently, and rewriting history after a merge costs
more than a re-sign now.

It reads the environment in preference to `.overnight.conf`, which is the
documented rule and which the first version got backwards — `set -a; . conf`
overwrites what is already exported, so an environment `OVERNIGHT_GPG_BLOCKING=1`
was replaced by the file's `0` and the stop condition committed unsigned
instead of stopping.

**Smoke-testing a deployed stack needs no credential.** Run
`scripts/smoke.sh --base <url> --no-login`. Measured, because the first version
of this paragraph guessed: **8 checks locally** (`make smoke`), **6 against a
remote `--base`** — the three login checks drop, and against a remote target the
two migration checks drop as well. The credentialed checks would need an admin
password the run has no unattended way to obtain; do not put one where the run
can read it just to get three more checks.

Numbers in this file are measurable. Do not write one you have not run — #8's
agent caught the guessed "9 of 11" above, which is exactly the class of error
this contract keeps telling agents not to make.

---

### Deploy access

**`docker` works without `sudo`.** The deploy user was initially not in the
`docker` group — a bare `docker ps` failed with `permission denied while trying
to connect to the docker API`, which would have broken `deploy-verify.sh`'s
byte-identity check (it shells out to plain `docker compose exec`) at the worst
possible moment. The operator added the user to the group on 2026-08-15;
re-verified over a **fresh** ssh session, since group membership only applies to
new logins: `docker ps` returns 3 containers and `docker compose version`
answers, both unprivileged. `deploy-verify.sh` runs as written.

**The hostname is never in the repo.** `OVERNIGHT_DEPLOY_HOST` is the alias
`atrium-ddns-deploy`, resolved by `~/.ssh/config`. Anything that needs to name
the target uses the alias. A `grep -rIn` for the estate's domain across the
whole checkout returns nothing, and it should stay that way.

Agent-independence is verified, not assumed — the deploy key authenticates with
`SSH_AUTH_SOCK` unset, pointed at a nonexistent socket, and pointed at the empty
launchd agent. `preflight.sh` run with no agent reports **PASS** including
*deploy host reachable with the dedicated key*.

One pre-flight line is expected noise here: `could not import atrium_ddns` is
**ROUTINE**, not a problem. The host package is installed inside the api
container, not in a local venv, because that is where the gate runs.

---

### Still OPEN

**One thing:** no milestone, no epic, no issues yet. The issue set is drafted in
`docs/ops/refactor-plan.md` § 5 and every issue must carry `Depends on:` and
`## Scope` or the run can neither compute readiness nor parallelise.

Everything else in this card is settled. Design decisions binding on every
agent — in `docs/ops/refactor-plan.md` § 6, not re-litigated per issue:

- **No query-parameter auth.** HTTP Basic only on `/nic/*`.
- **Multi-tenant**: users own their domains and provider credentials.
- **Devices** are the DDNS credential and the unit a hostname belongs to; one
  device per hostname (1:N), not M:N.
- **Device secrets are hashed** — argon2id for new, bcrypt verification kept for
  migrated rows, shown once, never re-displayable.
- **Provider credentials are encrypted per-user** through `SecretBlob` +
  `UserSecret` (atrium ≥ 0.28), owner read from the row.
- **Logs searchable** by user / device / domain, ~30-day retention, pruned by a
  scheduled job rather than on the write path.
- **Account deletion warns loudly, then really destroys** the tenant's provider
  credentials.

✅ **The upstream blocker is gone.** Per-user secrets shipped in
[atrium v0.28.0](https://github.com/brendanbank/atrium/releases/tag/v0.28.0)
(closes #227). This repo is pinned to `:0.28`, SDK packages at `^0.28`, atrium's
chain at `0012_user_secret_keys`. **V1M2 is unblocked**; V1M1 remains the
natural first milestone because the compat table has to be frozen before there
is anything to write host code against.

The API is `SecretBlob()` + `UserSecret(purpose=…, owner_attr=…, column=…)` and
an `await unlock_user_secrets(session, owner_id)` before the value is touched —
**not** `EncryptedText(scope="user")`, which raises permanently and names the
replacement. Plan § 3.1.1.

---

## Standing decisions

Settled by the operator once per milestone so a run does not stall on them.
These are permissions, not suggestions. A run that wants to do something *not*
on this list and not in *Stop conditions* writes it down and proceeds.

**Settled 2026-08-15: all approved. This is the development phase — full
permissions.** The table below records what that means concretely, because
"full permissions" and "no constraints" are not the same thing, and the two
lines that still carry a constraint are the ones a run would otherwise get
wrong at 03:00.

| | |
|---|---|
| **Deploy** | ✅ Approved, and **the milestone tip may go to production** during this phase — the run does not wait for a release PR to see its work running. Target is the deploy host, **beside the existing service on port 8443** (§ Project card). Nothing else, and no other host. Deploys stay serial and the orchestrator's alone; agents merge and hold. |
| **Live data** | ✅ On for the whole milestone. No per-issue asking. |
| **Production writes** | ✅ Approved for the development phase. Still: narrowly scoped, labelled synthetic, reverted in-session, and **the revert verified** — an unverified revert is the thing that turns an approved write into an incident. The old service's live DNS records are not a test target. |
| **Release PR** | ✅ The run opens and merges it once the exit criterion is *demonstrated* — met or not. An honest "exit 2 with named numbers" still merges. |
| **Unsigned commits** | ✅ Permitted — `OVERNIGHT_GPG_BLOCKING=0`, set by the operator for the development phase. Commits are still signed whenever the token is unlocked; a locked token no longer halts the run. **Revisit before anything ships from an unattended batch:** unsigned commits show as unverified on GitHub permanently, and rewriting history to fix that after a merge is worse than the pause would have been. |
| **Smoke tests** | ✅ May inject against declared targets. `smoke_test_dns.sh` performs **real DNS writes** — its allow-list is a dedicated test zone, and widening it to a zone carrying live records stays the operator's call. |
| **Order** | ✅ Review debt before feature work, where the debt changes how the feature work is done. |

**Why "deploy" is a standing decision and not an optimisation.** In one
milestone, four defects were invisible to a green 683-test suite and obvious
within ninety seconds of bringing the stack up: a database rejecting the app's
auth, an unmigrated schema, a migration tool silencing the app's logging, and a
per-container label leaking metrics cardinality. Three of the four reported
`healthy` throughout. A run that cannot deploy ships work that passes CI and
does not run.

---

## Reporting discipline

**Report once, when the batch ends.** Not per issue, not per merge.

The audit trail is the issue comment and the PR body. Write the detail there,
where it persists, is reviewable, and sits next to the code it describes. The
chat transcript is for the closing summary and for stop conditions — nothing
else.

A finished issue is **not** a checkpoint. Neither is a surprising result, a bug
found in your own earlier work, a decision you made and documented, or a
milestone completing. An unattended run that pauses after every merge is not
unattended; it is a slow conversation.

**But silence is not a report either.** A hung agent and a working agent look
identical from outside. Between merges, post a liveness table — agent, issue,
last transcript write, elapsed — against the elapsed times of the issues that
have already finished, so "running long" is a comparison and not a feeling.
This was added after an operator asked "are the agents still running?" and the
honest answer was *"I wasn't checking anything; I was treating silence as
normal."*

If something genuinely needs the operator, it is one of the stop conditions.
Everything else: keep going and write it down.

---

## Pre-flight — before any work

Fail here, in a second, rather than at the first push an hour in. Adapt to
`<CREDENTIALS>`:

```bash
ssh-add -l                                # transport key, if used
printf t | gpg --clearsign -q >/dev/null  # signing token unlocked?
gh auth status                            # or your forge equivalent
docker info >/dev/null                    # if the gate needs it
git worktree list                         # see Orchestration
```

All of these are hardware- or daemon-backed and all of them have failed
mid-run. Record which ones are *routine and handled* versus *stop conditions*
in the Project card, and echo the result in the first comment of the first
issue so the audit trail is explicit.

**A pre-flight probe has the same failure mode as everything else in this
file.** One run probed `<APP>_ENGINE_DSN`; the real variable was
`<APP>_MYSQL_ENGINE_DSN`. All three probes came back `unset`, which reads
identically whether the variable is missing or **the name asked about does not
exist** — and the orchestrator was one step from contradicting a correct agent
report on the strength of a question that could not have returned anything
else. Check the *name* exists before believing the *value* is absent.

---

## Operator invocation

> **use template `docs/ops/overnight-template.md` to run issues #A, #B, #C**
>
> **use template `docs/ops/overnight-template.md` to autonomously work the active
> milestone, and roll into the next when this one's issues are done**

On seeing that shape:

1. **Read this file in full** — it is the contract.
2. **Determine live-data mode** from the prompt and the standing decisions. Echo it.
3. **Resolve `<MILESTONE_BRANCH>` once** for the batch. If it does not exist on
   `origin`, cut it from `<TRUNK>` and push it — do not wait to be asked. Only
   cut the branch for the milestone actually being worked; a branch cut months
   early drifts and the first release PR becomes a merge fight.
4. **Board hygiene first.** Any listed issue missing its milestone or absent
   from `<BOARD>` gets added before work starts.

   **Then map the exit criterion onto the issues, clause by clause.** Read the
   milestone's description, split it at the "and"s, and name the issue that
   satisfies each part. A clause no issue owns is a **seam** — open the issue
   for it *now*, before any work, not at close-out.

   This is not theoretical. One milestone closed all sixteen of its issues with
   its criterion unmet, because nothing turned an approved rule into an output:
   one issue put evaluation out of scope, one produced a verdict rather than an
   output, one proved the drill-through with a hand-built artefact. Every issue
   was individually complete and correctly scoped. **Seams are invisible to
   per-issue review precisely because each issue passes on its own terms**; only
   the criterion sees them, and by close-out it is a whole extra issue's work.
5. **Claim each issue as you start it** with `<BOARD>`'s claim command, which
   must both post a claim comment naming the branch **and move the board item to
   In Progress**. Cut a real branch with exactly that name off
   `<MILESTONE_BRANCH>`.

   **Verify the board actually moved.** An issue being worked must never still
   read `Todo`; that column is what another agent reads to decide the issue is
   free. If the move failed, fix it before writing any code. Late in one run,
   two issues were not on the board at all and a third still read `Todo` while
   an agent worked it — found only because an agent flagged board hygiene in a
   close comment.

   **A claim is released by editing the claim comment** to begin `RELEASED —`,
   or by moving the item back to `Todo`. A *separate* comment saying the claim
   looks stale is **not** a release, no matter how confidently worded. This rule
   exists because it was broken: one run claimed an issue, posted that it could
   not do the work and had moved on, another took it over, and the first came
   back and finished — two complete implementations, one thrown away.

   If an issue is claimed and not released, **stop and pick the next one.**
6. **Close the loop on the board.** Set the item to Done when the issue closes,
   in the same step. The board is the shared state between parallel runs.
7. **Sequencing is the run's job, not the operator's.** Spawn one agent per
   issue, each in its own worktree. Do not ask permission to parallelise and do
   not ask for sequential either — pick per issue and say which you picked.
8. **Track progress** with a task per issue, and re-check the tracker rather
   than the task list. One run reported an issue as *running* three times when
   it had never been dispatched — the board read `Todo`, unclaimed, no branch.
   Reading your own task list instead of the shared state is the same defect
   this template catalogues in code.

---

## Orchestration

### One agent, one worktree. Always.

Two agents in one checkout share a working tree and an index, so `git checkout
-B`, `git stash` and `git add -A` operate on each other's files. Spawn each with
the agent tooling's isolation mode; do not point several agents at the shared
checkout and hope their file lists do not overlap.

This is written down because it was done wrong. Four agents were dispatched
against one checkout:

- a routine `git stash -u`, taken for a coverage baseline, swallowed another
  run's ninety-one-line in-flight change to a file it had never touched
- the tree ended on a detached HEAD carrying eleven modified files from two
  different issues, interleaved
- every coverage number was wrong in both directions, because each run was
  measuring the others' uncovered work-in-progress
- two of the four detected the collision and rescued themselves

Nothing was lost, but only because one agent noticed and hand-extracted its
files before committing. **The near miss is the point: any `git add -A` would
have committed another issue's half-finished work into an unrelated PR, passed
the gate, and merged.**

### Its own environment, too — this one is worse

A worktree is not enough. **Each agent needs its own dependency environment,
built inside its worktree**, before trusting any test result.

A shared checkout typically installs the project **editable**, resolving to the
*shared checkout's* source. So an agent working in an isolated worktree, running
the shared environment's test runner, imports and tests code it did not write —
and gets a green suite for it.

That is strictly nastier than the index collision. A stomped index produces a
messy diff someone notices. This produces **a clean run, a passing gate and an
honest-looking coverage number for the wrong source tree**, and the agent has no
reason to doubt any of it.

One line settles it — print the resolved package path and compare it to your
own worktree:

```bash
python -c "import <pkg>, pathlib; print(pathlib.Path(<pkg>.__file__).parents[1])"
```

If that is not your own worktree, every number you have is about someone else's
code.

### Detecting a collision mid-run

`git worktree list` catches a branch checked out *elsewhere*. It cannot catch
two agents in *one* tree — there is no second checkout and no branch conflict to
see. The signal that does show is **uncommitted changes to files outside the
claimed issue's scope**. Check before any `git add`, and if it is there, stop
and say so rather than staging around it.

### The scarce resources, and scheduling against them

Parallelism is bounded by contention, not by ambition. Hold **three substantive
agents** as a default ceiling, and hold at two rather than manufacture a
conflict. The things that genuinely contend:

- **`<DEPLOY_HOST>`.** One host, one deployer. See *Deploying*.
- **`<MIGRATION_SLOT>`.** A single-head migration chain admits one author at a
  time. Track who holds it and release it explicitly. Two issues in one run
  *declined* the slot on the grounds that they would have shipped a column with
  no writer — which is the right reason to release it.
- **Shared files.** Schedule off the **issues' own declared file lists**, not
  off a table in a handover document. In one run the handover's co-scheduling
  table missed four shared-file claimants; the corrected map was rebuilt from
  the issue bodies and written to `<STATE_FILE>` so it survived compaction.
- **Docs sections.** Two issues editing the same section of one design document
  is a merge conflict with a straight face.

**Not all conflicts are equal, and the distinction is worth using.** A code
conflict is surfaced by the merge and caught by the gate. A migration branch
merges *cleanly* and fails only at upgrade time on the deployed host. One run
deliberately parallelised two issues sharing a source file — with instructions
to add delimited blocks, rebase immediately before merging, and re-run the gate
post-rebase — rather than serialise the critical path behind an hour-long smoke
test. That call was right, and it is only available if you know which risk you
are taking.

### The orchestrator's context is the binding constraint

Not wall-clock. Working every issue inline fills the orchestrator's context
after three or four, and the run then stops mid-milestone for reasons that have
nothing to do with the work. One agent per issue keeps that flat — each returns
a paragraph, not a transcript.

**Dispatch briefs are the leak.** Briefs that restate estate facts, standing
rules and known traps run 2–3k tokens each, and in a long run they are most of
the consumption. Once the rules are written down, point agents at `<STATE_FILE>`
and `<EPIC>` instead of restating them; stop re-reading issue bodies before
dispatch (the agent reads them anyway); and stop duplicating verification the
agent already did — a cross-check is worth doing once, not every time.

There is a real benefit beyond tokens: **several agents corrected the
orchestrator's framing precisely by going to the primary source rather than the
summary of it.**

### Durable state, so a reset costs nothing

Maintain two handovers, continuously, not at the end:

- **`<STATE_FILE>`** — position, deploy procedure, credential quirks, the
  measured facts that supersede the design docs, standing rules, date
  constraints, and **which agents are running so they are not re-dispatched**.
- **`<EPIC>`** — the narrative version, on the tracker, readable from anywhere.

Writing a state file does not shrink the conversation; it buys *survivability*.
Say so plainly rather than calling it "compacting."

### Resume, don't re-dispatch

When a session limit, a crash or a compaction interrupts a run, **verify each
agent's state before assuming anything was lost**, and resume by agent ID where
the tooling allows. In one interruption, one agent had merged and only its
post-deploy check was outstanding, one was cut off mid-sentence on a follow-up
rather than mid-work, and two had unpushed worktrees that resumed intact. A
fresh agent would have repeated hours of measurement — and the board claim was
still `In Progress`, so re-dispatching produces exactly the stale-claim
collision this template warns about.

### Pausing and shutdown

**Push before you stop.** Unpushed work in a worktree is the only state a
shutdown cannot recover. An agent that cannot finish either pushes WIP or
releases its claim back to `Todo` — never leaves a stale `In Progress` with
nothing behind it.

Two specific stranding modes, both observed:

- **A commit made on the deploy host.** One agent committed locally on
  `<DEPLOY_HOST>` to test against production, and that commit existed nowhere
  else. Recoverable, but nobody would think to look.
- **A push you believe failed.** One check ran `git ls-remote` *without* the
  HTTPS override, hit a locked agent, failed silently, and the empty output was
  read as "not pushed". **An empty result and a refused query are not the same
  thing** — the rule this whole template is built around, applied to `git`.

Stand down anything risky rather than racing a shutdown. A production write done
hurriedly is the wrong way to do it; that decision belongs in a fresh session.

---

## Per-issue workflow

1. **Read the AC.** If it is ambiguous, comment on the issue asking — do not
   guess. If it is *wrong*, say so and argue it; see *Correct the issue*.
2. **Check dependencies are closed** with `<READY_CHECK>`. If not, stop; the
   issue was mis-dispatched.
3. Cut the claim branch off `<MILESTONE_BRANCH>`.
4. **Implement, staying inside the issue's declared file list.** That list keeps
   parallel *reviews* legible and keeps merges apart; it does **not** protect a
   shared index, which is what worktree isolation is for. If the work genuinely
   needs a file outside scope, comment on the issue before editing.

   **Stage the files the issue owns — `git add -A` is unsafe by default here.**
   It is the habit that turns a missing isolation into a wrong commit, and it
   fails silently: the diff looks coherent and the gate passes.
5. **Run `<GATE>` in your own worktree.** See *The gate*.
6. **Smoke test.** See *Evidence*. Paste actual output; "tests pass" is not a
   smoke test.
7. Commit, push, open a PR with `--base <MILESTONE_BRANCH>`.
8. **Merge, then hold.** Deploys are serial and belong to the orchestrator, so
   the deployed half of the evidence runs after the orchestrator deploys the
   wave and **verifies the deploy contains this merge**.
9. **Close** with a comment summarising what was done, the deployed evidence,
   and anything measured that contradicts the issue body — and move the board
   item to Done in the same step.

### Rebase hazards

If workspaces share an object store, **the target branch can move underneath a
running rebase.** One rebase silently dropped another issue's block from a
shared document and auto-merged cleanly with nothing complaining. It was caught
only by `git diff --stat` showing deletions where a purely additive change
should show none. Run that check before merging; a purely additive change with
non-zero deletions is a defect until proven otherwise.

Also: **a stale fetch reads as a missing merge.** One agent re-fetched, saw an
older tip, and concluded a dependency had not landed when it had. Fetch through
the same credential path you push through.

### Correct the issue rather than satisfying it

Several of the strongest results in two milestones came from agents that
**overturned the premise of the issue they were given**:

- An issue's motivating measurement had been taken on a data path that had
  carried nothing for days. Run as written, its smoke test would have **passed
  by not being seen**.
- An issue offered a cheap option and an expensive one; the agent showed the
  trade **collapsed**, because the cheap option would have been rejected by an
  existing constraint. "Cheaper" was only true while the expensive half was
  hypothetical.
- An issue asserted the schema already supported its feature. True about the
  schema, false about the data: **nothing wrote those rows**, so the feature
  could never fire.
- An AC asked for "an age below the cadence" and the measured age was 1.82×
  cadence — correct by construction, because the job fetches the previous day's
  input. The agent argued that in the close comment instead of quietly
  satisfying the literal wording.

An agent that reports a number weakening its own case, having been told the
strong version, is doing the job correctly. **Three separate agents in one run
corrected a figure the orchestrator had passed them, and each correction made
the underlying case stronger.**

---

## The gate

Record the exact commands and the **current numbers on the milestone branch**,
so a regression has something to be a regression *against*:

```bash
<GATE>                     # lint, types, tests, and any tagged suites
```

| | wall clock | tests |
|---|---|---|
| `<test command>` | ~Ns | N passed, N skipped |
| `<tagged suite>` | ~Ns | N passed |

Three rules about the gate itself:

**There may be no CI.** If the workflow was removed while the project moves
fast, this run is the *only* thing between a change and the milestone branch.
Nothing downstream re-checks it. Do not merge without a clean run pasted or
summarised in the PR — and do not poll for checks that do not exist.

**Never wait in an unbounded loop.** `until <check>; do sleep; done` spins
forever if the condition *errors* rather than turning true, and the run looks
hung for no reason anyone can see. Bound every wait with a maximum number of
attempts and fail loudly when they are exhausted.

**Measure your own baseline; never inherit one.** An orchestrator propagated a
branch-local suite count as the milestone-branch count and two separate agents
had to correct it. Every brief since says: measure your own.

**An intermittent failure in a container-backed suite is diagnosed by sweeping,
not by re-running.** A whole milestone of intermittent failures was attributed
to a shared daemon while one deterministic alignment fault sat behind that
attribution — and re-running is the one diagnostic that cannot distinguish them,
because green is the common case.

**Watch container count, not a config file.** If the suite is slow, run
`docker ps` in another shell while it runs: a container a few seconds old, next
to a reaper that has been up for the length of the run, means something is
starting one per test. This warning used to read "check `conftest.py` still owns
the only container", and it was **literally true** for the five weeks that suite
spent drifting to fourteen minutes — `conftest.py` did still own the only
container *of the kind it knew about*. Eleven fixtures had grown up outside it.
**A warning phrased as "the thing I know about is still fine" cannot see the
thing it does not know about; a count can.**

---

## Evidence — the part that is not optional

This section is the difference between a run that produces work and a run that
produces *trustworthy* work. Across two unattended milestones, **roughly a dozen
real defects were caught by the rules below and none by a test** — and in almost
every case *the failing version looked cleanest*. They are grouped here by the
shape they took.

### Two instruments, differently shaped

**Any number that matters is measured twice, by two instruments of different
shape, and both readings are reported.** Not the same query twice. Not the
implementation and a test of the implementation — those share an author.

Every one of these was found only because two instruments disagreed:

- A query stepping at the range selector measured the **previous calendar day**,
  reporting 1,717 lines where a second instrument reported 0. Its author's own
  words: *"0.42% being plausible enough that I would have written it up as a
  finding had I owned only one of them."*
- An attribution method based on timestamp proximity manufactured an **entire
  false diagnosis** — a NAT-reflection story the orchestrator had asserted and
  asked an agent to confirm. Switching to exact port matching collapsed it, and
  the real mechanism (address family) turned out to be a milestone-wide blind
  spot. **Had the agent confirmed what it was told, that finding would not
  exist.**
- A count reported `0 excluded` because the exclusions were applied server-side
  and the count derived from the already-filtered result. Live, the true figure
  was 1,156.
- A scrape published `rows: 0` while the store held 489,652 — the warm-up gate
  refused *before* reading the store.
- A quantile panel omitted its route matcher and returned 0.104 s against a true
  2.208 s — a **21× understatement**, caught only because the mean disagreed with
  the quantile. The off-the-shelf spelling of that panel produces the same error.

Corollaries worth stating separately:

- **Two instruments that agree exactly are a result, not a formality.** Report
  the agreement *and* its slack. One pair agreed at 161,962 vs 161,962 with the
  honest caveat that the allowance was exactly one line, so a one-line loss was
  undetectable.
- **Prefer the cross-check you did not author.** One agent led its close comment
  with the orchestrator's independent check rather than its own prediction,
  because *a prediction and its publisher share an author*.
- **Apply it to error messages too.** One fault produced three different message
  texts across three library builds. A fix keyed on message text would have
  passed on the workstation and silently drifted in the container.

### The probe that could not fail

**A value structurally unreachable on the path being exercised, rendered in the
same type as a real measurement.** This was the dominant defect family of two
milestones — seven instances in one run alone, and *four of five were caught by
reading real output, none by a test*.

- A probe answered **"100% — the blind spot does not exist"** because it put a
  clause in its own denominator that made the answer true by construction. It
  ran cleanly, returned a plausible number, and asserted the absence of the
  exact defect that had been documented an hour earlier.
- A claims row watching a data path probed `sum(...) > 0`. When the upstream
  rule was **narrowed rather than removed**, some lines kept arriving, so the
  probe stayed true through a **97.7% collapse** — undetected for five days. *A
  probe that can only detect extinction cannot detect a collapse.*
- A guard published `0` for 179 consecutive scrapes — the right value for the
  wrong reason, for as long as the series had existed. A before/after `curl`
  pair would have shown "0, then non-zero" and proved nothing; **scrape history
  spans the defect, a curl pair spans a restart.**
- A test that reads a metric into existence and then asserts it exists is this
  family wearing a test's clothing. Parse the exposition, do not call the
  accessor that creates the child.
- An inverted instance: a remedy's advertised reach was drawn from a population
  the project's own exclusion policy removes — **excluded members in a
  *numerator* inflating a remedy, the mirror of a clause in a *denominator*
  hiding a gap.** Same class, opposite direction, both written by people who had
  already internalised the lesson.

**Before trusting a probe, ask what it would print if the thing it measures were
absent.** If that is the same string it prints now, it is not a probe.

### `n/a` is never `0`

*Not measured*, *measured as zero*, *refused*, and *never ran* are four states.
Rendering them in one type is the single most common way this family arises.

- Publish `NaN` or an explicit refusal for "not measured", never `0`. One agent
  rejected `0` as a freshness sentinel for an operational reason rather than an
  aesthetic one: `0` is the epoch, so `now - 0` is fifty-six years and **every
  staleness rule alarms for a full cadence after each deploy**.
- **Refusing beats returning an arbitrary correct-looking answer.** When an
  identifier became many-to-many, the right change was for the resolver to
  *refuse* that type rather than return one of several holders.
- Prove a zero is a measurement by publishing its denominator on the same
  scrape: `0 refused / 7 polled` is a result; `0 refused` is silence.
- **Read counters with a rate/increase function, not as an instantaneous
  scrape.** An orchestrator reported a counter as "0, never spent" roughly
  fifteen times — in chat, in the handover, and in every dispatch brief. Read
  across process restarts it was **11 over 30 days, 8 of them in the last 24
  hours**, and the project's own alert would have fired for all eight. Several
  agents confirmed the zero back, because they read it the same way.
- **A three-state rendering can still mask a fourth.** "Memory reaches 0.0 d"
  reads as *not enough history yet* when the truth is *no history will ever
  accumulate*, because nothing writes the input.

### Name the population on both sides of the ratio, in the same sentence

Denominators move, and a moving denominator is not a rounding error.

- One population went **376 → 388 → 409 → 430 → 439 → 500 → 510 → 865** across a
  single milestone — +33% in one day. Every clause computed against it is a
  ratio over a divisor that moved a third while it was being measured.
- Re-derive the divisor at run time and **print its provenance beside the
  figure**. The drift is only visible because it is recomputed per call rather
  than stored.
- When several divisors are defensible, print the alternatives beside the one
  you chose and say why you chose it. One agent chose its divisor because it was
  *"the criterion's own word, the one the deployed code enforces against, the
  only population rather than a window sample, and the least flattering."*
- **The smaller number is not automatically the honest one.** One population was
  inflated and deflated at the same time: 96% of its rows were minted by an
  automated sweep and were not machines, while 47 real sources resolved to no
  row at all.
- Watch for a headline that moves more than the fact. A set-difference list
  collapsing 16 → 3 looked like thirteen resolutions; it was **one**, because
  binding a single member removes a whole group from the list.

### Assertions on the report, not on the thing being reported

Five times in one milestone, **the faulty thing was an instrument's own report**:

- A reconciliation printed *"A vs B agree"* on a pass where the comparison never
  ran.
- A report printed *"0 rows — an empty registry, which is a measurement"* over a
  store holding 4,879 rows.
- A permission test **passed on a failed login**, because the login-failure code
  was not in its denied-codes list. With a mistyped password: 66 failures with
  the blindness, 85 with the guard — *19 cells passing on an account that could
  not log in at all.*
- A log line's data leak was a **suffix** on a string whose **prefix** is exactly
  what the test asserts. "The log line contains a diagnosis" passes identically
  with or without the appended payload.
- A grant audit read `information_schema` **as the wrong account** and got an
  empty result — not an error. Two independent ways that instrument renders `0`,
  and `0` is what a healthy store looks like on exactly the rows that matter.

### Prove the guard bites

A test that cannot fail is worth less than no test, because it is believed.

- **Show the mutations.** The strongest verification of one run showed its guard
  failing five ways with counts — success moved into a `finally`, a `0` sentinel,
  a missing baseline, a reader folding absence into zero, and a hand-kept list —
  each mutation reverted immediately.
- **Show the failure demonstration against the natural implementation.** One
  guard, broken to the way anyone would write it first, failed 6 of 16 with 10
  still passing.
- **Say when a guard of your own was unreachable.** One agent found that 12 of
  13 mutations were caught and **M10 survived**, because no test reached that
  branch.
- **Derive, don't hardcode.** A hardcoded field list in a parser is the identical
  defect one release later. Derive the watch list from the settings object, the
  header table from the writer's own table, the job list from the scheduler's own
  registry — *so a job deleted from the table takes its reachability with it*.
- **Prefer deleting a derived number to maintaining one.** One document heading
  read "Ten decisions" above eleven; the fix removed the count and added a test
  forbidding its return. *An uncounted list cannot be miscounted.*
- **A guard that fails uninformatively gets deleted rather than investigated.**
  Make it name the actual and expected values, not "no match found".
- **A guard invalidated by an unrelated merge is a guard whose subject was
  borrowed rather than owned.** Assert the property directly, with a vacuity
  guard.

### Local green is not deployed correct

The artefact is correct, the tests pass, and it is dead on the deployed host.
This family recurred so often it earned its own sweeps.

- **The artefact with no writer.** Five instances in one milestone: metric
  families declared with nothing writing them, a column whose feed did not exist,
  a table with no writer since its migration, a human-judgement field defaulted
  uniformly across all rows. They render as **confident zeros** on the very
  dashboards added to prevent that.
- **The mirror: a writer nothing calls.** A module shipped, deployed, and
  reachable by nothing on the scheduler. It passes an "artefact has a writer"
  guard **by declaring no artefact at all.**
- **A third variant: reachable in the AST, unreachable in the process.** A static
  walk counts a writer inside `if False:`.
- **A one-shot CLI cannot move a scraped endpoint.** A counter lives in the
  process that incremented it, so running the CLI a hundred times and then
  pasting `/metrics` beside it *looks* like proof and is not. Evidence must show
  the endpoint moving after a **scheduled tick**. One agent turned this into
  positive evidence by a *mismatch*: its endpoint read differed from its own CLI
  run and matched the scheduler's log line to the digit.
- **A single reading cannot distinguish a live gauge from one written once at
  boot.** Take two ticks, or read the scheduler's own history.
- **Environment-variable spellings diverge between local and deployed.** One
  sweep found eighteen sites reading unprefixed names the deployed container
  never sets: eight dead on the host, two blind to spellings they claimed to
  cover, eight already correct. One read the *unprefixed* name **first**, so a
  developer with both set and the deployed system resolved different endpoints
  with neither knowing. The practical lesson: **a name-based search does not find
  the call sites that reach the value through a settings object** — 58 hits
  across 28 files for a DSN's name, but six real call sites contained no spelling
  of it at all.
- **Sweep to a negative result.** *"We looked, and there are exactly N"* closes
  the question; *"here are the three we found"* invites the next agent to
  rediscover it. **An accidental find is not a sampling method** — and the sixth
  instance of one leak was found only because someone searched for a different
  protocol's header shape.

### Smoke tests

- **Mandatory, and the real output goes in the PR body.**
- **A smoke test must not cause the outage it demonstrates containment of.** One
  agent ran its forced-failure half as a *second* process on its own port rather
  than breaking the deployed scheduler's configuration, and still produced real
  scrape evidence of the real job body.
- **Grade *unobservable* apart from *silent*.** A smoke test that runs, exits
  cleanly and proves nothing is worse than one that fails. One agent's pilots
  showed its target could not be observed on the logged path; it **declined the
  full 65-minute run** rather than shortening it, because 65 minutes watching an
  unlogged path produce zero lines *would have looked like a pass*. It graded the
  half `ALL_ORGANIC`, not `pass`.
- **Some things are unsmoke-testable by definition, and that is a finding.** One
  rule's own clause excluded every host from which the test could be run: *the
  only host the smoke test could succeed from is one that has already stopped
  doing the thing the rule detects.* Anyone who later "fixes" that is about to
  weaken the rule.
- **Two vantage points can produce two different faults with one apparent
  symptom.** The same probe run from a host shell and from inside a container
  failed for entirely different reasons.

### Disclosure

- **Redact secrets, never diagnostics.** Tracebacks, SQL, error codes and library
  messages are wanted in full; an early version of one error class reduced its
  cause to `type(exc).__name__` and turned a one-line diagnosis into three
  deploys.
- **Never put real hostnames, addresses or internal domains into an issue, PR,
  commit or comment.** Withhold values by default in refusal messages: *"looked
  for A, B, C, D (values withheld)"*.
- **`str()` on a database error can append bound parameters.** One refused insert
  logged its parameters — real identifiers — and that line shipped to the log
  store, which had deletion disabled. Two lines out of 1.2 million, found by a
  smoke test and by no test.
- **A credential on a command line reaches the local transcript.** Use the
  container-environment form so the password never appears as an argument.
- Say plainly where a disclosure reached and where it did not (commit? PR?
  comment? only a local transcript?), and what the remedy costs.

---

## Deploying

Deploys are **serial and the orchestrator's alone**, whatever else is
parallelised. Fill in `<DEPLOY>`, then enforce these four rules regardless of
mechanism.

**1. Get commits to the host through a path that does not depend on a
key-agent.** A bundle over a dedicated key needs no credentials on the host at
all. Fetching from the forge *on the host* typically authenticates through the
**forwarded** agent from the workstation — so the dependency the dedicated key
was meant to remove is still there, one hop further along, and it fails on the
host rather than locally, which is what makes it easy to misread. It works right
up until the agent locks, and an overnight run deploys many times.

**2. Verify the deploy by content, never by exit code.** `docker compose up`
exiting 0 says the deploy ran, not that it carries your merge. Assert ancestry
(`git merge-base --is-ancestor <sha> HEAD` on the host) *and* something
behavioural or byte-identical — the module hashing the same as the blob in the
merge commit, the new symbol importable inside the running container, the
migration head reading what you expect.

**3. Agents must never touch `<DEPLOY_HOST>`.** This is a control, not etiquette.
One agent deployed a branch that did not contain a migration revision another
agent had already applied: the provisioning step exited 1, **one service never
started and every other container stayed healthy** — so from outside it looked
fine, and the estate was down for four minutes. The brief said "merge, then
hold" and never said "and never touch the host". Two derived rules:

- **Read the deployed migration version on the host and refuse any branch that
  does not contain it.** The single-head rule protects the *merge*; nothing
  protected the *deploy*. A branch carrying no migration at all took the estate
  down purely by being one behind.
- A run that deploys every 20–40 minutes will never see an hourly job fire.
  Note it as a pending verification rather than concluding the job is broken.

**4. Know the traps that cost a deploy each.** Add yours to *Repo-specific
traps*; these three are near-universal:

- **`command:` does not replace an image's `ENTRYPOINT`, it appends to it.** Use
  `entrypoint:`. This trap was documented in the original template and *still*
  caught an agent, which ran a second scheduler against production for about ten
  minutes and caused four services to be re-created. It self-reported; the estate
  was verified independently and dedup absorbed it.
- **A file read at runtime must be copied into the image.** Scripts and docs
  directories usually are not, so a checker that reads them fails inside the
  container with a confusing message.
- **A directory a volume mounts over must exist *in the image*, owned by the
  runtime uid.** A fresh named volume is seeded from the image directory
  *including ownership*; with nothing there, the mountpoint is root-owned and the
  container is not root. The symptom was a job that verified its work, stamped
  its success metric, and then died writing the evidence — green metric, no
  output.
- **If the log driver ships to a log store, `docker compose logs` returns
  nothing.** Query the store. A stale conclusion here cost an hour.

**Credentials.** Prefer a dedicated passphraseless deploy key over an interactive
agent, which locks mid-session and takes SSH *and* push down together. Push can
usually fall back to a token over HTTPS:

```bash
gh auth setup-git
git -c url."https://github.com/".insteadOf="git@github.com:" push -u origin <branch>
```

**HTTPS does not help with signing** — separate credentials, similar-looking
failures. If the signing token locks, the standing decision decides: halt, or
continue with `--no-gpg-sign` and **list every unsigned commit in the closing
summary**. Keep unsigned commits on unmerged branches where possible — one
amend each — rather than letting them accumulate on the milestone branch, where
re-signing means a rewrite. One run finished with sixteen unsigned commits on
the milestone branch; another amended its WIP commit and landed fully signed.

---

## Merged is not running

If the repo has a `<PROMOTION>` step — anything where the deployed system reads
its behaviour from a store, a registry or a config service rather than from the
merged file — **that step is where the work becomes real, and it is the single
most reliable place for a milestone to lie to itself.**

The canonical incident: eight artefacts were authored in one day, every one
gate-passed against production data, every one merged, every one reported by its
agent as shipping — and the deployed system was running **none** of them. Each
agent had satisfied every acceptance criterion it was given. The defect was in
the seam between *"the artefact is correct"* and *"the artefact is loaded"*, and
no issue owned that seam. A demonstration script passed throughout, honestly: it
drove the engine against files read from the source tree, which proves the
artefacts are valid and says nothing about what the deployed system runs.

Rules that follow:

- **Evidence is the deployed store's own listing**, pasted into the PR. "I ran
  the promote command" is not evidence.
- **Beware a name collision between a field in the file and a column in the
  store.** In one project a `status:` key in the file was a *wish* and the
  `status` column in the store was the *fact*; the file field was removed
  entirely and replaced with a comment stating the tier being argued for, with a
  test failing if it returns.
- **Bootstrap-style commands usually only load artefacts with no revision.** The
  *second* time you touch one — a threshold corrected, a selector fixed — the
  merged file and the running artefact part company and **nothing in the ordinary
  workflow puts them back together.** Do it by hand: propose, then approve.
- **Read the per-item tally, not the exit code.** One project's `--check`
  asserted "every file has *some* in-force revision", which stays true after a
  file is corrected. **Seventeen of fifty-one items were diverged for a whole
  milestone while every agent pasted a green exit code as proof its work had
  landed.** The check now prints a per-item state and exits non-zero on
  divergence — and a later run caught a live divergence *belonging to a different
  issue* by reading that tally.
- **Sequence merges and promotions deliberately.** One agent held its PR back
  because the doc change altered the artefact's hash, so promoting first would
  have graded it diverged rather than annotated. Merge, deploy, then promote —
  and the brief transient divergence in between is the expected shape, not the
  permanent one.

---

## Milestone close-out

When every issue in the milestone is closed:

1. **Verify the exit criterion is actually met — not merely that the issues are
   closed.** Re-check the clause map from step 4, and **demonstrate** each clause
   rather than reasoning that it holds: run the thing end to end and paste the
   output. If a clause does not hold, open an issue for the gap rather than
   declaring victory.
2. **Do not tune the demonstration until it passes.** State the expected verdicts
   up front, so a disagreement reads as a signal rather than a script bug. A
   green run here is the most damaging artefact a milestone can produce.
3. **Count gaps apart; never average them.** Two milestones closed at *exit 2 with
   named numbers* — "3 gaps and 1 unmeasurable, counted apart" — and both times
   that was the intended outcome, not a shortfall. **A criterion is not amended to
   make it pass**; amendment is the milestone owner's call, on the record, with
   the original struck through rather than replaced.
4. **Check the demonstration can actually reach every clause.** One script swept
   six hours against a clause requiring seven days, so that clause read
   `UNTESTED` **structurally — every run, forever**. The seven-day reading had to
   be taken by hand, outside the script that exists so claims are not taken by
   hand. That is the probe-that-could-not-fail family aimed at the exit criterion
   instead of the code.
5. **Re-run at close-out rather than quoting the reading on file** — the estate
   moves. One re-run found a *third* gap that had not existed when the criterion
   was first demonstrated.
6. Open a release PR from `<MILESTONE_BRANCH>` into `<TRUNK>`, merge, and close
   the milestone.
7. **Leave the milestone open if a clause is genuinely time-bound.** Closing an
   epic whose own AC says "exit criterion met" when it is not is the failure this
   whole template guards against. Re-home stragglers explicitly.
8. Regenerate any dependency graph or index the repo keeps.

### Rollover into the next milestone

Advance to the next open milestone, cut its branch, expand its epic into issues,
and loop. Two things make the next milestone better rather than merely next:

- **Carry the method forward explicitly.** Tell the expansion agent what the last
  milestone learned: which guard is live and will fail a PR, which defect family
  to expect, which estimate in the design doc is now known to be wrong, and which
  dates are calendar-bound.
- **De-risk first.** Where a milestone's plan rests on an estimate, make the
  measurement the first issue and block the build behind it. One such issue
  measured the estate at 5.15M lines/day against the design's 1.71M, found the
  planned rate **6.3× outside** the documented band at its most favourable
  reading, and rewrote five downstream issues **before a line of the feature
  existed**. That is what a de-risk issue is for.

---

## Escalation and stop conditions

**Decisions that belong to the milestone owner, not an agent:**

- Amending an exit criterion, or choosing between defensible divisors when the
  choice changes the verdict.
- Widening a smoke-test allow-list.
- Changing a grant, a credential, or anything on infrastructure this repo reads
  and does not own.
- Approving or promoting anything that establishes a baseline the estate has not
  earned. One agent's framing is the right test: **whether one approval is a
  *fixture* or a *review* is decided by the proportion, not the label** —
  approving one of eight reads as a fixture; approving seven of eight quietly
  asserts a baseline.

When the orchestrator takes such a decision on the owner's behalf under a
standing decision, **record who authorised it and on what evidence**, and attach
limits: what must be shown failing, what must not change as a side effect, and
what to do if the finding turns out to be the opposite of what was expected.

**Stop and ask rather than improvising when:**

- An AC is ambiguous or contradicts the design document
- The work needs a file outside the issue's declared scope
- A smoke test cannot be run without mutating production
- The design does not cover something the issue needs — a genuine gap is worth
  surfacing, not designing around
- The deploy credential itself is refused (as distinct from the routine
  key-agent lock, which is handled — say which path was used and why)

This list is exhaustive. Anything not on it is written down and worked through,
not escalated.

**Leaving an inert artefact behind is not neutral.** One run stood down cleanly
but left a `pending` proposal batch on the deployed store. It changed nothing,
but *a pending batch nobody owns is the proposal-surface equivalent of a stale
board column.* Name it in the handover with an explicit approve-or-reject ask.

---

## Repo-specific traps

Append to this section as the project teaches you things. One entry per trap:
what happened, what it cost, and the check that would have caught it.

Keep the incident attached to the rule. A rule without its incident reads as
fussiness and gets dropped by the next person tidying the file — and the two
warnings in the original version of this template that *were* kept, and *were*
still walked into, are the reason each is now written as a story rather than an
instruction.
