# Legacy route parity — the V1M3 exit criterion, demonstrated

> **every legacy page either exists as a registration or is deleted because
> atrium covers it — demonstrated against the deployed stack**

**Verdict: exit 2. ~~15~~ ~~11~~ ~~10~~ 6 of 39 legacy routes are in neither
column**, in ~~five~~ two capability groups, counted apart and never averaged.

This file is the route-by-route walk. Every verdict in it is a live response
from a stack the run that wrote it stood up. Reproduction commands are inline so
the table can be re-run rather than believed.

---

## 0. Four readings, and what moved between them

A verdict is amended **visibly** here, never replaced. Changed cells are struck
through and the reasoning is kept.

| | #47 (2026-08-16) | #69 (2026-08-16) | #47-rerun (2026-08-16) | **#75 (2026-08-16)** |
|---|---|---|---|---|
| deleted — atrium covers it | 13 (33.3%) | 13 (33.3%) | 13 (33.3%) | 13 (33.3%) |
| registered | 11 (28.2%) | 15 (38.5%) | 15 (38.5%) | **19 (48.7%)** |
| **deliberately dropped** — *a third disposition, see §3.4* | — | — | 1 (2.6%) | 1 (2.6%) |
| **neither — the finding** | **15 (38.5%)** | **11 (28.2%)** | **10 (25.6%)** | **6 (15.4%)** |
| gaps restricted to the 22 *pages* | 9 (40.9%) | 7 (31.8%) | 7 (31.8%) | **5 (22.7%)** |

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

### What this re-run did, and why it was not a formality

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

### 3.2 Registered — name the registration (~~11~~ ~~15~~ 19 routes)

All ~~15~~ 19 registrations were confirmed **in the bundle the running stack
serves** — ~~`GET /host/main.js -> 200, 748,893 bytes`~~
`GET /host/main.js -> 200, 763,680 bytes` at #75 — not in `frontend/src/`.

