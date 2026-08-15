# atrium-ddns — refactor plan

Rewrite `brendanbank/dyndns-route53` (Flask + SQLite + Bootstrap 5) as an
atrium host extension (FastAPI + MySQL + a Mantine host bundle on
`ghcr.io/brendanbank/atrium:0.28`).

The method is the one the operator asked for: **the compatibility suite is
written first, against the old implementation**, and the new implementation is
finished when that suite is green against it. Everything else in this document
serves that ordering.

---

## 1. The compatibility contract

The thing that must survive is the **wire behaviour of three endpoints**, not
any internal structure. Routers in the field — OPNsense, ddclient, inadyn,
Fritz!Box — are the users, and they cannot be asked to change.

Extracted from `dyndns-route53/dyndns.py` at `main`. Every **`GET`** response is
HTTP **200**, including failures; clients parse the body, and a 401 would break
them.

**This paragraph used to read "every response is HTTP 200 with
`Content-Type: text/plain`", and both halves were wrong** — corrected by issue
#11 against measurements taken in that issue, not against a reading of the code:

- **`Content-Type` is not always `text/plain`.** `/nic/checkip` in its default
  mode answers `text/html; charset=utf-8`, which the very next table in this
  section says and the sentence above it contradicted. 13 of the frozen table's
  114 cases are `checkip` cases and 5 of them assert `text/html`.
- **Not every response is 200.** A non-`GET` request answers 405 before any
  handler runs (divergence 7 below). Measured against a throwaway legacy
  instance: `POST`, `PUT`, `DELETE` and `PATCH` on `/nic/update` and `POST` on
  `/nic/checkip` all answer `405`, `text/html; charset=utf-8`, 153 bytes.

The claim that survives is the load-bearing one: **on the `GET` path the status
is always 200**, `badauth` included. 105 of the table's 114 cases are `GET` and
every one of them expects 200; the other 9 exist to assert the 405 and the
`HEAD` decision, and before they were written the "always 200" claim was
unfalsifiable from inside the table.

### §1 was reconciled against measurement by issue #11

This section was extracted before any of it had been run. Six issues corrected
it, and the corrections are in place below rather than appended: **nine
divergences, not five**; **90 bytes, not 91**; **147 legacy tests, not 146**;
the `HEAD` decision; and the two above. Issue #11 re-derived every number in
this section against its own throwaway legacy instance rather than inheriting
one — `tests/compat/baseline.md` is that reading, and where it disagrees with
what was written here, this section has been changed and the disagreement is
named rather than smoothed over.

### `GET /nic/checkip` — unauthenticated

| case | body |
|---|---|
| default | `<html><head><title>Current IP Check</title></head><body>Current IP Address: {ip}</body></html>`, `text/html`, IP HTML-escaped |
| `?format=plain` | bare `{ip}`, `text/plain` |
| `remote_addr` not parseable as an IP, `?format=plain` | empty string |
| `remote_addr` not parseable as an IP, default format | the same HTML wrapper with an empty IP — 90 bytes, **not** an empty body |

`format` is compared for exact equality with `plain`; every other value,
including `PLAIN`, falls back to HTML.

The IP is `str(ipaddress.ip_address(...))`, so it is **normalised**:
`2001:0db8:0000::1` comes back as `2001:db8::1`. The `html.escape` on it is
structurally unreachable — every string that constructor can produce is drawn
from `[0-9a-f.:%]` — so the compat suite documents it rather than asserting it.
See `tests/compat/README.md`.

### `GET /nic/update`

Auth: HTTP Basic **only** — see *Removed: query-parameter auth* below. Response
is one line per requested hostname, newline-joined, **in request order** and
**with no trailing newline** — `"\n".join(lines)`, so 25 hostnames give 25 lines
and 24 newlines. The trailing newline is stated because a framework adding one
is the single most likely way this regresses, it is invisible to any comparison
that strips whitespace, and the table asserts it byte-for-byte
(`update-multi-hostname-body-has-no-trailing-newline`, plus `line_count` and
`body_ends_with_newline` on the multi-hostname cases).

| condition | body |
|---|---|
| missing / wrong credentials, or inactive user | `badauth` |
| rate limit exceeded | `abuse` |
| no `hostname` parameter | `911` |
| `myip` present but unparseable | `911` |
| any hostname fails the label regex, or the comma list has an empty element | `notfqdn` |
| hostname not owned by the caller (legacy: the user; rewrite: the **device** — see §3.3) | `nohost {ip}` |
| hostname owned, but zero backends | `911 {ip}` |
| backend row has no stored credentials | contributes `911` |
| backend service name unknown to the factory | contributes `911` |
| hostname is not inside the backend's zone | contributes `nohost` |
| backend returned good / unchanged / error | contributes `good` / `nochg` / `dnserr` |
| **aggregate**: any `good` | `good {ip}` — and `last_ip_v4`/`last_ip_v6` + `last_updated_at` are persisted |
| **aggregate**: all `nochg` | `nochg {ip}` |
| **aggregate**: otherwise | `{first non-good/nochg status} {ip}` |

`myip` defaults to `request.remote_addr` when omitted, and an empty `myip=`
takes the same path as an omitted one. `updatetype` is accepted, warned about,
and ignored; unrecognised parameters are ignored outright (the protocol doc says
they may trigger `abuse`).

Three orderings the table above does not show, and which the compat suite
asserts because getting them wrong is invisible in the common case:

- `hostname=` present-and-empty is `911`, not `notfqdn` — `not hostnames` fires
  before the regex runs. A *trailing comma* is `notfqdn`, because the empty
  element is inside the list.
- `myip` is validated before hostname syntax, so a request wrong in both ways
  answers `911`.
- the response echoes the **normalised** IP, not the spelling the client sent.

### `GET /nic/delete`

Same auth, same rate limit, same hostname resolution. **Response codes carry no
IP suffix** — `good`, `nochg`, `nohost`, `911`, `dnserr`. `myip` present ⇒ delete
only the matching family; absent (or empty) ⇒ delete both A and AAAA.

**One real asymmetry with update:** delete does *not* substitute `remote_addr`
for a missing `myip`. On update, `myip` absent and no parseable client address
gives `911` even for a valid registered hostname; on delete the same request
succeeds. Delete also persists nothing — `last_ip_v4`/`last_ip_v6` are untouched.

### Deliberate divergences from the protocol doc

`docs/DYNDNS-PROTOCOL.md` in the old repo documents the de-facto standard. The
implementation diverges from it in **nine** places — five listed when this plan
was written, a sixth found by issue #7 while reading the regex rather than the
summary of it, and three more (7–9) found by issue #9 sweeping §1 of the
document clause by clause after the table had been frozen. Each is a
**preserve-or-fix decision**, and the compat suite must encode the decision, not
the doc:

1. `nohost {ip}` on update, bare `nohost` on delete. The doc says bare `nohost`.
   *Preserve* — clients parse the first token.
2. No `badagent`. The `User-Agent` header is never inspected. *Preserve* —
   enforcing it now would break clients that work today.
3. No `numhost`. There is no 20-hostname cap. *Preserve*, and add a cap only
   behind a config knob defaulting to off.
