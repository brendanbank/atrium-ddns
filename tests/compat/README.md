# DynDNS v2 compatibility case table

`protocol_cases.yaml` is the frozen wire contract for `/nic/checkip`,
`/nic/update` and `/nic/delete`. It is **data**: no Python, no expressions, no
imports. A runner loads it, replays `cases` against a base URL and compares the
response with `expect`.

The source of truth is the **behaviour of `brendanbank/dyndns-route53`'s
`dyndns.py` at `main`** (commit `ec605c5`), read directly, together with
`auth.py`, `rate_limiter.py` and `lib/accounts.py`. It is **not**
`docs/DYNDNS-PROTOCOL.md` in that repo, which documents the de-facto standard
the implementation deliberately departs from in six places. Where the two
disagree, the implementation wins and the case carries a `divergence/*` tag.

Routers in the field — OPNsense, ddclient, inadyn, Fritz!Box — are the users,
and they cannot be asked to change. A case that asserts the document instead of
the behaviour is a bug in this file.

## Layout

| key | what it is |
|---|---|
| `spec_rows` | every row of `docs/ops/refactor-plan.md` §1's three tables, plus the six divergences, given an id |
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

## Running the table

`conftest.py` + `test_protocol.py` are the runner. One test per case, named
after the case, so a failure names the behaviour rather than an index.

```bash
python -m venv .venv && .venv/bin/pip install pytest pyyaml

.venv/bin/python -m pytest tests/compat --target legacy --base-url http://127.0.0.1:5000
.venv/bin/python -m pytest tests/compat --target host   --base-url http://localhost:8153
```

`pytest` and `PyYAML` are the only dependencies. The wire is spoken with
stdlib `http.client`, not requests/httpx, because this suite asserts exact
bytes and exact headers: `http.client` adds only `Host` and
`Accept-Encoding: identity`, so every other header on the wire is one the
runner put there and `omit_headers: [user-agent]` genuinely omits it.

### The two options, and why neither has a default

| option | required | notes |
|---|---|---|
| `--target legacy\|host` | **always** | decides which cases run. No default: a runner that picks one will be pointed at the wrong service eventually |
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

A real block, from `--target host` against the local stack on `8_overnight`
(abridged only in the id lists). It is red, because the host has no `/nic/*`
yet — an invented green example here would be the first thing to rot:

```
compat accounting (target=host):
  cases in table             101
  excluded by `targets:`       0  []
  selected for this target   101
  unmet preconditions          3  ['update-abuse-rate-limited', ...]
  executable                  98
  wire-only                   11  cases carrying `effects:` ... that this runner does NOT assert
  ran this session           101  98 failed, 3 skipped
  reachability probe        GET http://localhost:8153/nic/checkip -> HTTP 200
```

`selected` is what the table offers; `ran this session` is what pytest actually
executed, counted from its own report objects. They are two numbers on purpose
— a `-k` filter or a collection that stopped early shows up as a `NOT RUN`
line rather than as a run that says 98 and did twelve.

Six guards (seven tests — the partition one runs per target) need no service
and fail loudly rather than letting the run shrink:

| guard | fails when |
|---|---|
| `test_every_case_declares_known_targets` | a `targets:` typo removes a case from every run without removing it from the table |
| `test_target_selection_partitions_the_table` | selected + excluded stops equalling the table |
| `test_deleted_cases_are_never_executed` | a `deleted_cases` entry grows an `expect`, or an id appears in both lists |
| `test_no_url_shape_inference` | a loopback literal appears in `conftest.py` — comments included |
| `test_request_building_cannot_depend_on_the_target` | `build_request` grows a `target` parameter, i.e. a per-target request |
| `test_every_case_builds_a_request` | any case in the table, either target, stops turning into a request |

### Collected-case readings

Taken on `8_overnight`, not inherited. Two instruments, and the runner's own
collection is the second one:

