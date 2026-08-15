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