4. Everything is HTTP 200, `badauth` included. *Preserve* — load-bearing.
   **Bounded by D7 below:** that is true of every case in the table, all of
   which are `GET`. It is not true of the *endpoint* — a non-`GET` request gets
   Werkzeug's 405 and an HTML body.
5. A single label with no dot (`foo`) passes the hostname regex and falls
   through to `nohost`. *Preserve* — it is unreachable in practice and changing
   it moves a `nohost` to a `notfqdn`.
6. A **trailing newline** in a label passes the regex too. The pattern ends in
   `$`, which in Python matches before a trailing newline, so `foo\n` validates;
   `isvalidhostname` then returns the *unstripped* element, the lookup misses,
   and the answer is `nohost`. *Preserve*, for divergence 5's reason — both
   spellings already end at `nohost`, so preserving costs nothing, while a "fix"
   would turn a per-hostname `nohost` line into a whole-request `notfqdn`.
   Added by issue #7; verified by executing `BaseAccount.isvalidhostname` from
   the legacy checkout, not by reading it.
7. **Non-`GET` methods answer HTTP 405 with an HTML body**, not `badagent`.
   §1.3 of the document says otherwise. Flask's default `methods` are
   `GET`/`HEAD`/`OPTIONS` and no route in `dyndns.py` overrides them, so
   Werkzeug answers 405 with a 153-byte error page before any handler runs;
   `OPTIONS` answers 200 with an `Allow` header naming those same three
   methods. **The `Allow` header's order is not stable and must never be
   asserted** — this sentence used to name a specific order, and #11 measured
   two throwaway instances of the *same* checkout answering
   `HEAD, GET, OPTIONS` and `GET, OPTIONS, HEAD`, each stable within its own
   process. Werkzeug joins a Python `set`, so the order follows the process's
   hash seed. Nothing in the table asserts it — there is no `OPTIONS` case —
   which is luck rather than design, and is written down here so that adding
   one is a deliberate decision to assert a set rather than a sequence.
   *Preserve* — a
   rewrite on FastAPI answers 405 here anyway, for the different reason that
   the route declares `GET`. **This bounds divergence 4**: "everything is HTTP
   200" is true of every case in the table, all of which are `GET`, and false
   of the endpoint. Cases `update-post-is-405-with-an-html-body` and
   `checkip-post-is-405-with-an-html-body` (legacy), with `-on-the-host`
   counterparts, because only the *status* is common to the two frameworks —
   Werkzeug renders HTML, FastAPI renders `{"detail":"Method Not Allowed"}`.
8. **`offline=YES` is accepted and silently ignored, and `!donator` has no
   writer anywhere.** §1.5 defines `offline` (and does *not* mark it
   deprecated, unlike `wildcard`/`mx`/`backmx`, which are ignored
   *compliantly*), and §1.6 defines `!donator`. Counting writers across
   `dyndns.py` and every provider: `numhost` 0, `badagent` 0, `!donator` 0.
   *Preserve* — the same family as divergences 2 and 3, and the third
   documented return code the implementation cannot emit. Case
   `update-offline-yes-is-inert-and-never-answers-donator`, which asserts the
   parameter is **inert** (the update happens normally) rather than merely
   absent.
9. **`Client-IP` is not honoured for IP detection.** §1.7 names it beside
   `X-Forwarded-For`; `ProxyFix` reads only `X-Forwarded-For`, so half the
   clause is implemented. *Preserve* — honouring it now would let any client
   nominate the address written into DNS through a header nothing validates,
   which is a behaviour change dressed as a compliance fix. Cases
   `checkip-client-ip-header-is-not-honoured`,
   `checkip-client-ip-does-not-shadow-x-forwarded-for` and
   `update-client-ip-does-not-supply-a-missing-myip`.

**These nine are exactly the cases where a "fix" looks like an improvement and
is a regression.** They are the reason the suite is written against the old
implementation first — and divergence 6 is the reason it is written against the
old *implementation* rather than against this document: five of them were
visible in a careful reading and the sixth was not. Divergences 7–9 are the
reason it is written against the *whole* document rather than against this
plan's summary of it: they were found only by walking §1 clause by clause, and
three of the nine were invisible to the reading that produced the first six.

### `HEAD`: refused where the handler is not safe — decided

**`HEAD /nic/update` performs the update.** Flask adds `HEAD` to every route
that allows `GET` and serves it by running the `GET` handler and discarding the
body, so a `HEAD` is a full update with no body to say so. Measured against a
throwaway legacy instance: `Content-Length: 18`, empty body, and
`last_ip_v4` moves `198.51.100.99 → 203.0.113.201`. The document says nothing
about `HEAD`, so this is not a divergence from it; it is a property that needs a
decision, and leaving it unstated would make it an accidental one.

**Decision: the host answers `405` to `HEAD /nic/update` and `HEAD /nic/delete`,
and keeps `HEAD /nic/checkip` at `200`.** Not preserve-everything — and the
argument is not that HTTP requires `HEAD` to be safe, though it does
(RFC 9110 §9.2.1). It is that the protocol's `GET` is *already* unsafe by
design and cannot be fixed without breaking every router in the field, so
`HEAD` is the only place where safety is recoverable at zero cost to clients.
Four things decided it:

