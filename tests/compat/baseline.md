# Baseline: the case table against the legacy service, and against the host

Four readings, kept side by side. The first three are `--target legacy`; the
fourth is the same frozen table pointed at **this repository's own service**,
which is half of V1M2's exit criterion.

| | issue #9, the 101-case table | issue #11, the frozen 114-case table | issue #29, the 131-case table at v3 | **issue #18, v3 vs `--target host`** |
|---|---|---|---|---|
| target | legacy | legacy | legacy | **host** |
| selected for the target | 98 | 107 | 124 | **127** |
| executed by the runner | 95 | 104 | 121 | **124** |
| measured out of band | 3 | 3 | 3 | **3** |
| agree with the table | 98 | 107 | 124 | **124** |
| diverge from the table | **0** | **0** | **0** | **0** |
| divergences from `docs/DYNDNS-PROTOCOL.md` §1 | 9 (6 carried + 3 found) | 9, all carried, all confirmed live | 9, unchanged | *not re-derived — §8 measures the host, not the document* |

The two target columns do not have the same denominator and must not be read as
if they did: 4 cases are legacy-only (`targets: [legacy]`) and 7 are host-only,
so 124 legacy-selected and 127 host-selected share 120. §8.3 runs the
*legacy*-selected table against the host to say exactly how far that partition
is a real difference rather than a convenience.

**§8 is the current reading for the host and §7 for the legacy service.** §7 was
taken by #29 on a third independently built fixture when the table went from v2
to v3 for address-family coverage; §0 is #11's V1M1 close-out re-run at v2, and
§§1–6 are #9's. All are kept because the mutation work and the failure modes in
them are not superseded by a later green run. Nothing in §7 or §8 is inherited:
every number in each, including the "before" figures it improves on, was
re-derived with that issue's own instruments, and where one disagrees with what
was already written down the disagreement is stated rather than reconciled
(§7.7, §8.5).

---

## 0. Re-run at close-out — issue #11, 2026-08-15

**Nothing here is inherited.** The close-out rule is to re-run rather than quote
the reading on file, so this issue built its own throwaway legacy instance, its
own fixture, and its own second instrument, and re-derived every number below.
Where a number disagrees with what the milestone had been claiming, the
disagreement is stated rather than reconciled.

### 0.1 The verdict, in the exit criterion's own words

> **107 cases were run against the legacy service — 104 by the runner and 3 out
> of band — 107 agree and 0 diverge from the table. Exactly 9 behaviours
> diverge from `docs/DYNDNS-PROTOCOL.md` §1, enumerated below and each
> confirmed live. The table is frozen at version 2.**

A clause-by-clause re-walk of §1 — done from the document, not from the table —
found **no tenth**. That is the negative result the milestone exists to
produce: nine is a count, not a collection of anecdotes.

### 0.2 The service under test

A throwaway local copy. Never the deployed one: the table registers hostnames,
exercises `badauth`, and — in §0.6 — deliberately exhausts a user's rate limit.

```
legacy checkout HEAD  = 5d1c941fe31ea41175cb0a75849367dc94871d18
legacy checkout dirty = 0 file(s) modified

same  auth.py                      sha=4d6c4add8801
same  config.py                    sha=2cf2d5a884da
same  dyndns.py                    sha=b6a608e7a213
same  forms.py                     sha=fb0f65c6afd4
same  getpwd.py                    sha=6421cbacefed
same  health_checker.py            sha=8b421835eb91
same  lib/__init__.py              sha=4974036dcccd
same  lib/account/__init__.py      sha=5bdd5bb48253
same  lib/account/aws.py           sha=5055cd6e7748
same  lib/account/hetzner.py       sha=6cd7612a98e0
same  lib/account/nsupdate.py      sha=fc148aeb2871
same  lib/accounts.py              sha=15831a998ebb
same  lib/log.py                   sha=7bf57ded827c
same  models.py                    sha=4de73398b22f
same  rate_limiter.py              sha=28c606bf3970
same  web_routes.py                sha=bc52a6d6101a
ADDED  lib/account/stub.py          (harness, not legacy source)

integrity: 16 legacy .py files compared, 0 differ
```

**Every digest matches the ones #9 recorded**, independently recomputed here —
which is the reading that says the legacy source has not moved under two
milestones' worth of measurement, and the only instrument that would have seen
it if it had. (#11 compares 16 files to #9's 15; `getpwd.py` is the extra, and
it is on no request path.)

The fixture is **derived from the table's own `fixture:` block** rather than
written beside it, so a change to the table's world is a change to the seeded
world or a loud failure. Seeded and served in **one process**, per the trap in
`README.md`: the database directory is deleted and rebuilt at the top of the
script and the listener starts at the bottom of it, so the listener's start
time *is* the seed time and there is no second process to go stale on the port.

`hn.get_backends()` was printed per hostname after seeding, because
`hostname_backends` has no ordering column and the aggregate cases depend on
the order:

```
  ok  firsterr.example.com   get_backends()=['stub-nochg-a', 'stub-nocreds', 'stub-dnserr']
  ok  mixed.example.com      get_backends()=['stub-dnserr', 'stub-good']
  ok  allnochg.example.com   get_backends()=['stub-nochg-a', 'stub-nochg-b']
```

All twelve matched their declared order. The `DomainBackend` rows are created
in a topological sort of the per-hostname sequences, not in the fixture's own
listing order — the two differ (`dnserr` is listed before `no-credentials` and
`firsterr` needs the opposite), and creating them in listing order would have
produced `dnserr` where the table expects `911`. The sort refuses rather than
guesses if the sequences ever conflict.

**And the probe that would have caught the #9 disaster, run before anything
else:** `GET /nic/checkip` answers 200 without touching the database, so it says
nothing about the fixture. `GET /nic/update?hostname=ok.example.com&myip=…`
answering `good 203.0.113.10` — not `nohost` — is the one that does.

### 0.3 The run, instrument 1: the pytest runner

```
compat accounting (target=legacy):
  cases in table             114
  excluded by `targets:`       7  ['checkip-post-is-405-on-the-host', 'update-post-is-405-on-the-host',
                                   'update-head-is-refused-by-the-host',
                                   'update-query-parameter-credentials-are-rejected',
                                   'update-query-parameter-credentials-do-not-shadow-basic-auth',
                                   'delete-head-is-refused-by-the-host',
                                   'delete-query-parameter-credentials-are-rejected']
  selected for this target   107
  unmet preconditions          3  ['update-abuse-rate-limited', 'update-abuse-precedes-911',
                                   'delete-abuse-rate-limited']
  executable                 104
  wire-only                   15  cases carrying `effects:` (DNS ops / persisted columns)
                                  that this runner does NOT assert
  ran this session           107  104 passed, 3 skipped
  reachability probe        GET http://127.0.0.1:5211/nic/checkip -> HTTP 200
135 passed, 3 skipped in 14.89s
```

135 = 104 cases + 7 runner guards + 18 model guards + 6 contract tests.

### 0.4 Instrument 2: `curl`, and a raw socket for `HEAD`

Does not import `conftest.py`. It reads the YAML itself, builds its own
requests, and shells out to a different HTTP client.

```
curl replay (target=legacy) against http://127.0.0.1:5211
  executed            104
  agree with table    104
  diverge from table  0
  not executable      3  (rate_limited: true -- a fixture precondition, not a request)
```

| | pytest runner (`http.client`) | `curl` + raw socket |
|---|---|---|
| selected for `--target legacy` | 107 | 107 |
| executed | 104 | 104 |
| agree with the table | **104** | **104** |
| diverge from the table | **0** | **0** |

**Where the two are not independent, stated rather than hidden.** Both leave
`,` and `:` unencoded in the query string and percent-encode everything else,
because that is what a router in the field puts on the wire. If that convention
is wrong, both instruments are wrong together. Nothing else is shared.

**And the second instrument was wrong on its first run, which is why it is
worth having.** It reported 3 divergences — all three `HEAD` cases — because
`curl --head` writes the header block to `-o` as well as to `-D`, so it cannot
answer "were there any body bytes on the wire". Replaced with a raw socket that
reads every byte back and splits on `\r\n\r\n`. An instrument that agrees with
the first one on its first attempt is one that has not been checked.

### 0.5 Five mutations, five predictions, five exact matches

Each applied to a **fresh copy** on its own port and database; the copy is
deleted afterwards rather than reverted, because there is no revert to get
wrong. The red set was predicted before each run, and each run re-ran the whole
107 rather than the cases expected to move.

| mutation | predicted red | actual red |
|---|---|---|
| **MU1** `$` → `\Z` in the label regex | 2 | **2** — `update-nohost-trailing-newline-in-label`, `delete-nohost-trailing-newline-in-label` |
| **MU2** `httpReply` aborts 405 on `HEAD` | 2 | **2** — the two write-side `HEAD` cases; `checkip-head-…` stayed green |
| **MU3** `offline=YES` answers `!donator` | 2 | **2** — the D8 case **and** `update-unknown-parameters-are-ignored` |
| **MU4** `checkip` prefers `Client-IP` over `remote_addr` | 2 | **2** — both `checkip-client-ip-*` cases |
| **MU5** `nohost {ip}` → `nohost` on update | 9 | **9** — and the two that were predicted to *stay green* did |

**MU5 is the one worth reading twice.** It removes a **suffix**, which a
whitespace-stripping comparison cannot see, and its prediction was not "every
case whose body contains `nohost`". `update-nohost-hostname-outside-backend-zone`
and `update-multi-hostname-mixed-statuses-in-order` carry a `nohost` line that
comes from the *aggregate* path, which keeps its suffix, so both were predicted
to stay green and both did. A prediction that only names the reds is half a
prediction.

