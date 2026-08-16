# Legacy route parity — the V1M3 exit criterion, demonstrated

> **every legacy page either exists as a registration or is deleted because
> atrium covers it — demonstrated against the deployed stack**

**Verdict: ~~exit 2~~ exit 0. ~~15~~ ~~11~~ ~~10~~ ~~2~~ **0** of 39 legacy
routes are in neither column**, in ~~five~~ ~~one capability group~~ no groups.
The counts were kept apart and never averaged on the way down, which is why the
zero is a result and not a rounding.

This file is the route-by-route walk. Every verdict in it is a live response
from a stack the run that wrote it stood up. Reproduction commands are inline so
the table can be re-run rather than believed.

**A zero deserves more suspicion than any other number here**, so §4 records
what the instrument did on its first pass rather than only its last: three rows
disagreed with their stated predictions, and the walk exits non-zero when that
happens. All three turned out to be probe defects and are described in §0 — but
they are the reason this reading is a measurement rather than a formality, and a
"0 gaps" run that had never printed a gap would be worth very little.

---

## 0. Five readings, and what moved between them

A verdict is amended **visibly** here, never replaced. Changed cells are struck
through and the reasoning is kept.

| | #47 | #69 | #47-rerun | #75 + #73, merged tree | **#74, merged tree (2026-08-16)** |
|---|---|---|---|---|---|
| deleted — atrium covers it | 13 (33.3%) | 13 (33.3%) | 13 (33.3%) | 13 (33.3%) | 13 (33.3%) |
| registered | 11 (28.2%) | 15 (38.5%) | 15 (38.5%) | 23 (59.0%) | **25 (64.1%)** |
| **deliberately dropped** — *a third disposition, see §3.4* | — | — | 1 (2.6%) | 1 (2.6%) | 1 (2.6%) |
| **neither — the finding** | **15 (38.5%)** | **11 (28.2%)** | **10 (25.6%)** | **2 (5.1%)** | **0 (0.0%)** |
| gaps restricted to the 22 *pages* | 9 (40.9%) | 7 (31.8%) | 7 (31.8%) | 2 (9.1%) | **0 (0.0%)** |

**The gap column is empty.** Every reading before this one closed routes with a
page; #74's two needed a schema change, which is why they were the last two
standing and why three earlier readings reported rather than built them. §4
carries the walk that measured it and the first run's disagreements, which are
what establish that the instrument can still say `gap` at all.

Both denominators were re-derived from the legacy source at each reading and
have not moved: 39 routes, 22 pages, `dyndns-route53` still pinned at
`5d1c941`. **The counts moved; the divisors did not** — a shrinking gap over a
shrinking denominator would not be the same result.

### What #75 changed, and what it deliberately did not

Four routes, three unrelated groups, all previously `405`. Each is now
registered **and demonstrated over HTTP** against a stack this run stood up
(`COMPOSE_PROJECT_NAME=ddns75`, API on `:8175`), migrated on both chains, admin
seeded, host bundle promoted with `make seed-bundle` — see §3.3 G3/G4/G5 for
the responses.

Every absence probe in those three sections is a **mutation**, never a `GET`,
and each carries §2.1's control taken in the same session:

```
POST /api/atrium_ddns/made-up-dddeef -> 405 application/json   <- control: absent
GET  /api/atrium_ddns/made-up-dddeef -> 200 text/html          <- the same path, off the SPA catch-all
```

**One thing #75 declined to do, and it is the boundary the issue drew.**
`POST /admin/events/clear` stays in §3.4 as a struck route. G3's `clear` resets
*health-check results* — four columns on `ddns_hostname` — and deletes nothing;
`tests/test_router_health_checks.py` asserts that a clear leaves the
`ddns_event` rows in place, because the cheapest wrong implementation of
"clear the health checks" is a `DELETE` and it would look tidy in review.
Clearing a result and clearing a log are different operations on different
tables. The operator's decision named one route; extending it by analogy would
be inventing a disposition.

**And one it left open.** G5 asked whether the help entry should be registered
or struck as a third-disposition route, and said the call is the operator's.
The entry is registered — the issue's own default, and the cheap thing — but
the question it raises is recorded under G5 rather than answered.

### What #74 changed, and the probe it had to correct to see it

**G1 is closed and its two routes move to *registered*.** They were the per-
hostname *backend* screen, and the rewrite had no data model for any of the
three things it did — which is why #47, #69 and the #47 re-run each reported
them and none built them. `0004_hostname_backends_and_ttl` adds
`ddns_hostname_backend` and `ddns_hostname.ttl`; `POST /hostnames/{id}/update`
is the manual trigger, running the wire's own publish path. §3.3's G1 block
keeps the finding struck through and carries the closing evidence, taken from a
stack this run stood up (`COMPOSE_PROJECT_NAME=ddns74`, API on `:8074`).

**The two denominators did not move.** Instrument A was re-run against
`dyndns-route53` at `5d1c941` for this reading: 39 routes, 22 pages. The gap
went to zero over a fixed divisor.

**The walk found two probe defects of its own, and one of them is #73's lesson
pointing the other way.** #73 corrected its `/nic/*` probes to read the body
because `GET /nic/update` answers `200 text/plain badauth` — the wire carries
errors in the body, not the status. `/nic/checkip` needs the body for the
*opposite* reason: it answers **`200 text/html` from the router**, because
`CHECKIP_HTML` is an HTML wrapper by legacy contract. So a probe that reads
`text/html` as "this is the SPA catch-all" — which is exactly what §2.1 tells
you to do — classifies a real, frozen-table-covered route as **absent**.
Content type discriminates two of the three `/nic/*` routes and misreads the
third. Only the body separates all three from the shell.

**And a registration key composed at run time is not in the bundle.** #73's
three settings routes register as `` `atrium-ddns-settings-${key}` ``, so the
composed keys never appear as literals in the shipped artefact; the **paths**
do. A bundle-grep keyed on the registration key reports ABSENT for a surface
that is present, and it reports it identically to a surface that is genuinely
missing. §3.2's claim that all registrations "were confirmed in the bundle the
running stack serves" holds for those three rows by path and not by key, which
is worth knowing before the next reading greps for a key.

Both defects were in **this** walk's first run, both were corrected, and both
are recorded rather than quietly fixed — see §4.

### What #73 changed, and the one thing it does not claim

**G2 is closed and the four routes move to *registered*.** The
`registerSettingsGroup` container plan §4 asked for exists, with three child
pages covering every field of the namespace — eleven when #73 was written,
twelve once #75's `health_check_manual_cooldown_seconds` merged, and the page
needed no edit for the twelfth because its field list is derived from the model
rather than typed out — and
`PATCH /api/atrium_ddns/devices/{id}` changes a device's limit without
rotating its credential. §3.3's G2 block below keeps the finding struck through
and carries the closing evidence, all of it taken from a stack this run stood
up (`COMPOSE_PROJECT_NAME=ddns73`, API on `:8173`).

**The two denominators did not move.** Still 39 routes and 22 pages, still
`dyndns-route53` at `5d1c941`. The gap shrank over a fixed divisor.

**What it does not claim, stated here rather than buried in §3.3.** Nobody
loaded the page in a browser. This repository has no browser harness — no
Playwright, no `*.spec.ts`, nothing that renders atrium's shell — so the
sidebar entry is evidenced from **both bundles the stack serves** (the host
calls `registerSettingsGroup`; the shell carries that function's own
implementation, warning string and all) and from the vitest suite, and not from
a click. Everything *below* the sidebar is demonstrated over HTTP: the schema,
the write, the refusals, and the rate limiter changing behaviour. §3.3's G2
block says which is which, line by line.

### The two readings above were taken against the same base, and this one
### was not

**#75 and #73 were written in parallel off `4ff2da4` and merged here.** Each
independently measured the gap column at **10 → 6**, each correctly against the
base it could see, and neither accounted for the other. So the merged figure is
neither side's 6 — and it is not "obviously 2" either, because a number arrived
at by subtracting two diffs is exactly the kind of arithmetic this file is not
allowed to carry. It was **re-measured against the merged tree**, from a stack
built from it (`COMPOSE_PROJECT_NAME=ddns73`, API on `:8173`), and the counts in
§0 and §4 are that reading. The predicted value is recorded beside it in §4 so a
disagreement would have been visible rather than absorbed.

### What the #47 re-run did, and why it was not a formality

**#69 re-measured G1 only** and recorded, in this file, that G2–G5 were carried
forward without being re-taken. Carried-forward verdicts are where a stale pass
hides, so **every group was measured again here**, from a stack this run stood
up (`COMPOSE_PROJECT_NAME=ddns47r`, API on `:8147`), migrated on both chains,
admin seeded, host bundle promoted with `make seed-bundle`. Four things came
back different from the file:

