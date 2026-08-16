# Legacy route parity — the V1M3 exit criterion, demonstrated

> **every legacy page either exists as a registration or is deleted because
> atrium covers it — demonstrated against the deployed stack**

**Verdict: exit 2. ~~15~~ 11 of 39 legacy routes are in neither column**, in
five capability groups.

This file is the route-by-route walk. Everything in it was measured against a
running stack on 2026-08-16, not read out of a prior PR body. Reproduction
commands are inline so the table can be re-run rather than believed.

## Re-run 2026-08-16 (#69) — G1 closed from 6 routes to 2

The original reading is left standing above and below with the changed cells
struck through, per the rule that a verdict is amended visibly rather than
replaced. **Only G1's rows were re-measured**; every other verdict in this file
is the 2026-08-16 (#47) reading, untouched.

The blocking finding — *"`Hostname` has no writer anywhere in the shipped
application"* — is **resolved**. `POST /api/atrium_ddns/hostnames` and three
sibling routes exist, gated on `atrium_ddns.hostname.manage`, and a tenant
holding only the `user` role reaches a rendered resolution strip from an empty
account. Measured against a stack this run stood up itself
(`COMPOSE_PROJECT_NAME=ddns69`, API on `:8069`), migrated on both chains, host
bundle promoted with `make seed-bundle`.

| | before (#47) | after (#69) |
|---|---|---|
| deleted — atrium covers it | 13 (33.3%) | 13 (33.3%) |
| registered | 11 (28.2%) | **15 (38.5%)** |
| **neither — the finding** | **15 (38.5%)** | **11 (28.2%)** |
| gaps restricted to the 22 *pages* | 9 (40.9%) | **7 (31.8%)** |

Both denominators were re-derived at re-run time and are unchanged: the legacy
tree is still pinned at `5d1c941`, so 39 routes and 22 pages still stand. The
four routes that moved are named in §3.2 and struck through in §3.3.

**A stricter reading gives 13, not 11**, and it is worth stating because the
choice changes the headline. Two of the four closed routes are the *admin
acting on another tenant's names* pair, and they are served by the **same**
endpoints under a widened scope (`atrium_ddns.admin`) rather than by a distinct
per-user page — demonstrated below, but with one real difference from the legacy
surface: the admin sees a single merged list with no tenant column and no tenant
filter, so "whose name is this" is answerable only from the zone. A reader who
requires the admin variant to be its own surface should count those two as still
open, giving **13 of 39 (33.3%)** and **8 of 22 pages (36.4%)**. The looser
count is the one in the table because the criterion's own word is *capability*
("either exists as a registration"), and the capability is reachable; the
stricter count is here so that choice is visible rather than assumed.

---

## 1. The population, and how it was counted

The criterion says *page*, and "page" and "route" are different populations in
a Flask app: `POST /admin/domains/<id>/delete` is a route and not a page. Both
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

Neither number was inherited. Both were run on
`/Users/brendan/src/dyndns-route53` at its current tip.

**Instrument A — static, definition side.** Walk the Python AST of
`web_routes.py` and `dyndns.py` and collect every `@blueprint.route(...)`
decorator. Never executes the module, so it cannot see a route registered at
run time.

**Instrument B — runtime, authoritative.** Build the real Flask app, register
both blueprints, and enumerate `app.url_map`. Sees everything Flask will
actually serve, including anything `add_url_rule` added and any decorator
stacking A might mis-attribute.

```
INSTRUMENT B - Flask url_map (runtime, authoritative)
  web blueprint rules       : 36
  nic_update blueprint rules: 3
  other (flask builtins)    : 1 -> ['static']
  TOTAL app-authored rules  : 39
```

**They agree exactly: 36 + 3 = 39.** State the slack honestly — both readings
derive from importing the same two source files, so they are not independent
about *which files* were examined; the sweep for other `Blueprint(` /
`add_url_rule` call sites across the repo is what closes that, and it returns
only the two blueprints already counted. What the pair genuinely establishes is
that no route is registered by a mechanism the static walk cannot see: had
`add_url_rule` been used anywhere, B would exceed A. It does not.

**Provenance of the tree that was counted.** All three readings were taken on
`brendanbank/dyndns-route53` at **`5d1c941`** — the same commit
`tests/compat/legacy_behaviour/legacy_inventory.yaml` already pins as this
repo's frozen legacy reference — with a clean working tree for `web_routes.py`,
`dyndns.py` and `templates/`, and `git diff --stat 5d1c941 HEAD` empty across
all three. So this table and the compat suite are describing the same legacy
behaviour.

Do not confuse the two counts: `legacy_inventory.yaml`'s **147** is the legacy
*pytest suite*, hand-classified into ported/dropped. This file's **39** is
*routes*. They are different populations measured for different purposes, and
neither is a check on the other.

**Instrument C — consumer side.** 32 distinct `web.*` endpoints are referenced
by `url_for(...)` in `templates/`. The four defined-but-never-linked endpoints
are `index`, `health`, `totp_verify` and `totp_setup` — reached by redirect or
by a monitoring probe, never by a link, which is the expected shape and not a
discrepancy. 32 + 4 = 36.

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
python3 -c "
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

Against a stack this run stood up itself (`COMPOSE_PROJECT_NAME=ddns47`, API on
`:8047`), migrated on both chains, admin seeded, host bundle promoted with
`make seed-bundle`. Not against source.

| column | instrument | what it reads |
|---|---|---|
| **registered** | the host bundle *the running stack serves* — `GET /host/main.js` | the registration key and route path are present in the shipped artefact |
| **registered** | `GET /openapi.json` on the running stack | the endpoint the page calls exists |
| **deleted** | authenticated requests to atrium's own API on the running stack | the replacement surface answers |
| **gap** | authenticated request to the endpoint that would have to exist | the stack's own refusal |

The gap column is the one that could most easily lie, so it is the one driven
hardest: every gap below was probed as a **super_admin holding all seven
`atrium_ddns.*` permissions**, including `atrium_ddns.hostname.manage`. A 401
would have meant "the session died", not "the surface is absent"; the probe
asserts against 401 for exactly that reason. What comes back is 405.

```
permissions held: 20, including atrium_ddns.*: ['atrium_ddns.admin',
  'atrium_ddns.device.manage', 'atrium_ddns.domain.manage',
  'atrium_ddns.events.read.all', 'atrium_ddns.hostname.manage',
  'atrium_ddns.read', 'atrium_ddns.write']
```

---

## 3. The table

### 3.1 Deleted — atrium covers it (13 routes)

Each row's evidence is a live response from the running stack, not a reading of
atrium's source.

| legacy route | what in atrium covers it | demonstrated |
|---|---|---|
| `GET /` | atrium `/` `HomePage` — the shell root | shell serves `/` |
| `GET /health` | atrium `/healthz`, `/readyz`, `/health` | `GET /healthz -> 200`, `GET /readyz -> 200` |
| `GET,POST /admin/login` | atrium `/login` `LoginPage` | `POST /api/auth/jwt/login -> 204` + `atrium_auth` cookie |
| `GET /admin/logout` | atrium header user menu | `POST /api/auth/jwt/logout -> 204`, then `/api/users/me/context -> 401` |
| `GET,POST /admin/totp-verify` | atrium `/2fa` `TwoFactorPage` | `GET /api/auth/totp/state -> 200` |
| `GET,POST /admin/totp-setup` | atrium `/profile` 2FA card + `TwoFactorSetupModal` | `GET /api/auth/totp/state -> 200` |
| `GET,POST /admin/profile` | atrium `/profile` (host adds `registerProfileItem atrium-ddns-profile`, slot `after-roles`) | `GET /api/users/me/context -> 200` |
| `POST /admin/profile/reset-2fa` | atrium `/profile` → disable 2FA | `/api/auth/totp/disable` in schema |
| `GET /admin/users` | atrium `/admin/users` | `GET /api/admin/users -> 200 (1 item)` |
| `GET,POST /admin/users/new` | atrium `/admin/users` invite flow | `/api/invites` in schema |
| `GET,POST /admin/users/<id>` | atrium `/admin/users` edit + `/admin/roles` — RBAC replaces the legacy `is_admin` boolean | `GET /api/admin/roles -> 200 (3)`, `GET /api/admin/permissions -> 200 (20)` |
| `POST /admin/users/<id>/delete` | atrium `/admin/users` delete, plus self-serve deletion on `/profile` | `/api/admin/users/{user_id}/delete` in schema; `/api/users/me/delete` in schema; `auth.allow_self_delete = True`, `auth.delete_grace_days = 30` |
| `POST /admin/users/<id>/reset-2fa` | atrium admin TOTP reset | `/api/admin/...` totp reset in schema |

Atrium's audit log (`GET /api/admin/audit -> 200, 7 items`) and notifications
(`GET /api/notifications/unread-count -> 200 {'count': 0}`) are additional
surfaces with no legacy counterpart. They are not in the table because the
criterion runs legacy→atrium, not the reverse.

### 3.2 Registered — name the registration (~~11~~ 15 routes)

All 13 registrations were confirmed **in the bundle the running stack serves**
(`GET /host/main.js`, 738,655 bytes), not in `frontend/src/`.

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
| `GET /nic/update` | `router_nic.py` | `200` |
| `GET /nic/delete` | `router_nic.py` | `200` |
| **`GET,POST /admin/hostnames`** (#69) | `registerRoute atrium-ddns-names` `/atrium-ddns/names` + `registerNavItem atrium-ddns-names-nav` ("Names"), plus a *Manage names* link on the board and on the zones page | `GET`/`POST /api/atrium_ddns/hostnames` |
| **`POST /admin/hostnames/<id>/delete`** (#69) | same page | `DELETE /api/atrium_ddns/hostnames/{hostname_id}` |
| **`GET,POST /admin/users/<uid>/hostnames`** (#69) | same page, under `atrium_ddns.admin` — see the stricter-reading caveat above | same endpoints, scope widened |
| **`POST /admin/users/<uid>/hostnames/<hn>/delete`** (#69) | same | same |

The four #69 rows were confirmed **in the bundle the running stack serves**
(`GET /host/main.js`, 748,893 bytes) and against the stack's own API, as an
ordinary `user`-role tenant holding exactly the three permissions `0002` grants
that role:

```
roles: ['user']
atrium_ddns perms: ['atrium_ddns.device.manage', 'atrium_ddns.domain.manage',
                    'atrium_ddns.hostname.manage']

GET /host/main.js -> 748893 bytes
  PRESENT  atrium-ddns-names
  PRESENT  /atrium-ddns/names
  PRESENT  atrium_ddns.hostname.manage
  PRESENT  atrium_ddns/hostnames
```

The admin pair, demonstrated rather than assumed — the same two endpoints, as a
caller holding `atrium_ddns.admin`, against a zone owned by a *different*
tenant:

```
admin POST into tenant A zone -> 201 {"id":478,"name":"byadmin.…","domain_id":1294,…}
   tenant A now sees: ['byadmin.…', 'home.…', 'note2e69b.…']
admin DELETE another tenant name -> 204
   tenant A now sees: ['home.…', 'note2e69b.…']
```

Two registrations are real and have **no legacy counterpart**, so they appear
nowhere above: `registerRoute atrium-ddns-devices` (`/atrium-ddns/devices`) and
its nav item. The device is the object the rewrite added; §4 is right that it is
what makes the rest cohere.

**Three registrations are still the scaffold.** `atrium-ddns-widget` (a counter
with a *Bump counter* button), `atrium-ddns-page` (*"Replace this page with your
real domain UI"*) and `atrium-ddns-admin-tab` (placeholder prose wrapping the
same counter) are the `create-atrium-host` template's demo, unchanged. They are
counted as registrations because they *are* registrations and the criterion asks
for a registration — but a reader should know that the dashboard row above rests
on the board and the log page, not on the home widget.

### 3.3 In neither column — the finding (~~15~~ 11 routes, 5 groups)

Counted apart, never averaged.

#### G1 — the hostname lifecycle (~~6~~ 2 routes) · severity: ~~blocking~~ medium

| legacy route | what it did | 2026-08-16 |
|---|---|---|
| ~~`GET,POST /admin/hostnames`~~ | list my hostnames **and create one** (prefix + domain + TTL) | **closed by #69** — §3.2 |
| ~~`POST /admin/hostnames/<id>/delete`~~ | remove a hostname | **closed by #69** |
| `GET,POST /admin/hostnames/<id>/backends` | choose which provider backends a hostname publishes to; edit its TTL; trigger a manual DNS update | **still open** |
| ~~`GET,POST /admin/users/<uid>/hostnames`~~ | the same, for another tenant, as an admin | **closed by #69** (scope-widened; see the stricter reading) |
| ~~`POST /admin/users/<uid>/hostnames/<hn>/delete`~~ | " | **closed by #69** |
| `GET,POST /admin/users/<uid>/hostnames/<hn>/backends` | " | **still open** |

**What is left, and why it is not a UI omission either.** The two surviving
routes are the per-hostname *backend* screen, and the rewrite has no data model
for two of the three things it did:

- **Which backends a hostname publishes to** is not a choice under this schema.
  A hostname publishes to every backend bound to its domain — `router_nic`
  iterates `Domain.backends` and aggregates — so there is no column to edit and
  no join table to populate. Closing this is a schema change, not a page.
- **Per-hostname TTL** has no column either. TTL lives in
  `ddns_domain_backend.config`, i.e. per binding, not per name.
- **A manual DNS update trigger** has no endpoint; the health check is
  scheduled only, which is the same gap G3 records for
  `POST /admin/health-checks/run`.

Counted as open on the criterion's literal wording, at medium rather than
blocking severity: the product is now usable end to end without them, which is
the change from the reading below.

<details>
<summary>the original finding, for the record (#47, 2026-08-16)</summary>

This is not "the UI was not built yet". **Nothing in the shipped application
ever constructs a `Hostname`.** The only call site in the whole package is
`scripts/seed_compat_fixture.py`, a test-fixture seeder gated on
`ATRIUM_DDNS_COMPAT_STUB=1` and refused when `ENVIRONMENT=prod`. The permission
`atrium_ddns.hostname.manage` is seeded by `0002_ddns_core` and referenced by no
endpoint.

Demonstrated as a super_admin holding that permission:

```
== 3. Can this admin create a HOSTNAME? ==
   POST /api/atrium_ddns/hostnames                       -> 405 {'detail': 'Method Not Allowed'}
   POST /api/atrium_ddns/devices/1/hostnames             -> 405 {'detail': 'Method Not Allowed'}
   POST /api/atrium_ddns/domains/1/hostnames             -> 405 {'detail': 'Method Not Allowed'}

== 4. What does the BOARD show after creating a domain and a device? ==
   GET /api/atrium_ddns/board -> 200
   devices on board: 1
     device 'router47'  hostnames=0  strips=0
```

Corroborated by the second instrument — the running stack's own OpenAPI
document lists 15 host operations and **no path containing "hostname"**.

The compounding consequence is worth stating plainly, because it is invisible
when the surfaces are reviewed one at a time: the resolution strip is the
milestone's signature element and §4's whole argument, and **a tenant cannot
cause one to render.** The board, the strip and most of the log surface are
correct, tested, deployed, and unreachable from a standing start. Nothing in
V1M3's per-issue reviews could have seen this — #44 built the board against
seeded rows, #45 built domains and devices, #46 built the log, and each was
right on its own terms.

</details>

**The strip, reached from an empty account** — the closing half of the finding
above, driven entirely over HTTP as a `user`-role tenant, with the device
authenticating on `/nic/update` by HTTP Basic exactly as a router does:

```
== 1. the account is empty ==
   GET /api/atrium_ddns/domains -> 200 []
   GET /api/atrium_ddns/devices -> 200 []
   GET /api/atrium_ddns/hostnames -> 200 []
   GET /api/atrium_ddns/board -> devices=0 unassigned=0
…
== 6. create the hostname ==
   POST /api/atrium_ddns/hostnames -> 201 {"id":464,"name":"home.e2e69b.example.invalid",…}

== 8. the board, BEFORE the device has published anything ==
   home.e2e69b.example.invalid      strips=0  <- correct: nothing published yet

== 9. the device calls in, over HTTP Basic — exactly as a router does ==
   GET /nic/update -> 200  body: good 203.0.113.69

== 10. THE STRIP ==
   device e2e69b-router  liveness=active
     home.e2e69b.example.invalid  strips=1
       family        : A
       published     : 203.0.113.69 at 2026-08-16T08:01:21.804296Z
       answered      : never_checked None
       called from   : 203.0.113.69 (evaluated)
       upper joint   : not_measured_never
       lower joint   : agreed
       joints agreed : 1 of 1 compared (n/a 0, unmeasured 1)
```

**Step 8 is the correction #69 made to its own acceptance criterion.** The issue
asked for "create a domain, a device and a hostname, and see a resolution strip
render" — three steps. Three steps produce a hostname on the board with **zero**
strips, and that is the right answer: `_strips_for` renders a family only when
the name has been published or answered in it, which is #44's argued-for reading
of `ui-design.md` §3.4 (the alternative gives every v6-only hostname a permanent
blank `A` rail). The board says so in words rather than drawing nothing —
*"nothing published yet — no strip to draw"*. The strip needs a fourth step, and
the fourth step is the device doing the thing the device exists for.

#### G2 — operator configuration has no UI (4 routes) · severity: high

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

There is no UI for any of them, and there is no route by which one appears:

- the host never calls `registerSettingsGroup` — confirmed **absent from the
  bundle the stack serves**, not just from the source;
- atrium has **no generic namespace editor**. Its four config screens
  (`SystemAdmin`, `AuthAdmin`, `BrandingAdmin`, `TranslationsAdmin`) are
  hand-built forms bound to the literals `system`, `auth`, `brand`, `i18n`.
  Nothing loops over the namespaces the admin API returns.

So `atrium_ddns` config is reachable only by `curl` against
`PUT /api/admin/app-config/atrium_ddns`. §4 says *"Rate limits, health-check
config, retention become one nested group via `registerSettingsGroup`"* — that
clause is unbuilt, and §5 opened no issue for it.

The per-device limit is a narrower miss inside the same group: `DeviceList`
offers it on the **create** form only, there is no device `PATCH` endpoint at
all, and the stored value is never displayed. Changing a device's limit today
means delete-and-recreate, which rotates the credential.

```
== 5. Legacy per-user RATE LIMIT override ==
   PATCH /api/atrium_ddns/devices/1 -> 405 {'detail': 'Method Not Allowed'}
```

#### G3 — on-demand operator actions (3 routes) · severity: low

| legacy route | status |
|---|---|
| `POST /admin/health-checks/run` | `-> 405`. No manual trigger; the check is scheduled only. |
| `POST /admin/health-checks/clear` | `-> 405`. |
| `POST /admin/events/clear` | `-> 405`. |

`/admin/events/clear` deserves its reasoning on the record rather than a quiet
reclassification. Plan §3.4 deliberately replaced on-demand clearing with a
bounded, scheduled retention prune, and that is a better design. But the
criterion's two columns are *registered* and *deleted because **atrium** covers
it*, and atrium covers none of these three — the replacement is a host feature.
Inventing "superseded by design" as a third column is precisely what the issue
forbids, so they are counted here, at the severity they deserve.

#### G4 — domain rename (1 route) · severity: low

`GET,POST /admin/domains/<id>` renamed a domain. The running stack serves
`GET`/`POST /api/atrium_ddns/domains` and `DELETE /api/atrium_ddns/domains/{id}`
and no `PATCH`. Delete-and-recreate is not equivalent: the delete cascades.

#### G5 — help (1 route) · severity: low

`GET /admin/help` rendered `help.html`. Atrium ships **no** help page — swept
for a help route, nav entry or anchor across atrium's frontend and found none —
and the host bundle registers none. Swept to a negative result: there are
exactly zero help surfaces in the deployed system.

---

## 4. Tally

| column | routes (#47) | share | routes (#69 re-run) | share |
|---|---|---|---|---|
| deleted — atrium covers it | 13 | 33.3% | 13 | 33.3% |
| registered | 11 | 28.2% | **15** | **38.5%** |
| **neither — the finding** | **15** | **38.5%** | **11** | **28.2%** |

Restricted to the 22 *pages* (the criterion's own word), the gaps were
`/admin/hostnames`, `/admin/hostnames/<id>/backends`,
`/admin/users/<uid>/hostnames`, `/admin/users/<uid>/hostnames/<hn>/backends`,
`/admin/rate-limits`, `/admin/rate-limits/user/<id>`,
`/admin/health-checks/config`, `/admin/domains/<id>` and `/admin/help` — **9 of
22, 40.9%**. #69 closed the first and the third, leaving **7 of 22, 31.8%**.
The narrower denominator does not flatter the result, which is why both are
given.

Both denominators were re-derived at re-run time rather than carried forward:
the legacy tree is still pinned at `5d1c941` and `git diff --stat 5d1c941 HEAD`
is still empty across `web_routes.py`, `dyndns.py` and `templates/`, so 39 and
22 both stand. **The counts moved; the divisors did not**, which is worth
saying explicitly because a shrinking gap over a shrinking denominator would
not be the same result.

**Both denominators were re-derived at write time rather than carried forward,
and the shares are printed beside the counts they came from.**

---

## 5. What this does not measure

Stated so a later reader does not take the table for more than it is.

- It is a **capability** map, not a visual one. "Registered" means the surface
  exists and its endpoint answers; it does not mean the replacement is as
  complete as the page it replaced, only that a tenant can get at the function.
- The three scaffold registrations (§3.2) are counted as registrations. A stricter
  reading that required a registration to be *product* UI would move
  `GET /admin/` into the gap column and make it 16.
- It says nothing about the `/nic/*` wire protocol beyond "the route exists" —
  that is the frozen 131-case compat table's job, not this file's.
- `/admin/events/clear`, `/admin/health-checks/run|clear` and the domain rename
  are counted as gaps on the criterion's literal wording. A milestone owner may
  reasonably strike them, which would give ~~**11 gaps in 3 groups**~~ **7 gaps
  in 3 groups** after #69's re-run. That is the owner's call to record, not a
  reader's to assume — the counts stand above either way.
- **The #69 re-run measured G1 only.** Every verdict in G2–G5 is #47's reading,
  carried forward without being re-taken. They are unlikely to have moved — no
  issue since has touched those surfaces — but "unlikely to have moved" is not
  a measurement, and the next re-run should not treat them as one.