**MU2 is the one that argues for the `HEAD` split.** It refuses `HEAD` inside
`httpReply`, which every `/nic/*` reply goes through **except** the `checkip`
HTML path, which builds its own response — so `checkip-head-returns-headers-and-no-body`
stayed green while the two write-side cases went red. That is "refuse `HEAD`
where the handler is not safe, keep it where it is", demonstrated.

### 0.6 The three cases the runner refuses, measured — and shown to be measured

`rate_limited: true` is fixture state, not a request. A second instance was
stood up with the users' limits dropped to **1 request per minute**, one request
was spent, and the three cases were replayed:

```
warm-up (spends the one allowed request): good 203.0.113.10

curl replay (target=legacy) against http://127.0.0.1:5212   [limit = 1/min]
  executed            3
  agree with table    3      <- all three answer `abuse`
  diverge from table  0
```

**A probe that can only print `abuse` is not a probe**, so the same three
requests were replayed against the *unlimited* instance, where they must not
answer `abuse`:

```
curl replay (target=legacy) against http://127.0.0.1:5211   [limit = 100000/min]
DIVERGE update-abuse-rate-limited     body b'good 203.0.113.10' != b'abuse'
DIVERGE update-abuse-precedes-911     body b'911'               != b'abuse'
DIVERGE delete-abuse-rate-limited     body b'good'              != b'abuse'
  agree with table    0
  diverge from table  3
```

3 red on an unlimited instance and 3 green on a limited one is what makes the
green a measurement of the rate limiter rather than of the string `abuse`.

So the honest total is **107 of 107 selected cases measured, 107 agree, 0
diverge** — 104 by the runner, 3 by a fixture manipulation the runner is right
to refuse.

### 0.7 Reproduced on a fixture built a second time

Torn down — database files deleted, not truncated — reseeded from scratch on a
second copy and a second port: `104 passed, 3 skipped, 0 failed`, same
accounting block. The `Allow`-header finding in §0.9 was found *because* there
were two instances, which is a second reason to build the fixture twice.

### 0.8 The nine divergences, re-confirmed live

25 cases carry a `divergence/*` tag; 23 of them are legacy-selected (the other
two are the `-on-the-host` halves of D7). All 23 were replayed with the `curl`
instrument: **23 executed, 23 agree, 0 diverge.**

D7, D8 and D9 were then measured *directly*, from the document's wording rather
than from the table, because a case written from a measurement and asserted
against the same service is one instrument twice:

```
D7 -- §1.3 "Other HTTP methods ... trigger a badagent return code"
  POST /nic/update    -> 405 text/html; charset=utf-8 153B
  PUT /nic/update     -> 405 text/html; charset=utf-8 153B
  DELETE /nic/update  -> 405 text/html; charset=utf-8 153B
  PATCH /nic/update   -> 405 text/html; charset=utf-8 153B
  POST /nic/checkip   -> 405 text/html; charset=utf-8 153B
  OPTIONS /nic/update -> 200 0B, Allow: HEAD, GET, OPTIONS

D8 -- §1.5 offline, §1.6 !donator
  offline=YES        -> good 203.0.113.10
  offline=NO         -> good 203.0.113.10
  no offline at all  -> good 203.0.113.10

D9 -- §1.7 "If the client sends X-Forwarded-For or Client-IP ..."
  Client-IP only, no X-Forwarded-For  -> 127.0.0.1      (the socket address)
  X-Forwarded-For only                -> 198.51.100.5
  both, disagreeing                   -> 203.0.113.10   (X-Forwarded-For wins)
  neither                             -> 127.0.0.1
```

The **return-code writer census** is the second instrument on D8, and on
divergences 2 and 3. Counted across `dyndns.py`, `lib/accounts.py` and every
provider:

| code | good | nochg | badauth | notfqdn | nohost | numhost | abuse | badagent | !donator | dnserr | 911 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| occurrences | 17 | 17 | 2 | 2 | 8 | **0** | 4 | **0** | **0** | 23 | 18 |
| reaching a client | 2 | 2 | 2 | 2 | 4 | **0** | 2 | **0** | **0** | 9 | 12 |

**The non-zero columns disagree with #9's census and the zero columns agree
exactly.** #9 read `good` 8, `dnserr` 9, `911` 8; this reads 17, 23, 18 —
because this census counts substrings across a wider file set and #9's counted
something narrower. Neither is wrong; they are different questions, and the
disagreement is named rather than averaged. Nothing rests on the non-zero
columns. What does rest on this census is `numhost` / `badagent` / `!donator`
= **0 writers**, and both readings agree on all three.

**The clauses the sweep calls "compliant" were measured too**, because a
verdict of compliant is a claim like any other:

```
wildcard=ON / wildcard=NOCHG / mx=… / backmx=YES  -> good 203.0.113.10   (deprecated: compliant)
system=dyndns / url=… / system=                   -> good 203.0.113.10   (accepted without error)
totally=unknown&and=another                       -> good 203.0.113.10   (§1.5 says "may": permissive)
no myip, X-Forwarded-For 198.51.100.77            -> good 198.51.100.77  (auto-detect)
25 hostnames                                      -> 25 lines, every one `nohost` (no numhost cap)
User-Agent absent / generic / descriptive         -> good 203.0.113.10   (never inspected)
```

And the 90-byte `checkip` body, measured a third and fourth time:

```
curl Content-Length : 90
wc -c on the body   : 90
the bytes           : <html><head><title>Current IP Check</title></head><body>Current IP Address: </body></html>
```

### 0.9 What #11 measured that disagrees with what was written down

Four, all small, all corrected in the plan rather than noted here.

1. **§1's opening sentence was wrong twice.** "Every response is HTTP 200 with
   `Content-Type: text/plain`, including failures" — `/nic/checkip` answers
   `text/html` in its default mode (the very next table in §1 says so), and a
   non-`GET` request answers 405. The surviving claim is "every **`GET`**
   response is 200", which is the load-bearing one.
2. **The `Allow` header's order is not stable.** §1 and §4.2 below both record
   `Allow: GET, HEAD, OPTIONS`. Two throwaway instances of the *same* checkout
   answered `HEAD, GET, OPTIONS` and `GET, OPTIONS, HEAD`, each stable within
   its own process — Werkzeug joins a Python `set`, so the order follows the
   hash seed. Nothing in the table asserts it; that is luck, and it is now
   written down so adding an `OPTIONS` case is a deliberate decision to assert
   a set rather than a sequence.
3. **The `Content-Length` figures are not constants.** `HEAD /nic/checkip` is
   102 bytes for `203.0.113.10` and 99 for `127.0.0.1`; `HEAD /nic/update` is
   18 for `good 203.0.113.201` and 17 for `good 203.0.113.10`. The plan quoted
   both as though they were properties of the endpoint.
4. **"Refusing `HEAD` is free" is true of FastAPI and false of this stack.**
   The largest of the four, and it is in §0.10.

### 0.10 The host side: what the two green cases really mean

The frozen table's `known_gaps` says two host-only cases pass against a host
with no `/nic/*` routes at all. #11 re-measured that against its own stack,
starting from the route table rather than from the wire:

```
routes matching /nic: []          <- read from app.routes inside the api container
total routes: 90

GET  /nic/checkip            -> 200 text/html; charset=utf-8   (the SPA)
POST /nic/checkip            -> 405 application/json
POST /nic/update             -> 405 application/json
POST /this/path/does/not/exist -> 405 application/json         <- indistinguishable
```

```
compat accounting (target=host):
  selected for this target   110
  ran this session           110  105 failed, 2 passed, 3 skipped
```

The two passes are `checkip-post-is-405-on-the-host` and
`update-post-is-405-on-the-host`, named by the runner's own report rather than
read off by eye. **The record in `README.md` is honest and complete**: it is
exactly 2, not more. The two host `HEAD` cases are not a third and fourth
instance — they expect 405 and the catch-all answers 404, so they fail.

**But the mechanism that manufactures those two false greens also manufactures
two false reds, and nobody had measured that.** The `HEAD` decision's cost line
in §1 said refusing `HEAD` is free on FastAPI. Three readings:

| stack | `HEAD` on a `GET`-only route |
|---|---|
| bare FastAPI, no catch-all (in-process `TestClient`) | **405** — what the table freezes |
| the same route behind a `GET`-only SPA catch-all mount | ~~**200**~~ → **404** |
| this repository's own stack, over the wire, `/api/healthz` | **404** |

> **Corrected by #16 and re-measured by #18 — the middle row is 404, not 200.**
> The reading struck through above was #11's and it stood in this file, in plan
> §1 and in the frozen table's `known_gaps` for two issues. It is wrong against
> `app.static.SPAStaticFiles` as shipped in `atrium:0.28`: the fallback to
> `index.html` is guarded by `scope["method"] == "GET"`, so a `HEAD` miss
> re-raises the underlying 404 rather than being served the shell. #18's own
> reading, five stacks, taken in the api container and over the wire — see
> §8.4 for the full table and for why the wrong *reason* mattered even though
> the decision did not move.

A `Mount` matches every method, so the 405 the route would have produced never
reaches the wire. `POST` still answers 405 through the same mount, which is
exactly why the two `-post-` cases pass. **The decision stands and the two
frozen `HEAD` cases stay at 405** — what changes is that V1M2 has to write a
deliberate `HEAD` handler. #16 wrote one; §8.4 has the reading that proves it
is the handler and not the framework.

### 0.11 The table's own arithmetic, two instruments

| instrument | live cases | deleted cases | ids | spec rows |
|---|---|---|---|---|
| PyYAML `len()` | 114 | 11 | — | 37 |
| `grep -c` on `expect:` / `reason:` / `- id:` | 114 | 11 | **125** | — |