| instrument | `--target legacy` | `--target host` |
|---|---|---|
| `pytest --collect-only`, `test_case[...]` nodes | 98 | 101 |
| PyYAML over `cases`, filtered on `targets` | 98 | 101 |

101 in the table, 3 host-only, 98 shared; 11 `deleted_cases`, 0 of them
collected. `grep -c '^  - id:'` = 112 = 101 + 11.

### No credential reaches this output that was not already committed

The runner has exactly one credential source: the `fixture:` block of the case
table — no environment variable, no file, no prompt. Basic passwords are
withheld twice over in the failure report, once in the `auth:` line and once in
the `Authorization:` header (base64 is not encryption). The three
query-parameter cases *do* print their credentials in the request line, because
the query string is the thing they assert; those values are synthetic and
already committed a few hundred lines above.

## The six divergences from the protocol document

§1 of the plan listed five. This issue found a sixth by reading the regex. All
six are **preserve**.

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

Divergence 6 is preserved for divergence 5's reason: both spellings already end
at `nohost`, so preserving costs nothing, while "fixing" it would turn a
per-hostname `nohost` line into a whole-request `notfqdn` — a behaviour change
for a case nobody hits.

## Behaviours §1 did not state, encoded here

Found by reading `dyndns.py` and `lib/accounts.py` rather than §1's summary of
them. §1 has been corrected for the first two.

- **`/nic/checkip` with an unparseable client address returns an empty body only
  in `?format=plain`.** §1 said "empty string" without qualifying the format. In
  the default HTML mode the wrapper is still emitted with an empty IP — 91
  bytes, not 0. Cases `checkip-unparseable-remote-addr-plain` and
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
mutations, each reverted immediately, against the readings above:

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

Readings taken when this file was written, on `7_overnight`:

| instrument | live cases | deleted cases | ids |
|---|---|---|---|
| PyYAML `len()` | 101 | 11 | — |
| `grep -c` | 101 | 11 | 112 |

101 + 11 = 112. Coverage checked from the data: 32 `spec_rows`, **0 uncovered**,
0 `covers` entries naming a row that does not exist, 0 duplicate ids, 0 cases
without an `expect.body`, 0 `deleted_cases` carrying an `expect`.

By endpoint: `/nic/update` 56, `/nic/delete` 37, `/nic/checkip` 8.

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
/tmp/compat-venv/bin/python -m pytest tests/compat/legacy_behaviour -q \
    --confcutdir=tests/compat/legacy_behaviour
```

18 passed, ~0.1s. Same two dependencies as the wire runner, and no others.

**`--confcutdir` is not decoration, and it should not be needed.**
`tests/compat/conftest.py` raises `UsageError` from `pytest_configure` when
`--target` is absent, and `pytest_configure` runs whenever that conftest is
loaded — which is any run rooted at or under `tests/compat/`. So
`pytest tests/compat/legacy_behaviour` fails with *"--target is required"*
before collecting anything, and so would `pytest tests/`. These guards have no
target: they read two YAML files and never open a socket, and passing
`--target host` to satisfy the parser would be asserting something untrue about
what is being run.

`--confcutdir` stops pytest walking up to that conftest at all, which is why it
works. It is a workaround, and it is recorded here rather than smoothed over:
the durable fix is for the `--target` check to fire when a protocol case is
*collected* rather than at configure time, so a sibling directory under
`tests/compat/` can be run on its own. Flagged for #8 rather than edited here —
`conftest.py` is that issue's declared file.

Going the other way, `pytest tests/compat --target host` now collects these 18
alongside the wire runner's 108, for 126. The runner's own accounting block is
unaffected — it still reports 101 selected cases, 3 unmet preconditions, 11
wire-only — because it counts the table, not the session.

**Neither half of `tests/compat/` is in the milestone gate.** The Dockerfile
copies `backend` to `/opt/host_app` and nothing else, so `make test-backend`
(`pytest /opt/host_app/tests`) cannot reach this directory. The wire runner, the
wire table and these guards are all outside it. That is a real gap in the gate
and no V1M1 issue owns closing it.

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