- **Preserving is the expensive option, not the cheap one.** FastAPI's
  `APIRoute` does not add `HEAD` to a `GET` route (Starlette's plain `Route`
  does; FastAPI's does not), so the rewrite refuses `HEAD` unless someone
  writes `methods=["GET", "HEAD"]` on purpose. Preserving would mean
  deliberately re-enabling an unsafe `HEAD` on the endpoint that writes DNS.

  **But "refusing is free" is only true of a bare FastAPI app, and this is not
  one.** #11 measured it three ways and the readings do not agree with each
  other, which is the finding:

  | stack | `HEAD` on a `GET`-only route |
  |---|---|
  | bare FastAPI, no catch-all (in-process `TestClient`) | **405** — what the table freezes |
  | the same route behind a `GET`-only SPA catch-all mount | **200** — the catch-all serves it |
  | this repository's own stack today, over the wire | **404** |

  Atrium mounts a catch-all at the root, and a `Mount` matches every method, so
  the 405 the route would have produced never reaches the wire. `POST` still
  answers 405 through the same mount, which is why the two `-post-is-405-on-the-host`
  cases pass. **The decision stands and the cases stay frozen at 405** — what
  changes is the cost line: V1M2 has to write a deliberate `HEAD` handler (or
  stop the SPA mount shadowing `/nic/*`), because the framework's own refusal
  does not survive the mount. Recorded in the table's `frozen.known_gaps`.
- **Nothing in the field sends it.** 105 of the 114 cases in the table are
  `GET`; the other 9 — 5 `HEAD`, 4 `POST` — exist to assert this decision and
  divergence 7. ddclient, inadyn, OPNsense and Fritz!Box send `GET`.
- **The realistic sender is a monitor, and the failure is worse than "an extra
  update".** `HEAD` needs valid Basic credentials, so it is not an open door —
  but a DynDNS update URL carries its credentials, and pasting one into an
  uptime checker is ordinary. Measured: a `HEAD /nic/update?hostname=…` with no
  `myip` set the record to **the prober's** address
  (`198.51.100.99 → 198.51.100.77`). The hostname ends up pointing at the
  monitoring service, refreshed every poll.
- **`checkip` is safe, so `HEAD` stays.** The split is the rule applied rather
  than a blanket: `HEAD` is preserved exactly where the handler is read-only.
  `HEAD /nic/checkip` answers 200 with an empty body — a perfectly reasonable
  liveness probe, and the host must declare `HEAD` there.

  **The `Content-Length` figures in this section are not constants**, and #11
  re-measured them rather than repeating them: the length tracks the IP in the
  body. `HEAD /nic/checkip` is 102 bytes for `203.0.113.10` and 99 for
  `127.0.0.1`; `HEAD /nic/update` is 18 bytes for `good 203.0.113.201` and 17
  for `good 203.0.113.10`. What is load-bearing is that a body is *generated*
  at all, and — for update — that `last_ip_v4` moves, which is the half a
  `Content-Length` cannot show and the wire cannot see.

**The mechanism named in issue #25 does not work, and that is worth recording
rather than quietly not using.** The issue proposes `methods=["GET"]` "turning
`HEAD` from 200 into 405". Werkzeug adds `HEAD` to any rule that allows `GET`
(`routing/rules.py`: `if "HEAD" not in methods and "GET" in methods:
methods.add("HEAD")`). Measured on a mutated throwaway copy: with
`methods=["GET"]`, `POST` and `PUT` become 405 while `HEAD` stays 200, still
updates, and the `Allow` header still reads `OPTIONS, GET, HEAD`. In Flask the
fix has to be taken in the view (or with a custom rule); in FastAPI it is the
default. Nobody is going to fix the legacy service, so this changes nothing
about the decision — but a future reader who tries `methods=["GET"]` and
believes it worked would ship an unsafe `HEAD` with a green table.

Cases: `update-head-runs-the-handler-and-writes-dns` and
`delete-head-runs-the-handler-and-deletes-records` (legacy, with the write
recorded in `effects:` because the wire cannot see it),
`update-head-is-refused-by-the-host` and `delete-head-is-refused-by-the-host`
(host), and `checkip-head-returns-headers-and-no-body` (both).

### Removed: query-parameter auth — decided

`?username=&password=` is **gone**. It was only ever a workaround for router
vendors not in use here, and it is the sole reason the old repo carries its
entire Traefik/Loki credential-leak apparatus: `queryparameters.defaultmode`,
the discovery that `redact` is accepted by the flag parser and silently behaves
as `keep`, a canary password reaching Loki in clear text through a pipeline that
redacted correctly in every isolated test, and a standing Loki query that has to
stay at zero. Removing the input removes all of it — the producer-side stripping,
the `_scrub_password_from_environ` before-request hook, and the warning log that
was the guard.

The compat table must **delete** those cases rather than mark them expected-fail,
and record the deletion. HTTP Basic is the only accepted transport for
`/nic/*` credentials.

Done in issue #7: **11 cases deleted**, kept as data in the table's
`deleted_cases` block — id, legacy behaviour and reason, no `expect` — so the
deletion is auditable and the table cannot shrink silently. Three replacement
cases run against the **host only** and assert the input is inert rather than
merely absent.

### How the suite is built — two instruments

One table, two targets. `tests/compat/protocol_cases.yaml` holds every case as
(request, auth state, fixture state) → exact expected body. A pytest runner
executes the table against a base URL, selected by `--target legacy|host`.

- Run it against the **old Flask app** first. Cases it fails are calibration
  findings — a doc bug or a wrong expectation — not compat requirements, and
  each one gets resolved in the table before any host code is written.
- The same table then gates the new implementation.

A suite that has only ever run against the implementation it was written
alongside proves nothing about compatibility. Report the legacy baseline as a
**negative result**: "we ran N cases against the legacy service, and exactly M
diverge from the protocol document, enumerated here."

**N and M, filled in.** Issue #11 stood up its own throwaway legacy instance
and ran the frozen table end to end, deriving every number in this paragraph
rather than quoting one:

> **107 cases were selected for `--target legacy` and all 107 were measured —
> 104 by the pytest runner and 3 out of band, because their precondition is
> fixture state the wire cannot arrange. 107 agree with the table and 0
> diverge. Separately, exactly 9 behaviours diverge from
> `docs/DYNDNS-PROTOCOL.md` §1, all 9 confirmed live, and a clause-by-clause
> re-walk of §1 found no tenth.**

`tests/compat/baseline.md` is the reading, with both instruments, the five
mutations that produced the predicted red sets, and what is still not measured.
The table is **frozen at version 2** (`frozen:` block, guarded by
`test_the_table_is_frozen_at_its_recorded_shape`), so V1M2 is measured against
a file that cannot change without a PR that says so.

The old repo's **147** pytest tests are a second source — 121 in `test_app.py`
plus 26 in `test_hetzner.py`, agreed exactly by `pytest --collect-only` and by
`grep -cE '^\s*def test_'`, re-measured by #11. (`dyndns-route53/CLAUDE.md` says
146; it is stale, and this plan repeated it without counting.) Port the ones
asserting *behaviour*; drop the ones asserting Flask internals, template
contents, or Bootstrap markup — atrium owns that surface now.

### What the frozen table does not cover — named at freeze time

A contract that overstates its own reach is worse than a narrow one, so the
gaps are part of §1 rather than a footnote in the test directory. All four are
in the table's own `frozen.known_gaps`, and all four were measured by #11:

- **`effects:` is data, not an assertion.** 15 cases carry a DNS operation or a
  persisted column; no runner asserts either. The runner prints the count every
  run so the gap stays a number rather than an assumption.
- **3 cases are not executable over the wire.** `rate_limited: true` is a
  precondition, not a request. They are skipped with the precondition named and
  measured separately — and the measurement is only worth having because the
  same three were run against an *unlimited* instance and went red there, which
  is what tells you `abuse` was measured rather than printed by construction.
- **2 host-only cases pass against a host with no `/nic/*` routes at all.**
  `checkip-post-is-405-on-the-host` and `update-post-is-405-on-the-host` meet
  atrium's SPA catch-all, which also declares only `GET`, so nothing on the
  wire distinguishes "no route" from "`GET`-only route". They assert *`POST` is
  refused*, never *the endpoint exists*. Re-measured by #11 against a host
  whose route table contains **zero** `/nic` paths: 110 selected, **2 passed,
  105 failed, 3 skipped** — the 105 are what says the endpoint is not there
  yet, and the 2 greens are not coverage. The two host `HEAD` cases do **not**
  pass vacuously (they expect 405 and the catch-all answers 404), so the
  overstatement is exactly 2 and not 4.
- **The table is weighted against the traffic it protects.** 104 of 114 cases
  are IPv4-only and 4 touch IPv6, measured from the table's `myip`, `client_ip`
  and expected bodies. §3.3.1 measured production at **143 IPv4 / 305 IPv6**
  events and concluded, before anyone checked, that "a table exercising `A`
  thoroughly and `AAAA` once is testing the wrong record type". It was
  describing this table. The divergence count is unaffected — the document says
  nothing family-specific — but the *coverage* claim is, and V1M2 inherits it
  as a known gap rather than discovering it.

---

## 2. What atrium replaces