114 + 11 = 125. Collected cases: `pytest --collect-only` gives 107 / 110 for
legacy / host, and PyYAML filtered on `targets` gives 107 / 110 — 103 shared,
4 legacy-only, 7 host-only. 0 uncovered spec rows, 0 duplicate ids, 0 cases
without an `expect.body`, 0 `deleted_cases` carrying an `expect`. By endpoint:
`/nic/update` 62, `/nic/delete` 39, `/nic/checkip` 13. By method: `GET` 105,
`HEAD` 5, `POST` 4.

The legacy suite, also two instruments: `pytest --collect-only` reports **147**
(121 + 26) and `grep -cE '^\s*def test_'` reports **147** (121 + 26). They
agree exactly, and the slack is that both count *declarations* — a
`parametrize`d suite would make them disagree, and this one has none.

### 0.12 The freeze, and proving the freeze bites

`protocol_cases.yaml` carries a `frozen:` block at **version 2** — the counts,
the spec-row count, and a sha256 over the parsed `cases` and `deleted_cases`.
`test_the_table_is_frozen_at_its_recorded_shape` recomputes all four every gate
run.

The version field is a guard rather than a note because **it had already failed
as a note**: `version: 1` was written by #7 against a 101-case table and was
still reading `1` when #25 left the table at 114.

Six mutations, each reverted immediately. **6 applied, 6 caught, 0 survived:**

| mutation | caught by |
|---|---|
| M1 a whole case deleted | `cases: recorded 114, actual 113` |
| M2 one expected byte changed (`203.0.113.10` → `.11`) | `content_digest` — the counts do not move |
| M3 a case added after the freeze | `cases: recorded 114, actual 115` + digest |
| M4 a byte changed with the counts left in step | `content_digest` only |
| M5 `version` bumped in one place | `top-level version: 3 disagrees with frozen.version: 2` |
| M6 the `frozen:` block removed | "protocol_cases.yaml has no `frozen:` block" |

**Two of those six mutations were themselves defective on the first attempt,
and both read as results until they were checked.** M5 "survived" — because
BSD `sed`'s `0,/re/` address form silently matched nothing, so the mutation was
never applied. M3 was "caught" — by a YAML parse error, because the naive
string replacement hit `deleted_cases: 11` *inside the new frozen block* rather
than the top-level key. A mutation that does not apply and a mutation that
breaks the parser are both indistinguishable from a working guard if you only
read the exit status.

---

## §§1–6. The original calibration — issue #9, the 101-case table

Kept as taken, because the mutation work and the failure modes here are not
superseded by a later green run. **These are #9's numbers and not the current
reading**; §0 is. Where §0 disagrees with something below it says so — §4.2's
`Allow` header is the one instance.

> **98 cases were selected for `--target legacy` and 95 executed against a live
> legacy service. Zero diverge from the table.** Separately, **9 behaviours
> diverge from `docs/DYNDNS-PROTOCOL.md`** — the six the table already carries,
> confirmed live, plus **three the table does not carry**, found by sweeping §1
> of the document clause by clause.

The three unrecorded ones are the finding. Everything else is calibration.

All numbers below were measured in this worktree, on branch `9_overnight`, on
2026-08-15. Nothing is inherited from #7's or #8's PR bodies; where a number
here disagrees with one of theirs, the disagreement is called out.

---

## 1. What was run, and against what

The service under test was a **throwaway local instance**, never the deployed
one. It is a copy of `brendanbank/dyndns-route53` at `main` with its own SQLite
files, its own `.env`, and one added file. `tests/compat/README.md` §*Standing
the legacy service up* has the procedure; the integrity check matters more than
the procedure and is repeated here:

```
legacy checkout HEAD  = 5d1c941fe31ea41175cb0a75849367dc94871d18
legacy checkout dirty = (clean)

same   auth.py               sha=4d6c4add8801
same   config.py             sha=2cf2d5a884da
same   dyndns.py             sha=b6a608e7a213
same   forms.py              sha=fb0f65c6afd4
same   health_checker.py     sha=8b421835eb91
same   lib/__init__.py       sha=4974036dcccd
same   lib/account/__init__.py  sha=5bdd5bb48253
same   lib/account/aws.py    sha=5055cd6e7748
same   lib/account/hetzner.py   sha=6cd7612a98e0
same   lib/account/nsupdate.py  sha=fc148aeb2871
same   lib/accounts.py       sha=15831a998ebb
same   lib/log.py            sha=7bf57ded827c
same   models.py             sha=4de73398b22f
same   rate_limiter.py       sha=28c606bf3970
same   web_routes.py         sha=bc52a6d6101a
ADDED  lib/account/stub.py   (harness, not legacy source)
```

**The table names `main` at `ec605c5`; the checkout is one commit further on, at
`5d1c941`.** The difference is `docker-compose.override.yml.example` — 47 lines
of Loki documentation, nothing on any code path. `git diff --stat ec605c5 HEAD`
touches exactly that one file, so the table's provenance claim still holds.

`dyndns.py`, `lib/accounts.py`, `auth.py`, `rate_limiter.py` and `models.py` are
**byte-identical to the checkout**. That is not a formality: the single thing
that would invalidate every number here is testing a modified legacy service
without noticing, and a sha comparison is the only instrument that sees it.

### The one file that is not legacy source

`lib/account/stub.py`, 60 lines. The table's `fixture:` block asks for
`service: stub` — "a provider the account factory knows and whose result is
scripted by `result`" — and the legacy service ships only aws, hetzner and
nsupdate, all of which talk to a real nameserver.

It sits **strictly below `hostnameperzone`**. The zone check, the credential
check, the factory lookup, the aggregation and the response formatting are all
unmodified legacy code. The provider decides only what a backend *returns*,
which is precisely the `result:` column of the fixture the table already
specifies. Mutation **M3** below is the check that this is load-bearing rather
than decorative.

---

## 2. The numbers

### The table itself, two instruments

| instrument | live cases | deleted cases | ids |
|---|---|---|---|
| PyYAML `len()` over `cases` / `deleted_cases` | 101 | 11 | — |
| `grep -c '^    expect:'` / `'^    reason:'` / `'^  - id:'` | 101 | 11 | 112 |

101 + 11 = 112. **Agrees with #7's reading**, taken independently here rather
than copied. By endpoint: `/nic/update` 56, `/nic/delete` 37, `/nic/checkip` 8.
By target: 98 `legacy+host`, 3 `host`-only. 32 `spec_rows`, 0 uncovered, 0
duplicate ids, 0 cases without an `expect.body`, 0 `covers` naming a row that
does not exist.

### The run

```
compat accounting (target=legacy):
  cases in table             101
  excluded by `targets:`       3  ['update-query-parameter-credentials-are-rejected',
                                   'update-query-parameter-credentials-do-not-shadow-basic-auth',
                                   'delete-query-parameter-credentials-are-rejected']
  selected for this target    98
  unmet preconditions          3  ['update-abuse-rate-limited', 'update-abuse-precedes-911',
                                   'delete-abuse-rate-limited']
  executable                  95
  wire-only                   11  cases carrying `effects:` (DNS ops / persisted columns)
                                  that this runner does NOT assert
  ran this session            98  95 passed, 3 skipped
  reachability probe        GET http://127.0.0.1:5109/nic/checkip -> HTTP 200
102 passed, 3 skipped in 13.48s
```

102 = 95 cases + 7 runner guards (six guards, one of them parametrised over both
targets).

### Both instruments, side by side

| | pytest runner (`http.client`) | `curl`, table replayed independently |
|---|---|---|
| selected for `--target legacy` | 98 | 98 |
| executed | 95 | 95 |
| agree with the table | **95** | **95** |
| disagree with the table | **0** | **0** |
| not executable (`rate_limited`) | 3, skipped with the precondition named | 3, same three |

The `curl` instrument does not import `conftest.py`. It builds requests from the
YAML itself and sends them with the `curl` binary — a different HTTP client, a
different header stack, a different reader of the response bytes.

**Where the two are not independent, stated rather than hidden:** both leave `,`
and `:` unencoded in the query string and percent-encode everything else,
because that is what a router in the field puts on the wire. If that convention
is wrong, both instruments are wrong together. Nothing else is shared.

### The three cases the runner refuses, measured anyway

`rate_limited: true` is a precondition the wire cannot establish, so the runner
skips those three. Skipped is *not measured* — it is neither a pass nor a fail —
and three unmeasured rows in a 98-row table is a gap, not a result. It closes
from the fixture side: drop alice's limit to one request per minute, spend it,
and replay the three requests with `curl`.

```
warm-up (spends the one allowed request): HTTP 200 body=b'abuse'

PASS  update-abuse-rate-limited    HTTP 200, body=b'abuse' (5 bytes), expected 'abuse'
PASS  update-abuse-precedes-911    HTTP 200, body=b'abuse' (5 bytes), expected 'abuse'
PASS  delete-abuse-rate-limited    HTTP 200, body=b'abuse' (5 bytes), expected 'abuse'

limits restored; a normal request answers: HTTP 200 body=b'good 203.0.113.10'
```

So the honest total is **98 of 98 selected cases measured, 98 agree, 0 diverge**
— 95 by the runner, 3 by an out-of-band fixture manipulation the runner is right
to refuse.

### What is still not measured

**11 cases carry an `effects:` block and no instrument here asserts it.** DNS
operations and persisted columns are invisible on the wire. The runner prints
the count every run so the gap stays a number; this run did not close it, and
says so rather than implying wire agreement covers it. (One `effects`-adjacent
fact *was* measured, in §4.3 below, and it is not reassuring.)

