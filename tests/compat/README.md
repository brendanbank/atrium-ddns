# DynDNS v2 compatibility case table

`protocol_cases.yaml` is the frozen wire contract for `/nic/checkip`,
`/nic/update` and `/nic/delete`. It is **data**: no Python, no expressions, no
imports. A runner loads it, replays `cases` against a base URL and compares the
response with `expect`.

The source of truth is the **behaviour of `brendanbank/dyndns-route53`'s
`dyndns.py` at `main`** (commit `ec605c5`), read directly, together with
`auth.py`, `rate_limiter.py` and `lib/accounts.py`. It is **not**
`docs/DYNDNS-PROTOCOL.md` in that repo, which documents the de-facto standard
the implementation deliberately departs from in nine places. Where the two
disagree, the implementation wins and the case carries a `divergence/*` tag.

Routers in the field — OPNsense, ddclient, inadyn, Fritz!Box — are the users,
and they cannot be asked to change. A case that asserts the document instead of
the behaviour is a bug in this file.

## Layout

| key | what it is |
|---|---|
| `spec_rows` | every row of `docs/ops/refactor-plan.md` §1's three tables, plus the nine divergences and the two `HEAD` rows, given an id |
| `fixture` | one shared world: three users, seven backends, twelve hostnames. Each hostname exercises exactly one backend condition |
| `defaults` | applied to any case that does not override them |
| `cases` | the table. Each names the `spec_rows` it `covers` |
| `deleted_cases` | query-parameter-auth cases, removed rather than marked expected-fail. No `expect` block, never executed |

### Case fields

| field | meaning |
|---|---|
| `id` | unique, stable, referenced from PR bodies and issue comments |
| `path` | `/nic/checkip`, `/nic/update` or `/nic/delete` |
| `query` | query parameters, as sent. Values are literal — `""` means the parameter is present and empty, which is not the same as absent |
| `auth` | `{type: none}` or `{type: basic, username, password}` |
| `headers` / `omit_headers` | request headers to set or suppress |
| `client_ip` | the address the server must see as the client. `null` means *unparseable / absent* |
| `rate_limited` | the fixture must put the user over its limit before the request |
| `targets` | `[legacy, host]` by default. `[host]` marks a case that is deliberately **not** legacy behaviour |
| `legacy_behaviour` | on a host-only case, what the legacy service answers instead |
| `covers` | `spec_rows` ids |
| `effects` | side effects that are invisible on the wire: DNS operations and persisted columns |
| `expect` | `status`, `content_type`, `body`, and optionally `line_count` / `body_ends_with_newline` |

`expect.body` is an **exact byte comparison**. Multi-line bodies use a `|-`
block scalar, which preserves interior newlines and strips the trailing one —
which is what `"\n".join(lines)` produces. Do not compare with whitespace
stripped: a framework appending a trailing newline is the most likely way this
regresses and a stripped comparison cannot see it.

### Things the runner has to get right

- **`client_ip` is not `myip`.** The legacy service sits behind
  `ProxyFix(x_for=1)`, so its `remote_addr` comes from `X-Forwarded-For`. The
  table states the address the server must *see*; how the runner arranges that
  is the runner's business. `client_ip: null` means the server sees no parseable
  address at all.
- **`targets` is not decoration.** `--target legacy` must skip the three
  host-only cases; running them against the legacy service will fail correctly
  and uselessly.
- **`deleted_cases` is not a case list.** It carries no `expect`. A runner that
  starts executing it will fail loudly, which is the intent.
- **A green run against only one target proves nothing about compatibility.**
  Run `--target legacy` first and treat failures as calibration findings against
  this table, not as compat requirements.

## Standing the legacy service up

*Added by #9, which ran the table against it. The result is `baseline.md`.*

**The legacy service you test against is a local throwaway instance. Never the
deployed one.** The table registers hostnames, exercises `badauth` paths and, if
you close the rate-limit gap below, deliberately exhausts a user's limit.
Pointing it at the live service writes real state and trips the limiter for real
clients. No DNS credentials are needed and none should be present: every case
stops at `nohost` / `911` / `badauth` / `notfqdn`, or reaches a backend that is
deliberately unconfigured.

### Six things that are not obvious

**1. It needs its own directory, because `config.py` hardcodes its database
path.** `SQLALCHEMY_DATABASE_URI` is `sqlite:///<dir of config.py>/instance/…`,
and `.env` is loaded from the same `basedir`. A copy of the checkout is
therefore the unit of isolation: own directory, own `instance/`, own `.env` with
a throwaway `FERNET_KEY`, `SECRET_KEY` and `ADMIN_PASSWORD`.

**Check the copy by digest, not by eye.** The one thing that invalidates every
number a calibration run produces is testing a *modified* legacy service without
noticing, and a sha256 comparison against the checkout is the only instrument
that sees it. Print the digests, and print the checkout's HEAD and porcelain
status beside them.

**2. The fixture needs a DNS provider that does not talk to DNS.** The table's
`fixture:` block asks for `service: stub`, "a provider the account factory knows
and whose result is scripted by `result`". The legacy service ships aws, hetzner
and nsupdate, all of which reach a real nameserver. One ~60-line module in
`lib/account/` supplies it; `AccountFactory._register` globs that directory, so
it is picked up with no other change.

Carry the scripted result in the **service name**, not the credentials:
`DomainBackend` is unique on `(domain_id, backend_type)` and the fixture needs
four differently-scripted stubs on one domain. Keep the module strictly below
`hostnameperzone` — the zone check, the credential check, the factory lookup,
the aggregation and the response formatting must stay unmodified legacy code, or
the table is calibrating against you.

**3. Three of the fixture's conditions are not spelled the way the table spells
them.** The mapping is not one-to-one and getting it wrong produces plausible
wrong answers rather than errors:

| the table says | the legacy schema needs |
|---|---|
| `backends[].zone` | the **hostname's domain** — `dyndns.py` passes `hn.domain.name` to the provider. So `wrong-zone` is a hostname whose domain is not a suffix of it |
| `backends: []` | a domain with **no `DomainBackend` rows**. It cannot be "no hostname-specific backends": `Hostname.get_backends()` falls back to *all* of the domain's backends |
| `credentials: absent` | a `DomainBackend` with no `BackendConfig` rows, so `get_credentials()` returns `{}` |
| `service: no-such-service` | a `backend_type` no provider matches |

**Backend order is load-bearing and the schema does not carry it.**
`hostname_backends` has no ordering column, so `hn.backends` comes back in
`domain_backends.id` order. `update-aggregate-first-non-good-nochg-status`
expects `911` from `[nochg, 911, dnserr]`, and would expect `dnserr` from the
same set in a different order. Create the backend rows in the order the fixture
lists them, and print `hn.get_backends()` per hostname after seeding so the
order is a reading rather than an assumption.

**4. A stock instance answers `abuse` from about case 31.** The legacy default
is 30 requests/minute and the table is ~95 requests in 13 seconds. The table's
`rate_limited: false` default is a **precondition** — "the user is not over its
limit" — so raising the fixture users' limits is what makes the default true,
not a way of dodging a failure. The three `rate_limited: true` cases are skipped
by the runner and are unaffected; `baseline.md` §2 measures them separately by
dropping the limit back to 1.

**5. Seed before the service starts, and seed hostnames with a `last_ip_v4`.**
`create_app` resolves A and AAAA from real DNS for every hostname with no
tracked IP, at every boot — 24 lookups against names that do not exist. Any
non-null `last_ip_v4` skips it; nothing on the wire reads the column.