Do not port any of this. Atrium ships it, and reimplementing it is the one
hard rule in `docs/new-project/SKILL.md`.

| old file | disposition |
|---|---|
| `auth.py`, `getpwd.py`, TOTP flow, Flask-Login | **delete** — atrium: fastapi-users, cookies, TOTP, WebAuthn, idle-session timeout |
| `models.py` `User` | **delete** — atrium owns `users`. Host columns hang off a `user_id`-joined host table |
| `models.py` `Event` | **port and widen** — DNS update audit stays domain data; atrium's `audit_log` covers auth/admin actions. Now needs indexed user / device / domain columns (§3.1) |
| `models.py` `RateLimitConfig` / `RateLimitEvent` | **port, re-keyed to the device** — atrium's PAT rate limiter is per-token; ours is per-device |
| `web_routes.py` (865 lines), `templates/`, `static/style.css`, `forms.py` | **delete** — replaced by the host bundle (§4) |
| CSRF, ProxyFix, WAL-mode setup, one-time `ALTER TABLE` migrations | **delete** — atrium + alembic |
| `models.py` `Domain` / `DomainBackend` / `BackendConfig` / `Hostname` | **port onto `HostBase` and re-shape** — domains become tenant-owned, and a new `Device` sits between user and hostname (§3.1) |
| `lib/accounts.py`, `lib/account/{aws,nsupdate,hetzner}.py` | **port**, stripped of Flask and of env-var credential fallbacks |
| `health_checker.py` (APScheduler) | **port** to `init_worker(host)` + `host.scheduler.add_job(...)` |
| Fernet credential encryption | **replace** with `app.host_sdk.crypto` at `scope="user"` — **provider credentials only**; the device secret is hashed, not encrypted (§3.2). shipped in atrium 0.28 as `SecretBlob` + `UserSecret` (§3.1.1) |

`BackendConfig` is the interesting one. The old repo encrypts provider
credentials with a Fernet key it manages itself; atrium 0.28 ships the
primitive for exactly this:

```python
# ILLUSTRATIVE — not the API. Upstream confirmed a TypeDecorator cannot see
# the row, so user-scope columns get a different declaration shape entirely
# (descriptor, or mapper load/before_flush events). See §3.1.3.
from app.host_sdk.crypto import EncryptedJSON, MaskedSecret

credentials: Mapped[MaskedSecret] = mapped_column(
    EncryptedJSON(purpose="ddns_backend.credentials", scope="user"),
    nullable=False,
)
```

**Superseded by atrium 0.28** — `EncryptedText(scope="user")` raises
permanently and names the replacement. The real declaration is `SecretBlob()` +
`UserSecret(purpose=…, owner_attr="user_id", column=…)`; see §3.1.1.

AES-256-GCM, per-column HKDF-derived key, headered wire format, reads return a
`MaskedSecret` you must `.reveal()` at the point of use. Requires
`SECRET_ENCRYPTION_KEY` (32 bytes hex) — **a prod stack refuses to boot without
it even with no encrypted columns**, and the scaffolder does not emit it. It is
in this repo's `.env.example`; back it up outside the database dump.

---

## 3. Tenancy, devices, and credentials

The old service is single-tenant with admin-owned global domains. The rewrite is
**multi-tenant**, and it introduces the object the old model was missing: the
**device**.

### 3.1 The model

```
atrium User ──1:N── Domain ──1:N── DomainBackend ──1:1── credentials (encrypted)
     │                 │
     │                 └─────1:N── Hostname ──N:1── Device
     └──1:N── Device ──────────────────┘
```

- **Domain** — a zone, **owned by a user**, not global. The owner supplies their
  own provider credentials, so one installation serves tenants who share nothing.
  Uniqueness of the zone name stays global (DNS is global); ownership is the new
  part.
- **DomainBackend** + credentials — **owned by the user**, one row per (domain,
  backend type). A tenant's Route53 key, Hetzner token or TSIG secret is *their*
  credential: it must never be usable by another tenant, and it should die with
  their account. Nothing about it is installation-wide. Encrypted per-user via
  `SecretBlob` + `UserSecret` (atrium ≥ 0.28) — see §3.1.1.
- **Device** — *the thing that makes the call.* Owned by a user, carries its own
  `username` (globally unique) and `password_hash`, and holds the operational
  signal that matters: `last_seen_at`, `last_ip_v4` / `last_ip_v6`,
  `last_user_agent`. This is the DynDNS client — a router, a NAS, a script.
- **Hostname** — belongs to one Domain and one Device. `device_id` is nullable
  so a hostname can be registered before it is assigned, and reassigned without
  being deleted.

**Authorisation is now a security boundary, not a convenience.** Cross-tenant
reads are the failure mode to design against. Atrium ships the hook for exactly
this: `app/auth/scope.py` provides a no-op `AdminScope` and expects hosts that
need row-level filtering to define their own scope class plus a `get_scope`
dependency. Every host query goes through it — none of them filter by
`user_id` by hand, because the one that forgets is the leak.

Permissions to seed in `0001`: `atrium_ddns.domain.manage`,
`atrium_ddns.device.manage`, `atrium_ddns.hostname.manage` (own rows), and
`atrium_ddns.admin` + `atrium_ddns.events.read.all` for cross-tenant access.
`super_admin` is auto-granted; unknown role codes warn and skip.

#### 3.1.1 Per-user credentials — resolved upstream

This was the sharpest constraint in the refactor. It is now closed.

Provider credentials are per-user, and until 0.28 the platform could not express
that. The history matters only because the plan was written across it: verified
in `app/host_sdk/crypto.py` at `v0.27.0`, `EncryptedText` / `EncryptedJSON`
called `_reject_user_scope(scope)` in `__init__`, and `scope="user"` raised

> scope='user' is not implemented. It is not a different salt — it needs a
> request-scoped owner binding, defined behaviour outside a request scope
> (workers included), the owner bound into the AEAD's associated data, and a
> key-wrap record with shredding semantics.

The rejection is deliberate: ADR 0003 says silently falling back to `site` would
ship a cross-user isolation break as a bug. And ADR 0003's own definition of a
user secret — *"belongs to one person, must never be usable for another, and
should die when they are deleted"* — describes this column exactly. We are the
use case the unbuilt half was written for.

**Decided by the operator: every stored domain secret uses `scope="user"`.**
Provider API keys, TSIG secrets and Hetzner tokens go through
`app.host_sdk.crypto` at user scope. (The *device* secret is hashed rather than
encrypted — §3.2 — so it is not in this set.) Not
`scope="site"`, and not host-rolled crypto — ADR 0003 exists because
`atrium-pa` rolled its own twice and the second grew a `KeyProvider` protocol, a
per-user KEK wrap table, a Vault Transit backend and a blind-index module.

**Delivered — atrium v0.28.0, 2026-08-15.** #227 is closed and the feature
shipped, so this is no longer a blocker. It is recorded here because the shape
it landed in is not the shape this plan originally assumed, and V1M2 is written
against the delivered API rather than the sketch.

Chronology, since the intermediate states are confusing on a re-read: #225
shipped the primitive with `site` implemented and `user` rejected at
construction, deliberately, with a test asserting the rejection. #227 asked for
the `user` half plus one requirement #225 had not written down. v0.28.0
delivers it. `EncryptedText(scope="user")` still refuses — permanently now —
and points at the replacement.