---

## 3. Divergences from the table: zero, and why that is believable

A green run against a service whose fixture you built yourself is the weakest
evidence in this repository. The fixture author and the expectation author are
the same person, and every mechanism in §5 of the overnight template says to
distrust it. Four things were done about that.

### 3.1 Five mutations, each with a prediction, each reverted

Each mutation is one string replacement in the throwaway copy, applied, measured
and reverted in a `finally`. The revert is verified by re-running the sha check
in §1 — every legacy file is back to its original digest.

| mutation | what it breaks | cases turned red |
|---|---|---|
| **M1** `$` → `\Z` in the label regex | divergence 6's exact mechanism | **2** — `update-nohost-trailing-newline-in-label`, `delete-nohost-trailing-newline-in-label`. **Predicted before the run, exact match, nothing else moved** |
| **M2** `ProxyFix(x_for=1)` → `x_for=0` | `client_ip` arrangement | 11 — all 8 `checkip` cases plus the three that depend on `myip` defaulting to the client address |
| **M3** stub `good` → `nochg` | the scripted backend result | 26 |
| **M4** `nohost {ip}` → `nohost` on update | divergence 1, as a **suffix** — the difference a whitespace-stripping comparison cannot see | 9 |
| **M5** append `\n` to every `httpReply` body | a framework adding a trailing newline | 91 of 95 |

**M5 leaves 4 cases green, and that is correct rather than a hole.** `httpReply`
is not on the `/nic/checkip` HTML path, which builds its own response — so the
four HTML `checkip` cases are structurally unreachable by that mutation.
Predicted from reading `dyndns.py`, confirmed by the count.

**M1 is the strongest single result here.** It was the only mutation whose red
set was predicted case-by-case in advance, and it came back exact: the sixth
divergence is real, it is the `$` anchor, and the table's two cases are the only
things in 98 that notice.

### 3.2 The green run was reproduced on a database built twice

The first 95/95 came from a fixture seeded once. It was then torn down —
database files deleted, not truncated — reseeded from scratch, and re-run:
95 passed, 3 skipped, 0 failed again.

### 3.3 A red run was produced, by accident, and is worth recording

Between the two green runs, one run came back **40 failed, 62 passed**. Nothing
was wrong with the table. A stale server process from an earlier step was still
holding port 5109 and reading a **deleted** SQLite inode, left in a half-seeded
state by a re-seed that had aborted on a unique-constraint violation.

Two things follow, and both are now in `README.md`:

- **The reachability probe was perfectly happy.** `GET /nic/checkip -> HTTP 200`,
  because `checkip` needs no database at all. A probe that cannot see the
  fixture cannot tell you the fixture is gone.
- `pkill -f 'compat-legacy/run.py'` **does not match** a process started as
  `cd .compat-legacy && python run.py`, because argv carries the bare `run.py`.
  The kill silently matched nothing and the run silently read the wrong service.

The failures were coherent, plausible and entirely wrong — `ok.example.com`
answering `nohost` with authentication succeeding reads exactly like a genuine
compatibility finding.

### 3.4 What a green run here does and does not prove

It proves the **table describes the legacy service's wire behaviour**, on 98 of
98 selected cases, across two clients and two independently built fixtures.

It does **not** prove the table is complete. §4 is what completeness testing
found, and it found three things.

---

## 4. Divergences from the protocol document

The document is `docs/DYNDNS-PROTOCOL.md` in the legacy repo. §1 is the
normative part; §§2–4 describe other vendors and bind nothing here.

### 4.1 The six the table carries — all confirmed live

17 cases carry a `divergence/*` tag. All 17 were replayed with `curl` and all 17
match the table byte for byte.

| divergence | cases | replayed and agreeing |
|---|---|---|
| 1 — `nohost {ip}` on update, bare `nohost` on delete | 6 | 6/6 |
| 2 — `User-Agent` is never inspected | 3 | 3/3 |
| 3 — no 20-hostname cap, 25 hostnames give 25 lines | 2 | 2/2 |
| 4 — everything is HTTP 200, `badauth` included | 3 | 3/3 |
| 5 — a single label with no dot passes the regex | 2 | 2/2 |
| 6 — a trailing newline in a label passes the regex | 2 | 2/2 |

Case-by-case, expected against actual, from the `curl` replay:

| case | div. | expected | actual | |
|---|---|---|---|---|
| `checkip-default-is-html` | 4 | `<html>…Current IP Address: 203.0.113.10</body></html>` (102 B) | identical | match |
| `update-badauth-no-credentials` | 4 | `badauth`, HTTP 200 | `badauth`, HTTP 200 | match |
| `delete-badauth-no-credentials` | 4 | `badauth`, HTTP 200 | `badauth`, HTTP 200 | match |
| `update-nohost-unregistered-hostname` | 1 | `nohost 203.0.113.10` | `nohost 203.0.113.10` | match |
| `update-nohost-hostname-owned-by-another-user` | 1 | `nohost 203.0.113.10` | `nohost 203.0.113.10` | match |
| `update-nohost-hostname-outside-backend-zone` | 1 | `nohost 203.0.113.10` | `nohost 203.0.113.10` | match |
| `delete-nohost-unregistered-hostname` | 1 | `nohost` | `nohost` | match |
| `delete-nohost-carries-no-ip-suffix-even-when-myip-is-given` | 1 | `nohost` | `nohost` | match |
| `delete-nohost-single-label-no-dot` | 1, 5 | `nohost` | `nohost` | match |
| `update-no-badagent-for-missing-user-agent` | 2 | `good 203.0.113.10` | `good 203.0.113.10` | match |
| `update-no-badagent-for-generic-user-agent` | 2 | `good 203.0.113.10` | `good 203.0.113.10` | match |
| `delete-no-badagent-for-generic-user-agent` | 2 | `good` | `good` | match |
| `update-no-numhost-for-25-hostnames` | 3 | 25 × `nohost 203.0.113.10` | 25 lines, identical | match |
| `delete-no-numhost-for-25-hostnames` | 3 | 25 × `nohost` | 25 lines, identical | match |
| `update-nohost-single-label-no-dot` | 5 | `nohost 203.0.113.10` | `nohost 203.0.113.10` | match |
| `update-nohost-trailing-newline-in-label` | 6 | `nohost 203.0.113.10` | `nohost 203.0.113.10` | match |
| `delete-nohost-trailing-newline-in-label` | 6 | `nohost` | `nohost` | match |

**Divergence 6 is confirmed twice over** — once by the case passing, and once by
M1, which is the stronger of the two: a case that passes tells you the body is
`nohost`, while M1 tells you it is `nohost` *because* `$` matches before a
trailing newline, and that nothing else in 98 cases depends on it.

**The `client_ip: null` mechanism is confirmed too.** #8's runner sends the
literal `not-an-ip` rather than omitting `X-Forwarded-For`, on the grounds that
`ProxyFix` ignores an absent or empty value. Measured three ways:

| what was sent | body of `/nic/checkip?format=plain` |
|---|---|
| `X-Forwarded-For: not-an-ip` | `` (0 bytes) — unparseable, as the table requires |
| no `X-Forwarded-For` header at all | `127.0.0.1` (9 bytes) — the socket address, perfectly parseable |
| `X-Forwarded-For:` present and empty | `127.0.0.1` (9 bytes) — same |

Omission and emptiness are indistinguishable from each other and **both are
wrong** for this case. The runner's choice is correct.

### 4.2 Three divergences the table does not carry

Found by walking §1 clause by clause rather than by accident, so the answer is a
count and not an anecdote. Each was measured live.

#### D7 — non-`GET` methods answer HTTP 405 with an HTML body, not `badagent`

§1.3: *"Other HTTP methods are not supported and trigger a `badagent` return
code."*

```
POST /nic/update   -> HTTP/1.0 405 METHOD NOT ALLOWED  text/html; charset=utf-8
                      153 bytes: "<!doctype html>...<title>405 Method Not Allowed</title>..."
PUT /nic/update    -> HTTP/1.0 405 METHOD NOT ALLOWED  (identical)
DELETE /nic/update -> 405
POST /nic/checkip  -> 405
OPTIONS /nic/update-> HTTP/1.0 200 OK, Allow: GET, HEAD, OPTIONS, Content-Length: 0
```

> **#11 corrects the `Allow` line above.** The three methods are right; the
> *order* is not a property of the service. Two throwaway instances of the same
> checkout answered `HEAD, GET, OPTIONS` and `GET, OPTIONS, HEAD`, each stable
> within its own process, because Werkzeug joins a Python `set`. Read it as a
> set, and never assert it as a sequence.

Flask's default `methods` is `["GET", "HEAD", "OPTIONS"]` and no route in
`dyndns.py` overrides it, so Werkzeug answers before any handler runs.

**This bounds divergence 4.** "Everything is HTTP 200, `badauth` included" is
true of every case in the table — all 101 are `GET` — and false of the endpoint.
A client that POSTs gets a 405 and an HTML body. The rewrite has to decide
deliberately whether to keep that; today nothing in the table would notice
either answer.

#### D8 — `offline=YES` is silently ignored; `!donator` has no writer at all

§1.5 lists `offline` as *"`YES` activates offline redirect for the hostname …
Requires a credited account"*, and §1.6 specifies `!donator` for *"premium
feature requested by non-credited user"*. §4.3 repeats it.

```
GET /nic/update?hostname=ok.example.com&myip=203.0.113.10&offline=YES
  -> HTTP 200, "good 203.0.113.10"
```

The record is updated normally and `offline` never reaches a comparison.
Counting writers for each of the document's eleven return codes in `dyndns.py`
plus every provider:

