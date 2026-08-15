# Baseline: the case table against the legacy service

Calibration run for issue #9. The deliverable is a **negative result**, and the
headline is two numbers that do not mean the same thing:

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