**The API.** Two lines in the model, not one, because a SQLAlchemy column type
never sees the row it belongs to and the row is where the owner lives:

```python
from app.host_sdk.crypto import SecretBlob, UserSecret, unlock_user_secrets

class Device(HostBase):
    __tablename__ = "device"
    user_id: Mapped[int] = mapped_column(HostForeignKey("users.id"))
    secret_ct: Mapped[bytes | None] = mapped_column(SecretBlob(), nullable=True)

    secret = UserSecret(
        purpose="device.secret", owner_attr="user_id", column="secret_ct"
    )
```

```python
row = await session.get(Domain, domain_id)
await unlock_user_secrets(session, row.user_id)   # owner from the ROW
row.provider_credentials.reveal()
```

**One `await` before you touch the value.** Unlocking is a database read, and
there is no honest way to hide a database read behind a plain attribute access
in an async app. Forget it and you get `SecretLockedError` naming the call to
make. For `/nic/update` this means the unlock happens as part of the same async
fetch that loads the domain rows — not lazily at `.reveal()` deep inside the
per-backend loop.

**Verified here, not taken on trust.** `backend/tests/test_user_scope_secrets.py`
asserts the four properties this host depends on, against a live database, in
the shape this host uses them:

| property | why it is ours to test |
|---|---|
| round trip with **no authenticated user anywhere** | the `/nic/update` shape — a router on HTTP Basic, no session, no PAT. If this breaks, the DDNS hot path is dead |
| locked access raises and names the fix | an async database read behind an attribute; the failure has to be legible |
| tenant A's ciphertext will not decrypt in tenant B's row | bad import script, or database write access |
| **ciphertext captured *before* a shred is unreadable after it** | reading the row after a shred proves only the row changed. The pre-shred copy stands in for a backup tape, and that is the property the whole decision was taken for |

Atrium has its own suite and it passes. This one exists because a feature tested
only by its author is tested on its author's terms.

#### 3.1.2 Owner-from-row — the requirement, and how it landed

Between the rejection message and #225's own notes, most of the specification
already exists: owner binding in the AEAD's associated data, a key-wrap record
with shredding semantics, defined behaviour outside a request scope, and the
`register_pre_user_delete` seam to shred against. One requirement is **not** in
any of it, and if the successor is built without it the feature will not work
here at all:

> **The owner must be derivable from the row, not from the request principal.**

Both hot paths in this service run with no authenticated atrium user:

- `/nic/update` is called by a **router**, authenticated as a *device* over HTTP
  Basic. There is no atrium session, no cookie, no PAT. Decrypting that domain's
  provider credentials needs `owner = domain.user_id`, read from the row being
  decrypted.
- The **device secret** is verified in that same request. It is hashed rather
  than encrypted (§3.2) so it needs no key — but the domain rows the request
  then touches do, and by that point the only identity established is a device,
  not an atrium user.
- Worker jobs (health checks, retention prune) have no request at all.

An implementation shaped as *"owner = the currently authenticated user"* — the
obvious reading of "request-scoped owner binding" — covers none of these.