| code | good | nochg | badauth | notfqdn | nohost | numhost | abuse | badagent | !donator | dnserr | 911 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| occurrences | 8 | 7 | 2 | 2 | 2 | **0** | 2 | **0** | **0** | 9 | 8 |

`numhost` (divergence 3) and `badagent` (divergence 2) are already recorded as
having no writer. **`!donator` is the third, and it is not in the six.** This is
the same family — a documented return code the implementation can never emit —
and it belongs in the list on the same grounds the other two do.

Note that `wildcard`, `mx` and `backmx` being ignored is **compliant**: §1.5
marks those three *"Deprecated — currently ignored by Dyn"*. `offline` is not so
marked. `update-unknown-parameters-are-ignored` sends `offline=YES` among four
others and expects `good`, so the table records the behaviour — but as an
unknown-parameter case, not as a departure from a clause the document states.

#### D9 — `Client-IP` is not honoured for IP detection

§1.7: *"If the client sends `X-Forwarded-For` or `Client-IP` headers, those
values are returned instead."*

```
Client-IP: 198.51.100.5, no X-Forwarded-For  -> "127.0.0.1"     (the socket address)
X-Forwarded-For: 198.51.100.5                -> "198.51.100.5"
```

`ProxyFix` reads `X-Forwarded-For` only. Half the clause is implemented.

### 4.3 A related finding that is not a document divergence: `HEAD` writes DNS

Werkzeug's default `methods` includes `HEAD`, and Flask serves it by running the
`GET` handler and discarding the body. So a `HEAD /nic/update` is a **full
update**, with no body to say so:

```
before HEAD: last_ip_v4='198.51.100.99'  events=127
HEAD /nic/update?hostname=ok.example.com&myip=203.0.113.201
  -> HTTP/1.0 200 OK, Content-Type: text/plain; charset=utf-8, Content-Length: 18
after  HEAD: last_ip_v4='203.0.113.201'  events=128

verdict: HEAD DID execute the update (198.51.100.99 -> 203.0.113.201),
         and returned no body to say so.
```

`Content-Length: 18` alone would only have shown that a body was *generated*.
The persisted column moving is what shows the write happened — which is one of
the eleven `effects:` the wire cannot see, measured here because a monitoring
system polling `HEAD /nic/update` would be writing DNS records.

The document says nothing about `HEAD`, so this is not a divergence from it. It
is a behaviour the rewrite should decide about on purpose.

### 4.4 The sweep, as a negative result

Every clause of §1 that constrains server-side behaviour, and its verdict:

| clause | verdict |
|---|---|
| §1.1 path `/nic/update` | implemented |
| §1.2 HTTP Basic | implemented. Query-parameter credentials are a documented vendor extension and are removed by design (`deleted_cases`, 11 entries) |
| §1.3 non-`GET` ⇒ `badagent` | **D7** — 405 + HTML |
| §1.4 `User-Agent` ⇒ `badagent` | divergence 2 |
| §1.5 `hostname` max 20 ⇒ `numhost` | divergence 3 |
| §1.5 `wildcard` / `mx` / `backmx` ignored | compliant — the document marks them deprecated and ignored |
| §1.5 `system`, `url` accepted without error | compliant |
| §1.5 `offline` | **D8** |
| §1.5 unrecognised parameters *may* trigger `abuse` | permissive ("may"); `update-unknown-parameters-are-ignored` covers it |
| §1.6 one line per hostname, in request order | implemented; 6 cases, including a reversed-order pair |
| §1.6 `nohost` carries no IP | divergence 1 |
| §1.6 `!donator` | **D8** (no writer) |
| §1.6 the other 8 return codes | all emitted |
| §1.7 checkip response body | implemented byte-exactly |
| §1.7 `X-Forwarded-For` honoured | implemented |
| §1.7 `Client-IP` honoured | **D9** |
| §1.8 rate limiting / `abuse` | implemented; measured in §2 |

**Nine, and we looked at all of §1 to say so.** `/nic/delete` is deliberately
absent from this table: §1 does not describe it at all (it is an nsupdate.info
extension, §2.7), so its bare `good` / `nochg` / `nohost` bodies cannot diverge
from a document that says nothing about them. The table records them under
`delete/no-ip-suffix` instead, which is the right home.

### 4.5 What should happen to D7, D8 and D9

**Not resolved in this issue.** #9's declared scope is this file and
`README.md`; `protocol_cases.yaml` was frozen by #7 and adding cases to it is a
change to a file this issue does not own. The recommendation, for the table's
owner:

| | recommendation | cases it would take |
|---|---|---|
| **D7** non-`GET` ⇒ 405 | **Add.** It is cheap, it bounds divergence 4's wording, and a rewrite on FastAPI will answer 405 for a different reason — worth pinning before that coincidence is mistaken for compatibility | 2 (`POST /nic/update`, `POST /nic/checkip`) |
| **D8** `offline` / `!donator` | **Record as a divergence, one case.** Same family as 2 and 3 — a documented code with no writer | 1 |
| **D9** `Client-IP` | **Record, do not add a case.** Honouring it would be a behaviour change, and the table asserts what the service does | 0 |

Divergences 2, 3, 5 and 6 are all *preserve*, and so are these: preserving costs
nothing, and every one of them already ends at a body a client can parse.

---

## 5. Corrections to what was already written down

Two, both small, both measured.

**`tests/compat/README.md` said the empty-IP `checkip` HTML body is 91 bytes. It
is 90.** Confirmed three ways: `curl` reports `Content-Length: 90`, `wc -c` on
the response body reports 90, and `len()` of the table's own `expect.body`
string is 90. The *table* is right — that is why
`checkip-unparseable-remote-addr-html` passes — only the prose count was wrong.
Fixed in `README.md` by this issue.

**The same wrong count appears in `protocol_cases.yaml`**, in the `note:` on
`checkip-unparseable-remote-addr-html` ("the body is 91 bytes, not 0"). It is a
comment, asserts nothing, and lives in a file this issue does not own — flagged
here for whoever next opens the table rather than edited.

---

## 6. Reproducing this

```bash
# 1. stand the throwaway legacy service up  (README.md, "Standing the legacy
#    service up") -- copy, stub provider, seed, serve on a port of your own
# 2. the runner
python -m venv .venv && .venv/bin/pip install pytest pyyaml
.venv/bin/python -m pytest tests/compat --target legacy --base-url http://127.0.0.1:<port>
```

Expected: `95 passed, 3 skipped`, and an accounting block reading `98 selected`,
`3 unmet preconditions`, `95 executable`, `ran this session 98`.

**If it comes back green on the first attempt, that is the beginning of the
work, not the end of it.** §3 is what has to happen next.

---

## 7. Address family — issue #29, 2026-08-15, table v2 → v3

The table was frozen at v2 carrying its own weighting as a known gap: 4 of 114
cases touched IPv6 against production traffic measured at **143 IPv4 / 305
IPv6** events (plan §3.3.1). This section is the measurement that closed as
much of it as a wire table can close, and the measurement of what it cannot.

**Landed before #16, not after.** V1M2's router work is measured against this
file, so changing it afterwards would mean re-measuring #16 against a table it
did not develop against.

### 7.1 The verdict

> **The table is frozen at version 3, 131 cases. 124 selected for
> `--target legacy`, 121 executed, 121 agree, 0 diverge. All 17 additions were
> measured against a throwaway legacy instance before being asserted. IPv6 now
> covers 5 of 5 update status tokens, 2 of 2 `checkip` formats and 3 of 3
> family-specific parse refusals, against 1, 1 and 0 at v2. The v6 share of the
> table is 15.3%, not §3.3.1's 68%, and §7.4 is the defence.**

And the finding that argues against the issue's own framing:

> **The DNS record type is invisible on the wire on both endpoints.**
> `getiptype` was mutated to answer `A` for every address — turning every
> `AAAA` the service would write into an `A` — and the whole 131-case table ran
> **121 executed, 0 failed**. No number of cases in this file can detect it.

### 7.2 The service under test

A third throwaway local copy, built independently of #9's and #11's. Never the
deployed one.

```
legacy checkout HEAD  = 5d1c941fe31ea41175cb0a75849367dc94871d18
legacy checkout dirty = 0 file(s) modified

same  auth.py                      sha=4d6c4add8801
same  config.py                    sha=2cf2d5a884da
same  dyndns.py                    sha=b6a608e7a213
same  forms.py                     sha=fb0f65c6afd4
same  getpwd.py                    sha=6421cbacefed
same  health_checker.py            sha=8b421835eb91
same  lib/__init__.py              sha=4974036dcccd
same  lib/account/__init__.py      sha=5bdd5bb48253
same  lib/account/aws.py           sha=5055cd6e7748
same  lib/account/hetzner.py       sha=6cd7612a98e0
same  lib/account/nsupdate.py      sha=fc148aeb2871
same  lib/accounts.py              sha=15831a998ebb
same  lib/log.py                   sha=7bf57ded827c
same  models.py                    sha=4de73398b22f
same  rate_limiter.py              sha=28c606bf3970
same  web_routes.py                sha=bc52a6d6101a
ADDED  lib/account/stub.py          (harness, not legacy source)

integrity: 16 legacy .py files compared, 0 differ
```

**Every digest matches #11's, which matched #9's** — three independent copies,
three milestones apart, the same sixteen values. `dyndns.py` at `b6a608e7a213`
is also byte-identical to the `ec605c5` the table names as its source of truth:
`git diff --stat ec605c5 HEAD -- dyndns.py lib/ auth.py rate_limiter.py
config.py models.py` is empty, so the checkout having moved to `5d1c941` since
the freeze moved nothing on a request path.