| legacy route | registration | backing endpoint on the running stack |
|---|---|---|
| `GET /admin/` (dashboard) | `registerRoute atrium-ddns-board` `/atrium-ddns/board`, `registerRoute atrium-ddns-logs` `/atrium-ddns/logs`, `registerHomeWidget atrium-ddns-widget` | `GET /api/atrium_ddns/board`, `GET /api/atrium_ddns/events` |
| `GET,POST /admin/domains` | `registerRoute atrium-ddns-domains` `/atrium-ddns/domains` + `registerNavItem atrium-ddns-domains-nav` ("Zones and providers") | `GET`/`POST /api/atrium_ddns/domains` |
| `POST /admin/domains/<id>/delete` | same page | `DELETE /api/atrium_ddns/domains/{domain_id}` |
| `GET,POST /admin/domains/<id>/backends/new` | same page | `POST /api/atrium_ddns/domains/{domain_id}/backends` |
| `GET,POST /admin/domains/<id>/backends/<db>/config` | same page | `PATCH /api/atrium_ddns/backends/{backend_id}` |
| `POST /admin/domains/<id>/backends/<db>/delete` | same page | `DELETE /api/atrium_ddns/backends/{backend_id}` |
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
| **`GET,POST /admin/domains/<id>`** (#75) | *Rename* on `registerRoute atrium-ddns-domains` | `PATCH /api/atrium_ddns/domains/{domain_id}` — §3.3 G4 |
| **`GET /admin/help`** (#75) | `registerRoute atrium-ddns-help` `/atrium-ddns/help` + `registerNavItem atrium-ddns-help-nav` ("Help") | none — the page calls no endpoint, see §3.3 G5 |

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

**Three registrations are still the scaffold.** `atrium-ddns-widget` (a counter
with a *Bump counter* button), `atrium-ddns-page` (`/atrium-ddns`, *"Replace
this page with your real domain UI"*) and the admin tab — whose key, read from
the served bundle, is **`atrium-ddns`**, ~~`atrium-ddns-admin-tab`~~ as the #47
reading recorded it — are the `create-atrium-host` template's demo, unchanged.
They are counted as registrations because they *are* registrations and the
criterion asks for a registration; a reader should know that the dashboard row
above rests on the board and the log page, not on the home widget.

### 3.3 In neither column — the finding (~~15~~ ~~11~~ ~~10~~ 6 routes, ~~5~~ 2 groups)

Counted apart, never averaged.

#### G1 — the hostname lifecycle (~~6~~ 2 routes) · severity: ~~blocking~~ medium

| legacy route | what it did | this re-run |
|---|---|---|
| ~~`GET,POST /admin/hostnames`~~ | list my hostnames **and create one** | **closed by #69** — re-demonstrated §3.3.1 |
| ~~`POST /admin/hostnames/<id>/delete`~~ | remove a hostname | **closed by #69** — re-demonstrated |
| `GET,POST /admin/hostnames/<id>/backends` | choose which provider backends a hostname publishes to; edit its TTL; trigger a manual DNS update | **still open** |
| ~~`GET,POST /admin/users/<uid>/hostnames`~~ | the same, for another tenant, as an admin | **closed by #69** (scope-widened; see §4's stricter reading) |
| ~~`POST /admin/users/<uid>/hostnames/<hn>/delete`~~ | " | **closed by #69** — re-demonstrated |
| `GET,POST /admin/users/<uid>/hostnames/<hn>/backends` | " | **still open** |

Measured again on this stack, as the super_admin above:

```
POST /api/atrium_ddns/hostnames/1/backends -> 405 {'detail': 'Method Not Allowed'}
POST /api/atrium_ddns/hostnames/1/update   -> 405 {'detail': 'Method Not Allowed'}
GET  /api/atrium_ddns/hostnames/1/backends -> 200 text/html  (the §2.1 catch-all, not an endpoint)
/api/atrium_ddns/hostnames/{hostname_id}/backends : NOT in the served OpenAPI schema
```

**What is left, and why it is not a UI omission either.** The two surviving
routes are the per-hostname *backend* screen, and the rewrite has no data model
for the three things it did. The single hostname mutator the stack serves is
`PATCH /api/atrium_ddns/hostnames/{id}`, and its request body — read from the
running stack's own schema, not from the source — is device assignment and
nothing else:

```
HostnameAssignIn properties: ['device_id']
HostnameCreateIn properties: ['device_id', 'domain_id', 'name']
```

- **Which backends a hostname publishes to** is not a choice under this schema.
  A hostname publishes to every backend bound to its domain — `router_nic`
  iterates `Domain.backends` and aggregates — so there is no column to edit and
  no join table to populate. Closing this is a schema change, not a page.
- **Per-hostname TTL** has no column either. TTL lives in
  `ddns_domain_backend.config`, i.e. per binding, not per name.
- **A manual DNS update trigger** has no endpoint; the health check is scheduled
  only, which is the same gap G3 records for `POST /admin/health-checks/run`.

Counted as open on the criterion's literal wording, at medium rather than
blocking severity: the product is usable end to end without them, which is the
change from #47's reading. **Reported, not built** — this re-run's brief is
explicit that a schema change is not its scope.

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

#### G2 — operator configuration has no UI (4 routes) · severity: high

**This is the honest remainder.** Never scoped, model exists, page does not.
Reported, not built.

| legacy route | the setting that replaced it |
|---|---|
| `GET,POST /admin/rate-limits` | `atrium_ddns.rate_limit_per_minute` |
| `GET,POST /admin/rate-limits/user/<id>` | re-keyed to `ddns_device.rate_limit_per_minute` |
| `POST /admin/rate-limits/user/<id>/delete` | " |
| `GET,POST /admin/health-checks/config` | `atrium_ddns.health_check_*` |

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

| column | routes | share |
|---|---|---|
| deleted — atrium covers it | 13 | 33.3% |
| registered | ~~15~~ 19 | ~~38.5%~~ 48.7% |
| deliberately dropped (§3.4) | 1 | 2.6% |
| **neither — the finding** | ~~**10**~~ **6** | ~~**25.6%**~~ **15.4%** |
| | **39** | **100%** |

Restricted to the 22 *pages* — the criterion's own word — the gaps are
`/admin/hostnames/<id>/backends`, `/admin/users/<uid>/hostnames/<hn>/backends`,
`/admin/rate-limits`, `/admin/rate-limits/user/<id>` and
`/admin/health-checks/config` — ~~**7 of 22, 31.8%**~~ **5 of 22, 22.7%**.
~~**The page figure did not move from #69's**, because the one route that left
the gap column since — `POST /admin/events/clear` — is a `POST` and renders no
template.~~ **It moved at #75**: `/admin/domains/<id>` and `/admin/help` were
both pages and both are now registered. The one remaining non-page gap is
`/admin/rate-limits/user/<id>/delete`; the two `health-checks` actions left with
#75. The narrower denominator does not flatter the result, which is why both are
given — and note that it flatters it *less* here than the route figure does
(22.7% against 15.4%), because what #75 closed was three pages' worth of
capability and the remaining gap is disproportionately pages.

**A stricter reading gives ~~12~~ 8, not ~~10~~ 6**, and it changes the
headline, so it is stated rather than assumed. Two of the four routes #69 closed are the *admin
acting on another tenant's names* pair, and they are served by the **same**
endpoints under a widened scope (`atrium_ddns.admin`) rather than by a distinct
per-user page. Demonstrated above — and with one real difference from the legacy
surface, measured on this stack:

```
admin GET /api/atrium_ddns/hostnames -> 200, 4 rows
row keys: ['created_at', 'device_id', 'device_name', 'domain_id',
           'domain_name', 'id', 'last_ip_v4', 'last_ip_v6',
           'last_updated_at', 'name']
'owner'/'user' key present: False
```

The admin sees a single merged list with no tenant column and no tenant filter,
so *"whose name is this"* is answerable only from the zone. A reader who
requires the admin variant to be its own surface should count those two as still
open, giving ~~**12 of 39 (30.8%)**~~ **8 of 39 (20.5%)** and
~~**8 of 22 pages (36.4%)**~~ **6 of 22 pages (27.3%)**. The looser count
is the one in the table because the criterion's own word is *capability*
("either exists as a registration") and the capability is reachable; the
stricter count is here so the choice is visible.

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
  that is the frozen 131-case compat table's job, not this file's. The
  ~~gate run that accompanies this reading executed **0 of 131** wire cases by
  design (`compat: NO --target GIVEN`)~~ **#75 ran it explicitly against its own
  stack — `make test-compat TARGET=host BASE_URL=http://api:8000` — and the
  accounting block reads `executable 124 … 124 passed, 3 skipped` out of 131 in
  the table (4 excluded by `targets:`, 3 with unmet preconditions). It is still
  not in the gate, and running it stays an explicit act.**
- ~~`/admin/health-checks/run|clear` and the domain rename are still counted as
  gaps on the criterion's literal wording. Striking them is the milestone
  owner's call to record, not a reader's to assume; the counts stand above
  either way.~~ **All three are registered as of #75 and the question of
  striking them does not arise.** The one disposition question still open is
  G5's, and it is stated there rather than decided.
- **A prune tick was not observed** (§3.4). Registration was.
- ~~**Every group was re-measured for this reading.**~~ **#75 re-measured G3, G4
  and G5 only** — the three groups it changed — with `POST`-based probes and
  §2.1's control taken in the same session. **G1 and G2 are carried forward from
  the #47 re-run and were not re-taken here.** That is the same
  carried-forward exposure that re-run was written to close, said out loud
  rather than left to be discovered: the next reader should treat the G1 and G2
  cells as older than the rest of this file, and the whole file as older than
  the estate.