1. **The gap instrument the two earlier readings used was partly blind.** The
   shell answers **`200 text/html`** for *every* unmatched `GET` — so
   `GET <an endpoint that does not exist>` returns 200, and status code alone
   cannot tell an absent surface from a present one. §2.1 makes this the
   control it should always have been. The earlier readings happen to survive
   it because their gap probes were `POST`/`PATCH` (which the catch-all does
   not serve, so `405` is the router's own answer), but the method was one
   `GET` away from a table full of false negatives.
2. **`/admin/events/clear` is now recorded as *deliberately dropped*** — a
   third disposition, on the operator's decision, with the replacement named.
   §3.4 says plainly that this is a third column rather than pretending it fits
   one of the two.
3. **G2's verdict is re-taken with a stronger instrument and holds.** #47 read
   atrium's *source* for "there is no generic namespace editor". This run reads
   the **shell bundle the stack serves** and enumerates every call site of the
   namespace-parameterised mutation hook: five, of which four are literals and
   one is the definition. §3.3.
4. **Two cell-level corrections** to §3.2's registration list — the admin tab's
   key and the bundle's size. Small, but both were quoted as measurements.

**The operator's `/admin/users` / `/admin/roles` decision changes no count, and
that is worth stating rather than silently applying.** The instruction for this
re-run was to strike those rows with atrium's surface named, the predecessor
having declined to. Re-reading the file: the predecessor did **not** decline —
all six legacy user-management routes were already in §3.1's deleted column,
with `/admin/users` and `/admin/roles` named as the atrium surfaces covering
them. They are re-demonstrated live below. The decision is now on the record and
the count is unchanged at 13; nothing was struck a second time.

---

## 1. The population, and how it was counted

The criterion says *page*, and "page" and "route" are different populations in a
Flask app: `POST /admin/domains/<id>/delete` is a route and not a page. Both
denominators are given, and every table below is over **routes**, which is the
larger and less flattering of the two.

| population | count | how |
|---|---|---|
| routes in `web_routes.py` | **36** | two instruments, below |
| routes in `dyndns.py` (the `/nic/*` blueprint) | **3** | same |
| **total app-authored routes** | **39** | |
| of those, routes that render a template (*pages*) | **22** | `render_template` call sites, attributed to their view |
| distinct page templates | **19** | 20 `.html` files less `base.html`, which is a layout no route renders |
| routes reached by `add_url_rule` | **0** | swept for; there are none |

### Two instruments on the count

Neither number was inherited. Both were re-run for this reading on
`/Users/brendan/src/dyndns-route53`.

**Instrument A — static, definition side.** Walk the Python AST of
`web_routes.py` and `dyndns.py` and collect every `@blueprint.route(...)`
decorator. Never executes the module, so it cannot see a route registered at run
time. → `web_routes.py: 36`, `dyndns.py: 3`.

**Instrument B — runtime, authoritative.** Build the real Flask app, register
both blueprints, enumerate `app.url_map`. Sees everything Flask will actually
serve, including anything `add_url_rule` added and any decorator stacking A
might mis-attribute.

```
INSTRUMENT B - Flask url_map (runtime, authoritative)
  web blueprint rules       : 36
  nic_update blueprint rules: 3
  other (flask builtins)    : 1 -> ['static']
  TOTAL app-authored rules  : 39

PAGES - views whose body contains render_template(...)
  page routes (render_template call sites) : 22
```

**They agree exactly: 36 + 3 = 39, and 22 pages.** State the slack honestly —
both readings derive from importing the same two source files, so they are not
independent about *which files* were examined; the sweep for other `Blueprint(`
/ `add_url_rule` call sites across the repo is what closes that, and it returns
only the two blueprints already counted. What the pair genuinely establishes is
that no route is registered by a mechanism the static walk cannot see: had
`add_url_rule` been used anywhere, B would exceed A. It does not.

**Instrument C — consumer side.** 32 distinct `web.*` endpoints are referenced
by `url_for(...)` in `templates/`; 0 are referenced and undefined. The four
defined-but-never-linked endpoints are `web.index`, `web.health`,
`web.totp_verify` and `web.totp_setup` — reached by redirect or by a monitoring
probe, never by a link, which is the expected shape and not a discrepancy.
32 + 4 = 36.

**Provenance of the tree that was counted.** All readings were taken on
`brendanbank/dyndns-route53` at **`5d1c941`** — the same commit
`tests/compat/legacy_behaviour/legacy_inventory.yaml` already pins as this
repo's frozen legacy reference — with `git status --porcelain` empty and
`git diff --stat 5d1c941 HEAD` empty across `web_routes.py`, `dyndns.py` and
`templates/`. So this table and the compat suite describe the same legacy
behaviour.

Do not confuse the two counts: `legacy_inventory.yaml`'s **147** is the legacy
*pytest suite*, hand-classified into ported/dropped. This file's **39** is
*routes*. Different populations measured for different purposes, and neither is
a check on the other.

<details>
<summary>reproduce</summary>

```bash
# A — AST over the decorators
python3 - <<'EOF'
import ast, pathlib
for f in ("web_routes.py", "dyndns.py"):
    tree = ast.parse(pathlib.Path(f).read_text())
    n = sum(1 for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            for d in node.decorator_list
            if isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "route")
    print(f, n)
EOF

# B — the real url_map (needs the legacy deps in a venv)
.venv/bin/python -c "
from flask import Flask; import models
app = Flask(__name__); app.config.update(SECRET_KEY='x', SQLALCHEMY_DATABASE_URI='sqlite://')
models.db.init_app(app)
from flask_login import LoginManager; LoginManager().init_app(app)
from web_routes import web_bp; from dyndns import nic_update_bp
app.register_blueprint(web_bp); app.register_blueprint(nic_update_bp)
print(len([r for r in app.url_map.iter_rules() if r.endpoint != 'static']))"

# sweep for routes neither instrument would attribute to those two files
grep -rn 'add_url_rule\|Blueprint(' --include='*.py' . | grep -v tests/
```
</details>

---

## 2. How each column was demonstrated

Against a stack this run stood up itself (`COMPOSE_PROJECT_NAME=ddns47r`, API on
`:8147`), migrated on both chains, admin seeded, host bundle promoted with
`make seed-bundle`. Not against source.

| column | instrument | what it reads |
|---|---|---|
| **registered** | the host bundle *the running stack serves* — `GET /host/main.js` | the registration key and route path are present in the shipped artefact |
| **registered** | `GET /openapi.json` on the running stack | the endpoint the page calls exists |
| **deleted** | authenticated requests to atrium's own API and SPA on the running stack | the replacement surface answers |
| **gap** | `POST`/`PATCH` to the endpoint that would have to exist, plus a content-type read on `GET` | the stack's own refusal — see §2.1 |

Every gap below was probed as a **super_admin holding all seven `atrium_ddns.*`
permissions**, including `atrium_ddns.hostname.manage`. A 401 would have meant
"the session died", not "the surface is absent"; the probe asserts against 401
for exactly that reason.

```
GET /api/users/me/context -> 200
roles: ['admin', 'super_admin']
permissions held: 20, atrium_ddns.*: ['atrium_ddns.admin',
  'atrium_ddns.device.manage', 'atrium_ddns.domain.manage',
  'atrium_ddns.events.read.all', 'atrium_ddns.hostname.manage',
  'atrium_ddns.read', 'atrium_ddns.write']
```

### 2.1 The control that should have been here from the first reading

The shell serves `index.html` for every unmatched `GET`. So a `GET` against an
absent endpoint returns **200**, and the status code is uninformative. The
control row is a path made up at run time from a random tag — it cannot exist,
and it answers identically to the four real absences:

```
GET /api/atrium_ddns/board                -> 200 application/json   REAL ENDPOINT
GET /api/atrium_ddns/rate-limits          -> 200 text/html          SPA index.html — NOT an endpoint
GET /api/atrium_ddns/hostnames/1/backends -> 200 text/html          SPA index.html — NOT an endpoint
GET /api/atrium_ddns/made-up-a6602e       -> 200 text/html          SPA index.html — NOT an endpoint   <- control
GET /api/atrium_ddns/health-checks        -> 200 text/html          SPA index.html — NOT an endpoint
```

**Before trusting a probe, ask what it would print if the thing it measures were
absent.** Here, `200`. Content-type discriminates; status alone does not. Every
`GET`-shaped verdict below reads the content-type, and every mutation-shaped one
reads `405` — which comes from the router, not from the catch-all, because the
catch-all serves `GET` only.

---

## 3. The table

### 3.1 Deleted — atrium covers it (13 routes)

Each row's evidence is a live response from the running stack, not a reading of
atrium's source.

| legacy route | what in atrium covers it | demonstrated |
|---|---|---|
| `GET /` | atrium `/` shell root | `GET / -> 200` |
| `GET /health` | atrium `/healthz`, `/readyz`, `/health` | `GET /healthz -> 200`, `GET /readyz -> 200`, `GET /health -> 200` |
| `GET,POST /admin/login` | atrium `/login` `LoginPage` | `/login` in the shell's route table; `POST /api/auth/jwt/login -> 204` + `atrium_auth` cookie |
| `GET /admin/logout` | atrium header user menu | `POST /api/auth/jwt/logout -> 204`, then `/api/users/me/context -> 401` |
| `GET,POST /admin/totp-verify` | atrium `/2fa` `TwoFactorPage` | `/2fa` in the shell's route table; `GET /api/auth/totp/state -> 200` |
| `GET,POST /admin/totp-setup` | atrium `/profile` 2FA card + `TwoFactorSetupModal` | `/profile` in the shell's route table; `GET /api/auth/totp/state -> 200` |
| `GET,POST /admin/profile` | atrium `/profile` (host adds `registerProfileItem atrium-ddns-profile`, slot `after-roles`) | `GET /api/users/me/context -> 200` |
| `POST /admin/profile/reset-2fa` | atrium `/profile` → disable 2FA | `/api/auth/totp/disable` in schema |
| `GET /admin/users` | **atrium `/admin/users`** | `users` is a registered admin section in the served shell bundle (below); `GET /api/admin/users -> 200 (1 item)` |
| `GET,POST /admin/users/new` | **atrium `/admin/users`** invite flow | driven end to end for this reading: `POST /api/invites role_codes=['user'] -> 201`, `POST /api/invites/accept -> 201 {'user_id': 6}`, then that user logs in `-> 204` |
| `GET,POST /admin/users/<id>` | **atrium `/admin/users` edit + `/admin/roles`** — RBAC replaces the legacy `is_admin` boolean | `roles` is a registered admin section (below); `GET /api/admin/roles -> 200 (3)`, `GET /api/admin/permissions -> 200 (20)`, `/api/admin/roles/{role_id}` in schema |
| `POST /admin/users/<id>/delete` | atrium `/admin/users` delete, plus self-serve deletion on `/profile` | `/api/admin/users/{user_id}/delete` in schema; `/api/users/me/delete` in schema; `auth.allow_self_delete = True`, `auth.delete_grace_days = 30` |
| `POST /admin/users/<id>/reset-2fa` | atrium admin TOTP reset | `/api/admin/users/{user_id}/totp/reset` in schema |

The three bolded rows are the operator's `/admin/users` + `/admin/roles`
decision, now on the record. They were already here; §0 says why the count does
not move.

**Their evidence had to be re-taken, and this is §2.1 biting the writer of
§2.1.** *"`GET /admin/users -> 200`"* was in the first draft of this table and is
worthless: the shell answers 200 for every path, including the made-up control.
What is not worthless is the SPA's own section registry, read out of the shell
bundle the stack serves — 11 admin sections, `defaultSection: 'admin'`, so
`/admin/<key>`:

```
system  auth  users  branding  roles  tokens  translations  emails  outbox
reminders  audit
```

`users` and `roles` are in it. Every login/2FA/profile row above is evidenced
the same way — from the shell's react-router table, which registers `/login`,
`/2fa`, `/profile`, `/profile/tokens`, `/accept-invite`, `/reset-password` and
`/admin/:section` — plus the API responses in the right-hand column. **No row in
this table rests on a bare 200 from the catch-all.**

Atrium's audit log (`GET /api/admin/audit -> 200`) and notifications are
additional surfaces with no legacy counterpart. They are not in the table
because the criterion runs legacy→atrium, not the reverse.

### 3.2 Registered — name the registration (~~11~~ ~~15~~ ~~23~~ 25 routes)

All ~~15~~ ~~23~~ 25 registrations were confirmed **in the bundle the running
stack serves** — ~~`GET /host/main.js -> 200, 748,893 bytes`~~ ~~763,680 at
#75~~ ~~767,491 after #73~~ ~~783,112 on the merged tree~~ `GET /host/main.js
-> 200, 801,320 bytes` after #74 — not in `frontend/src/`.

**With one caveat #74's walk found, which applies to three rows below.** The
settings routes register as `` `atrium-ddns-settings-${key}` `` — a template
literal composed at run time — so those three keys are **not** in the bundle as
literals and cannot be. Their *paths* are. Confirming them "in the bundle"
therefore means by path, and a later reading that greps for the key will get
`False` from a surface that is present, indistinguishable from one that is
absent. Every other row here is confirmable by key.

| legacy route | registration | backing endpoint on the running stack |
|---|---|---|
| `GET /admin/` (dashboard) | `registerRoute atrium-ddns-board` `/atrium-ddns/board`, `registerRoute atrium-ddns-logs` `/atrium-ddns/logs`, `registerHomeWidget atrium-ddns-widget` | `GET /api/atrium_ddns/board`, `GET /api/atrium_ddns/events` |
| `GET,POST /admin/domains` | `registerRoute atrium-ddns-domains` `/atrium-ddns/domains` + `registerNavItem atrium-ddns-domains-nav` ("Zones and providers") | `GET`/`POST /api/atrium_ddns/domains` |
| `POST /admin/domains/<id>/delete` | **#88: moved** to `registerRoute atrium-ddns-zone-detail` `/atrium-ddns/zones/:id` | `DELETE /api/atrium_ddns/domains/{domain_id}` |
| `GET,POST /admin/domains/<id>/backends/new` | **#88: moved** to the zone detail route — *and* the create-zone form, which now carries the first binding in the same submission | `POST /api/atrium_ddns/domains/{domain_id}/backends`, or `POST /api/atrium_ddns/domains` with a `backend` |
| `GET,POST /admin/domains/<id>/backends/<db>/config` | **#88: moved** to the zone detail route | `PATCH /api/atrium_ddns/backends/{backend_id}` |
| `POST /admin/domains/<id>/backends/<db>/delete` | **#88: moved** to the zone detail route | `DELETE /api/atrium_ddns/backends/{backend_id}` |
| `GET /admin/events` | `registerRoute atrium-ddns-logs` `/atrium-ddns/logs` + `registerNavItem atrium-ddns-logs-nav` ("Log search") | `GET /api/atrium_ddns/events` |
| `GET /admin/health-checks` (results list) | the `answered` station of the resolution strip on `atrium-ddns-board` | `GET /api/atrium_ddns/board` |
| `GET /nic/checkip` | `router_nic.py`, frozen compat table | `200` (excluded from OpenAPI by design) |
| `GET /nic/update` | `router_nic.py` | `200 good 203.0.113.147` — §3.3.1 step 8 |
| `GET /nic/delete` | `router_nic.py` | `200` |
| **`GET,POST /admin/hostnames`** (#69) | `registerRoute atrium-ddns-names` `/atrium-ddns/names` + `registerNavItem atrium-ddns-names-nav` ("Names") | `GET`/`POST /api/atrium_ddns/hostnames` |
| **`POST /admin/hostnames/<id>/delete`** (#69) | same page | `DELETE /api/atrium_ddns/hostnames/{hostname_id}` |
| **`GET,POST /admin/users/<uid>/hostnames`** (#69) | same page, under `atrium_ddns.admin` — see the stricter reading in §4 | same endpoints, scope widened |
| **`POST /admin/users/<uid>/hostnames/<hn>/delete`** (#69) | same | same |
| **`POST /admin/health-checks/run`** (#75) | the *Check now* button on `atrium-ddns-board` (`board/HealthCheckActions.tsx`) | `POST /api/atrium_ddns/health-checks/run` — §3.3 G3 |
| **`POST /admin/health-checks/clear`** (#75) | *Clear results*, same strip | `POST /api/atrium_ddns/health-checks/clear` — §3.3 G3 |
| **`GET,POST /admin/domains/<id>`** (#75, moved by #88) | *Rename* on `registerRoute atrium-ddns-zone-detail` `/atrium-ddns/zones/:id` — the legacy route was a **per-zone page**, and this is the first registration that is one | `PATCH /api/atrium_ddns/domains/{domain_id}` — §3.3 G4 |
| **`GET /admin/help`** (#75) | `registerRoute atrium-ddns-help` `/atrium-ddns/help` + `registerNavItem atrium-ddns-help-nav` ("Help") | none — the page calls no endpoint, see §3.3 G5 |
| **`GET,POST /admin/rate-limits`** (#73) | `registerSettingsGroup atrium-ddns-settings` → child `rate-limits`, backed by `registerRoute atrium-ddns-settings-rate-limits` `/atrium-ddns/settings/rate-limits` | `GET /api/admin/app-config` (values), `GET /api/atrium_ddns/config/schema` (shape), `PUT /api/admin/app-config/atrium_ddns` (write) |
| **`GET,POST /admin/rate-limits/user/<id>`** (#73) | re-keyed to the device: `registerRoute atrium-ddns-devices`, the *Rate limit* control on each row | `PATCH /api/atrium_ddns/devices/{device_id}` |
| **`POST /admin/rate-limits/user/<id>/delete`** (#73) | same control, emptied — *inherit* is `null`, not `0` | same endpoint, `{"rate_limit_per_minute": null}` |
| **`GET,POST /admin/health-checks/config`** (#73) | same group → child `health-checks`, `registerRoute atrium-ddns-settings-health-checks` `/atrium-ddns/settings/health-checks` | the three endpoints above |
| **`GET,POST /admin/hostnames/<id>/backends`** (#74) | `registerRoute atrium-ddns-names` — the *Publishing* modal on each row, "same page" in the sense the four `/admin/domains/<id>/backends/*` rows already use | `GET`/`PUT /api/atrium_ddns/hostnames/{hostname_id}/backends`, `POST /api/atrium_ddns/hostnames/{hostname_id}/update` — §3.3 G1 |
| **`GET,POST /admin/users/<uid>/hostnames/<hn>/backends`** (#74) | same, under `atrium_ddns.admin` — see §4's stricter reading | same endpoints, scope widened |

The third child, `retention` (`/atrium-ddns/settings/retention`), has **no legacy
counterpart** and so appears in no row: the old service pruned inside
`log_event` and had no retention screen. It is counted nowhere and named here
for the same reason the two device registrations are.

The ~~six~~ seven nav items in the served bundle, read out of the bundle rather
than out of `main.tsx`:

```
key=atrium-ddns-nav            label=Atrium Ddns          to=/atrium-ddns
key=atrium-ddns-board-nav      label=Devices and names
key=atrium-ddns-domains-nav    label=Zones and providers
key=atrium-ddns-devices-nav    label=Devices
key=atrium-ddns-names-nav      label=Names
key=atrium-ddns-logs-nav       label=Log search
key=atrium-ddns-help-nav       label=Help                 to=/atrium-ddns/help   (#75)
```

Two registrations are real and have **no legacy counterpart**, so they appear
nowhere above: `registerRoute atrium-ddns-devices` (`/atrium-ddns/devices`) and
its nav item. The device is the object the rewrite added; §4 of the plan is
right that it is what makes the rest cohere.

**#88 adds a fourteenth registration and no eighth nav item.**
`registerRoute atrium-ddns-zone-detail` serves `/atrium-ddns/zones/:id` — the
first path this bundle registers that carries a react-router parameter, and the
first that is deliberately *not* in the sidebar: it is reached from a zone row,
and a nav item pointing at it would be a link to a literal colon. Four rows
above moved onto it. `ui-design.md` §12 argues route over drawer over split
pane on the measured width budget — one resolution strip needs ≈592px, atrium's
shell gives 1168px, a 360/800 split leaves ~790px, and Mantine's `lg` drawer at
620px is *below* the one-strip minimum.

**Three registrations are still the scaffold.** `atrium-ddns-widget` (a counter
with a *Bump counter* button), `atrium-ddns-page` (`/atrium-ddns`, *"Replace
this page with your real domain UI"*) and the admin tab — whose key, read from
the served bundle, is **`atrium-ddns`**, ~~`atrium-ddns-admin-tab`~~ as the #47
reading recorded it — are the `create-atrium-host` template's demo, unchanged.
They are counted as registrations because they *are* registrations and the
criterion asks for a registration; a reader should know that the dashboard row
above rests on the board and the log page, not on the home widget.

### 3.3 In neither column — the finding (~~15~~ ~~11~~ ~~10~~ ~~2~~ 0 routes, ~~5~~ ~~one group~~ none)

Counted apart, never averaged. **This section is now empty of open rows**; every
group below is kept, struck through, with the evidence that closed it.

#### ~~G1 — the hostname lifecycle (~~6~~ 2 routes) · severity: ~~blocking~~ medium~~ — **CLOSED by #74**

| legacy route | what it did | disposition |
|---|---|---|
| ~~`GET,POST /admin/hostnames`~~ | list my hostnames **and create one** | **closed by #69** — re-demonstrated §3.3.1 |
| ~~`POST /admin/hostnames/<id>/delete`~~ | remove a hostname | **closed by #69** — re-demonstrated |
| ~~`GET,POST /admin/hostnames/<id>/backends`~~ | choose which provider backends a hostname publishes to; edit its TTL; trigger a manual DNS update | **closed by #74** — schema change `0004`, demonstrated §3.3.2 |
| ~~`GET,POST /admin/users/<uid>/hostnames`~~ | the same, for another tenant, as an admin | **closed by #69** (scope-widened; see §4's stricter reading) |
| ~~`POST /admin/users/<uid>/hostnames/<hn>/delete`~~ | " | **closed by #69** — re-demonstrated |
| ~~`GET,POST /admin/users/<uid>/hostnames/<hn>/backends`~~ | " | **closed by #74** — same endpoints under `atrium_ddns.admin`, §3.3.2 step 11 |

**Registration**: `registerRoute atrium-ddns-names` `/atrium-ddns/names` +
`registerNavItem atrium-ddns-names-nav` ("Names") — a *Publishing* modal on each
row, in the same "same page" sense §3.2 already uses for the four
`/admin/domains/<id>/backends/*` routes. **It registers no new route**, so the
bundle's registration sweep stays at thirteen surfaces and #75's help guard —
which asserts every registered route has a help entry — is satisfied without a
new entry. The Names entry's blurb was extended anyway: the guard's letter did
not require it and its intent does.

**Backing endpoints**: `GET`/`PUT
/api/atrium_ddns/hostnames/{hostname_id}/backends` and `POST
/api/atrium_ddns/hostnames/{hostname_id}/update`.

~~Measured again on this stack, as the super_admin above:~~ superseded — the
`405`s below were the correct reading in the #47 re-run and are kept for the
record:

```
POST /api/atrium_ddns/hostnames/1/backends -> 405 {'detail': 'Method Not Allowed'}
POST /api/atrium_ddns/hostnames/1/update   -> 405 {'detail': 'Method Not Allowed'}
```

Re-measured on the merged tree, with a **control first** so "routed" is a
comparison rather than a hope:

```
PUT  /api/atrium_ddns/hostnames/1/no-such-route  -> 405   <- the control: absent
PUT  /api/atrium_ddns/hostnames/999999/backends  -> 422   <- routed
POST /api/atrium_ddns/hostnames/999999/update    -> 422   <- routed
served OpenAPI schema: /api/atrium_ddns/hostnames/{hostname_id}/backends ['get', 'put']
served OpenAPI schema: /api/atrium_ddns/hostnames/{hostname_id}/update   ['post']
```

The three sub-features and where each now lives:

- **Which backends a hostname publishes to** — `ddns_hostname_backend`, a
  selection table. **An empty selection means *inherit the zone*, not *publish
  nowhere*.** That is the legacy service's own `Hostname.get_backends()` and the
  frozen model case
  `backends-empty-selection-resolves-to-all-of-the-domains-backends`
  (`preserve`) — and it is the only reading under which introducing the table is
  safe, because every hostname that exists has no row in it. The migration
  therefore backfills nothing. The alternative is argued down in `0004`'s
  docstring rather than left implied: a backfill freezes each name's backend set
  at migration time, so a binding added to a zone afterwards would be published
  to by new names and not by old ones — the same silence, moved six months out.
  `backends-empty-selection-tracks-the-domain-live` is `preserve` and is exactly
  that property. The cost, stated where it is paid: "publish nowhere" becomes
  unspellable.
- **Per-hostname TTL** — `ddns_hostname.ttl`, nullable. NULL is *inherit*, and
  inherit resolves to `ddns_domain_backend.config['ttl']`, which falls back to
  `providers.DEFAULT_TTL`. NULL is deliberately not 60: a name at NULL follows a
  later change to its binding and a name explicitly set to 60 does not.
- **A manual DNS update trigger** — `POST /hostnames/{id}/update`, running the
  wire's own publish path (`load_plans` → `run_dns_phase` → `aggregate` →
  `persist_updates` → `record_hostname_events`, by identity, asserted).
  Rate-limited on the **device's** existing `ddns_rate_limit_event` budget —
  the same control #73's settings pages expose — because a separate budget
  would let a caller draw the metered provider quota twice. Logged as a distinct
  `manual_update` event type so pressing the button cannot make a dead router
  read as live on the board; §3.3.2 step 12 shows `liveness=never_seen` after
  five successful manual publishes. G3's `POST /admin/health-checks/run` is a
  different button, closed separately by #75, and its debounce is a different
  mechanism (atrium's `audit_log`) for a different reason — that one is not
  per-device.

**The one thing this closes that is not a route.** `scripts/import_legacy.py`
refuses two legacy states it can now represent — a zone whose hostnames disagree
about TTL, and a *selective* `hostname_backends` binding. Both refusals still
refuse rather than corrupt, and both are now unnecessary. Not changed by #74 (it
is #50's file and outside that issue's acceptance criteria); recorded so the
next migration-cutover issue finds it.

<details>
<summary>the original blocking finding, for the record (#47, 2026-08-16)</summary>

This was not "the UI was not built yet". **Nothing in the shipped application
ever constructed a `Hostname`.** The only call site in the whole package was
`scripts/seed_compat_fixture.py`, a test-fixture seeder gated on
`ATRIUM_DDNS_COMPAT_STUB=1` and refused when `ENVIRONMENT=prod`. The permission
`atrium_ddns.hostname.manage` was seeded by `0002_ddns_core` and referenced by
no endpoint.

```
== 3. Can this admin create a HOSTNAME? ==
   POST /api/atrium_ddns/hostnames                       -> 405 {'detail': 'Method Not Allowed'}
   POST /api/atrium_ddns/devices/1/hostnames             -> 405 {'detail': 'Method Not Allowed'}
   POST /api/atrium_ddns/domains/1/hostnames             -> 405 {'detail': 'Method Not Allowed'}

== 4. What does the BOARD show after creating a domain and a device? ==
   device 'router47'  hostnames=0  strips=0
```

Corroborated by a second instrument — the running stack's OpenAPI document
listed 15 host operations and **no path containing "hostname"**. The compounding
consequence was invisible when the surfaces were reviewed one at a time: the
resolution strip is the milestone's signature element and §4's whole argument,
and a tenant could not cause one to render.

</details>

#### 3.3.1 The closing half, re-driven from an empty account

Not carried forward from #69. Re-run here end to end over HTTP as a plain
`user`-role tenant created through atrium's own invite flow, with the device
authenticating on `/nic/update` by HTTP Basic exactly as a router does. The
zone's provider is one of the frozen fixture's scripted stub slots
(`ATRIUM_DDNS_COMPAT_STUB=1`), so **the walk publishes nothing to any real
zone**; the catalogue's three real adapters each reach a real nameserver.

```
   roles: ['user']
   atrium_ddns perms: ['atrium_ddns.device.manage', 'atrium_ddns.domain.manage',
                       'atrium_ddns.hostname.manage']

== 1. the account is empty ==
   GET /api/atrium_ddns/domains   -> 200 []
   GET /api/atrium_ddns/devices   -> 200 []
   GET /api/atrium_ddns/hostnames -> 200 []
   GET /api/atrium_ddns/board     -> 200 devices=0 unassigned=0

== 2. the bundle the tenant's browser loads carries the Names surface ==
   GET /host/main.js -> 200, 748893 bytes
   PRESENT  atrium-ddns-names
   PRESENT  /atrium-ddns/names
   PRESENT  atrium_ddns.hostname.manage
   PRESENT  atrium_ddns/hostnames

== 3. create a zone ==
   POST /api/atrium_ddns/domains -> 201 {'id': 5, 'name': 'za6602e.example.invalid', ...}
== 4. bind a provider backend to the zone ==
   POST /api/atrium_ddns/domains/5/backends (stub1) -> 201 {'credentials_set': True, ...}
== 5. create a device ==
   POST /api/atrium_ddns/devices -> 201 {'device': {'id': 4, 'username': 'ddns-…', ...}}
== 6. create the hostname — the registration #69 added ==
   POST /api/atrium_ddns/hostnames -> 201 {'id': 7, 'name': 'home.za6602e.example.invalid', ...}

== 7. the board, BEFORE the device has published anything ==
   home.za6602e.example.invalid   strips=0   <- correct: nothing published yet

== 8. the device calls in, over HTTP Basic — exactly as a router does ==
   GET /nic/update -> 200  body: good 203.0.113.147

== 9. THE STRIP ==
   device a6602e-router  liveness=active
     home.za6602e.example.invalid  strips=1
       family        : A
       published     : {'address': '203.0.113.147', 'updated_at': '2026-08-16T08:25:30.526099Z'}
       answered      : {'address': None, 'status': 'never_checked', 'checked_at': None}
       called from   : {'address': '<redacted>', 'reason': 'declared_myip',
                        'declared_address': '203.0.113.147'}
       upper joint   : not_measured_never
       lower joint   : not_applicable
       joints agreed : 0 of 0 compared (n/a 1, unmeasured 1)
```

**Step 7 is the correction #69 made to its own acceptance criterion, and it
reproduces.** Three steps produce a hostname on the board with **zero** strips,
and that is the right answer: `_strips_for` renders a family only when the name
has been published or answered in it, which is #44's argued-for reading of
`ui-design.md` §3.4 (the alternative gives every v6-only hostname a permanent
blank `A` rail). The strip needs a fourth step, and the fourth step is the
device doing the thing the device exists for.

**One reading differs from #69's and is not a regression.** #69 recorded
`joints agreed : 1 of 1 compared`; this walk records `0 of 0 compared (n/a 1,
unmeasured 1)` with `lower_joint: not_applicable`. The divisor moved because the
*device* did: this walk's device declared `myip=`, so the lower joint has
nothing to compare and the strip says so instead of counting it. That is the
denominator §3.4 of `ui-design.md` says moves — 2 for a device publishing its
own address, 1 for one declaring `myip` — behaving as specified. It is recorded
here because a count that changes between two runs of "the same" demonstration
is exactly the thing that should not be waved through.

The admin pair, demonstrated rather than assumed — the same two endpoints, as a
caller holding `atrium_ddns.admin`, against a zone owned by a *different*
tenant:

```
admin POST into tenant A's zone -> 201 {"id":8,"name":"byadmin.za6602e.example.invalid",…}
   tenant A now sees: ['byadmin.za6602e.example.invalid', 'home.za6602e.example.invalid']
admin DELETE another tenant's name -> 204
   tenant A now sees: ['home.za6602e.example.invalid']
```

#### 3.3.2 The publishing half (#74), driven from two empty accounts

Not carried forward and not asserted from inside the process: driven over HTTP
from **outside** it, against a stack built from the merged tree
(`COMPOSE_PROJECT_NAME=ddns74`, API on `:8074`), by two `user`-role tenants
created through atrium's own invite flow. The zone's three providers are the
frozen fixture's scripted stub slots (`ATRIUM_DDNS_COMPAT_STUB=1`), scripted
`nochg` / `good` / `dnserr` so that the aggregate is a **measurement** rather
than a tautology — with all three the answer is `good`, and dropping the middle
one makes it `dnserr`, which holds only if the selection is read *and* read in
the zone's own order.

Nothing reaches a real nameserver, and nothing is written to the database except
through the API.

*(The bundle line carries two numbers because the first draft printed one and it
was the wrong one: `801114` is the **character** count of the decoded body and
`801320` is the byte count, the 206 difference being this bundle's own curly
quotes and its `≠` glyph. §3.2's figures are bytes. A 0.03% disagreement between
two instruments is exactly the size that gets read as noise when only one of
them is ever printed.)*

```
== 0. the routes exist. Probed with PUT/POST — never GET ==
   PUT  /hostnames/1/no-such-route  -> 405   <- the control: absent
   PUT  /hostnames/999999/backends  -> 422   <- routed (the body was validated)
   POST /hostnames/999999/update    -> 422   <- routed (the body was validated)
   GET /host/main.js -> 801320 bytes (801114 characters)
     PRESENT  publishing-save   publishing-update   inherits_backends

== 4. THE DEFAULT. A name nobody has configured — what does it publish to? ==
   inherits_backends : True
   selected rows     : []                  <- no ddns_hostname_backend rows
   publishes_to      : [7805, 7806, 7807]  <- all three, resolved
   ttl               : None  (None = inherit)
   effective_ttl     : [60]

== 5. publish it now, with no selection: all three backends are contacted ==
   POST /hostnames/7495/update -> 200
   aggregate  : good   published=True
     stub1    -> nochg
     stub2    -> good
     stub3    -> dnserr
   rate limit : 30/minute for this device

== 6. narrow it to two, dropping the one that answers `good` ==
   PUT  /hostnames/7495/backends {"backend_ids":[stub3,stub1],"ttl":300} -> 200
   inherits_backends : False
   publishes_to      : [7805, 7807]  <- zone order, not request order
   ttl               : 300
   effective_ttl     : [300]
   POST /hostnames/7495/update -> 200
   aggregate  : dnserr  <- the aggregate MOVED, so the selection is read
     stub1    -> nochg
     stub3    -> dnserr

== 7. the log ==
   GET /events?hostname_id=7495 -> 200, 5 rows
     manual_update  stub3    dnserr  client_ip=None
     manual_update  stub1    nochg   client_ip=None
     manual_update  stub3    dnserr  client_ip=None
     manual_update  stub2    good    client_ip=None
     manual_update  stub1    nochg   client_ip=None
   event types written: ['manual_update']
   vocabulary offers  : ['auth', 'delete', 'manual_update', 'update']

== 8. clear the selection: back to inheriting, NOT to publishing nowhere ==
   PUT  /hostnames/7495/backends {"backend_ids":null,"ttl":null} -> 200
   inherits_backends : True
   publishes_to      : [7805, 7806, 7807]  <- all three again

== 9. the refusals, each a different sentence ==
   manual update on a name with no device      -> 409 "…is not assigned to a device…"
   manual update with an address that is not   -> 422
   ttl below the legacy form's minimum of 30   -> 422
   ttl above the legacy form's maximum         -> 422

== 10. tenant B cannot see or touch tenant A's name ==
   B: GET   backends   -> 404  (404 = not yours, never 403)
   B: PUT   backends   -> 404
   B: POST  update     -> 404

== 11. THE ADMIN PAIR, cross-tenant, against a zone owned by another tenant ==
   admin GET  another tenant's publishing -> 200 domain=yc06692.example.invalid
   admin PUT  selection + ttl=120         -> 200 publishes_to=[7808] ttl=120
   admin POST manual update               -> 200 status=good
   B sees the admin's change              -> 200 ttl=120
   B's OWN log carries the admin's publish -> 200, 1 rows, attributed to B

== 12. the board, after everything ==
   device router-c06692  liveness=never_seen
     home.zc06692.example.invalid  strips=1
       family      : A
       published   : {'address': '203.0.113.74', 'updated_at': '…'}
       answered    : {'address': None, 'status': 'never_checked', …}
       called from : {'address': None, 'reason': 'no_update_on_record', …}
       lower joint : not_applicable
```

**Step 12 is the result worth reading twice, and it is not decoration.** Five
successful manual publishes have moved `last_ip_v4` and drawn a strip, and the
device still reads `liveness=never_seen` with `called from:
no_update_on_record`. That is the reason `manual_update` is a distinct
`event_type` rather than another `update` row: `worker_jobs.device_statuses`
derives "which of my devices stopped calling in" from `event_type == 'update'`,
so folding the button's own traffic into it would let an operator answer the
board's liveness question by pressing a button. A router offline for a week
would have read as active.

**Step 11's last line is the one an attribution bug leaves looking fine.** The
admin's publish is attributed to the *owner* in `ddns_event` — so it appears in
the owner's log search, which is the surface built to answer "why did my name
change". The admin's identity is recorded separately, in atrium's audit log,
where "an admin did this to somebody's zone" belongs. The owner's own log is
read back rather than assumed.

**What the walk does not show, and where it is shown instead.** That a hostname
row created *before* `0004` ran still publishes to every backend of its zone. No
row on this stack predates the migration, so the walk cannot reach that clause —
it is asserted in `backend/tests/test_router_hostname_backends.py` §0 against a
row inserted naming only the columns that existed at `0003`, together with two
structural readings that make the reconstruction faithful: the live
`information_schema` says `ttl` is nullable with no server default, and `0004`'s
own AST contains no data-writing call (parsed rather than grepped, because the
docstring argues about backfilling at length; vacuity-guarded on `add_column`
and `create_table`).

The independent second reading is the frozen 124-case wire table, whose twelve
fixture hostnames are seeded by a script that has never heard of
`ddns_hostname_backend`: three of its cases aggregate across two or three
backends, so "empty means nowhere" takes them to `911`. Measured both ways —
**124/124** with the change, and **46 of 124 failing** when `resolve_backends` is
mutated to the reading the table's shape suggests. A second mutation — the
selection stored and never read — fails **6 of 42** host tests and **0 of 124**
wire cases, because every fixture hostname has an empty selection. That is the
honest limit of the frozen table as an instrument here, and the reason the
reconstruction exists.

#### ~~G2 — operator configuration has no UI (4 routes) · severity: high~~ — **CLOSED by #73**

**This was the honest remainder** — never scoped, model exists, page does not —
and it is now built. The four routes are in §3.2. The finding is kept below,
struck through rather than deleted, because the *shape* of it is the useful
part: a namespace the API served completely and no screen named.

| legacy route | the setting that replaced it | closed by |
|---|---|---|
| ~~`GET,POST /admin/rate-limits`~~ | `atrium_ddns.rate_limit_per_minute` | the `rate-limits` child page |
| ~~`GET,POST /admin/rate-limits/user/<id>`~~ | re-keyed to `ddns_device.rate_limit_per_minute` | `PATCH /api/atrium_ddns/devices/{id}` + the *Rate limit* control |
| ~~`POST /admin/rate-limits/user/<id>/delete`~~ | " | the same control, emptied |
| ~~`GET,POST /admin/health-checks/config`~~ | `atrium_ddns.health_check_*` | the `health-checks` child page |

<details>
<summary><b>the closing evidence, from a stack this run stood up</b>
(<code>COMPOSE_PROJECT_NAME=ddns73</code>, API on <code>:8173</code>)</summary>

Every line is a live response, **re-taken on the merged tree** after #75
landed — not carried from #73's own branch, which is the carried-forward
staleness this file keeps warning about. The expected verdicts were written
**before** the first run, and the two disagreements are recorded below rather
than tuned away.

**1. The group and its three pages, in the bundle the stack serves.**

```
GET /host/main.js -> 200, 783112 bytes, text/javascript; charset=utf-8
  PRESENT  registerSettingsGroup
  PRESENT  atrium-ddns-settings
  PRESENT  /atrium-ddns/settings/rate-limits
  PRESENT  /atrium-ddns/settings/health-checks
  PRESENT  /atrium-ddns/settings/retention
  PRESENT  app_setting.manage
  PRESENT  /atrium_ddns/config/schema
  PRESENT  /admin/app-config
```

The other end of the same seam — the shell has to *implement* what the host
calls. Read out of the served shell bundle, and not from the atrium checkout,
which is at 0.26.12 while the image is 0.28 and therefore cannot answer this:

```
shell bundle : /assets/index-C362tQAs.js  1,536,603 bytes
'registerSettingsGroup' in the SHELL bundle          : PRESENT
'[atrium-registry] registerSettingsGroup({ key: "'   : PRESENT
    ^ the implementation's own warning string, not just the property name
'atrium_ddns' occurrences in the SHELL bundle        : still 0
    ^ unchanged, and correct: the shell never names the namespace. The host does.
```

**2. Every field of the namespace, on a page, with the bounds the server
enforces.** The field list, types, bounds, defaults and help text are derived
from `DdnsConfig`'s own JSON schema — the model atrium validates the PUT
against — so the population is the model's and cannot be a subset of it.

**The merge is the demonstration of that, and it was not planned as one.** #73
was written against eleven fields. #75, on a branch cut from the same base,
added a twelfth (`health_check_manual_cooldown_seconds`). No page, no form and
no test data changed: the field appeared on the Health checks page with its
bounds, its default and its help text because the page reads the model. What
*did* have to change was one line of grouping — and the guard found it, rather
than a reviewer: an unassigned field lands in the `ungrouped` bucket and
`test_settings_schema` refuses that bucket being non-empty.

```
GET /api/admin/app-config           -> 200   atrium_ddns fields served: 12
                                             (11 at #73; #75 added one)
GET /api/atrium_ddns/config/schema  -> 200
  write_path: /admin/app-config/atrium_ddns   permission: app_setting.manage
  groups: [('rate-limits', 2), ('health-checks', 6), ('retention', 4)]
  every served field is on a page : PASS      no group is 'ungrouped' : PASS
  every field carries help text   : PASS

  rate_limit_per_minute                 integer  [0, 10000]  default=30
  rate_limit_event_retention_hours      integer  [1, 8760]   default=24
  health_check_enabled                  boolean  [-, -]      default=True
  health_check_interval_minutes         integer  [1, 1440]   default=15
  health_check_batch_size               integer  [1, 10000]  default=200
  health_check_timeout_seconds          number   [0.1, 60.0] default=5.0
  health_check_concurrency              integer  [1, 64]     default=8
  health_check_manual_cooldown_seconds  integer  [0, 86400]  default=60   <- #75
  event_retention_days                  integer  [1, 3650]   default=30
  prune_batch_size                      integer  [1, 50000]  default=1000
  prune_max_batches                     integer  [1, 10000]  default=100
  device_idle_window_days               integer  [1, 365]    default=7
```

**3. The bounds bite on the server, not only in the browser** — swept over
every bounded field rather than asserted for the one the issue named by hand:

```
PUT /api/admin/app-config/atrium_ddns, one step below each minimum:
  rate_limit_per_minute                = -1    -> 400
  rate_limit_event_retention_hours     = 0     -> 400
  health_check_interval_minutes        = 0     -> 400
  health_check_batch_size              = 0     -> 400   <- AC 2's own example
  health_check_timeout_seconds         = 0.05  -> 400
  health_check_concurrency             = 0     -> 400
  health_check_manual_cooldown_seconds = -1    -> 400   <- #75's field, swept
  event_retention_days                 = 0     -> 400   without being added
  prune_batch_size                     = 0     -> 400   to this list
  prune_max_batches                    = 0     -> 400
  device_idle_window_days              = 0     -> 400
  ...and the namespace is back to where the walk found it : PASS
```

The sweep is over the served schema, so #75's field was covered the first time
the walk ran on the merged tree without anybody adding a row to it. That is the
same property as the page, aimed at the evidence instead of the UI.

**4. The per-device limit changes without touching the credential.** The stored
hash is read from MySQL directly on both sides — a byte comparison is the only
instrument that can see a credential quietly rewritten, and the response body
cannot:

```
POST /api/atrium_ddns/devices -> 201
  rate_limit_per_minute=None  effective_rate_limit_per_minute=30
stored hash BEFORE : $argon2id$v=19$m=65536… len=97
PATCH /api/atrium_ddns/devices/{id} {"rate_limit_per_minute": 2} -> 200
  rate_limit_per_minute=2  effective_rate_limit_per_minute=2   'secret' in body: False
stored hash AFTER  : $argon2id$v=19$m=65536… len=97
  byte-identical across the PATCH : PASS
```

**5. AC 5 — the rate limiter honouring the changed value, which is the
demonstration.** Reading the config row back is not; these are `/nic/update`
calls made by the device over HTTP Basic with the credential it already had.

*Per-device, at limit 2:*

```
four calls: ['good 203.0.113.73', 'good 203.0.113.73', 'abuse', 'abuse']
  the credential still works after the PATCH (calls 1-2 admitted) : PASS
  the limit written through the API is enforced   (calls 3-4)     : PASS
```

*Installation-wide, through the namespace the settings page writes:*

```
before: rate_limit_per_minute = 30
  two calls BEFORE : ['good 203.0.113.73', 'good 203.0.113.73']
PUT /api/admin/app-config/atrium_ddns  rate_limit_per_minute=0 -> 200
  two calls AFTER  : ['abuse', 'abuse']
PUT it back to 30 -> 200
  one call after   : ['good 203.0.113.73']
```

**Two predictions were wrong, and the service was right both times.** The
first: the walk expected
`['good …', 'nochg …', 'abuse', 'abuse']` at limit 2 and observed `good` twice.
`nochg` is the *provider's* answer aggregated by `router_nic._aggregate`, not a
comparison against the stored row, and this zone's backend is a scripted `stub1`
pinned to `result: "good"` — so `nochg` was never reachable on this fixture. The
expectation was corrected with the reason, and the assertion narrowed to the two
facts that carry the criterion.

The second, on the merged tree: the walk asserted the namespace serves
**eleven** fields and it serves twelve, because #75 added one. The literal was
the defect — the page's field list is derived, so a hard-coded count in the
evidence fails on precisely the change the page handles correctly. Replaced by
a set-equality against the served namespace plus a floor, which is
non-vacuous and does not need editing the next time a field is added.

Both are recorded because a demonstration edited until it passes is the most
damaging artefact a milestone can produce, and the difference between *editing
the expectation* and *editing the assertion's subject* is only visible if the
first version is written down.

**6. The gate, in both directions, over HTTP.** A `user`-role tenant created
through atrium's own invite flow — a real, working account, which is what makes
the three 403s a gate rather than a dead session:

```
POST /api/invites role_codes=['user'] -> 201 ; accept -> 201 ; login -> 204
  roles: ['user']
  atrium_ddns perms: ['atrium_ddns.device.manage', 'atrium_ddns.domain.manage',
                      'atrium_ddns.hostname.manage']
  holds app_setting.manage: False
  GET /api/atrium_ddns/devices                   -> 200   <- not refused everything
  GET /api/atrium_ddns/config/schema             -> 403
  GET /api/admin/app-config                      -> 403
  PUT /api/admin/app-config/atrium_ddns          -> 403
  ...and the super_admin still reads the schema  -> 200
```

**7. The absence probes, with the §2.1 control.** `PATCH`, never `GET`:

```
PATCH /api/atrium_ddns/devices/{id}   -> 200 application/json   <- new, #73
GET   /api/atrium_ddns/config/schema  -> 200 application/json   <- new, #73
PATCH /api/atrium_ddns/domains/{id}   -> 405 application/json   <- G4, still open
GET   /api/atrium_ddns/made-up-…      -> 200 text/html          <- the control
```

**What is NOT in this evidence.** Nobody clicked the sidebar. There is no
browser harness in this repository, so *"a super_admin sees a **DDNS
configuration** group in the Admin sidebar with three children, and clicking one
loads the page"* rests on: the host bundle calling `registerSettingsGroup`
(asserted in vitest against a recorder installed on the registry, because
`@brendanbank/atrium-test-utils` does **not** record settings groups and every
assertion about one would otherwise pass against a bundle that never registered
any); the string evidence above from both served bundles; and atrium's own unit
coverage of `useAdminSectionItems`. Everything below the sidebar — the schema,
the values, the write, the refusals and the limiter — is demonstrated over HTTP
above.

</details>

<details>
<summary><b>the original finding, for the record</b> (#47's re-run, 2026-08-16)</summary>

The settings exist and the running stack serves all eleven of them:

```
GET /api/admin/app-config -> 200
namespaces: ['atrium_ddns', 'auth', 'brand', 'i18n', 'pats', 'system']
atrium_ddns namespace IS served by the API, 11 fields:
  device_idle_window_days                = 7
  event_retention_days                   = 30
  health_check_batch_size                = 200
  health_check_concurrency               = 8
  health_check_enabled                   = True
  health_check_interval_minutes          = 15
  health_check_timeout_seconds           = 5.0
  prune_batch_size                       = 1000
  prune_max_batches                      = 100
  rate_limit_event_retention_hours       = 24
  rate_limit_per_minute                  = 30
```

**Re-taken with a stronger instrument than #47's, and it holds.** #47 read
atrium's *source* for "there is no generic namespace editor". This reading takes
apart the **shell bundle the stack serves**, which is one file with no lazy
chunks — so the sweep below is complete rather than a sample:

```
shell bundle : /assets/index-C362tQAs.js  1,536,603 bytes
lazy chunks referenced by it: none — one bundle, so this sweep is complete

'atrium_ddns' anywhere in the SHELL bundle : 0 occurrences
the namespace-parameterised mutation hook  : PUT /admin/app-config/${e}
every call site of it, exhaustively        : ['e', `auth`, `brand`, `system`, `i18n`]
registerSettingsGroup in the HOST bundle   : ABSENT
```

The hook is generic; **every caller passes a literal**, and the one non-literal
is the definition itself. Nothing derives the namespace from what
`GET /api/admin/app-config` returns, so a namespace the API serves and no screen
names is unreachable from the SPA — which is what `atrium_ddns` is. Config is
reachable only by `curl` against `PUT /api/admin/app-config/atrium_ddns`.

The second reading of the same negative, from the other end: the shell's own
admin-section registry, exhaustively, out of the served bundle —

```
system  auth  users  branding  roles  tokens  translations  emails  outbox
reminders  audit
```

Eleven hand-built sections. Four of them edit a config namespace (`system`,
`auth`, `branding`, `translations` → `i18n`); none of the other seven does, and
none of the eleven loops over the namespaces the admin API returns. The host's
own `registerAdminTab` adds a twelfth — and it is the scaffold's counter widget,
not a settings form, which is the plan §4 clause that does not hold.

Plan §4 says *"Rate limits, health-check config, retention become one nested
group via `registerSettingsGroup`"*. That clause is unbuilt, and §5 opened no
issue for it.

The per-device limit is a narrower miss inside the same group: `DevicesPage`
offers it on the **create** form only, there is no device `PATCH` endpoint at
all, and the stored value is never displayed. Changing a device's limit today
means delete-and-recreate, which rotates the credential.

```
PATCH /api/atrium_ddns/devices/1 -> 405 {'detail': 'Method Not Allowed'}
```

</details>

#### ~~G3~~ — on-demand operator actions (~~3~~ ~~2~~ 0 routes) · **closed by #75**

| legacy route | ~~#47-rerun~~ | this re-measurement (#75) |
|---|---|---|
| ~~`POST /admin/health-checks/run`~~ | ~~`POST -> 405`. No manual trigger; the check is scheduled only.~~ | **`POST -> 200`** — registered, §3.2 |
| ~~`POST /admin/health-checks/clear`~~ | ~~`POST -> 405`.~~ | **`POST -> 200`** — registered, §3.2 |
| ~~`POST /admin/events/clear`~~ | **moved to §3.4 — deliberately dropped**, on the operator's decision | unchanged; see §0 for why it was *not* extended to the two above |

Re-measured with `POST` against the running stack, beside the §2.1 control taken
in the same session (`POST` to a made-up path answers `405`; `GET` to the same
path answers `200 text/html` off the catch-all):

```
POST /api/atrium_ddns/health-checks/run -> 200 application/json
  {"batch_size": 200, "enabled": true, "error": 0, "forced": true,
   "hostnames_checked": 3, "hostnames_considered": 12,
   "hostnames_never_written": 9, "mismatch": 0, "missing": 5, "ok": 0,
   "records_checked": 5, "transitions": 0, "truncated": false}

POST /api/atrium_ddns/health-checks/run -> 429 application/json
  Retry-After: '60'
  "a manual health check was run less than 60s ago; 60s remaining. …"

POST /api/atrium_ddns/health-checks/clear -> 200 application/json
  {"cleared": 3, "in_scope": 12}
```

The population above is the compat fixture's twelve names, cross-tenant because
the probing account holds `atrium_ddns.admin`; nine had never published an
address and are counted rather than dropped. **No resolved address is printed
here and none is on the wire** — `HealthCheckRunOut` carries counts only.

**`run` is the scheduled job, not a second sweep.** It calls
`worker_jobs.run_health_checks` — the same function object the scheduler
registers, asserted by identity in `tests/test_router_health_checks.py` — with
`due_only=False` and the caller's `DdnsScope`. The batch ceiling, the
concurrency semaphore and the timeout are the namespace's own
(`health_check_batch_size`, `health_check_concurrency`,
`health_check_timeout_seconds`), so there is no second set of knobs.

`due_only=False` is the one difference, and dropping it would have made the
button a probe that could not fail in the inverse direction: with the staleness
clause kept, an operator who has just watched a check run presses the button,
nothing is due, the run reports `0 checked` — and that is indistinguishable
from a working button against a healthy estate. The test drives both paths over
one row whose `dns_checked_at` is *now* and demands they disagree.

**The debounce is per actor and it is persisted.** The claim is an
`audit_log` row written *and committed before* the fan-out starts — a debounce
recorded on the way out admits two simultaneous presses — keyed on
`(entity='ddns_health_check', action='run', actor_user_id)`, with the window in
`health_check_manual_cooldown_seconds` (default 60, `0` disables it). A
process-local counter was rejected for the reason `models.RateLimitEvent`
already records: api and worker are separate containers and api may be several
processes. A new table was rejected because the migration chain admits one
author at a time and a debounce is not worth the slot. Per *actor* rather than
installation-wide, so one operator's press cannot block every other tenant's
board — asserted in both directions.

**`clear` is not rate-limited, and the asymmetry is deliberate**: `run` fans out
to other people's nameservers, `clear` is one scoped `UPDATE` against our own
database. It writes `NULL` into all four health-check columns, which reads back
as `NEVER_CHECKED` — not `MISSING`, which would assert a measurement nobody
took. It deletes nothing: no hostname, no published address, no `ddns_event`
row.

A defect this section found in its own first implementation, kept because the
class recurs: `cleared` was the driver's `rowcount`, which on this driver counts
rows **matched** rather than rows **changed**, so a second clear of the same row
reported `cleared: 1` about a row that carried nothing. Pressing the button once
agrees with either implementation; the test presses it twice.

#### ~~G4~~ — domain rename (1 route) · **closed by #75**

`GET,POST /admin/domains/<id>` renamed a domain. ~~Re-measured on this stack:~~

```
PATCH /api/atrium_ddns/domains/1 -> 405 {'detail': 'Method Not Allowed'}   <- #47-rerun
PUT   /api/atrium_ddns/domains/1 -> 405 {'detail': 'Method Not Allowed'}   <- #47-rerun
```

Re-measured at #75, end to end — claim a zone, register a name under it, then
try both a rename that would orphan the name and one that would not:

```
POST   /api/atrium_ddns/domains   {"name": "g4-….example.invalid"}      -> 201
POST   /api/atrium_ddns/hostnames {"name": "box.g4-….example.invalid"}  -> 201

PATCH  /api/atrium_ddns/domains/896 {"name": "other-….example.invalid"} -> 409
  renaming 'g4-….example.invalid' to 'other-….example.invalid' would leave
  1 of 1 hostname outside the zone: 'box.g4-….example.invalid'. /nic/update
  answers nohost for a hostname outside its domain's zone, so those rows
  would exist and never be updatable again. …

PATCH  /api/atrium_ddns/domains/896 {"name": "BOX.g4-….example.invalid."} -> 200
  {"id": 896, "name": "box.g4-….example.invalid", "hostname_count": 1}
  hostnames under the renamed zone, read back: ['box.g4-….example.invalid']

PUT    /api/atrium_ddns/domains/896 -> 405
DELETE /api/atrium_ddns/domains/896 -> 204
```

**The disposition, stated: it rejects, it does not rewrite.** A rename is not a
string swap — every hostname under the zone must still satisfy `zone_contains`
afterwards or the rename has minted rows that are creatable once and updatable
never. The two options were rewrite-transactionally and reject, and the model
had already decided against the first one route along: `HostnameAssignIn` says
a hostname's name is not editable, because *"a hostname is the DNS name — it is
what /nic/update looks the row up by and what a provider has already published
a record under. Renaming the row would leave the old record in the zone with
nothing pointing at it and no way to reach it again."* A rename that rewrote
names would do that to every name at once, silently, as a side effect of
correcting a spelling. So the rename is refused, with the count and a sample of
the offending names, and the tenant keeps the choice the model already gives
them.

Both directions are asserted, and the second one is not decoration: without it,
an endpoint hardcoded to refuse everything would pass the refusal test. The
`200` above is a *narrowing* rename — the new zone still contains the existing
name — and the hostname row is read back afterwards to prove nothing was
rewritten. Containment is decided by `zone_contains`, the **same function
object** `/nic/update` and `POST /hostnames` reach, quirks included; there is no
second copy, in Python or in TypeScript.

`PUT` stays `405` and that is the decision rather than an omission — recorded as
a test, because "we chose `PATCH`" and "we forgot `PUT`" look identical from
outside. Every partial mutation on this surface is a `PATCH`
(`/backends/{id}`, `/hostnames/{id}`), and a second spelling of one operation is
how a divergent validation path starts.

#### ~~G5~~ — help (1 route) · **closed by #75 — with a question left open**

`GET /admin/help` rendered `help.html`. ~~**Swept to a negative result across
both deployed bundles** — the shell and the host bundle, seven spellings
each:~~

```
                shell   host        <- #47-rerun
/help             0      0
helpHref          0      0
HelpPage          0      0
IconHelp          0      0
'help' "help" `help`   0 0 0   /  0 0 0
```

Re-measured at #75, in the bundle the running stack serves:

```
GET /host/main.js -> 200, 763,680 bytes
  'atrium-ddns-help'      occurrences in the served bundle: 2
  'atrium-ddns-help-nav'  occurrences in the served bundle: 1
  '/atrium-ddns/help'     occurrences in the served bundle: 1
  'docs/ops/ui-design.md' occurrences in the served bundle: 1
```

`registerRoute atrium-ddns-help` at `/atrium-ddns/help`, plus a nav item
(§3.2's seventh). The page lists the surfaces this bundle registers and links
the operator documentation in `docs/`.

Two decisions on it. **The documentation is linked, not embedded**: `docs/` is
not copied into the image, so a page that read a Markdown file at run time would
work in a checkout and 404 in the container — the template's own *"a file read
at runtime must be copied into the image"* trap. **The surface list is derived,
not typed**: each row imports the path constant its registration uses, and
`HelpPage.test.tsx` sweeps the registry to a negative result — *every* route
this bundle registers has an entry, with two named exemptions (the scaffold's
demo page, and the help page itself).

**The question the issue asked, left open for the operator.** G5's genuine
question was whether help should be registered at all or struck as a
third-disposition route the way `/admin/events/clear` was. The entry is
registered, which is the issue's own default and the cheap thing. The argument
that could be made for striking it, recorded rather than acted on: **everything
in `docs/` is engineering documentation** — design notes, a migration plan, an
operations contract, this file — written for the people building the service.
There is no end-user handbook to point at, so what shipped is a page of
in-product orientation plus links to developer material. If the operator's
view is that a help entry is only worth having once a real operator handbook
exists, striking it and reopening it with that handbook is a defensible call and
it is theirs to make. Nothing below depends on which way it goes: the route is
registered either way until it is not.

### 3.4 Deliberately dropped — a third disposition (1 route)

**Say plainly what this is: a third column, not one of the criterion's two.**

`POST /admin/events/clear` cleared the event log on demand. Plan §3.4
deliberately replaced it with a bounded, scheduled retention prune, and that is
a better design — the old code pruned inside `log_event`, an unbounded delete on
the DNS-update write path.

But the criterion's two columns are *registered* and *deleted because **atrium**
covers it*, and **atrium covers none of this: the replacement is a host
feature.** So it does not fit either column literally. #47 counted it as a gap
for exactly that reason and named it as something a milestone owner might strike;
the owner has now decided, and the honest way to record that decision is a third
disposition with the replacement named — not a quiet promotion into "deleted",
which would assert that atrium covers something atrium has never heard of.

**The replacement, named and measured on the running stack:**

```
worker | atrium_ddns.init_worker.jobs_registered
         jobs=['atrium_ddns-health-check', 'atrium_ddns-retention-prune']
```

`run_retention_prune` is registered on the deployed worker's scheduler at a
3600 s interval, keyed on the primary key, `prune_batch_size = 1000` per batch
and stopping after `prune_max_batches = 100`, against
`event_retention_days = 30` for `ddns_event` and
`rate_limit_event_retention_hours = 24` for the rate-limit table — two
retentions on purpose, so "how long do we keep logs" does not silently also mean
"how long do we keep rate-limit counters".

**Two instruments, and the honest gap between them.** Registration is measured
above. Whether the registry's jobs *execute* is a different question, and the
sibling job answers it: `atrium_ddns-health-check` ticks every 60 s and has
fired on this same worker —

```
worker, last 10 minutes:
   8 atrium_ddns.health_check.nothing_due
   1 atrium_ddns.init_worker.jobs_registered
   0 retention-prune lines
```

Eight ticks, not one — a single reading cannot distinguish a live scheduler from
one that ran a body once at boot, and eight across eight minutes at a 60 s
interval is the shape a running interval trigger makes. So the `guarded()`
bodies do execute on schedule in this deployment; they were not merely added.

**A prune tick itself was not observed here, and the zero above is a
measurement rather than a silence**: the interval is 3600 s against a worker
uptime of 8 minutes, so zero is the *expected* count and its denominator is
printed beside it. Recorded as a pending verification rather than asserted, per
the template's own rule about runs that never live long enough to see an hourly
job fire.

---

## 4. Tally

**Re-derived route by route on the merged tree, not computed.** #75 and #73
were written in parallel off `4ff2da4`; each measured the gap column at
**10 → 6**, each correctly against the base it could see, and neither could
account for the other. Subtracting the two diffs would have given the right
answer here for the wrong reason, and the next time it would not. So the 39
routes were walked again against a stack built from the merged tree
(`COMPOSE_PROJECT_NAME=ddns73`, API on `:8173`), one probe per route, with the
**predicted disposition stated per row before the probe ran**. The script exits
non-zero on any disagreement.

**Two rows disagreed, and the stack was right both times.** `GET /nic/update`
and `GET /nic/delete` were predicted to answer `401` without credentials. They
answer **`200 text/plain` with a body of `badauth`** — the wire contract (plan
§1), which carries its errors in the body and not in the status, and which is
the entire reason the frozen table exists. It is also §2.1 in miniature: `200`
alone does not distinguish a real route from the SPA catch-all, and the
**content type** does — `text/plain` from the router, `text/html` from the
catch-all. The probe was corrected to read the body and the type; the two rows
are registered, as claimed, for a reason the first version of the probe could
not have seen.

```
deleted       13   33.3%
registered    23   59.0%
dropped        1    2.6%
gap            2    5.1%
total         39

gap routes (2):
   GET,POST /admin/hostnames/<id>/backends
   GET,POST /admin/users/<uid>/hostnames/<hn>/backends
of which pages: 2 of 22 = 9.1%
```

**#74 re-walked all 39 again rather than subtracting its own two.** The
arithmetic was obvious — the two remaining gap routes were the two that issue
closed — and that is precisely why it was not taken. Same method: a stack built
from the merged tree (`COMPOSE_PROJECT_NAME=ddns74`, API on `:8074`), one probe
per route, the disposition **stated per row before its probe ran**, exit
non-zero on any disagreement. Instrument A was re-run against `5d1c941` first
and still answers 39 routes and 22 pages.

**Three rows disagreed on the first run, and all three were the probe's fault —
which is the outcome that makes the walk worth running.** Two are recorded in §0
(`/nic/checkip` answers `200 text/html` *from the router*, so a content-type
verdict misreads it; the settings routes' registration keys are composed at run
time and are not in the bundle as literals). They are named here because they
are also the evidence that **this instrument can still return `gap`** — a walk
whose final line is "0 gaps" and which never once printed one would be the
probe-that-could-not-fail, aimed at the exit criterion. It printed three, and
each was investigated rather than tuned away.

```
control: GET  /api/atrium_ddns/made-up-fdca7f -> 200 text/html
control: POST /api/atrium_ddns/made-up-fdca7f -> 405

deleted       13   33.3%
registered    25   64.1%
dropped        1    2.6%
gap            0    0.0%
total         39

gap routes (0):
   (none)
of which pages: 0 of 22 = 0.0%

every row's probe agreed with its stated prediction.
```

| column | routes | share |
|---|---|---|
| deleted — atrium covers it | 13 | 33.3% |
| registered | ~~15~~ ~~19~~ ~~23~~ 25 | ~~38.5%~~ ~~59.0%~~ 64.1% |
| deliberately dropped (§3.4) | 1 | 2.6% |
| **neither — the finding** | ~~**10**~~ ~~**6**~~ ~~**2**~~ **0** | ~~**25.6%**~~ ~~**5.1%**~~ **0.0%** |
| | **39** | **100%** |

Restricted to the 22 *pages* — the criterion's own word — the gaps are
~~`/admin/hostnames/<id>/backends`, `/admin/users/<uid>/hostnames/<hn>/backends`,~~
~~`/admin/rate-limits`, `/admin/rate-limits/user/<id>`,
`/admin/health-checks/config`, `/admin/domains/<id>` and `/admin/help`~~ —
**none**: ~~**7 of 22, 31.8%**~~ ~~**2 of 22, 9.1%**~~ **0 of 22, 0.0%**. The
last two were pages, so the page figure and the route figure reached zero
together. ~~**The page figure did not move from
#69's**, because the one route that left the gap column since —
`POST /admin/events/clear` — is a `POST` and renders no template.~~ **It moved
twice, in parallel.** #75 closed `/admin/domains/<id>` and `/admin/help`, both
pages; #73 closed `/admin/rate-limits`, `/admin/rate-limits/user/<id>` and
`/admin/health-checks/config`, also pages, and one non-page
(`/admin/rate-limits/user/<id>/delete`, a `POST` that renders no template). The
narrower denominator does not flatter the result, which is why both are given.

**A stricter reading gives ~~4~~ 3, not 0**, and it changes the headline — it is
the difference between *the gap column is empty* and *the gap column is empty
except for the admin variants* — so it is stated rather than assumed. Two of the
four routes #69 closed are the *admin acting on another tenant's names* pair;
#74 adds a third of the same shape
(`/admin/users/<uid>/hostnames/<hn>/backends`). All three are served by the
**same** endpoints under a widened scope (`atrium_ddns.admin`) rather than by a
distinct per-user page. Demonstrated above — and with one real difference from
the legacy surface, measured on this stack:

```
admin GET /api/atrium_ddns/hostnames -> 200, 4 rows
row keys: ['created_at', 'device_id', 'device_name', 'domain_id',
           'domain_name', 'id', 'last_ip_v4', 'last_ip_v6',
           'last_updated_at', 'name']
'owner'/'user' key present: False
```

The admin sees a single merged list with no tenant column and no tenant filter,
so *"whose name is this"* is answerable only from the zone. A reader who
requires the admin variant to be its own surface should count those ~~two~~
three as still open, giving ~~**12 of 39 (30.8%)** and **8 of 22 pages
(36.4%)**~~ ~~**4 of 39 (10.3%)** and **3 of 22 pages (13.6%)**~~ **3 of 39
(7.7%)** and **3 of 22 pages (13.6%)** after #74. The looser count is the one in
the table because the criterion's own word is *capability* ("either exists as a
registration") and the capability is reachable; the stricter count is here so
the choice is visible.

**#74's admin half has one property #69's does not**, and on the strict reading
it is the argument against counting it open: the cross-tenant publish is
attributed to the **zone's owner** in `ddns_event`, not to the admin, so it
lands in the owner's log search rather than the admin's. Measured at §3.3.2 step
11 by reading the owner's own log back. The legacy per-user page had no log at
all.

---

## 5. What this does not measure

Stated so a later reader does not take the table for more than it is.

- It is a **capability** map, not a visual one. "Registered" means the surface
  exists and its endpoint answers; it does not mean the replacement is as
  complete as the page it replaced, only that a tenant can get at the function.
- The three scaffold registrations (§3.2) are counted as registrations. A
  stricter reading that required a registration to be *product* UI would move
  `GET /admin/` into the gap column and make it 11.
- It says nothing about the `/nic/*` wire protocol beyond "the route exists" —
  that is the frozen 131-case compat table's job, not this file's. ~~The gate
  run that accompanies this reading executed **0 of 131** wire cases by design
  (`compat: NO --target GIVEN`).~~ **Both #75 and #73 ran it explicitly against
  their own stacks, and it was run again on the merged tree** — #73 because it
  changes `/nic/update`'s rate-limit inputs, and a reading of that surface with
  the table unrun would be worth nothing. `make test-compat TARGET=host
  BASE_URL=http://api:8000` → `executable 124`, **124 passed**, 3 skipped on
  unmet preconditions (the three `rate_limited:` cases, which are not
  arrangeable over the wire), 4 excluded by `targets:`. It is still not in the
  gate, and running it stays an explicit act. Note the three skipped are the
  cases closest to #73's change, so the table does **not** cover it; §3.3 G2
  does, twice.
- **The sidebar entry is not demonstrated in a browser** (§3.3, G2). There is no
  browser harness in this repository — no Playwright, no `*.spec.ts`. The three
  settings *pages* and everything they call are demonstrated over HTTP; the
  group's appearance in atrium's Admin sidebar rests on both served bundles and
  on vitest.
- ~~`/admin/health-checks/run|clear` and the domain rename are still counted as
  gaps on the criterion's literal wording. Striking them is the milestone
  owner's call to record, not a reader's to assume; the counts stand above
  either way.~~ **All three are registered as of #75 and the question of
  striking them does not arise.** The one disposition question still open is
  G5's, and it is stated there rather than decided.
- **A prune tick was not observed** (§3.4). Registration was.
- ~~**Every group was re-measured for this reading.**~~ ~~**#75 re-measured G3,
  G4 and G5 only** — the three groups it changed — with `POST`-based probes and
  §2.1's control taken in the same session. **G1 and G2 are carried forward from
  the #47 re-run and were not re-taken here.** That is the same
  carried-forward exposure that re-run was written to close, said out loud
  rather than left to be discovered: the next reader should treat the G1 and G2
  cells as older than the rest of this file, and the whole file as older than
  the estate.~~
- **#74 re-walked all 39 routes on the merged tree** (§4), so no cell in the
  tally is carried forward any more — every disposition in it was probed in one
  session against one stack. What *is* carried forward is the prose: the
  narrative blocks for G2–G5 are #73's and #75's own words, re-checked only to
  the extent that §4's walk touches the routes they describe. G1's block and
  §3.3.2 are #74's, taken live.
- **Nobody has loaded any of this in a browser**, and #73's caveat still stands
  unchanged for #74's modal: this repository has no browser harness. The
  Publishing surface is evidenced from the served bundle, from vitest, and from
  the endpoints it calls — not from a click.