Seeded and served in **one process**, from the table's own `fixture:` block, on
port 5229. All twelve hostnames came back in their declared backend order:

```
  backend creation order = ['nochg-a', 'no-credentials', 'dnserr', 'good',
                            'unknown-service', 'wrong-zone', 'nochg-b']
  ok  firsterr.example.com   get_backends()=['stub-nochg-a','stub-nocreds','stub-dnserr']
  ok  mixed.example.com      get_backends()=['stub-dnserr','stub-good']
  ok  allnochg.example.com   get_backends()=['stub-nochg-a','stub-nochg-b']
```

And the probe that says the *fixture* exists rather than that the *service* is
up — `/nic/checkip` touches no database and answers 200 either way:

```
$ curl -s -u alice:… -H 'X-Forwarded-For: 203.0.113.10' \
    'http://127.0.0.1:5229/nic/update?hostname=ok.example.com&myip=203.0.113.10'
good 203.0.113.10
```

The stub provider was extended to append every `createrecords` /
`deleterecords` call to a JSONL file — backend, op, `rtype`, hostname, IP — so
`effects:` is a reading rather than an assumption. It stays strictly below
`hostnameperzone`.

### 7.3 The baseline, re-derived before anything changed

Run against the **v2** table on this fixture, to establish that the harness
reproduces the number it is about to move:

```
compat accounting (target=legacy):
  cases in table             114
  selected for this target   107
  unmet preconditions          3
  executable                 104
  ran this session           107  104 passed, 3 skipped
136 passed, 3 skipped in 16.89s
```

**107 selected, 104 executed, 104 pass, 0 fail** — #11's reading exactly, on a
fixture built without reference to it. (136 against #11's 135 because #11 added
the freeze guard as its last act; the case count is what matters and it agrees.)

The family split, re-derived with #29's own instrument rather than inherited:

| | #11's reading, v2 | #29's instrument, v2 | #29's instrument, v3 |
|---|---:|---:|---:|
| IPv4-only | 104 | **106** | **107** |
| touches IPv6 | 4 | **4** | **18** |
| IPv6-*shaped* literal, deliberately unparseable | — | **0** | **2** |
| no address at all | 6 | **4** | **4** |
| total | 114 | 114 | 131 |

**The two instruments agree exactly on the load-bearing number and differ by 2
on a bystander.** The IPv6 set is the same four case ids under both. The
difference is where the line between "IPv4-only" and "no address at all" is
drawn — #11's instrument is not in the repo, so the boundary cannot be
reproduced, and the honest report is the disagreement rather than a
reconciliation. The claim the gap was about is "4 cases touch IPv6", and both
instruments say 4.

### 7.4 Which cases can the family reach at all — the defence of 15.3%

Every legacy-selected executable case was replayed **twice** against the
throwaway instance: once as the table spells it, once with every IPv4 literal
in `query` / `client_ip` / `headers` swapped for an IPv6 counterpart, comparing
status, content-type and body bytes. Deliberately not through `conftest.py` —
it speaks `http.client` directly and reads the YAML itself, so it is a second
instrument and not the runner twice.

At v2, of 104 executable cases:

| | |
|---|---:|
| reply **changed** | **36** |
| swapped, reply **identical** | **63** |
| no IPv4 literal to swap | **5** |

Three states, not two. *"There was nothing to swap"* is not *"swapping changed
nothing"*; collapsing them would have reported 68 identical and overstated the
dead ground by five.

So **68 of 104 cases sit where the family cannot alter a byte**: `badauth` and
`abuse` return before any address is parsed, `notfqdn` returns carrying no
address, and every `/nic/delete` case answers identically because delete's
replies carry no IP suffix at all. A v6 counterpart to any of those is padding.

Where it *does* reach the wire it reaches through three enumerable mechanisms,
and coverage is stated per mechanism — 36 near-identical IPv4 update cases
cover one mechanism 36 times, not IPv4 36 times:

| mechanism | IPv6 at v2 | IPv6 at v3 |
|---|---|---|
| `/nic/update` interpolates the address after a status token | **1 of 5** (`good`) | **5 of 5** |
| `/nic/checkip` echoes it | **1 of 2** formats (`plain`) | **2 of 2** |
| `myip` parse **refusals**, which differ per family | **0** IPv6-shaped vs 3 IPv4-shaped | **3 vs 3** |

Per status token, distinct cases whose reply carries an address of that family:

| token | IPv4 v2 | IPv6 v2 | IPv4 v3 | IPv6 v3 |
|---|---:|---:|---:|---:|
| `good` | 15 | 2 | 16 | 8 |
| `nochg` | 3 | 0 | 3 | 2 |
| `nohost` | 11 | 0 | 11 | 2 |
| `911` | 5 | 0 | 5 | 2 |
| `dnserr` | 1 | 0 | 1 | 1 |

**Reaching 68% would have meant roughly thirty more cases repeating mechanisms
already covered in both families.** The ratio the table is defended on is the
mechanism table above, not the share.

### 7.5 The 17 additions, each measured before it was asserted

Every one was sent by hand against the legacy service first — request line,
exact response bytes, the DNS operations the stub recorded, and the hostname
row's `last_ip_v4` / `last_ip_v6` / `last_updated_at` before and after — and
only then written into the table. The readings that are not obvious:

| case | measured reply |
|---|---|
| `checkip-ipv6-default-is-html` | 106-byte wrapper, against the v4 case's 102 and the empty-IP case's 90 |
| `checkip-ipv4-mapped-ipv6-is-not-unwrapped` | `::ffff:192.0.2.1` — **not** `192.0.2.1` |
| `update-myip-ipv4-mapped-ipv6-stays-an-aaaa-record` | `good ::ffff:192.0.2.1`, `rtype=AAAA`, `last_ip_v6` written |
| `update-myip-ipv6-with-embedded-ipv4-is-renormalised` | `64:ff9b::192.0.2.33` → `good 64:ff9b::c000:221` — the dotted tail re-rendered in hex |
| `update-myip-ipv6-scope-id-round-trips` | `fe80::1%eth0` accepted, zone preserved into the body, `rtype=AAAA` |
| `update-911-myip-ipv6-with-prefix-length` | `2001:db8::1/64` → `911` |
| `update-911-myip-ipv6-shaped-but-unparseable` | `2001:db8:::1` → `911` |
| `update-myip-defaults-to-a-v6-client-ip` | no `myip`, `X-Forwarded-For: 2001:db8:113::77` → `good 2001:db8:113::77`, `rtype=AAAA` |
| `update-nochg-single-backend-ipv6` | `nochg 2001:db8:113::20`, and `last_ip_v6` measured **unmoved** |
| `update-aggregate-any-good-wins-ipv6` | both stub calls carried `rtype=AAAA`, including the one answering `dnserr` |
| `update-multi-hostname-mixed-statuses-in-order-ipv6` | 89 bytes against the v4 twin's 73, same four hostnames, same order |
| `update-ipv4-persists-last-ip-v4-not-v6` | `last_ip_v4` moved, `last_ip_v6` measured unmoved beside it |
| `delete-myip-ipv4-mapped-ipv6-deletes-aaaa-only` | `good` on the wire; `op=delete, rtype=AAAA` in the stub log |
| `delete-911-myip-ipv6-shaped-but-unparseable` | `911` |

**One of the seventeen is deliberately IPv4.**
`update-ipv4-persists-last-ip-v4-not-v6` is the mirror of the v2 case that
asserted `last_ip_v4: unchanged` on a v6 update; nothing asserted the other
direction, so a host writing **both** columns on every update passed the frozen
table. Balancing families means pairing them, not counting them.

**`update-nochg-single-backend-ipv6` records a measurement it does not
assert.** `createrecords` *is* called on a `nochg` backend, with `rtype=AAAA` —
the stub log says so. Its v4 twin's `effects:` block records no `dns:` entry, so
the v6 twin does not either: the pair reads as a pair, and the reading is here
instead of quietly changing a convention in half the table.

### 7.6 The run, and proving the new cases bite

Instrument 1, the pytest runner, against the v3 table:

```
compat accounting (target=legacy):
  cases in table             131
  excluded by `targets:`       7
  selected for this target   124
  unmet preconditions          3  ['update-abuse-rate-limited',
                                   'update-abuse-precedes-911',
                                   'delete-abuse-rate-limited']
  executable                 121
  wire-only                   23  cases carrying `effects:` that this runner does NOT assert
  ran this session           124  121 passed, 3 skipped
  reachability probe        GET http://127.0.0.1:5229/nic/checkip -> HTTP 200
154 passed, 3 skipped
```

154 = 121 cases + 9 runner-guard nodes + 18 model guards + 6 contract tests.

Instrument 2 is the by-hand replay of §7.5: raw `http.client`, its own query
encoder, and SQLite reads of the hostname row, importing none of `conftest.py`.
Both instruments agree on all 17.

**Five mutations, five predictions, five exact matches.** Each applied to a
**fresh copy** of the legacy service on its own port and database; the whole v3
table run against it; the copy **deleted** afterwards rather than reverted,
because there is no revert to get wrong.

| mutation | predicted red | actual red |
|---|---|---|
| M-A `getiptype` always answers `A` | **0** | **0** of 121 |
| M-B `update` unwraps an IPv4-mapped v6 before echoing it | 1 | **1** — `update-myip-ipv4-mapped-ipv6-stays-an-aaaa-record` |
| M-C `getip` refuses a scoped IPv6 literal | 1 | **1** — `update-myip-ipv6-scope-id-round-trips` |
| M-D `getip` falls back to `ip_network` for a prefixed address | 2 | **2** — `update-911-myip-with-prefix-length` and its new `-ipv6-` twin |
| M-E `checkip` unwraps an IPv4-mapped v6 before echoing it | 1 | **1** — `checkip-ipv4-mapped-ipv6-is-not-unwrapped` |