Re-seeding a populated file fails: `Hostname.query.delete()` is a bulk delete
and does not cascade into the `hostname_backends` secondary, so the association
rows survive and the next insert hits their unique constraint. Delete the
database files instead. A throwaway database has no reason to be incremental.

**6. Restarting the service is where a run goes quietly wrong.** #9 lost a run
to this and it is worth the paragraph:

- `pkill -f 'legacy/run.py'` **does not match** a process started as
  `cd legacy && python run.py` — argv carries the bare `run.py`. The kill
  matched nothing, silently.
- The old process kept the port and kept reading a **deleted** SQLite inode, in
  a half-seeded state left by a re-seed that had aborted mid-way.
- **The reachability probe was green throughout.** `/nic/checkip` touches no
  database at all, so `GET /nic/checkip -> HTTP 200` says the service is up and
  nothing about whether the fixture exists.
- The result was 40 failed, 62 passed, all coherent, all wrong.
  `ok.example.com` answering `nohost` with authentication succeeding reads
  exactly like a real compatibility finding.

Start it with an absolute path so `pkill -f` can find it, check the listener's
`ps` start time against when you seeded, and if a run comes back red, confirm
the fixture is there — `GET /nic/update?hostname=ok.example.com&myip=…` should
answer `good`, not `nohost` — before writing up a single divergence.

**Better: make the trap unreachable rather than checkable.** #25 seeded and
served **in one process** — `create_app(config_class=…)` against a database
directory that is deleted and rebuilt at the top of the same script, then
`app.run()` at the bottom of it. There is no second process to go stale, no
`pkill` to mis-target, and the listener's start time *is* the seed time. The
three checks above are still worth running; they are just no longer the only
thing between you and 40 coherent, wrong failures. Mutation runs (below) each
got a fresh copy on a fresh port and the copy was **deleted** afterwards rather
than reverted, for the same reason: there is no revert to get wrong.

**Do not point the app at the checkout's own `config.py` defaults.** They
resolve `instance/dyndns.db` next to `config.py` — which, in a copy made
without excluding `instance/`, is a copy of whatever the operator had there —
and `load_dotenv(basedir/.env)` pulls the checkout's real `.env` into the
process environment. Passing a config class with explicit throwaway paths, and
excluding `.env*` and `instance/` from the copy, avoids both.

**Pick a port, and not 5000 on macOS.** `Control Centre` listens on 5000 for
AirPlay, so the worked invocation below will connect to something that is not
the legacy service. #9 used 5109, #25 used 5125 (and 5131–5136 for mutations).
Two agents calibrating at once need two ports, same as the compose stack.

### The result

`baseline.md`. Short version: 98 cases selected for `--target legacy`, 95
executed and 3 measured out of band, **98 agree and 0 diverge** — but three
divergences from `docs/DYNDNS-PROTOCOL.md` that this table did **not** carry
were found by sweeping §1 of the document, and they are the actual finding.