**Accepted upstream.** Atrium
[#227](https://github.com/brendanbank/atrium/issues/227) confirmed the
requirement, called the original rejection message's wording its own omission,
added owner-from-row to the acceptance criteria, and **implementation has
started**. The response also corrected two premises this plan carried, and both
corrections change what V1M2 can assume — see §3.1.3.

**`purpose` binds a ciphertext to a column, not to a row — and `scope="user"`
does not make the query scope optional.** Owner binding stops a blob being
*decrypted* under the wrong owner; it does nothing about a query that returns
the wrong tenant's row in the first place, and every non-secret column on that
row (zone name, hostname, last IP, event history) has no crypto on it at all.
§3.1's rule stands unchanged: no hand-written `user_id` filters, everything
through the host `Scope`. The two controls cover different failures, and the
cross-tenant isolation suite in V1M2 tests the query one.

Two mechanical notes for whoever writes the credential form. Use
`apply_secret_update(obj, field, incoming)` rather than assigning: `None` clears,
`""` **preserves** the stored ciphertext, anything else replaces it. Skip it and
editing an unrelated field on the same form silently blanks the credential, with
the failure surfacing much later as a provider auth error with no obvious cause.
And reads return a `MaskedSecret` — `.reveal()` at the point of use only, never
in a response model.

#### 3.1.3 Three predictions, closed out

The upstream response validated the code-level claims and the central
requirement, and overturned two premises this plan had been carrying. Recorded
because each one changes host code.

All three held, and each is now a fact rather than a caution.

**1. A user-scope column is not declared like a site-scope one.** ✅ Confirmed —
it landed as `SecretBlob()` column + `UserSecret(...)` descriptor, two lines.
`TypeDecorator.process_bind_param` / `process_result_value` receive no instance
and no row, in either direction — so an `owner_attr=` argument on the existing
type is not implementable, and adding one was never going to work. `scope="site"`
stays on the `TypeDecorator` path; user-scope columns get a **different
declaration shape** (a descriptor over a raw-bytes column, or mapper
`load` / `before_flush` events — both see the instance).

*Consequence for us:* the delivered shape is in §3.1.1 and that is what V1M2
writes against. Any `mapped_column(EncryptedJSON(..., scope="user"))` still
visible in this document is a superseded sketch — `EncryptedText(scope="user")`
raises permanently and names the replacement, so a copied snippet fails at
import rather than shipping something subtly wrong.

There is a second-order constraint upstream flagged and we inherit: unwrapping a
per-user key is a **database read**, and the obvious places to hide it are sync
call sites in an async-only codebase, some mid-result-iteration. The key has to
be resolved *before* the attribute is touched. For `/nic/update` that means the
device's and domain's keys are loaded as part of the same async fetch that loads
the rows — not lazily at `.reveal()`.

**2. Derivation and shredding are mutually exclusive — and this plan had asked
for both.** ✅ Confirmed and resolved: the key is *stored*, not derived. "`key_version` is already in the KDF info string, so per-user derivation
slots in without a format change" and "deleting a user destroys their key"
cannot both hold: a key derived from the master is recomputable from the master
forever, so there is nothing to destroy. Real shredding needs a **random
per-user key, stored wrapped under the master, and deleted** at shred time —
which means atrium's first secret-bearing table and an alembic migration. The
wire format genuinely does not move; **the schema does**.

*Consequence for us:* ✅ happened exactly as predicted — the 0.28 uptake brought
`0012_user_secret_keys` on atrium's own `alembic_version` chain, an empty table
on upgrade. Applied here on 2026-08-15. The image bump and `alembic upgrade
head` are one step, not two; the same will be true of every future atrium
uptake, so the migration-slot note in the Project card covers both chains.

*And a warning worth carrying:* upstream found that `atrium-pa`'s per-user keys
are HKDF-derived from root + `user_id`, its `shred_user` only sets
`shredded_at`, and `user_key` never reads it — so PA's "shredding" is access
control at the door, not cryptographic destruction. **Do not treat PA as a
reference implementation for this property.** If anything in this repo ever
claims a tenant's secrets are unrecoverable after deletion, that claim gets
tested against a row captured *before* the delete, or it is not made.

**3. The shred seam fires at hard delete, not when the user asks.** ✅ Confirmed
— and 0.28 ships `shred_user_key(session, user_id)` for doing it sooner if the
product wants that.
`register_pre_user_delete` runs immediately before `session.delete(user)`;
`soft_delete_user` does not run hooks. So key destruction happens after
`auth.delete_grace_days`, not at the click — and it has to, because shredding at
soft-delete would let a grace-window reinstatement hand back an account whose
every credential is permanently unreadable.

*Consequence for us:* "your provider keys are destroyed" and "destroyed in 30
days" are different promises, and the UI has to make the right one — decision 7
in §6, answered "warn loudly, then destroy". With `shred_user_key()` available,
destroying at the click is now a *choice* rather than an upstream ask; the
trade-off is that grace-window reinstatement then hands back an account whose
credentials are permanently unreadable, which is why atrium's own default waits.

**Open: can two devices update one hostname?** The model above says no (one
`device_id`). A failover pair — two routers, same name — would need M:N. Left
as 1:N until there is a real case, because M:N makes "which device last wrote
this record" ambiguous, and that is the question the UI is built around.

### 3.2 Credentials

**The device is the credential.** `/nic/update` and `/nic/delete` authenticate a
*device*, not a user: HTTP Basic, username and password from the `devices` row.

**Storage: hashed, not encrypted — decided.** `password_hash` is argon2id for
newly issued secrets, with **bcrypt verification retained** so migrated rows
work untouched. The plaintext is generated server-side, shown **once** at
create and at rotate, and never stored in recoverable form. Lookup is by
`username`, plaintext and indexed.

This reverses an earlier draft that put the device secret through
`app.host_sdk.crypto` alongside the provider credentials, and the reversal is
worth the two lines it costs to explain:

- **It shrinks the blast radius to the thing that has to be reversible.**
  Provider credentials *must* be decryptable — we hand the actual Route53 key to
  boto3 on every update, so there is no choice. A device secret only ever needs
  to be *compared*, so hashing it means `SECRET_ENCRYPTION_KEY` plus a database
  copy no longer yields every tenant's DNS-update credential. Encryption is for
  secrets we have to give back; hashing is for secrets we only have to check.
- **It makes the migration uniform.** The cutover carries legacy bcrypt hashes
  (§3.3) and we never possessed those plaintexts, so a migrated device could
  never have been re-displayable anyway. With hashing there is no split: *no*
  device secret is ever re-displayable, migrated or new, and the UI tells one
  story — "shown once; rotate to get a new one" — instead of explaining why some
  devices can be revealed and others cannot.

The verify path accepts both hash shapes, keyed off the stored prefix: `$2b$` /
`$2a$` / `$2y$` → bcrypt (migrated), `$argon2` → argon2id (new or rotated).
Re-hash to argon2id opportunistically on a successful bcrypt verify, the way
`pwdlib.verify_and_update` does — the fleet migrates itself as routers check in,
with no operator action and no client reconfiguration.

**Note:** hashing takes the device secret out of the encrypted set entirely, so
`app.host_sdk.crypto` is used only for *provider credentials*. Atrium 0.28's
release notes name a device password as an example use of `UserSecret`; we
deliberately do not, for the reason above — a secret we only ever compare should
not be stored in a form that can be read back.

The device secret *is* the permanent key for DDNS purposes, and it is a better
one than a shared per-user credential: scoped to a single device, rotatable from
that device's own page without touching its siblings, and revoked by deleting
the device. It also gives the log something to attribute an update to (§3.4),
which a user-level credential cannot.

**Atrium PATs stay — for the management API, not for `/nic/*`.** A PAT travelling
as `Authorization: Bearer atr_pat_…` is the right credential for scripting
hostname or device creation, and it works out of the box. It cannot serve
`/nic/*`, for two reasons verified in `app/auth/pat_middleware.py`: the
middleware only reads `Bearer` (DynDNS clients send Basic and fall through
unauthenticated), and **any request with `atr_pat_` in the path or query is
refused `400 token_in_url` unconditionally**, before the bearer parse.

Issuing a permanent atrium PAT, for the management API: Admin → **Tokens** →
*Create service account* → expiry **Never**. Requires
`auth.service_accounts.manage` — **super_admin only**, the `admin` role
deliberately does not hold it. "Never" is offered only while
`pats.max_lifetime_days` is `null` (its default); set to a number, the UI hides
the option and the API caps the request. Plaintext appears once and there is no
re-emit route — recovery is revoke plus recreate.

### 3.3 The migration invariant that will bite

Legacy hostnames are owned by a *user*; `/nic/update` returns `nohost` for a
hostname the caller does not own. After the migration the check is against the
*device*. So:

> Every legacy user migrates to **exactly one** device, carrying that user's
> username and bcrypt hash verbatim, and **all** of that user's hostnames are
> assigned to it.

Split a legacy user's hostnames across two devices and its router starts getting
`nohost` for half of them, with a 200 and no error anywhere. The importer must
assert the invariant and the compat suite must cover it: same credentials, same
hostname set, same responses.

Splitting one migrated device into several is then a **post-cutover** operation
the owner performs deliberately, per hostname, in the UI — not something the
importer guesses at.

#### 3.3.1 The actual production population, measured 2026-08-15

Read from the **live** database inside the running legacy container, not from a
copy. Small enough that the whole migration is a single transaction:

| | |
|---|---|
| users | 7 — one `admin` (0 hostnames, `web_login=1`) + six `user` rows |
| hostnames | 11, spread 4 / 2 / 2 / 1 / 1 / 1 across the six non-admin users |
| domains | 1 |
| domain_backends | 1 (`aws`) |
| backend_configs | 2 (`aws_access_key_id`, `aws_secret_access_key`, Fernet) |
| hostname_backends | empty — every hostname uses all of its domain's backends |
| ttl | uniformly 60 |
| orphan hostnames | 0 |

**⚠ `~/dyndns.db` is a stale snapshot and must not be the migration source.**
The copy in the home directory (both on the workstation and on the host) is
**two schema migrations and two hostnames behind** live: its `users` has no
`web_login`, its `hostnames` has no `ttl` / `last_ip_v4` / `last_ip_v6` /
`last_updated_at`, and it holds 9 hostnames against live's 11. The legacy app
adds those columns via one-time `ALTER TABLE` at boot, so a snapshot taken
before a deploy is silently a different schema. The importer reads the live
file out of the `dyndns-route53_dyndns-data` volume, and asserts the column set
before it starts.

**Three consequences for the migration.**

1. **Legacy users have no email address.** The `users` table is keyed by
   `username`; atrium's is keyed by email and requires one. Six accounts need an
   address that does not exist anywhere in the source data. That is an operator
   decision, not something an importer should invent — see §6.
2. **The one-device-per-user invariant is cheap here.** Six devices, the largest
   carrying four hostnames. The admin has none and needs no device — it becomes
   an atrium super_admin and nothing else.
3. **`web_login=0` for every non-admin user.** None of them can use the legacy
   web UI today, so nothing is lost by them not having atrium logins on day one.
   It also means the DDNS credential is the *only* thing they have, which is
   exactly what makes carrying the bcrypt hashes verbatim non-negotiable.

**The traffic is IPv6-first — but measure it with the right instrument.**

The first version of this paragraph read `hostnames.last_ip_v4/v6` and reported
"10 of 11 track a v6 address". That column is **not** a record of client
traffic: `dyndns.py` seeds it from `dns.resolver.resolve()` at boot for any
hostname with no tracked IP, so it counts hostnames that have an AAAA record in
the zone — including ones no client has ever updated. #10's agent caught it.

The instrument that answers the question is `events.ip_address`. Measured on
production: **448 events, 143 IPv4 and 305 IPv6** — about 68% of real client
updates are v6, from 3 distinct users. (The events table is pruned to 24 hours,
so that is a day of traffic, not all of it. The direction is unambiguous; treat
the ratio as indicative.)

The conclusion survives and is strengthened: `AAAA` is the common path here and
`A` is the variant, which is the opposite of how the legacy suite is weighted. A
table exercising `A` thoroughly and `AAAA` once is testing the wrong record
type.

**And the general lesson, which cost two corrections in one milestone:** a
column named `last_ip_v6` looks like it answers "do clients send v6". It
answers "did a zone lookup at boot find an AAAA record". Before trusting a
number, ask what it would read if the thing being measured were absent.

### 3.4 Logging — searchable by user, device, and domain

The old `Event` table denormalises `username` and nothing else, and prunes
everything older than 24 hours. Neither survives multi-tenancy: "which of my
devices stopped updating, and when" is unanswerable at 24 hours' depth, and
per-tenant filtering has nothing to filter on.

`dns_event` gets indexed, nullable FKs to **user, device, domain, and hostname**,
each `ON DELETE SET NULL`, alongside **denormalised name columns** captured at
write time (`user_email`, `device_name`, `domain_name`, `hostname`). Both halves
are needed and for different reasons: the FKs are what the filters index on, and
the denormalised strings are what keeps a log entry readable after the device it
describes has been deleted — which is precisely when the log is being read.

Query surface, all combinable, all scope-filtered before they reach the
database: user (admin only), device, domain, hostname, event type, response
code, client IP, time range. Compound index on `(user_id, created_at)` and
`(device_id, created_at)`; those two carry the common views.

**Retention becomes a setting, not a constant.** The 24-hour prune was sized for
a dashboard, not for search. Move it into a `register_namespace` config value
with a sane default (30 days), and run the prune as a scheduled job through
`init_worker` rather than on the write path — the old code prunes inside
`log_event`, so every DNS update pays for it.

**`n/a` is not `0`.** A device that has never called, a device whose last call
failed, and a device with zero updates in the window are three different states.
Render them as three different things — a dash, an error, and a zero. Collapsing
them into `0` is the most common way a status board lies.

## 4. UI — the rewrite is an information-architecture change

The operator's brief: *"the UI is scattered."* It is, and the cause is
structural. The old UI has seven-plus admin pages — users, domains, backends,
backend credentials, hostnames, hostname-backends, events, rate limits, health
checks — for what is one object graph with one question hanging off it.

**The subject.** A DDNS service exists to answer two things: *is my device still
checking in?* and *is this name pointing where I think it is?* Everything else
is configuration in service of those. The old UI buries the first question
entirely — it had no concept of a device — and hides the second in a
health-check panel on the dashboard.

The device concept is what makes the UI cohere. The old pages were scattered
because there was no object to hang them on; now there is.

**Direction.**

- **The device is the primary object, the hostname is its detail.** One surface,
  registered via `registerRoute` + `registerNavItem`: each device, when it last
  called in, from which IP, and the hostnames it maintains nested underneath. A
  status board, not a CRUD list. *A device that has gone quiet is the single
  most useful thing this product can tell you, and the old UI could not.*
- **The signature element: the resolution strip.** Per hostname, three values
  side by side — what the authoritative nameserver answers, what we last wrote,
  and what the device last claimed. Divergence between those three *is* the
  product; the old UI computes it and then hides it. Spend the boldness here
  and keep everything around it quiet.
- **Domains are a tenant surface, not an admin one.** A user manages their own
  domains and provider credentials on their own page. Cross-tenant views live
  behind `atrium_ddns.admin` as an admin tab.
- **Configuration collapses.** Rate limits, health-check config, retention
  become one nested group via `registerSettingsGroup` (atrium ≥ 0.25) rather
  than sibling pages.
- **Logs are a first-class search surface**, not a scrolling list: filter by
  device, domain, hostname, response code, and time range, with the same filters
  reachable pre-applied from any device or hostname row. Admins get a user
  filter on top.
- **Users are atrium's**, not ours. The old user-management pages disappear.

**Constraints the design must respect.** Atrium owns the shell — header,
sidebar, login, profile, admin tabs. The host bundle renders fragments inside
it, in its own React tree behind the wrapper-div pattern, with its own
`MantineProvider` and `QueryClient`. Palette, type and radius come from atrium's
Branding namespace and the `--mantine-*` tokens listed in `atrium/docs/theme.md`;
those are the stable contract. Light and dark both have to work — the switch is
`[data-mantine-color-scheme]` on `<html>`, not a media query.

**Process for the UI issue:** run the design pass properly — palette as 4–6
named values, display/body/utility type roles, a layout concept, and the one
signature element — and critique it against the brief *before* writing
components. The three looks that AI design work clusters into (cream + serif +
terracotta; near-black + acid accent; broadsheet hairlines) are defaults, not
choices; this brief leaves the axis free, so do not spend it there.

---

## 5. Milestones

Every issue body carries `Depends on:` (the dependency edge the ready-set is
computed from) and `## Scope` (the file list parallel scheduling is decided on).
Without both, the run cannot parallelise and cannot compute readiness.

**V1M0 — upstream: `scope="user"` in atrium — ✅ DONE.** Shipped as
[atrium v0.28.0](https://github.com/brendanbank/atrium/releases/tag/v0.28.0),
closing [#227](https://github.com/brendanbank/atrium/issues/227), on 2026-08-15.
Owner-from-row delivered; `SecretBlob` + `UserSecret` + `unlock_user_secrets()`
+ `shred_user_key()`; one new atrium migration (`0012_user_secret_keys`). This
repo is on `ghcr.io/brendanbank/atrium:0.28` with SDK packages at `^0.28`, both
alembic chains at head, and the four properties verified locally
(`backend/tests/test_user_scope_secrets.py`). **V1M2 is unblocked.**

**V1M1 — compatibility harness.** The table, the dual-target runner, and the
legacy baseline. Exit: N cases run against the legacy service, divergences from
the protocol document enumerated, table frozen. *No host code in this
milestone*, and no dependency on V1M0 — this is the work to do while the
upstream lands.

**V1M2 — host backend and tenancy.** Models on `HostBase` (Domain, Device,
Hostname, DomainBackend, DnsEvent) + `0001` migration + permissions; the host
`Scope` class and `get_scope` dependency; the `/nic/*` router and device auth
(§3.2); the three provider plugins stripped of Flask; the per-device rate
limiter; health checks and the retention prune in `init_worker`. Exit: the
frozen table green against `--target host`, **plus a cross-tenant isolation
suite** — every read path proven to return nothing for a second tenant's rows.
That suite is not optional and does not belong in V1M3; multi-tenancy without it
is a leak waiting for its first user.

**V1M3 — UI.** Design pass, then the bundle: device board, resolution strip,
per-tenant domains, log search, config group. Exit: every old page either exists
as a registration or is deleted because atrium covers it.

**V1M4 — migration and cutover.** SQLite → MySQL importer: one device per legacy
user carrying the bcrypt hash verbatim (§3.3, with the invariant asserted, not
assumed), and Fernet-decrypt → `SECRET_ENCRYPTION_KEY`-re-encrypt of every
stored provider credential. Deploy verified by content; smoke tests against the
deployed stack. Exit: **a real DynDNS client, with its configuration unchanged,
updates a record through the new stack and the record resolves.** That is the
demonstration — not a count of closed issues.

`V1M2` and `V1M3` overlap safely (disjoint scope: `backend/**` vs
`frontend/**`), but both write `0001`-chain migrations only through the single
`alembic_version_app` slot. Schedule that slot; do not parallelise it.

---

## 5b. Deploying beside the old service

**Decided: the new stack runs alongside the existing service on port 8443**
during the development phase. The old one keeps serving real clients until the
exit criterion is demonstrated, which is what makes the cutover reversible.

**The milestone tip may be deployed to production during this phase** — an
unattended run does not have to wait for a release PR to see its work running
on the host. That is deliberate: the whole reason deploy is a standing decision
is that a run which cannot deploy ships work that passes CI and does not run.
It is safe here *because* the new stack is beside the old one rather than in
front of it; the blast radius of a bad milestone tip is a service nothing yet
depends on.

Three things that follow, one of them load-bearing:

**8443 implies TLS, and atrium does not terminate it.** The atrium image speaks
plain HTTP on container port 8000 by design — its own compose comments say to
put a terminator in front. So `API_HOST_PORT=8443` publishes *HTTP* on 8443
unless something terminates TLS ahead of it. That matters more here than in a
normal app: DynDNS clients authenticate with **HTTP Basic**, so a plaintext
listener on 8443 puts every device secret on the wire in base64, and 8443 is
exactly the port a client will assume is TLS. Either front it with the host's
existing terminator (the old service already runs one) routing a hostname to
`127.0.0.1:8443`, or accept HTTP **only** while no real device credential has
been issued yet. Decide before the first device is created, not after.

**Port collisions.** The old service binds 80/443 on the host. 8443 is free of
those, and the compose file already parameterises the API and MySQL host ports
(`API_HOST_PORT`, `MYSQL_HOST_PORT`) precisely so two stacks can coexist. Set
both explicitly in the deploy host's `.env` — the dev defaults (8053 / 13353)
are for a laptop running several projects, not for this host.

**Two compose projects, one docker daemon.** Use an explicit project name
(`COMPOSE_PROJECT_NAME` or `-p`) so the new stack cannot collide with the old
one's container names, networks, or volumes. The host already runs 3 containers.
`deploy-verify.sh` asserts identity *inside a named container*; two projects with
colliding names is exactly how that check ends up interrogating the wrong one
and passing.

**Host capacity and access, checked live.** Ubuntu 24.04, docker 29.1.3, 3
containers already running, 31 G disk free, 2.9 G RAM available, 8443 unbound.
The deploy user is in the `docker` group, so unprivileged `docker compose` works
and `deploy-verify.sh` needs no change. RAM is the tightest resource — a second
full stack (api + worker + MySQL) alongside the existing three containers on
3.7 G total is worth watching; give MySQL an explicit buffer-pool limit rather
than letting it size itself against the whole box.

---

## 6. Open decisions

These are the operator's, and a run should not start without them.

1. ~~Project board~~ — **created**: board 2, *atrium-ddns delivery*,
   `Status` = Todo / In Progress / Done. `bootstrap-github.sh --check` reports
   setup COMPLETE.
2. ~~Query-param auth~~ — **decided: removed** (§1, *Removed: query-parameter
   auth*).
3. ~~Cutover credentials~~ — **decided: the device carries the legacy bcrypt
   hash verbatim**, so the field fleet needs no reconfiguration (§3.2, §3.3).
4. ~~Ship site-scoped and backfill later?~~ — **decided: no.** Report the gap
   upstream and wait for it; V1M0 blocks V1M2 (§3.1.1, *Considered and
   rejected*).
5. ~~Device secret: encrypted or hashed?~~ — **decided: hashed.** argon2id for
   new secrets, bcrypt verification retained for migrated rows, shown once at
   create/rotate, never re-displayable. Takes the device secret out of the
   encrypted set entirely (§3.2).
6. ~~Key rotation runbook~~ — **decided: v1 requirement.** The dual-column
   `key_version="v2"` procedure (declare alongside, backfill, drop the first)
   gets written up and *rehearsed* before cutover, not documented and left
   untried. A rotation runbook nobody has executed is a hypothesis. Owned by
   V1M4.
7. ~~When is a tenant's secret destroyed?~~ — **decided: warn loudly, then
   destroy.** No softened promise. Mechanically this lands at atrium's hard-delete
   hook, i.e. after `auth.delete_grace_days`, so the warning must name *when*:
   "deleting this account destroys your provider credentials after the N-day
   grace period — reinstating within it keeps them, after it nothing can recover
   them." If destruction is wanted at the click instead, that is a further
   upstream ask and reinstatement can no longer restore credentials (§3.1.3).
8. ~~Where does `SECRET_ENCRYPTION_KEY` live?~~ — **decided: `.env` on the
   deploy host**, gitignored, alongside `APP_SECRET_KEY` and `JWT_SECRET`. One
   follow-through: `.env` is now the single point of failure for every stored
   provider credential, so whatever backs up the host's `.env` *is* the key
   backup — and it must not be the database dump, since a dump plus the key in
   the same place defeats encryption at rest entirely.
9. ~~Two devices, one hostname?~~ — **decided: no, not now.** One `device_id`
   per hostname, 1:N. Revisit only if a real failover pair appears (§3.1).
10. ~~Log retention default~~ — **decided: ~30 days**, global, tunable via the
   host's config namespace. Affordable now the store is MySQL rather than the
   old SQLite file, and pruned by a scheduled job rather than on the write path
   (§3.4).
11. **What email address do the six legacy users get?** Their accounts are keyed
   by username; atrium requires an email and there is none in the source data
   (§3.3.1). Synthesised (`<username>@<domain>`) leaves six addresses that
   cannot receive mail — password reset and verification both dead-end.
   Real addresses need collecting from the people. Deferred to V1M4, but it
   gates the importer.
12. ~~Deploy host and cutover policy~~ — **decided: run beside the existing
   service on port 8443.** Both stacks live on the host during the development
   phase; the old one keeps serving until the exit criterion is demonstrated.
   See § *Deploying beside the old service* for the one thing 8443 implies.
13. ~~Standing decisions~~ — **all approved, development phase, full
   permissions**, with two constraints that survive it: unsigned commits remain
   a stop condition, and the DNS smoke test's allow-list stays a dedicated test
   zone. Recorded in `overnight-template.md` § *Standing decisions*.