Each mutated instance was checked with the fixture probe before its run; all
five answered `good 203.0.113.10`, so a red set of zero is a measurement and
not a broken fixture.

**M-A is the mutation that argues against this issue's own additions.** A
prediction of zero is only worth making because it is falsifiable: had a single
case gone red, the record type would have been observable somewhere and the
`effects:` gap smaller than claimed. It went **0 of 121**. `/nic/update` echoes
the normalised *address* and never the record type; `/nic/delete` echoes
neither. So the family cases here assert parsing, normalisation and echo — real
things — and assert nothing whatever about which record gets written. That is
now the first entry in `frozen.known_gaps`, with this number in it.

**M-D is the mutation that justifies keeping a pair rather than one case.** Both
halves go red, because a fallback to `ip_network` is not family-specific — but
the *plausible* mistake, reaching for it only in the v6 branch, shows up as one
red and not two. Two cases distinguish those; one cannot.

### 7.7 The freeze, and proving the freeze bites

Re-frozen at **version 3**: `cases: 131`, `deleted_cases: 11`, `spec_rows: 37`,
`content_digest: 2add2f37f3b5a41e82bd247353d633c99cdd00b2509de283f1d4ea748d93b83f`.
`spec_rows` is unchanged at 37 and that is a reading, not an omission: the
address-family gap was never a missing row of §1 — the document says nothing
family-specific — so all 17 cases map onto rows that already existed, with 0
uncovered rows and 0 `covers` entries naming a row that does not exist.

**The change is strictly additive, checked rather than asserted.** Comparing the
*parsed* case lists of v2 and v3 by id: **0 removed, 0 modified, 17 added**, and
`deleted_cases` and `spec_rows` identical. A purely additive change with
non-zero deletions is a defect until proven otherwise, so the check is run
rather than the intention stated.

Two counting instruments, both re-run:

| instrument | live cases | deleted | ids |
|---|---:|---:|---:|
| PyYAML `len()` | **131** | 11 | — |
| `grep -c '^    expect:'` / `'^    reason:'` / `'^  - id:'` | **131** | 11 | **142** |

131 + 11 = 142.

**Eight mutations against the freeze guards. 6 caught, 2 survived, and both
survivors are named.**

| mutation | outcome |
|---|---|
| M1 `frozen.cases` 131 → 130 | caught — `cases: recorded 130, actual 131` |
| M2 one expected byte changed in a new case | caught — digest `2add2f37…` vs `5752efc9…` |
| M3 top-level `version: 3`, `frozen.version: 2` | caught by both guards |
| M4 a whole new case deleted, counts not updated | caught — count **and** digest |
| M5 `frozen:` block removed entirely | caught by both guards |
| M6 a byte changed, all four readings recomputed, `version` left at 3 | **SURVIVED** |
| M7 `version` bumped to 4 over an unchanged table, `previous` copied wholesale | caught — digest identical to `previous` |
| M8 `frozen.previous` removed | caught |

**M6 is the hole #11's guard left, and it is the reason `frozen.previous` and
`test_the_version_advances_exactly_when_the_data_does` exist.** The four
readings say *"these numbers describe this table"*, which stays true however
often the table changes — so `version` was still on the honour system, one level
up from where #11 found it, and recomputing the readings is exactly what an
agent changing the table will do. The new guard reads `frozen.previous`, the
outgoing freeze recorded as **data** rather than as the prose comment it was
first written as, and requires the version to advance by exactly one when the
digest moves and not to advance when it does not.

**It still does not catch M6, and that is reported rather than papered over.**
`previous` is the last recorded *freeze*, not the file's last *state*, so a
change made at the current version passes. The evasion is available exactly
once: the next bump copies the current block into `previous`, and M9 — the same
mutation with `previous` correctly maintained — **is** caught. The freeze is
enforced at the boundary between versions; between edits inside one version the
enforcement is the PR review, which is why changing this file is a PR.

The worktree file was restored from a saved copy after every mutation and
verified byte-identical afterwards; the service-free guards run green at 33
passed, 1 skipped.

### 7.8 What §7 does not measure

- **The record type**, on either endpoint — §7.6, M-A, 0 of 121. This is the
  largest thing the table does not check and it is now a number.
- **`effects:` generally.** 25 cases carry a DNS operation or a persisted
  column and no runner asserts any of them. #29 read them from the stub's call
  log while authoring, which is how the `effects:` blocks got their values —
  but that is a harness reading, not an assertion the gate makes.
- **The host side.** All 17 additions are `targets: [legacy, host]`, and run
  against the host stack they are all red, because `/nic/*` does not exist
  there yet. They are calibration findings against the legacy service first,
  which is the ordering V1M1 was built on. The host reading was taken anyway,
  because it checks one thing: `make test-compat TARGET=host
  BASE_URL=http://api:8000` gives `127 selected / 124 executable / 122 failed /
  **2 passed** / 3 skipped`, and the 2 are the same two vacuous passes #11
  named. **The overstatement did not grow with the table** — 17 more cases and
  still exactly 2 — which is what says it is those two cases and not a property
  of adding cases.
- **The three `rate_limited: true` cases**, unchanged at 3 and still measured
  out of band — see §0.6.

### 7.9 Reproducing this

```bash
# 1. copy the legacy checkout, excluding .env* and instance/; verify by sha256
# 2. add a stub provider under lib/account/ that logs its calls and answers
#    from the service name; seed and serve in ONE process on a port of your own
# 3. the runner
.venv/bin/python -m pytest tests/compat --target legacy --base-url http://127.0.0.1:<port>
```

Expected: `154 passed, 3 skipped`, and an accounting block reading `131 cases`,
`124 selected`, `3 unmet preconditions`, `121 executable`, `ran this session
124`.

**If it comes back green on the first attempt, that is the beginning of the
work, not the end of it.** §7.4 and §7.6 are what has to happen next: measure
which cases the thing you changed can even reach, then break the service in the
way you are protecting against and check the right cases go red.

---

## 8. The frozen table against the **host** — issue #18, 2026-08-15, V1M2 close-out

The first seven sections measure the *legacy* service. This one is the other
half of V1M2's exit criterion:

> the **frozen** V1M1 table green against `--target host`

**Nothing here is inherited.** The close-out rule is to re-run rather than quote
the reading on file, so this issue stood up its own stack
(`COMPOSE_PROJECT_NAME=ddns18`, `API_HOST_PORT=8018`, `MYSQL_HOST_PORT=13318`),
built its own image, ran both alembic chains, seeded its own fixture world with
`make seed-compat-fixture`, and re-derived every number below — including the
gate baselines the dispatch brief supplied. Where a reading disagrees with what
the milestone had been claiming, the disagreement is stated rather than
reconciled (§8.5).

### 8.1 The verdict

```
compat accounting (target=host):
  cases in table             131
  excluded by `targets:`       4  ['checkip-post-is-405-with-an-html-body',
                                   'update-post-is-405-with-an-html-body',
                                   'update-head-runs-the-handler-and-writes-dns',
                                   'delete-head-runs-the-handler-and-deletes-records']
  selected for this target   127
  unmet preconditions          3  ['update-abuse-rate-limited',
                                   'update-abuse-precedes-911',
                                   'delete-abuse-rate-limited']
  executable                 124
  wire-only                   23  cases carrying `effects:` (DNS ops / persisted columns)
                                  that this runner does NOT assert
  ran this session           127  124 passed, 3 skipped
  reachability probe        GET http://api:8000/nic/checkip -> HTTP 200
======================== 155 passed, 5 skipped in 6.67s ========================
```

**124 executable, 124 passed, 0 failed, 3 skipped.** The 3 skips are the
`rate_limited: true` cases: that is fixture state and not a request, so the
runner refuses them with the precondition named rather than reporting them as
noughts. They are measured out of band by seven host tests —
`test_the_limit_answers_abuse_and_is_per_device`,
`test_a_zero_limit_is_not_the_same_as_an_absent_one`,
`test_abuse_precedes_the_hostname_parameter`, `test_badauth_precedes_abuse`,
`test_a_rate_limited_delete_is_logged_as_a_delete`,
`test_message_is_set_on_rate_limit_refusals_and_nothing_else`,
`test_badauth_and_abuse_rows_carry_no_backend_type` — **7 passed, 0 failed**.

The fixture stores the busiest tenant's secret as bcrypt on purpose, so a full
table run exercises the legacy verify path and the self-upgrade for real:

```
make verify-compat-rehash
  alice    seeded bcrypt   now argon2id
  carol    seeded argon2id now argon2id
  dave     seeded argon2id now argon2id
devices still stored as bcrypt: 0
```

### 8.2 Instrument 2: `curl`, from outside the container

The runner above is one instrument. The second is deliberately different in
every dimension that could hide a fault:

| | instrument 1 | instrument 2 |
|---|---|---|
| vantage point | inside the api container, compose network | the workstation, across the published host port |
| base URL | `http://api:8000` | `http://localhost:8018` |
| HTTP client | `httpx` via `conftest.CompatClient` | `/usr/bin/curl`, `--http1.1 --no-keepalive` |
| request building | `conftest.build_request` | written for this run |
| comparison | `test_protocol._compare` | written for this run |
| runner | pytest, fixtures, plugins | none |

```
curl replay (target=host, base=http://localhost:8018)
  cases in table           131
  deleted_cases            11  (never executed)
  excluded by `targets:`   4
  selected for this target 127
  unmet preconditions      3  ['update-abuse-rate-limited', 'update-abuse-precedes-911',
                               'delete-abuse-rate-limited']
  executed                 124
  passed                   124
  FAILED                   0
```