**#25 re-ran it on the extended table, on an independently built fixture**
(seeded in the same process that serves it, from a copy whose every legacy file
hashes the same as #9's did): **107 selected, 104 executed, 104 pass, 0 fail.**
The 9 additional legacy-selected cases are the ones that carry D7, D8, D9 and
the `HEAD` finding, and all 9 agree with what was measured by hand first.

**#29 re-ran it again at v3, on a third independently built fixture**, and got
the same 16 file digests a third time: **124 selected, 121 executed, 121 pass,
0 fail.** The 17 new cases are the address-family ones (below); every one was
measured against the legacy service first — request, exact body, DNS operation
and the `last_ip_*` columns before and after — and only then written into the
table. `baseline.md` §7 is the reading.

### The new cases bite — five mutations, five predictions

A case written from a measurement and then asserted against the same service is
one instrument twice. Each mutation below was applied to a **fresh copy** of the
legacy service on its own port and database, the whole table was run against it,
and the copy was deleted. The red set was predicted before each run.

| mutation | predicted red | actual red |
|---|---|---|
| M1 `update` falls back to `Client-IP` when `remote_addr` is unparseable | 1 | **1** — `update-client-ip-does-not-supply-a-missing-myip` |
| M2 `checkip` prefers `Client-IP` over `X-Forwarded-For` | 2 | **2** — both `checkip-client-ip-*` cases |
| M3 `offline=YES` answers `!donator` | 2 | **2** — the D8 case **and** `update-unknown-parameters-are-ignored` |
| M4 `/nic/update` declares `methods=["GET", "POST"]` | 1 | **1** — `update-post-is-405-with-an-html-body` |
| M5 `httpReply` aborts 405 on `HEAD` | 2 | **2** — the two legacy `HEAD` cases |

**M3 is the one that argues against its own case, and it is reported that way.**
`update-unknown-parameters-are-ignored` already sends `offline=YES` among four
others, so it goes red too: the new case adds *intent*, not detection. What it
adds is that the existing case would have flagged it as "an unknown parameter
stopped being ignored", which is the wrong diagnosis for a parameter the
protocol document defines and does not deprecate.

**M5 is the one that argues for the split decision.** It refuses `HEAD` inside
`httpReply`, which every `/nic/*` reply goes through **except** the `checkip`
HTML path, which builds its own response. So `checkip-head-returns-headers-and-no-body`
stayed green while the two write-side `HEAD` cases went red — the exact shape
of "refuse `HEAD` where the handler is not safe, keep it where it is",
demonstrated rather than asserted.

## Running the table

`conftest.py` + `test_protocol.py` are the runner. One test per case, named
after the case, so a failure names the behaviour rather than an index.

```bash
python -m venv .venv && .venv/bin/pip install pytest pyyaml

.venv/bin/python -m pytest tests/compat --target legacy --base-url http://127.0.0.1:5000
.venv/bin/python -m pytest tests/compat --target host   --base-url http://localhost:8153

# or, without a local venv, from inside the api container (#23):
make test-compat TARGET=host BASE_URL=http://api:8000
```

`pytest` and `PyYAML` are the only dependencies. The wire is spoken with
stdlib `http.client`, not requests/httpx, because this suite asserts exact
bytes and exact headers: `http.client` adds only `Host` and
`Accept-Encoding: identity`, so every other header on the wire is one the
runner put there and `omit_headers: [user-agent]` genuinely omits it.

### The two options, and why neither has a default

| option | required | notes |
|---|---|---|
| `--target legacy\|host` | **for any case to run** | decides which cases run. No default: a runner that picks one will be pointed at the wrong service eventually. Absent, the table is collected as one skip and reported as NOT RUN; given a `--base-url` and no target, the session is refused outright (exit 4) |
| `--base-url` | except with `--collect-only` | used **verbatim**. Nothing reads its shape |
| `--compat-timeout` | no | seconds, default 10 |
| `--compat-insecure` | no | skip TLS verification for an `https` base URL |

**Nothing infers "local" from a URL.** `scripts/smoke.sh` made exactly that
mistake — an ssh tunnel produces a `localhost` URL for a remote host, and the
check read the wrong stack while reporting green. `grep -n
'localhost\|127\.0\.0\.1' tests/compat/conftest.py` returns nothing, and that
is the check that keeps it true.

What *does* catch a mis-paired `--target` is the response, not the URL: the
three host-only cases carry `legacy_behaviour`, and when the actual body equals
it the failure report says so in as many words — *"the actual body is exactly
this case's documented `legacy_behaviour` … check that <url> is not the legacy
service"*. Derived from what came back, so an ssh tunnel cannot hide it.

`--target` also decides **collection**, so `--collect-only` needs it too; that
is the only place a mode changes what is required, and it is pytest's mode, not
the target's.

### What the runner does and does not assert

Asserted, per case: `status`, `content_type` (exact, no normalisation — a
charset or whitespace difference is a finding, not noise), `line_count`,
`body_ends_with_newline`, and `body` as **exact bytes**. Every mismatching
field is reported, not just the first.

Not asserted: `effects` (DNS operations and persisted columns). Eleven cases
carry one. They are invisible on the wire, and the runner prints the count
every run so the gap is a number rather than an assumption.

Not runnable: the three `rate_limited: true` cases. Getting a user over its
limit is fixture state, not a request, and hammering the endpoint to find the
limit would poison every case after it. They are **skipped with the
precondition named** — never quietly passed — and counted in the accounting
block.

The runner does **not** build the `fixture:` world. It assumes the world in the
table already exists behind `--base-url`, and one reachability probe at session
start turns an unreachable service into one clear message instead of ninety-odd
identical stack traces. *Reachable is not implemented*: pointed at the host
stack today, `/nic/checkip` answers **HTTP 200** with atrium's SPA
`index.html`, because the SPA catch-all serves any unmatched path. A runner
checking status alone would have called that green.

### `client_ip` is arranged with `X-Forwarded-For`

One header, same spelling for both targets — the legacy service reads it
through `ProxyFix(x_for=1)`, and the host must honour it too or the two are not
compatible. A per-target spelling here would be the branch this runner refuses
to have.

`client_ip: null` cannot be arranged by *omitting* the header:
`ProxyFix._get_real_value` ignores an absent or empty value and leaves
`REMOTE_ADDR` as the socket address, which is perfectly parseable. The runner
sends the literal `not-an-ip` instead, which no IP parser accepts.

### Reading the accounting block

Every run ends with its own arithmetic, because the failure mode of a
table-driven runner is not a wrong answer, it is a case that quietly stopped
running:

A real block, from `--target host` against the local stack on `25_overnight`
(abridged only in the id lists). It is red, because the host has no `/nic/*`
yet — an invented green example here would be the first thing to rot:

```
compat accounting (target=host):
  cases in table             114
  excluded by `targets:`       4  ['checkip-post-is-405-with-an-html-body', ...]
  selected for this target   110
  unmet preconditions          3  ['update-abuse-rate-limited', ...]
  executable                 107
  wire-only                   15  cases carrying `effects:` ... that this runner does NOT assert
  ran this session           110  105 failed, 2 passed, 3 skipped
  reachability probe        GET http://localhost:8125/nic/checkip -> HTTP 200
```

**The two green ones are worth naming rather than rounding off.**
`checkip-post-is-405-on-the-host` and `update-post-is-405-on-the-host` pass
against a host that has no `/nic/*` routes at all, because a non-`GET` on an
unmatched path meets the SPA catch-all — which also declares only `GET` — and
gets the same 405. Nothing on the wire distinguishes "no route" from "GET-only
route", so those two cases assert *POST is refused* and never *the endpoint
exists*. The 105 red cases beside them are what says the endpoint does not
exist yet — and the other 3 are skipped, not red, which is a third state and
not a quieter kind of failure. (The earlier version of this block, taken on `8_overnight` with a
101-case table, read `101 selected / 98 failed / 3 skipped` and `0 excluded`.)

At v3 (#29) the same block reads `131 / 4 excluded / 127 selected / 3 unmet /
124 executable`, and against the legacy service `131 / 7 excluded / 124
selected / 3 unmet / 121 executable / 23 wire-only`. `wire-only` moved 15 → 23
because 8 of the 17 new cases carry an `effects:` block — the number the runner
prints so the unasserted gap stays a number.

`selected` is what the table offers; `ran this session` is what pytest actually
executed, counted from its own report objects. They are two numbers on purpose
— a `-k` filter or a collection that stopped early shows up as a `NOT RUN`
line rather than as a run that says 98 and did twelve.

Eight guards (nine tests — the partition one runs per target) need no service
and fail loudly rather than letting the run shrink:

| guard | fails when |
|---|---|
| `test_every_case_declares_known_targets` | a `targets:` typo removes a case from every run without removing it from the table |
| `test_target_selection_partitions_the_table` | selected + excluded stops equalling the table |
| `test_deleted_cases_are_never_executed` | a `deleted_cases` entry grows an `expect`, or an id appears in both lists |
| `test_no_url_shape_inference` | a loopback literal appears in `conftest.py` — comments included |
| `test_request_building_cannot_depend_on_the_target` | `build_request` grows a `target` parameter, i.e. a per-target request |
| `test_every_case_builds_a_request` | any case in the table, either target, stops turning into a request |
| `test_the_table_is_frozen_at_its_recorded_shape` | any of the four `frozen:` readings stops describing the table |
| `test_the_version_advances_exactly_when_the_data_does` | (#29) a re-freeze bumps `version` over an unchanged table, or moves the data past `frozen.previous` without bumping. It does **not** see a change made at the *current* version — see below |

### Collected-case readings

Taken on `25_overnight`, not inherited. Two instruments, and the runner's own
collection is the second one:

| instrument | `--target legacy` | `--target host` | branch |
|---|---|---|---|
| `pytest --collect-only`, `test_case[...]` nodes | 107 | 110 | `25_overnight` |
| PyYAML over `cases`, filtered on `targets` | 107 | 110 | `25_overnight` |
| `pytest --collect-only`, `test_case[...]` nodes | **124** | **127** | `29_overnight` |
| PyYAML over `cases`, filtered on `targets` | **124** | **127** | `29_overnight` |

114 in the table at v2: **103 shared, 4 legacy-only, 7 host-only**; 11
`deleted_cases`, 0 of them collected. `grep -c '^  - id:'` = 125 = 114 + 11.

131 at v3 (#29): **120 shared, 4 legacy-only, 7 host-only** — all 17 additions
are shared, because none of them is about a framework's rendering. 11
`deleted_cases`, still 0 collected; `grep -c '^  - id:'` = 142 = 131 + 11.

`legacy`-only is new in #25 and the asymmetry is deliberate: four cases assert
what Werkzeug renders (a 405 error page, a `HEAD` that runs the handler) and
have `-on-the-host` counterparts asserting what the rewrite must do instead.
Only the *status* is common to the two frameworks, so a single shared case
would have had to assert one framework's rendering of the other.

The `8_overnight` reading, against the 101-case table, was 98 / 101 by both
instruments — 3 host-only, 0 legacy-only, `grep -c` = 112.

### No credential reaches this output that was not already committed

The runner has exactly one credential source: the `fixture:` block of the case
table — no environment variable, no file, no prompt. Basic passwords are
withheld twice over in the failure report, once in the `auth:` line and once in
the `Authorization:` header (base64 is not encryption). The three
query-parameter cases *do* print their credentials in the request line, because
the query string is the thing they assert; those values are synthetic and
already committed a few hundred lines above.

## The nine divergences from the protocol document

§1 of the plan listed five. #7 found a sixth by reading the regex. #9 swept §1
of the document clause by clause and found three more, which #25 added. All
nine are **preserve**.

> **Nine is now both what this table carries and what the document diverges
> by**, and it took three issues to make those the same number. #7 wrote six;
> #9 confirmed all six against the live legacy service (17 cases, 17 agreeing)
> and then found 7, 8 and 9 by reading the document rather than the table, but
> did not own this file; #25 added them, with a case each, and re-ran the
> whole table against the legacy service to confirm them. `baseline.md` §4.2
> and §4.4 are the sweep; the sweep is a **negative result** — every clause of
> §1 was walked, so nine is a count and not an anecdote.

1. **`nohost {ip}` on update, bare `nohost` on delete.** The doc says bare
   `nohost` for both. Clients parse the first token.
   Cases: `update-nohost-unregistered-hostname`,
   `delete-nohost-unregistered-hostname`,
   `delete-nohost-carries-no-ip-suffix-even-when-myip-is-given`.
2. **No `badagent`.** The `User-Agent` header is never read. The doc requires a
   descriptive one and specifies `badagent` otherwise.
   Cases: `update-no-badagent-for-missing-user-agent`,
   `update-no-badagent-for-generic-user-agent`,
   `delete-no-badagent-for-generic-user-agent`.
3. **No `numhost`.** There is no 20-hostname cap; 25 hostnames produce 25 lines.
   Cases: `update-no-numhost-for-25-hostnames`,
   `delete-no-numhost-for-25-hostnames`.
4. **Everything is HTTP 200**, `badauth` included. Load-bearing: a 401 breaks
   clients that parse the body.
   Cases: `update-badauth-no-credentials`, `delete-badauth-no-credentials`,
   `checkip-default-is-html`, and `expect.status` on every other case.
5. **A single label with no dot passes the hostname regex.** `"foo".split(".")`
   is `["foo"]`, every element matches the label pattern, so validation succeeds
   and the request falls through to the hostname lookup and answers `nohost`.
   The doc would call this `notfqdn`.
   Cases: `update-nohost-single-label-no-dot`,
   `delete-nohost-single-label-no-dot`.
6. **A trailing newline in a label passes the regex too.** The pattern ends in
   `$`, which in Python matches before a trailing newline, so `"foo\n"`
   validates. `isvalidhostname` returns the *unstripped* element, so the lookup
   then misses and the answer is `nohost`. Not in §1 as originally written;
   found by this issue and added to the plan.
   Cases: `update-nohost-trailing-newline-in-label`,
   `delete-nohost-trailing-newline-in-label`.
7. **Non-`GET` methods answer HTTP 405 with an HTML body**, not `badagent`
   (§1.3). Flask's default `methods` are `GET`/`HEAD`/`OPTIONS`, no route
   overrides them, and Werkzeug answers 405 with a 153-byte error page before
   any handler runs. **This bounds divergence 4** — "everything is HTTP 200" is
   true of every case in this table, all of which are `GET`, and false of the
   endpoint. Cases: `update-post-is-405-with-an-html-body`,
   `checkip-post-is-405-with-an-html-body` (legacy-only, exact Werkzeug bytes),
   `update-post-is-405-on-the-host`, `checkip-post-is-405-on-the-host`.
8. **`offline=YES` is inert and `!donator` has no writer** (§1.5, §1.6). The
   record is updated normally. `wildcard`/`mx`/`backmx` being ignored is
   *compliant* — the document marks those deprecated — and `offline` is not so
   marked, which is what makes this a divergence rather than a courtesy.
   Case: `update-offline-yes-is-inert-and-never-answers-donator`, which asserts
   the parameter is inert rather than absent.
9. **`Client-IP` is not honoured** (§1.7), though the document names it beside
   `X-Forwarded-For`. `ProxyFix` reads only the latter.
   Cases: `checkip-client-ip-header-is-not-honoured`,
   `checkip-client-ip-does-not-shadow-x-forwarded-for`,
   `update-client-ip-does-not-supply-a-missing-myip`.

Divergence 6 is preserved for divergence 5's reason: both spellings already end
at `nohost`, so preserving costs nothing, while "fixing" it would turn a
per-hostname `nohost` line into a whole-request `notfqdn` — a behaviour change
for a case nobody hits.

## Address family — where it reaches the wire, and where it cannot

*Added by #29, which took the table from v2 to v3. The gap was named in
`frozen.known_gaps` at v2 and in plan §1; this is the measurement that closed
as much of it as a wire table can close.*

Production traffic is **143 IPv4 / 305 IPv6** events (plan §3.3.1, from
`events.ip_address`). The v2 table was 4 cases touching IPv6 out of 114. The
obvious response — add v6 cases until the table's ratio looks like the
traffic's — is wrong, and the reason is measurable rather than aesthetic.

**Which cases can the family reach at all?** Every legacy-selected executable
case was replayed twice against a throwaway legacy instance: once as the table
spells it, once with every IPv4 literal in `query` / `client_ip` / `headers`
swapped for an IPv6 one, comparing status, content-type and body bytes. At v2,
of 104 executable cases:

| | |
|---|---:|
| the reply **changed** | 36 |
| swapped, reply **identical** | 63 |
| no IPv4 literal to swap | 5 |

Three states, not two: *"there was nothing to swap"* is not *"swapping changed
nothing"*, and collapsing them would have inflated the second number by five.

So 68 of 104 cases sit on paths where the address family cannot alter a byte of
the response — `badauth` and `abuse` return before any address is parsed,
`notfqdn` returns after `myip` is validated but carries no address, and **every
single `/nic/delete` case** answers identically because delete's replies carry
no IP suffix at all (divergence 1). A v6 counterpart for any of those is
padding.

**Where it does reach the wire, it reaches through three mechanisms**, and
coverage is stated per mechanism because 36 near-identical IPv4 update cases
cover one mechanism 36 times, not IPv4 36 times:

| mechanism | IPv6 at v2 | IPv6 at v3 |
|---|---|---|
| `/nic/update` interpolates the address after a status token | 1 of 5 tokens (`good`) | **5 of 5** (`good`, `nochg`, `nohost`, `911`, `dnserr`) |
| `/nic/checkip` echoes it | 1 of 2 formats (`plain`) | **2 of 2** (`plain`, `html`) |
| `myip` parse **refusals** differ per family | 0 IPv6-shaped against 3 IPv4-shaped | **3 against 3** |

That is the ratio the table is defended on: **15.3% of cases (20 of 131) touch
IPv6, and 100% of the wire-visible family-sensitive mechanisms are covered in
both families.** Reaching 68% would have meant roughly thirty more cases
repeating mechanisms already covered twice over.

**Four of the v6 cases have no IPv4 analogue at all**, and they are the ones
worth knowing about, because each is a place where a rewrite silently diverges
and nothing else notices:

- `update-myip-ipv4-mapped-ipv6-stays-an-aaaa-record` — `::ffff:192.0.2.1` is
  an `IPv6Address`, so `getiptype` answers `AAAA` and the body keeps the mapped
  spelling. Calling `.ipv4_mapped` changes the body; dispatching on "contains a
  dot" changes the record type. Both are plausible and both fail here.
- `update-myip-ipv6-with-embedded-ipv4-is-renormalised` — `64:ff9b::192.0.2.33`
  comes back as `64:ff9b::c000:221`. The existing normalisation case
  (`2001:0DB8:0000::1`) can be passed by lowercase-and-compress; this one
  cannot.
- `update-myip-ipv6-scope-id-round-trips` — `fe80::1%eth0` is accepted and the
  zone survives into the body. A stricter validator answers `911`.
- `update-911-myip-ipv6-with-prefix-length` and
  `update-911-myip-ipv6-shaped-but-unparseable` — the refusal surface. An
  implementation whose v4 parser rejects `/32` while its v6 parser falls back
  to `ip_network` passes the v4 case and fails the v6 one.

**Balancing families means pairing them, not counting them.** One of the 17
additions is deliberately IPv4: `update-ipv4-persists-last-ip-v4-not-v6`. The
v2 table asserted `last_ip_v4: unchanged` on a v6 update and nothing asserted
the mirror, so a host writing **both** columns on every update passed the
frozen table.

### The record type is invisible on the wire, on both endpoints

This is the honest limit of the whole exercise, and it was measured rather than
argued. `getiptype` was mutated on a throwaway copy to answer `A` for every
address — so every `AAAA` the service would have written became an `A` — and
the whole v3 table was run against it: **121 executed, 0 failed.**

Update's replies carry the normalised **address**, never the record type;
delete's carry neither. So the family cases in this table assert *parsing,
normalisation and echo*, which is real, and assert nothing at all about which
record gets written. A host that echoes `good 2001:db8::1` and writes an `A`
passes every case here.

That is why `/nic/delete` got 2 of the 17 additions and not a proportional
share. Of its 36 executable cases at v2, the swap changed **zero** replies while
the DNS operation underneath changed `rtype` from `A` to `AAAA` — read from the
stub provider's own call log, not inferred. The two added are one for the v6
parse branch, which delete reaches through its own `if myip:` block that
update's cases never execute, and
`delete-myip-ipv4-mapped-ipv6-deletes-aaaa-only`, whose own `note:` says in as
many words that it cannot fail on the wire. Closing this needs an `effects:`
assertion — the first entry in `frozen.known_gaps` — not more rows.

### The new cases bite — five mutations, five predictions

Each applied to a **fresh copy** of the legacy service on its own port and
database, the whole v3 table run against it, and the copy deleted. The red set
was predicted before each run. **5 applied, 5 exact.**

| mutation | predicted red | actual red |
|---|---|---|
| M-A `getiptype` always answers `A` | **0** — nothing on the wire carries the record type | **0** of 121 |
| M-B `update` unwraps an IPv4-mapped v6 before echoing it | 1 | **1** — `update-myip-ipv4-mapped-ipv6-stays-an-aaaa-record` |
| M-C `getip` refuses a scoped IPv6 literal | 1 | **1** — `update-myip-ipv6-scope-id-round-trips` |
| M-D `getip` falls back to `ip_network` for a prefixed address | 2 | **2** — `update-911-myip-with-prefix-length` **and** the new `-ipv6-` twin |
| M-E `checkip` unwraps an IPv4-mapped v6 before echoing it | 1 | **1** — `checkip-ipv4-mapped-ipv6-is-not-unwrapped` |

**M-A is the one that argues against its own additions, and it is reported that
way.** A prediction of zero is only worth making because it is falsifiable —
had any case gone red, the record type would have been observable somewhere and
the `effects:` gap smaller than claimed. It went 0 of 121.

**M-D is the one that justifies the pair.** The v4 case and the v6 case both go
red, because a fallback to `ip_network` is not family-specific — but an
implementation that reached for it *only* in the v6 branch, which is the
plausible mistake, would show up as one red and not two. Two cases can tell
those apart; one cannot.

## The one thing this table does not preserve, and why

`HEAD /nic/update` performs the update. Flask adds `HEAD` to every `GET` route
and serves it by running the handler and discarding the body, so a `HEAD` is a
full DNS write with no body to say so — measured, `Content-Length: 18`, empty
body, `last_ip_v4` moving. **The rewrite refuses it: 405 on `/nic/update` and
`/nic/delete`, 200 preserved on `/nic/checkip`.** The reasoning is in plan §1,
"`HEAD`: refused where the handler is not safe"; the short version is that
preserving it is the *expensive* option (FastAPI's `APIRoute` does not add
`HEAD`, so preserving means writing `methods=["GET", "HEAD"]` on purpose), that
no DynDNS client sends `HEAD`, and that the realistic sender is an uptime
monitor holding the router's credentials — which, measured with no `myip`,
repointed the hostname at the prober's own address.

This is the only wire behaviour in the table the rewrite changes for a reason
other than removing query-parameter auth, and it is expressed with the
mechanism already here: paired `targets: [legacy]` / `targets: [host]` cases,
the legacy half carrying the write in `effects:` and the host half carrying
`legacy_behaviour:`. No new schema, and nothing hidden in prose.

## Behaviours §1 did not state, encoded here

Found by reading `dyndns.py` and `lib/accounts.py` rather than §1's summary of
them. §1 has been corrected for the first two.

- **`/nic/checkip` with an unparseable client address returns an empty body only
  in `?format=plain`.** §1 said "empty string" without qualifying the format. In
  the default HTML mode the wrapper is still emitted with an empty IP — 90
  bytes, not 0. (This read 91 until #9 measured it three ways: `curl` reports
  `Content-Length: 90`, `wc -c` agrees, and `len()` of the table's own
  `expect.body` is 90. The table was always right; only this count was wrong.
  The same 91 survived in a `note:` in `protocol_cases.yaml` and in plan §1's
  own table, neither of which #9 owned; #25 corrected both, so the figure now
  reads 90 in all three places and the readings that produced it are recorded
  beside the case.) Cases `checkip-unparseable-remote-addr-plain` and
  `checkip-unparseable-remote-addr-html`.
- **The response echoes the *normalised* IP, not the spelling the client sent.**
  The body carries `str(ipaddress.ip_address(...))`, so `myip=2001:0DB8:0000::1`
  comes back as `good 2001:db8::1`. A table asserting the request spelling would
  pass against a naive implementation and fail against the legacy one. Cases
  `update-myip-ipv6-is-normalised-in-the-response`,
  `checkip-ipv6-is-normalised`.
- **`hostname=` (present and empty) is `911`, not `notfqdn`.** `not hostnames`
  fires before the regex runs. `hostname=a.example.com,` — a trailing comma — is
  `notfqdn`, because the empty element is inside the list. Cases
  `update-911-empty-hostname`, `update-notfqdn-trailing-comma`.
- **`myip` is validated before the hostname syntax**, so a request that is wrong
  in both ways answers `911`, not `notfqdn`. Case
  `update-911-myip-checked-before-hostname-syntax`.
- **`delete` does not substitute `remote_addr` for a missing `myip`; `update`
  does.** The one real asymmetry between the two handlers. On update, `myip`
  absent *and* no parseable client address gives `911` even for a valid
  registered hostname; on delete the same request succeeds and deletes both
  families. Paired cases `update-911-no-myip-and-no-client-ip` and
  `delete-myip-does-not-default-to-client-ip`.
- **A trailing dot validates but does not resolve.** `isvalidhostname` strips one
  trailing dot for the regex check and then returns the *original* element, so
  the lookup is for `ok.example.com.` and misses. Case
  `update-nohost-trailing-dot`.
- **Hostname lookup is case-insensitive** (`find_hostname` lowercases) but
  validation is not case-folding. Case
  `update-hostname-lookup-is-case-insensitive`.
- **The aggregate error is the first status that is neither `good` nor `nochg`,
  in backend order** — not the last error and not the first error overall. The
  `firsterr.example.com` fixture is ordered `nochg, 911, dnserr` precisely so
  that an implementation returning either of those wrong answers fails.
- **Unrecognised query parameters are ignored.** The doc says they may trigger
  `abuse`. Case `update-unknown-parameters-are-ignored`.
- **`/nic/checkip` is unauthenticated and unrate-limited.** Credentials are not
  consulted even when supplied. Case
  `checkip-ignores-credentials-entirely`.

### Deliberately not asserted

`/nic/checkip` HTML-escapes the IP before interpolating it. **That escape is
structurally unreachable**: the value is `str(ipaddress.ip_address(...))` or the
empty string, and every string that constructor can produce is drawn from
`[0-9a-f.:%]` — none of which `html.escape` touches. Verified across IPv4, IPv6
and a scoped IPv6 literal. A case asserting "the IP is escaped" would print the
same result whether the escaping were present or removed, so it is documented
here instead of being added as a probe that cannot fail.

## Query-parameter auth: deleted, not expected-fail

`?username=&password=` is gone. It was only ever a workaround for router vendors
not in use here, and it is the sole reason the old repo carries its Traefik/Loki
credential-leak apparatus — `queryparameters.defaultmode`, the discovery that
`redact` is accepted by the flag parser and silently behaves as `keep`, the
`_scrub_password_from_environ` before-request hook, and a standing Loki query
that has to stay at zero. Removing the input removes all of it.

**Eleven cases were drafted from the legacy behaviour and deleted.** They are
kept as data in `deleted_cases`, with the legacy behaviour and the reason, so
the deletion is auditable and the table cannot shrink silently. Three
replacement cases run against the **host only**, asserting that the input is
inert rather than merely absent:
`update-query-parameter-credentials-are-rejected`,
`update-query-parameter-credentials-do-not-shadow-basic-auth`,
`delete-query-parameter-credentials-are-rejected`.

HTTP Basic is the only accepted transport for `/nic/*` credentials.

## Counting the table

Two instruments, different shapes. Both must agree, and both belong in any PR
that touches this file.

```bash
# 1. parser — the length of the lists a runner will actually iterate
python -c "import yaml;d=yaml.safe_load(open('tests/compat/protocol_cases.yaml'));\
print(len(d['cases']),len(d['deleted_cases']))"

# 2. grep — counts a field every member of each list has and the other has none of
grep -c '^    expect:' tests/compat/protocol_cases.yaml   # live cases
grep -c '^    reason:' tests/compat/protocol_cases.yaml   # deleted cases
grep -c '^  - id:'     tests/compat/protocol_cases.yaml   # both; must equal the sum
```

The grep instrument counts `expect:` and `reason:` rather than `id:` on purpose:
a case that lost its expectation, or a deleted entry that grew one, shows up as
a disagreement instead of being absorbed. Demonstrated, not assumed — five
mutations, each reverted immediately, run by #7 against the 101-case table; the
deltas are relative and the mechanism is unchanged at 114:

| mutation | caught by |
|---|---|
| M1 delete a whole case | parser 101→100, `expect:` 101→100, ids 112→111 |
| M2 strip one case's `expect:` block | **`expect:` 101→100 only** — the parser still counts 101 cases |
| M3 give a `deleted_cases` entry an `expect:` | `expect:` 101→**102**, and the "deleted with expect" check names it |
| M4 drop the only `covers` of `divergence/3-no-numhost` | uncovered spec rows `[]` → `[divergence/3-no-numhost]` |
| M5 duplicate a case id | duplicate ids `[]` → `[delete-nochg-single-backend]` |

M2 is the one that justifies the design: had both instruments counted `id:`,
a case that quietly lost its expectation would have survived, and a table
counting 101 cases while asserting 100 is exactly the silently-shrunken table
this file exists to prevent.

Readings, each taken on the branch that took them and none of them inherited:

| instrument | live cases | deleted cases | ids | branch |
|---|---|---|---|---|
| PyYAML `len()` | 101 | 11 | — | `7_overnight` |
| `grep -c` | 101 | 11 | 112 | `7_overnight` |
| PyYAML `len()` | **114** | 11 | — | `25_overnight` |
| `grep -c` | **114** | 11 | **125** | `25_overnight` |
| PyYAML `len()` | **131** | 11 | — | `29_overnight` |
| `grep -c` | **131** | 11 | **142** | `29_overnight` |

131 + 11 = 142. Coverage checked from the data: 37 `spec_rows`, **0 uncovered**,
0 `covers` entries naming a row that does not exist, 0 duplicate ids, 0 cases
without an `expect.body`, 0 `deleted_cases` carrying an `expect`. `spec_rows` is
unchanged at 37 across the v3 additions, and that is itself a reading: the
address-family gap was never a missing *row* of §1 — the document says nothing
family-specific — so the 17 new cases map onto rows that already existed.

By endpoint, v3: `/nic/update` 75, `/nic/delete` 41, `/nic/checkip` 15 (v2:
62 / 39 / 13). By method: `GET` 122, `HEAD` 5, `POST` 4 — the 9 non-`GET` cases
are #25's, and before them the table's own divergence 4 ("everything is HTTP
200") was unfalsifiable from inside the table, because every case in it was a
`GET`.

**The delta from 101 is +13, and it is not "three divergences, three cases".**
D7 takes 4 (a legacy/host pair per endpoint, because only the status is shared
between Werkzeug's HTML page and FastAPI's JSON one), D8 takes 1, D9 takes 3
(inert as a fallback, inert beside a parseable `X-Forwarded-For`, and inert on
the endpoint that writes DNS), and the `HEAD` decision takes 5 (a legacy/host
pair for `update` and for `delete`, plus one shared case pinning that `HEAD` on
`checkip` is *kept*). #9's estimate was 2 + 1 + 0 = 3.

### Calibration against the legacy code

31 of the 101 cases — every `checkip` case and every `update`/`delete` case that
resolves before the handler touches the database — were replayed directly
against the legacy `BaseAccount.getip`, `BaseAccount.getiptype` and
`BaseAccount.isvalidhostname`, imported from
`/Users/brendan/src/dyndns-route53` at `main`. **31 replayed, 31 agree, 0
disagree.** The remaining 70 need an authenticated user, a rate-limit state or a
DNS backend, and are calibrated when the runner lands (plan §1, "How the suite
is built").

That is a partial calibration and is reported as such: it exercises the
validation layer, not the resolution or aggregation layers.

---

<!-- BEGIN legacy_behaviour (issue #10) — keep edits inside this block -->

# Model behaviour: `legacy_behaviour/`

`protocol_cases.yaml` owns the **wire**. `legacy_behaviour/` owns the **model** —
everything about the legacy service that survives the rewrite and that a runner
pointed at a base URL cannot see.

The two never assert the same thing, and that is enforced rather than intended:
`test_legacy_behaviour.py` fails if a model case's `then` reduces to a DynDNS
status token, if a model case grows any of the wire table's schema
(`expect`, `path`, `query`, `auth`, `client_ip`, `headers`, `targets`), or if a
case id collides with one in `protocol_cases.yaml`.

| file | what it is |
|---|---|
| `model_cases.yaml` | 81 model-behaviour rules. Data only, same discipline as the wire table |
| `legacy_inventory.yaml` | **the drop list.** All 147 legacy tests, each with a disposition |
| `test_legacy_behaviour.py` | 18 guards on those two files |
| `calibrate_against_legacy.py` | replays the rules against the legacy implementation. Not a pytest module — see below |

## The drop list

Every test in `dyndns-route53`'s suite appears in `legacy_inventory.yaml`
exactly once, with either `disposition: ported` and the model cases it became,
or `disposition: dropped` and one of three reasons. **A silent drop is
indistinguishable from an oversight**, so the file is the record that each of
the 147 decisions was taken.

| | | |
|---|---:|---|
| **ported** | 67 | became one or more model cases |
| dropped — `atrium-owns` | 34 | auth, sessions, TOTP, user CRUD, roles, boot-time admin. Atrium ships all of it |
| dropped — `flask-internal` | 27 | a template, a redirect, a flash message, a static file, or a form POST whose only surviving content is "a row was inserted" |
| dropped — `superseded-by-table` | 19 | already owned, byte-for-byte and more thoroughly, by `protocol_cases.yaml` |
| **total** | **147** | |

The dispositions must partition the suite and the total must equal an
independently measured count of it; three guards assert exactly that, and a
fourth re-derives the count from the legacy checkout when it is present.

`superseded-by-table` is the reason that could most easily be a lie, so it is
checked against the thing it points at: the wire table must be non-empty and
must cover all three endpoints, or the drop reason means nothing.

## Counting the legacy suite

Two instruments, different shapes. Both belong in any PR that touches these
files.

**Neither reading is 146.** The issue body says `test_app.py` is
"1,275 lines / ~146 tests" and plan §1 says "the old repo's 146 pytest tests".
1,275 lines is right; 146 is not a count of anything. `test_app.py` holds 121,
and 147 is the two files together. `docs/ops/refactor-plan.md` is outside this
issue's declared file list, so the number there is flagged rather than edited.

```bash
L=/path/to/dyndns-route53

# 1. the collector — what pytest would actually run
(cd $L && .venv/bin/python -m pytest tests/test_app.py tests/test_hetzner.py \
    --collect-only -q | tail -1)

# 2. grep — a different shape, and blind to collection rules
grep -cE '^\s*def test_' $L/tests/test_app.py $L/tests/test_hetzner.py
```

Readings taken 2026-08-15 on `10_overnight`:

| instrument | `test_app.py` | `test_hetzner.py` | total |
|---|---:|---:|---:|
| `pytest --collect-only` | 121 | 26 | **147** |
| `grep -cE '^\s*def test_'` | 121 | 26 | **147** |

They agree exactly, and the slack is worth stating: both count *declarations*,
so a suite using `@pytest.mark.parametrize` would make them disagree — this one
does not, which is why the agreement is exact rather than approximate.
67 + 34 + 27 + 19 = 147.

The third instrument is in the guards: `_ast_count` walks the legacy modules and
compares against the frozen number, and **skips with a reason** when the checkout
is absent rather than passing vacuously.

## Model behaviour vs the wire table

Where the boundary sits, in the cases where it is not obvious:

- **`update-911-hostname-with-zero-backends`** (table) owns the status string.
  `backends-a-domain-with-no-backends-resolves-to-none` (here) owns the resolver
  returning `[]`. The inventory row carries `wire_half` naming the first, so the
  split is visible from either side.
- **The table's aggregate cases assume a stable backend order** — its
  `firsterr.example.com` fixture is ordered `nochg, 911, dnserr` — and cannot
  assert one. `backends-resolution-order-decides-the-aggregate-error` is that
  assumption made explicit.
- **The table stubs the provider.** Its `stub` service returns `good` / `nochg` /
  `dnserr` because the fixture says so. Nothing in it proves a *real* adapter
  converts a raised exception into `dnserr` rather than a 500 — and a 500 breaks
  every client that parses the body. The whole `provider` subject exists to close
  that gap.
- **`update-nochg-single-backend`** records `last_updated_at: unchanged` for one
  request. `tracked-ip-moves-only-on-good-so-last-updated-at-is-not-a-liveness-signal`
  is what that means over time.
- **checkip is entirely the table's.** The issue listed "`checkip` formatting" as
  something to port; on inspection all four legacy checkip tests are strictly
  weaker than the table's eight cases, so all four are dropped
  `superseded-by-table` and nothing is ported. Overturning that clause is
  recorded here rather than fixed silently.

## Defects and planned changes, recorded rather than inherited

`preserve` is not the default. Every case carries a disposition — `preserve`,
`fix` (a legacy defect) or `change` (a plan decision) — and the non-preserve
ones each record what the legacy does (`legacy`) and what must replace it
(`host`), so the difference stays a decision rather than becoming a drift.
A `fix` or `change` case missing either half fails a guard.

The split is in the data, not counted here — read it with
`yaml.safe_load(...)` and `collections.Counter`, so it cannot go stale in prose.
The ones worth naming:

- `hostname-names-must-be-stored-lowercase` — the lookup lowercases the query and
  nothing lowercases the row, so a row stored with a capital letter is
  unreachable under **every** spelling. Replayed: both answer `nohost`.
- `event-a-rate-limited-delete-is-logged-as-dns_update` — the delete handler's
  rate-limit branch passes the literal `'dns_update'`. Invisible on the wire; the
  cost is that "show me this device's refused deletes" silently returns nothing.
- `event-ip-address-on-delete-is-the-raw-myip-parameter` — update logs
  `2001:db8::1`, delete logs `2001:0DB8:0000::1`, from the same request. The one
  endpoint where normalisation cannot be seen from outside (delete responses
  carry no IP suffix, divergence 1) is the one that skips it.
- `event-911-and-notfqdn-write-no-row` — a router sending a malformed hostname
  is refused on every attempt and appears in the log as complete silence,
  indistinguishable from a router that stopped calling. This is the support
  case the old service could not answer.
- `provider-must-not-fall-back-to-environment-credentials` — the legacy test
  asserts the fallback *works*; plan §2 removes it, so the assertion is ported
  inverted. Under multi-tenancy the fallback is a cross-tenant credential leak:
  one operator-set env var would serve every tenant whose row happens to be
  empty. The table's `update-911-backend-without-stored-credentials` would keep
  passing on any machine where the env happened to be unset.

One is flagged as a **seam**: `event-badauth-writes-no-row`. No V1M1 issue owns
"a failed `/nic/*` authentication is recorded somewhere", and today a device
secret being brute-forced leaves no trace in either store.

## Calibration — the second instrument

`model_cases.yaml` is one reading, authored by reading the legacy source.
`calibrate_against_legacy.py` is the other: it stands the legacy Flask app up on
throwaway SQLite databases and executes each rule against it.

```bash
DYNDNS_LEGACY_ROOT=/path/to/dyndns-route53 \
  /path/to/dyndns-route53/.venv/bin/python \
  tests/compat/legacy_behaviour/calibrate_against_legacy.py
```

It is deliberately **not** a pytest module: it needs the legacy checkout and that
repo's own virtualenv, neither of which exists in the api container. It refuses
to run rather than silently measuring nothing when the checkout is missing, and
it exits non-zero if any reading disagrees, if it replays an id the table does
not carry, or if the table claims `calibrated: agree` for a case it never
touched.

Reading taken 2026-08-15: **71 readings, 71 agree, 0 disagree**, covering 70 of
the 81 cases. The remaining 11 are marked `derived` (read off a schema, a form
validator or a call site) or `not-replayable`, and are labelled as such in the
data rather than counted as calibrated.

## Running the guards

```bash
uv venv /tmp/compat-venv && VIRTUAL_ENV=/tmp/compat-venv uv pip install pyyaml pytest
/tmp/compat-venv/bin/python -m pytest tests/compat/legacy_behaviour -q
```

18 passed, ~0.1s. Same two dependencies as the wire runner, and no others.

**`--confcutdir` used to be needed here and no longer is (#23).**
`tests/compat/conftest.py` raised `UsageError` from `pytest_configure` when
`--target` was absent, and `pytest_configure` runs whenever that conftest is
loaded — which is any run rooted at or under `tests/compat/`. So
`pytest tests/compat/legacy_behaviour` failed with *"--target is required"*
before collecting anything, and so did `pytest tests/`. These guards have no
target: they read two YAML files and never open a socket, and passing
`--target host` to satisfy the parser would have asserted something untrue about
what was being run.

The requirement now fires at **collection of the wire cases** instead. Without
`--target`, `test_case` is collected as one skip named
`NO-TARGET-GIVEN-WIRE-TABLE-NOT-RUN`, every service-free guard runs, and the
session prints *"the wire table was NOT RUN (0 of 131 cases executed)"* rather
than a zeroed accounting block that reads like a clean run. What did **not**
change: no wire case can execute without an explicit target, and a `--base-url`
with no `--target` is still refused outright with exit 4 — that one is not a
sibling suite being swept up, it is a service nobody named.
`test_runner_contract.py` asserts all four readings by running pytest as a
subprocess, because a runner cannot assert its own exit code.

Going the other way, `pytest tests/compat --target host` collects these 18
alongside the wire runner's **136** (127 cases + 9 guards) and the 6 contract
tests, for **160** — measured on `29_overnight`. The runner's own accounting
block is unaffected — it reports 131 cases, 127 selected, 3 unmet
preconditions — because it counts the table, not the session. (The same numbers
read 108 / 126 and 101 / 11 at #10, 108 / 132 at #23, 141 with 110 cases + 7
guards at #25, and 114 cases at #11's freeze. The guard count went 6 → 7 when
#11 added the freeze guard and 7 → 8 when #29 added the version guard;
`test_target_selection_partitions_the_table` is parametrised per target, so
eight functions present as **nine** nodes, with a target and without.)

## In the gate, and in CI

**Both halves of `tests/compat/` are in the gate as of #23** — the service-free
half unconditionally, the wire table not at all. That is still true. The wire
table's automation is CI's, not the gate's, and #130 changed only the CI half.

| | runs | how |
|---|---|---|
| model guards + runner guards + runner contract | every `make test-backend`, so every gate run | `tests/` is COPYed into the Dockerfile's `dev` stage at `/opt/compat_tests`; `make test-backend` runs a second pytest session over it |
| the wire table, `--target host` | CI's `compat-wire-table` job — PRs into `master` and pushes to it. **Never in the local gate** | that job raises a stack with `ATRIUM_DDNS_COMPAT_STUB=1`, runs `make seed-compat-fixture`, then `make test-compat TARGET=host BASE_URL=http://api:8000` followed by `make check-compat-executed` |
| the wire table, `--target legacy` | never automatically | `make test-compat TARGET=legacy BASE_URL=<url>` — both required, neither defaulted, and the legacy service has to be stood up by hand (§ *Standing the legacy service up*) |

**The host half stopped being "never automatically" at #130, and the gate did
not change.** Those are two claims and it is worth keeping them apart. The
table is still outside `make test` and outside the local gate, for the reason
below; what #130 added is a CI job that raises the service the table needs and
then names it on the command line, which is the only way this table was ever
meant to run. Before it, the frozen wire contract — the one artefact this
repository exists to preserve — was checked only when somebody remembered to
type the command.

**Why that job has a second step.** `make test-compat` answers *did a case
fail*. It does not answer *did a case run*, and the two come apart: with
`PYTEST_ARGS='-k checkip-default-is-html'` the target exits **0** having
executed 1 of the 127 selected cases, printing `NOT RUN 126` in an accounting
block nothing was reading. `make check-compat-executed LOG=<file>` reads that
block back and refuses four ways — the freshness guard's PASS line absent, any
`NOT RUN`, an unreadable block (which is *not* nought and *not* a pass), or
fewer cases passed than the block calls executable. Its invariants are
relational, so re-freezing the table at a different size cannot make it stale.

**There is no `moto` here, and #130 is where that was decided.** That issue
asked for a `moto`-backed Route 53 on loopback on the premise that the host
cases cannot run without one. The premise is wrong: this file's `fixture:`
block asks for `service: stub` on every backend it declares, and a
case-insensitive grep for `route53`, `aws` or `boto` across
`protocol_cases.yaml` matches exactly one line — the upstream repository's
name, in a comment. The provider under test on this path is
`atrium_ddns.compat_stub`. Measured on `130_overnight` with no AWS credentials
in the container, no `AWS_ENDPOINT_URL`, and boto3 resolving to the real
`https://route53.amazonaws.com`: **124 of 124 executable cases green**. A moto
server in that job would receive no request from any of the 131 cases, and a
service nothing reaches cannot go red — the "probe that could not fail" of
`docs/ops/overnight-template.md`, wearing a dependency pin. (For the record:
`AWS_ENDPOINT_URL` *does* redirect the client on this image — botocore 1.43.85
resolves `http://127.0.0.1:5053` from it despite `route53.py`'s `make_client`
passing no `endpoint_url`. The mechanism works. Nothing on this path needs it.)

`make test-compat` is deliberately outside `make test` and outside the gate: it
needs a live service, and which service it is has to be stated. Giving either
option a default to make the target "work" is the bug #8 was written about.
`BASE_URL` is resolved **from inside the api container** — `http://api:8000` is
the compose stack, a legacy service on the dev box is
`http://host.docker.internal:<port>` on Docker Desktop. Nothing infers that;
the runner prints the reachability probe it got back.

**`make up` does not rebuild, so an edited case file is invisible to the
container.** `make check-compat-fresh` (a prerequisite of `make test-backend`)
digests `tests/` in the worktree and inside the running container and refuses
when they differ, naming both hashes and the fix (`make build && make up`).
Without it the gate reads the image's older copy and reports green for it,
which is the same defect `compose.yaml`'s `image:` comment records for a shared
tag, one layer along.

## Prove the guards bite

Thirteen mutations, each reverted immediately. **13 applied, 13 caught, 0
survived.**

| mutation | caught by |
|---|---|
| M1 delete one dropped row from the inventory | partition total, per-file count, AST cross-check (3) |
| M2 invent a fourth drop reason | vocabulary check, declared counts (2) |
| M3 move one drop between reasons, total intact | declared counts |
| M4 rename a model case without updating the inventory | 4 cross-reference guards |
| M5 strip a case's `source` while a ported row still names it | **back-citation guard only** |
| M6 a model case whose `then` asserts a wire status | wire-assertion guard |
| M7 a model case carrying `expect:` / `path:` | wire-schema guard |
| M8 the frozen legacy count drifts below the real suite | AST cross-check + 2 |
| M9 rewrite one family-sensitive subject as IPv4-only | IPv6 coverage guard |
| M10 a ported row that names no case | 5 guards |
| M11 a `wire_half` naming a protocol case that does not exist | wire_half resolver |
| M12 a `fix` case that no longer says what replaces the defect | case well-formedness |
| M13 a case claiming `calibrated: agree` it never earned | the calibration script's own cross-check |

**M5 is the one that justifies its guard.** The first version of this suite
checked only inventory → case. A case that quietly dropped its `source` left the
inventory still reading as though the test had been ported — the port becoming a
claim rather than a link — and nothing failed. The back-citation guard was added
mid-issue because M5 survived without it.

**M13 was found by the instrument, not by a mutation.** The calibration script's
id cross-check was added after the harness and the table had already drifted
apart on seven case ids: readings taken against names the table did not carry,
reported as coverage. Same defect family as a probe measuring something the
record does not contain.

## What these guards do not do

They do **not** assert model behaviour. There is no host code to assert it
against — `backend/src/atrium_ddns/models.py` is still the scaffold's demo table.
They assert that the *record* of that behaviour cannot decay silently, which is
a smaller claim and the honest one. When the host models land, the 81 cases
become the specification the new tests are written from, and
`calibrate_against_legacy.py` gains a `--target host` sibling.

<!-- END legacy_behaviour (issue #10) -->