**The two agree exactly**, on every line of the accounting and on 124 / 124.

**The slack, stated because agreement without it is a formality.** The two
instruments share the YAML parse and the `defaults:` merge — the replay reads
the *resolved* cases out of the container rather than re-implementing
`resolve_case`. So a fault in the defaults merge is invisible to both. What is
not shared is everything downstream of a resolved case: selection,
percent-encoding, Basic-auth assembly, header override and removal, the request
itself, and every comparison. The `targets:` partition is re-derived
independently and reaches the same 4 / 127 split.

**The replay is shown able to fail**, because a comparator that has only ever
returned zero failures is not a comparator. Pointed at the same host with
`--target legacy` it selects the *legacy* half of the table and reports
**117 passed, 4 FAILED of 121 executed** — and the four are exactly the four
cases the `targets:` partition already excludes from `host`:

```
  checkip-post-is-405-with-an-html-body: content_type 'application/json' != 'text/html; charset=utf-8';
    body b'{"detail":"Method Not Allowed"}' != b'<!doctype html>\n<html lang=en>\n<title>405 Method Not
    Allowed</title>\n...'; line_count 1 != 6; trailing newline False != True
  update-post-is-405-with-an-html-body:  (the same, on /nic/update)
  update-head-runs-the-handler-and-writes-dns: status 405 != 200;
    content_type 'application/json' != 'text/plain; charset=utf-8'
  delete-head-runs-the-handler-and-deletes-records: status 405 != 200;
    content_type 'application/json' != 'text/plain; charset=utf-8'
```

That is a stronger result than it looks. It says the `targets:` lists are not
merely a filter someone maintained: run the legacy-selected table against the
host and **117 of 121 cases pass unchanged**, and the only four that do not are
the four where the two frameworks genuinely differ — Werkzeug's HTML 405 page
against FastAPI's JSON one, and the `HEAD`-writes-DNS behaviour this project
decided against. Nothing else in the legacy half of the table diverges.

### 8.3 The two vacuous host-only passes, re-measured

`frozen.known_gaps` records that `checkip-post-is-405-on-the-host` and
`update-post-is-405-on-the-host` pass against a host with **no** `/nic` routes
at all, because a non-`GET` on an unmatched path meets atrium's SPA catch-all,
which also declares only `GET`. Still true, and #18's own method matrix says
why: `POST` is 405 on **every** row of §8.4's table, including the rows with no
route at all. Those two cases assert "POST is refused" and never "the endpoint
exists".

What has changed is that the vacuity is no longer load-bearing. The non-vacuous
half is asserted in the host suite by `test_non_get_methods_are_refused` against
an app with no catch-all, and the reachability probe printed at the top of every
accounting block (`GET /nic/checkip -> HTTP 200`) fails against a host with no
routes.

### 8.4 The `HEAD` reading, corrected — 404, not 200

§0.10 and plan §1 recorded three readings of `HEAD` on a `GET`-only route, and
the middle one — *"behind a `GET`-only SPA catch-all mount: **200**, the
catch-all serves it"* — was wrong. #16 measured 404; #18 re-measured at
close-out, in the api container against the real `app.static.SPAStaticFiles`
from `atrium:0.28`, and over the wire against this stack:

| stack | `HEAD` | `GET` | `POST` |
|---|---|---|---|
| bare FastAPI, `GET`-only `/nic/update`, no mount | **405** | 200 | 405 |
| the same route behind `app.static.SPAStaticFiles` | **404** | 200 | 405 |
| the same route behind a stock `StaticFiles(html=True)` | **404** | 200 | 405 |
| `SPAStaticFiles` alone, no `/nic` route at all | 404 | 200 | 405 |
| stock `StaticFiles` alone, no `/nic` route at all | 404 | **404** | 405 |
| this stack over the wire, `/api/healthz` (no `HEAD` declared) | 404 | 200 | 405 |
| this stack over the wire, `/definitely-not-a-route` | 404 | 200 | 405 |
| this stack over the wire, `/nic/update` (#16's handler) | **405** | 200 | 405 |
| this stack over the wire, `/nic/checkip` (`HEAD` declared) | **200** | 200 | 405 |

**The mechanism.** `SPAStaticFiles.get_response` catches the underlying 404 and
falls back to `index.html` only `if exc.status_code == 404 and
scope.get("method") == "GET"`; otherwise it re-raises. So a `HEAD` miss is a
404, never the shell. Stock `StaticFiles(html=True)` reaches 404 by a different
road — it has no arbitrary-path fallback at all, only a directory index and a
`404.html`, which is visible in row five where even `GET` is 404.

**Why the wrong reason mattered although the decision did not move.** 404 is no
more the frozen 405 than 200 was, so the two `-head-is-refused-by-the-host`
cases fail without a hand-written handler either way. But "the catch-all serves
it" reads as though making the mount decline non-`GET` would restore the 405 —
and it already declines non-`GET`. A `Mount` is a **full** route match whatever
it then answers, so the `GET`-only route's own 405 never runs. The only thing
that produces the frozen status is declaring the method, which is what #16 did.

**The correction is recorded at v3 and is NOT a version bump.** `known_gaps`
lives in the `frozen:` block, and `frozen.content_digest` is sha256 over the
parsed `cases` and `deleted_cases` **only** — so correcting an entry there does
not move the digest, and `test_the_table_is_frozen_at_its_recorded_shape` stays
green with all four readings unchanged. Spelling the correction as a v3 → v4
bump was tried and the mechanism refuses it in its own words. By the book —
copy the outgoing block into `frozen.previous`, then bump:

```
E  AssertionError: `frozen.version: 4` claims a new freeze, but its content_digest
E  is identical to version 3's. The data did not move, so there is nothing to
E  re-freeze: either the change was lost, or the bump was reflexive.
```

…and bumping *without* copying `previous` fails the other direction of the same
guard: `frozen.version is 4 and frozen.previous.version is 2; the digest has
moved, so this must be 3`.

So the freeze mechanism, by construction, does not admit a `known_gaps`
correction as a re-freeze — and it should not: a version is a statement about
the *data* a runner iterates. What guards `known_gaps` instead is a PR review
plus, for any entry making an executable claim, a test.
`test_the_head_refusal_does_not_fall_out_of_the_framework` in
`backend/tests/test_router_nic.py` is that test for this entry: it takes four of
the readings above in one run and asserts the whole dict. Prose that can be
measured belongs in a guard, not in a digest.

### 8.5 What disagrees with what was written down

Four things, in the order they were found.

1. **`known_gaps`' fourth entry recorded 200 for the middle `HEAD` reading.**
   §8.4. It is 404. Plan §1, `known_gaps` and this file's §0.10 all carried it;
   all three are corrected.
2. **#16 declined to correct it for a reason that is also wrong.** Its write-up
   and the docstring of
   `test_the_head_refusal_does_not_fall_out_of_the_framework` both say
   "`known_gaps` is inside the digest, so correcting it is a version bump".
   `_content_digest` hashes `cases` and `deleted_cases`; `known_gaps` is outside
   it, and a bump over unmoved data is refused. Both halves false, and between
   them they kept a wrong reason on file for an extra issue. The docstring is
   corrected.
3. **The exit criterion's second clause was being measured against the wrong
   population.** "Every read path returns nothing for a second tenant's rows"
   was demonstrated per *model* — six models, six zero-row assertions, six
   emitted predicates. A read path is a *call site*. Measured: the host package
   has **14** query call sites naming a tenant model (10 through the scope, 1
   composed inside a scope call, 3 excused in writing). A fifteenth, written as
   a bare `sa.select(Domain)` in a new function, was caught by **1 test of 532**
   — the one added by this issue — and by nothing else. Swapping `scope.get` for
   `session.get` on the live `/nic/update` persist path is likewise caught by
   that one test and by no behavioural test.
4. **The census's own first cut counted 13 of 14.** `get` is spelled twice in
   this codebase — `session.get` (unscopeable) and `scope.get` (its
   replacement) — and the narrowing that keeps arbitrary `.get()` calls out
   dropped the scope's own, taking `router_nic.py:_persist_updates` with it. It
   reported a clean sweep over a population short by one. Caught by
   `test_every_scope_entry_point_appears_in_the_census`, which compares the
   census against the package's **call graph** and not against its source text,
   because `scope.predicate(DnsEvent)` appears in `scope.py`'s module docstring
   and a substring search calls that a call site.

Everything else on file survived re-measurement: 131 cases, the 4 / 127
`targets:` split, the 3 unmet preconditions, and the two vacuous host-only
passes.

### 8.6 Reproducing this

```bash
# pick your own ports; ATRIUM_DDNS_COMPAT_STUB=1 in .env
make build && make up && make migrate
make seed-compat-fixture

# instrument 1 — in the container, on the compose network
make test-compat TARGET=host BASE_URL=http://api:8000

# instrument 2 — curl, from the workstation, over the published port.
#   Dump the resolved cases out of the container, then replay them with
#   curl and compare status / content-type / body bytes / line count /
#   trailing newline against each case's `expect`, in code that shares
#   nothing with conftest.py below the resolved-case dict.
#   Then run it again with --target legacy and check it reports 4 failures.
```

Expected: `155 passed, 5 skipped`, an accounting block reading `131 cases`,
`127 selected`, `3 unmet preconditions`, `124 executable`; and the second
instrument reporting `124 passed, 0 FAILED` on the same partition.

**If both come back green on the first attempt, that is the beginning of the
work.** §8.2's `--target legacy` run and §8.4's nine stacks are what has to
happen next: show the comparator failing, and show that the readings being
quoted were taken rather than copied.
