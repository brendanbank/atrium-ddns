# Cutover runbook — `dyndns-route53` → `atrium-ddns`

**Status: written, not executed.** Executing it is #53, and #53 is operator-gated.
Nothing in this file has been run against the live service. What *has* been run
is every measurement it quotes; each one names the instrument it came from.

**Audience:** one operator, at a keyboard, with ssh to the deploy host. Every
command is given in full. Where a value is site-specific it is written
`<LIKE_THIS>` and explained the first time it appears.

**Read it end to end before starting.** The mechanical part takes under a
minute; the verification takes hours, and two of the decisions in § 2 have to be
made days earlier.

---

## 0. The shape of the thing

Two stacks are on the deploy host right now:

| | project | binds | serves |
|---|---|---|---|
| old | `dyndns-route53` | `traefik` on **80/443** | the real hostname, real clients |
| new | `atrium-ddns` | `proxy` on **8443**, `api` on **127.0.0.1:8444** | nothing that matters yet |

Cutover moves the real hostname from the first row to the second. It is four
things, in this order:

1. freeze the old service so the source database stops moving;
2. copy that database and import it;
3. move 80/443 from the old Traefik to the new one;
4. watch for a real client, and know what "a real client" looks like.

Step 4 is the hard one and § 5 is the longest section for that reason.

---

## 1. Six measured facts that decide the whole plan

Everything below follows from these. Each was measured on **2026-08-15**, and
each names how. Re-take the two marked ⏱ on the day — they age.

### 1.1 The live database has not been checkpointed since 19 July

```
-rw-r--r-- 1 app app     65536 2026-07-19T21:56:22 dyndns.db
-rw-r--r-- 1 app app     32768 2026-08-15T21:14:36 dyndns.db-shm
-rw-r--r-- 1 app app   4120032 2026-08-15T21:09:54 dyndns.db-wal
```

*(`docker exec dyndns-route53-web-1 ls -la /app/instance/`, read-only.)*

Everything since 19 July lives in a 4.1 MB write-ahead log. **Twenty-seven
days of production sit in the WAL and not in the main file.** § 3 turns this
into the single hardest rule in the runbook.

### 1.2 Two of eleven hostnames are actually dynamic

Age of each hostname's `last_updated_at`, in days, at the moment of the copy:

```
0.006   0.133   13.6   28.5   28.5   78.2   148.4   148.4   148.4   148.4   148.4
```

*(`select round(julianday('now')-julianday(last_updated_at),3) from hostnames`
against a WAL-safe copy — the names are withheld, the shape is the point.)*

Two hostnames moved in the last four hours. Five have not moved in **148 days**.
So *waiting for the fleet to check in* is not a verification strategy: for most
of it, silence is the normal state and carries no information at all. § 5.4.

### 1.3 The fleet sends ~19 requests an hour, and 68% of them are IPv6

24 hours of the legacy event log — 448 rows, `2026-08-14 21:16` →
`2026-08-15 21:14`, which is the whole table because legacy retention is 24 h:

| response | family | rows |
|---|---|---|
| `nochg` | IPv6 | **288** |
| `nochg` | IPv4 | 143 |
| `good` | IPv6 | **17** |
| `good` | IPv4 | **0** |

18.7 requests/hour, near-flat around the clock (min 15, max 21 per hour).
**Every single successful change in 24 hours was IPv6.** § 2.1 is entirely
about this number.

### 1.4 Three of six device credentials were exercised; three were not

`select count(distinct username) from events` → **3**, over six migrated
devices and eleven hostnames, of which **4** appear at all. The other three
devices and seven hostnames produced not one request in 24 hours — consistent
with § 1.2's 148-day tail.

### 1.5 `nochg` proves the Route 53 credential without writing to the zone ⏱

This is the resolution of the tension the whole milestone has been carrying.
`Route53Provider.createrecords` does two things before it can write:

```python
client = make_client(self.credentials)      # boto3 client — local, no network
...
zone_id = self._zone_handle(zonename)       # populated by _discover_zones()
...
if self.check_hostnameon_server(hostname, ip, rtype):
    results[hostname] = STATUS_NOCHG        # returns — no change_resource_record_sets
    continue
```

and `_discover_zones()` runs **at provider construction**
(`providers/base.py`, in `__init__`, inside a `try` because
`model_cases.yaml::provider-zone-discovery-failure-is-not-fatal`). It calls
`client.list_hosted_zones()` — a real, authenticated, **read-only** Route 53 API
call with the migrated credential.

So a request that ends `nochg`:

* authenticated the device against the migrated hash ✅
* resolved the hostname to a device the tenant owns ✅
* **used the migrated Route 53 credential against the real AWS account** ✅
* wrote nothing to the zone ✅ — `check_hostnameon_server` asked *DNS*, not
  Route 53, and returned before any `change_resource_record_sets`

The one link `nochg` does not prove is `change_resource_record_sets` itself.
That link is proved by #50 in-process behind two guards, and in production by
the first real `good` — which § 1.3 says arrives within about four hours.

### 1.6 The borrowed certificate is valid until 23 September, not 24 August ⏱

Two instruments, agreeing:

```
# the extracted copy the new stack serves
$ openssl x509 -in /usr/local/atrium-ddns/certs/cert.pem -noout -issuer -dates
issuer=C = US, O = Let's Encrypt, CN = YR2
notBefore=Jun 25 14:53:30 2026 GMT
notAfter=Sep 23 14:53:29 2026 GMT

# the old service's ACME store, decoded independently
resolver letsencrypt certs 1
notBefore=Jun 25 14:53:30 2026 GMT
notAfter=Sep 23 14:53:29 2026 GMT
  chain blocks: 3
```

See § 4 — this corrects a claim that has been repeated three times in this
milestone's issues and is wrong in a way that matters.

---

## 2. Prerequisites — days before, not on the day

Each of these is a **gate**. If one is red, the window does not open. They are
ordered by how long they take to fix.

### 2.1 ⛔ GATE: the new stack's container must be able to resolve AAAA

**This is the blocker most likely to be missed, and § 1.3 is why it matters.**

The old service's docker network enables IPv6; the new stack's does not:

```
$ docker network inspect atrium-ddns_default      --format '{{.Name}} EnableIPv6={{.EnableIPv6}}'
atrium-ddns_default EnableIPv6=false
$ docker network inspect dyndns-route53_web-network --format '{{.Name}} EnableIPv6={{.EnableIPv6}}'
dyndns-route53_web-network EnableIPv6=true
```

*(read-only `docker network inspect` on the deploy host.)*

On the development workstation, with the production hostnames imported, two
vantage points disagreed completely:

| | A records answered | AAAA records answered |
|---|---|---|
| from the workstation, `dig` | 4 of 11 | **10 of 11** |
| from inside the `api` container, `dns.resolver` | 4 of 11 | **0 of 11** |

The AAAA records exist — ten of them, exactly the ten hostnames carrying a
`last_ip_v6`. The container could not see a single one.

Two consequences, and the second is not cosmetic:

1. **The DNS health check reports ten of eleven hostnames `missing`.** Observed:
   `atrium_ddns.health_check.done … records_checked=14 ok=4 missing=10
   mismatch=0 error=0`. An operator reading that on cutover night concludes the
   migration destroyed the zone. It did not.
2. **`nochg` becomes `good`, and every one becomes a real Route 53 write.**
   `check_hostnameon_server`'s docstring is explicit that *"every failure path
   answers `False`, which makes the write proceed"*. A container that cannot
   resolve AAAA therefore turns § 1.3's **288 daily IPv6 `nochg` responses into
   288 daily `change_resource_record_sets` calls** — a wire response the frozen
   compat table says should be `nochg`, and 288 writes a day the old service
   never made.

**What is proved and what is not.** The disagreement above was measured on the
workstation (Docker Desktop, macOS). The enabling condition — an IPv4-only
docker network beside an IPv6-enabled one — was measured on the deploy host.
The third reading, *a AAAA query from inside the new stack's container on the
deploy host*, was **not taken**, because taking it means executing inside a
container on the deploy host and this issue's brief forbids touching that host.
It is one command and it is the first thing the operator runs:

```bash
ssh <DEPLOY_HOST> 'docker compose -f /usr/local/atrium-ddns/compose.yaml \
  exec -T api /opt/venv/bin/python -c \
  "import dns.resolver; print(len(dns.resolver.resolve(\"<A_HOSTNAME_WITH_AAAA>\",\"AAAA\")))"'
```

* **non-zero** → the gate is green, nothing to do.
* **`NoAnswer` / `0`** → the gate is **red**. Fix before cutover by adding
  `enable_ipv6: true` to the compose network (matching the old service) and
  recreating the stack, then re-run the probe. Tracked as **gap G2** in § 9.

Do not proceed on the assumption that it is probably fine. § 1.3 says two thirds
of the traffic depends on the answer.

### 2.2 ✅ GATE: the ACME hand-over is prepared and tested

**Status: shipped and gate-tested (#60). Gap G1 is closed.** What follows is a
description of configuration that exists in the repository, not a block to
paste. The gate test is `make test-acme-handover`; its results are in § 2.2.4.

The new stack's proxy today runs **no ACME at all** — it serves a static copy of
the old service's certificate, because that store already has exactly one writer
and a second would corrupt it (`scripts/extract-acme-cert.sh` explains the
failure mode: a lost account key, not a merge conflict).

At cutover it has to stop borrowing. **The plan is to give the new Traefik a
copy of the old account and certificate rather than have it issue a new one**,
for one reason: issuing at cutover puts an HTTP-01 challenge on the critical
path, at the exact moment 80/443 are changing hands, with Let's Encrypt rate
limits on the other side of it. Copying the store means TLS is already correct
the instant the new proxy binds 443.

#### 2.2.1 Three files, and why the hand-over is an overlay

| file | what it is |
|---|---|
| `compose.acme.yaml` | the `proxy` service, re-specified: entrypoints on 80 and 443, an ACME resolver pointed at **this stack's own** store, and a `./letsencrypt` volume. It `!override`s `command`, `ports` and `volumes` rather than merging them. |
| `infra/traefik/dynamic-acme.yml` | the `Host()` router with `certResolver: letsencrypt`, and the extracted copy kept as `defaultCertificate`. |
| `.env` | `TRAEFIK_HOSTNAME`, `LETSENCRYPT_EMAIL`, `LETSENCRYPT_CASERVER`. |

**It is an overlay and not an edit to `compose.yaml` on purpose.** The `proxy`
service in `compose.yaml` is what the deploy host runs *right now* on 8443. Had
the hand-over been written into it, the next routine `docker compose up -d`
would have tried to bind 80 and 443 — which the old Traefik is holding — and
the hand-over would have happened at whatever moment somebody deployed. As an
overlay, it happens when an operator names the file:

```bash
docker compose -f compose.yaml -f compose.acme.yaml --profile tls up -d proxy
# or: make acme-up
```

**The three variables have no defaults**, and the overlay refuses to load
without them:

```
$ docker compose -f compose.yaml -f compose.acme.yaml config
error while interpolating services.proxy.command.[]: required variable
LETSENCRYPT_CASERVER is missing a value: set the ACME directory explicitly —
staging and production are one character apart in effect and weeks apart in
symptom
```

That refusal is aimed at one failure in particular. The old compose defaults
`LETSENCRYPT_CASERVER` to **staging**; Traefik's own default is **production**.
Two different silent answers to the same omission, and with the extracted copy
still terminating TLS underneath, a stack that got the wrong one looks
completely healthy. The guard cannot live in `compose.yaml`: Compose
interpolates the whole document at load time **including services whose profile
is not active** (measured), so a `${VAR:?}` there breaks `make up` for
everybody.

**The hostname is never written into a tracked file.** It reaches the router
rule through Traefik's own file-provider templating —
``rule: Host(`{{ env "TRAEFIK_HOSTNAME" }}`)`` — evaluated inside the container
against the environment the overlay passes it. Two things measured rather than
assumed: templating works in a `.yml` file, and a file named `.yml.tmpl` is
**skipped** by the directory provider ("Skipping file, unsupported extension")
quietly enough that the stack comes up with no router at all.

#### 2.2.2 ⚠️ The fallback suppresses first issuance. Measured.

The `defaultCertificate` block pointing at the extracted copy **stays**, and
becomes a fallback: if ACME fails after the cutover, TLS still terminates on a
valid certificate. That much was the plan and it holds.

What was not known when this section was first written is that **the fallback
also stops Traefik asking for a certificate at all**. Measured on traefik:3.7,
same stack, one block of dynamic configuration the only difference:

| ACME store | `defaultCertificate` | what Traefik does |
|---|---|---|
| empty | present | `No ACME certificate generation required for domains` — **never contacts the CA** |
| empty | removed | `Trying to challenge certificate` → `Building ACME client` → `Unable to obtain ACME certificate` |

A certificate already provided for a domain is a certificate Traefik will not go
and ask for. So a hand-over in which **step 5.4(b) was skipped** — wrong path,
wrong permissions, forgotten — produces a stack that looks flawless: the
handshake succeeds, the chain validates, `curl` returns 200, `make tls-verify`
prints a valid date. Nothing is ever issued, and the first symptom is the
extracted copy expiring, on § 1.6's date, weeks later.

**Renewal is not suppressed**, and that is what makes the design work. With a
certificate inside the 720h (30-day) renew window in the store, the same stack
logs `Testing certificate renew...` and attempts it — the renewal path reads
the store's own contents, not the routers. Exercised end to end against a local
CA: the near-expiry certificate was renewed, the new one written to the store,
and the new one served (§ 2.2.4, phase E).

The consequence for the operator is one line at the end of step 5.4, and it is
not optional:

```bash
make acme-verify HOST=<TRAEFIK_HOSTNAME>
```

It compares the certificate **on the wire** with the one **inside this stack's
store**, by SHA-256 fingerprint. A match means the hand-over took. A mismatch
where the wire matches `certs/cert.pem` means the store was never copied, and
it says so in those words.

#### 2.2.3 ⏱ A renewal may fire seconds after the hand-over, not weeks later

The original wording of this section — *"the first renewal happens weeks later
with nothing else going on"* — is true only on one side of a date, and today is
on the other side of it.

Traefik checks for renewal **at start-up** and every 24h thereafter, and renews
anything within 720h of expiry. § 1.6's certificate expires **23 Sep 2026**, so
it enters that window on **24 Aug 2026**. Cutting over after that date with a
store the old Traefik has not yet refreshed means the new proxy attempts a real
HTTP-01 renewal within seconds of binding 443.

That is survivable — it is what the design is for — but it moves two things
onto the critical path that § 2.2 assumed were weeks away:

* `LETSENCRYPT_CASERVER` must already be right;
* port **80** must already reach the new proxy from the internet, because
  HTTP-01 is the challenge type configured.

If the old Traefik has already renewed by the time the store is copied, the
copy is fresh and none of this applies. Check which case you are in before the
window: `openssl x509 -in certs/cert.pem -noout -enddate`, and compare with
today.

#### 2.2.4 What the gate test covers, and what it does not

`make test-acme-handover` (`scripts/test-acme-handover.sh`) stands the
**shipped** proxy service up — the real `compose.yaml` + `compose.acme.yaml`
pair, through `docker compose`, on throwaway ports — against a synthetic ACME
store built by `openssl`. It never reads the incumbent's store and never binds
80 or 443. **60 checks, 0 failures**, most recently on 2026-08-16.

| phase | what it exercises | ACME endpoint |
|---|---|---|
| 0 | the overlay refuses to load with each variable unset, naming it | none |
| 1 | the rendered service is the hand-over: 80/443, resolver, `./letsencrypt` read-write, 8443 gone, the pre-hand-over dynamic file not mounted | none |
| 2 | `extract-acme-cert.sh` leaves the incumbent's store byte- and mtime-identical | none |
| A | the copied store terminates TLS, and **no ACME call is made** | unroutable `https://127.0.0.1:1/directory` |
| B/C | the fallback terminates TLS when the store is empty — and suppresses issuance, with the control that proves it | unroutable |
| D | a near-expiry certificate in the store **does** trigger a renewal, which fails safely and leaves the store intact | unroutable |
| E | a renewal completing end to end: account registered, certificate issued, store rewritten, new certificate on the wire | `pebble`, Let's Encrypt's own test CA, on a private docker network |
| F | this configuration reaching the real Let's Encrypt | **staging** — `https://acme-staging-v02.api.letsencrypt.org/directory` |

Phases A–D assert that a digest did *not* move; phase E is the control that
proves that digest can move at all. Phase C's assertion has its own control —
the same stack with the fallback removed, where the CA *is* contacted — so
neither is a probe that could only ever pass.

**Not covered, and worth knowing before the window:**

* **Registration and issuance against Let's Encrypt itself.** Phase F reaches
  staging and gets a protocol-level answer from it (a 400 from `new-acct` is a
  full TLS handshake and a signed JWS round trip), then stops at the account
  contact: Let's Encrypt validates the contact address first and refuses both
  `.invalid` and `example.com`, and every address that would pass is a real
  one. Issuance is covered by phase E instead, against a CA that accepts the
  contact. **Let's Encrypt production was contacted by no phase.**
* **The real hostname, the real store and the real port 80.** Everything here
  runs on `handover.example.invalid` and on loopback. The first time the
  configuration meets the actual name is step 5.4(c).
* **The 24h renewal ticker.** Only the start-up check is exercised; the
  periodic one is Traefik's and is taken on trust.

### 2.3 GATE: the MySQL port is not published to the world

```
atrium-ddns-mysql-1   0.0.0.0:13353->3306/tcp
```

The new stack publishes MySQL on all interfaces. That is a laptop default (the
`.env.example` comment says so in as many words) and it is wrong on a host that
is about to become the real service. Before cutover, either bind it to loopback
(`MYSQL_HOST_PORT` with `127.0.0.1:`) or delete the mapping. Nothing in the
stack needs it — the api reaches MySQL over the compose network.

Not a cutover step; a thing that should not still be true afterwards.

### 2.4 GATE: the gate is green on the deployed commit

Full local gate on the commit that is deployed, and `scripts/deploy-verify.sh`
green against the host. A cutover onto a stack whose identity has not been
verified by content is a cutover onto an unknown.

### 2.5 Decide the owner account, and re-enrol 2FA afterwards

The importer offers two owner paths; both are exercised and both are 1.4 s:

* `--owner-email <existing>` — adopt an atrium user that already exists;
* `--owner-email <new> --create-owner` — create it from the legacy admin row,
  **carrying that row's bcrypt hash verbatim**, so the operator's existing web
  password keeps working.

Pick one now. If the deployed stack already has an admin account the operator
uses, adopt it.

Either way the importer prints this, and it is a cutover-day action item, not a
note:

> **NOTE: the admin row has a legacy TOTP secret. It is NOT migrated**: it is
> Fernet-encrypted under the legacy key and atrium owns its own enrolment, with
> its own table. Re-enrol in atrium before relying on two-factor on the new
> stack.

If the operator's only path into the admin account depends on that TOTP secret,
**re-enrol before cutover, not after**, or the first thing that happens after
traffic moves is a locked-out administrator.

### 2.6 Have the legacy Fernet key to hand, and know it is a 0600 trap

The importer needs `LEGACY_FERNET_KEY` — the old service's `FERNET_KEY`, the
only thing that can decrypt the stored Route 53 credential. It lives in
`/usr/local/dyndns-route53/.env`.

**The trap, hit twice now:** a key file copied into the `api` container arrives
owned by the copying uid with mode 0600, and the container runs as `app`
(uid 1000). `cat` fails, and #50's harness *refused* rather than quietly running
without the credential check. Fix at the point of copy:

```bash
docker compose cp <keyfile> api:/tmp/fernet.key
docker compose exec -T -u 0 api chmod 0644 /tmp/fernet.key   # readable by uid 1000
```

and delete it from the container when the import is done (§ 8).

### 2.7 GATE: `git status` on the deploy host is clean, and `git add -A` is safe

One command, in the checkout on the deploy host, before the window opens:

```bash
git status --porcelain --untracked-files=all      # expect: no output at all
```

**Why this is a gate and not housekeeping.** #76: that checkout held a
`.admin-credential` file — mode 0600, correct, untracked, and matched by no
pattern in `.gitignore`. So it showed as `??`, and § 5 is the one place in
the whole runbook where an operator is typing git commands under time
pressure. A hurried `git add -A` commits it, and the repository is public.
It was mitigated at the time by appending the name to `.git/info/exclude`,
which is local to that one checkout and travels to no other host — a
perfectly good *stopgap*, and the way to tell a stopgap from a fix is that
a stopgap is invisible to every other clone. The fix is the `#76` block in
`.gitignore`.

Three things worth knowing before you reach for that block:

* **Nothing in this repository writes that file, and nothing needs to.**
  `make seed-admin` passes the password to `app.scripts.seed_admin`, which
  prints its result and performs no filesystem write; `scripts/dev-admin.sh`
  reads the login out of 1Password and writes nothing either. The file was
  made by hand — so the patterns cover *families* (`*credential*`, `*.key`,
  `*.pem`, `*.db`, `*.sql`, `*.bundle`) rather than the one name, because
  the next hand-made one will be spelled differently.
* **`make seed-admin` puts the password in your shell history** (README says
  so, and § 2.5 is where you will be doing it). Clear the history entry
  afterwards. Do not solve it by writing the password to a file here.
* **A non-empty `git status` is the finding, not the noise.** If something
  appears that this block does not cover, it is a file family nobody
  anticipated: add it to `.gitignore` so the next host inherits the answer
  rather than the discovery.

If you want the pattern-level reading rather than the behavioural one —
which of the two spellings actually matched, and whether it would still
match on this Linux host rather than only on a case-insensitive laptop:

```bash
git -c core.ignorecase=false check-ignore -v -- <path>
```

---

## 3. ⛔ Copy, never open. Never `immutable`.

**Opening a WAL-mode SQLite database creates files beside it — even with
`mode=ro`.** Read-only WAL access needs the shared-memory file, so SQLite
creates `<db>-shm` (32 KB) and `<db>-wal` (0 bytes) whenever the directory is
writable:

```
before:              src.db
after mode=ro open:  src.db  src.db-shm  src.db-wal
```

Pointed at the running service's data directory, that is **a write into
production from an operation everyone would call read-only**. #49 hit it for
real: its first `--dry-run` left the sidecars behind, and its second run refused
because of them.

**And do not reach for `immutable=1` to avoid it.** `immutable=1` tells SQLite
to ignore the WAL entirely. On this database — § 1.1's 65 KB main file beside a
4.1 MB WAL — that returns a coherent, `integrity_check`-clean, entirely
plausible database **as of 19 July**: twenty-seven days stale, with no error
message anywhere. A guard that turns a loud failure into a quiet wrong answer is
worse than no guard, and this milestone's entire risk is quiet wrong answers
about production data.

**The only sanctioned reader is `scripts/copy-legacy-db.sh`**, which runs
`sqlite3 ".backup"` *inside the running container* — the one reader already
attached to that database, already holding its `-shm` — streams the bytes off
over ssh, and verifies sha256 on both sides plus `PRAGMA integrity_check`
locally. It refuses to overwrite an existing destination, because two rehearsals
that silently share one file are one rehearsal reported twice.

Measured, twice: **1.35 s** for `dyndns.db` (65 536 B), **1.61 s** for
`events.db` (217 088 B).

Verified after four copies in #50 and two more in this issue: the live file's
size and mtime are unchanged, and no new sidecar appeared.

---

## 4. The certificate clock — and a correction

The claim carried through #52 and this milestone's briefs is that *"the borrowed
copy goes stale around 24 Aug, and if cutover happens after that the copy is
already expired."* **The second half of that is wrong**, and the difference is
four weeks of headroom.

| date | what actually happens |
|---|---|
| **~24 Aug 2026** | The old Traefik reaches 30 days before expiry and renews. From this moment the old service serves a *new* certificate and the extracted copy serves the *old* one. The copy is **stale** — the two stacks present different certificates — but it is still a valid, trusted, unexpired Let's Encrypt certificate. |
| **23 Sep 2026, 14:53:29 GMT** | The copy actually expires. From here TLS on 8443 fails for real. |

*Provenance: § 1.6, two instruments — the extracted `cert.pem` and an
independent decode of the live ACME store — agreeing to the second.*

**Which side of the date does this plan assume?** Neither, and it does not have
to:

* **Cut over before ~24 Aug** and the question never arises. The store is copied
  (§ 2.2) before the old Traefik ever renews, the new stack owns ACME from that
  moment, and the old Traefik is stopped so it renews into a store nobody reads.
* **Cut over between 24 Aug and 23 Sep** and one extra step is needed *before*
  step 5.4: re-run `make tls-refresh` on the new stack so the fallback
  certificate is the current one, and copy the *renewed* store in § 5.4 rather
  than a stale one. Both take seconds. Nothing is expired; nothing is broken.
  Step 5.4(c) now does the first half for you. **But read § 2.2.3**: in this
  branch the new proxy may attempt a real renewal within seconds of binding
  443, which is fine and is also not what "the first renewal happens weeks
  later" led anyone to expect.
* **Cut over after 23 Sep** and the fallback is dead. It does not stop the
  cutover — ACME on the new stack issues a fresh certificate — but the insurance
  policy in § 2.2 is gone, so a failed ACME hand-over becomes a TLS outage
  rather than an inconvenience. If the date has passed, re-extract first
  (`make tls-refresh`) and confirm with `make tls-verify` before opening the
  window.

**The reason the copy must stop being a copy, whichever branch applies:** as
long as the old Traefik is running it holds the only ACME account key and is the
only writer to the only store. Two Traefiks writing one `acme.json` corrupt it.
So the hand-over is not "the new stack also gets ACME"; it is **"the old writer
stops, then the new one starts"**, in that order, with a copy of the store
taken in between. Step 5.4 does exactly that and no more.

---

## 5. The sequence

Times are wall-clock, measured on the development workstation against the real
population (6 devices, 11 hostnames, 1 domain, 1 backend). The deploy host is
slower and busier; treat them as lower bounds and read the ratios, not the
absolutes. Where a step has no measurement, it says so.

**Total hands-on time: under two minutes. Total elapsed to a confident verdict:
about four hours** (§ 5.6), and that asymmetry is the whole design.

---

### Step 5.1 — Snapshot the "before" picture · ~5 s · **rollback: n/a, read-only**

Take both legacy databases and keep them. This is the last moment the old
service's own account of the world exists in one piece.

```bash
mkdir -p -m 700 ~/cutover-$(date +%Y%m%d)
scripts/copy-legacy-db.sh ~/cutover-$(date +%Y%m%d)/dyndns-before.db
scripts/copy-legacy-db.sh ~/cutover-$(date +%Y%m%d)/events-before.db \
    --source /app/instance/events.db
```

Both print a sha256 taken in the container and again locally, and refuse if they
disagree. Record both digests in the change log — they are the provenance for
everything that follows.

**`events.db` is easy to forget and it is the one with a deadline.** It is a
*second* SQLite file in the same volume, holding 24 hours of telemetry and
nothing older (§ 1.3). It is not migrated, it is not read by the importer, and
the old service prunes it continuously. If it is not copied now, the 24 hours
either side of the cutover cannot be reconciled later — see § 7.

---

### Step 5.2 — Freeze the old service · ~2 s · **rollback: `docker compose start web`**

```bash
ssh <DEPLOY_HOST>
cd /usr/local/dyndns-route53
docker compose stop web            # leave traefik running
```

Only the application stops. Traefik stays up and now answers **502** for the
real hostname. That is deliberate:

* the source database stops moving, so the copy in 5.3 is a *final* state rather
  than a moving one;
* clients get a clean, loud failure rather than a hang or a wrong answer;
* a router that retries during the freeze loses nothing — every DynDNS client in
  this fleet retries, and § 1.3 shows the whole fleet re-sends within minutes.

**How long may the freeze last?** At 18.7 requests/hour the fleet sends roughly
one request every 3.2 minutes, and a `good` roughly every 85 minutes. A freeze
of the length this runbook implies (**about 30 seconds** from here to step 5.5)
is expected to miss **zero to one** request and, at the observed rate, a `good`
with probability under 1%. If the window slips past ten minutes, expect to have
refused about three requests; none of them is lost, because the client will send
the same thing again.

**Do not stop Traefik yet.** It is still holding 80/443 and it is still the
rollback.

*Rollback:* `docker compose start web` — the old service resumes on the same
data, having missed nothing.

---

### Step 5.3 — Copy and import · ~5 s · **rollback: wipe and redo, ~17 s**

```bash
# on the workstation
scripts/copy-legacy-db.sh /tmp/cutover/final.db          # 1.35 s
```

Copy it and the Fernet key into the api container (§ 2.6 for the 0600 trap),
then:

```bash
# dry run first — it does everything including decryption and the
# collision check, then rolls back                          # 1.39 s
docker compose exec -T api sh -c 'LEGACY_FERNET_KEY=$(cat /tmp/fernet.key) \
  /opt/venv/bin/python -m atrium_ddns.scripts.import_legacy \
  --source /tmp/final.db --owner-email <OWNER> --dry-run'

# then for real                                            # 1.38 s
docker compose exec -T api sh -c 'LEGACY_FERNET_KEY=$(cat /tmp/fernet.key) \
  /opt/venv/bin/python -m atrium_ddns.scripts.import_legacy \
  --source /tmp/final.db --owner-email <OWNER>'
```

**Read the dry run's output, do not skim it.** It reports its counts through two
differently-shaped instruments and prints five NOTEs, of which § 2.5's TOTP one
is an action item. The real run ends:

```
two instruments — rows read from the source vs rows in the target:
                 source  written   target
  devices             6        6        6
  domains             1        1        1
  backends            1        1        1
  hostnames          11       11       11

  owner: <…> (atrium user N, adopted)
  hostname -> device: 11 of 11 compared as a whole set, IDENTICAL
  provider credentials: route53=<digest>
  device password hashes: 6 of 6 byte-identical to the legacy rows

both instruments agree on every count.
```

**The credential digest is the thing to check by eye.** It is the sha256[:16] of
the decrypted Route 53 credential, and the literal value is deliberately not
written down here — it is a fingerprint of live production material, and this
repository is public. On the real run it was arrived at
five independent times — by #49, by #50 through the legacy repo's own
`decrypt_value`, and three times in this issue from a copy taken hours later
along both owner paths. **Compare it against what the earlier steps printed; if
it differs, stop.** It means the credential
decrypted to something other than what the old service reads, and an
empty-looking-but-migrated credential is the failure mode this milestone was
built to refuse.

The dry run reports target counts as `n/a — NOT MEASURED, because nothing was
committed`, explicitly not as `0`.

*Rollback:* the importer **refuses to run twice** rather than merging —

```
REFUSED: this database already holds rows this import would create:
  - 6 of 6 device username(s) already exist
  - 1 of 1 domain name(s) already exist
  - 11 of 11 hostname(s) already exist
```

— so a bad import is undone by wiping and redoing, which is measured end to end:

| | |
|---|---|
| `docker compose down -v` | 1.98 s |
| `docker compose up -d` (image built, empty volume, waits for MySQL healthy) | 11.39 s |
| `make migrate` (both alembic chains, fresh database) | 2.55 s |
| re-import | 1.37 s |
| **total** | **≈ 17 s** |

Seventeen seconds is the entire cost of getting this step wrong, which is why
it comes before anything irreversible.

---

### Step 5.4 — Hand over the certificate, then move the ports · ~10 s · **rollback: reverse it, ~5 s**

Do it in exactly this order. Each sub-step is reversible until the last.

**Before the window**, once, on the new stack — this is preparation, not a
cutover step, and it is the only part of § 2.2 that needs typing:

```bash
cd /usr/local/atrium-ddns
# The three variables § 2.2.1 lists. TRAEFIK_HOSTNAME and LETSENCRYPT_EMAIL
# are the values the OLD service's .env already holds — copy them across, do
# not type them from memory. LETSENCRYPT_CASERVER is the production directory.
$EDITOR .env
mkdir -p -m 700 letsencrypt
make acme-config >/dev/null && echo "the overlay loads"   # refuses if one is missing
```

Then, in the window:

```bash
cd /usr/local/dyndns-route53

# (a) stop the only ACME writer.  The store now has no writer at all,
#     which is the only safe moment to copy it.
docker compose stop traefik           # 80/443 are now free; nothing serves the hostname

# (b) copy the account and certificate to the new stack.
#     THE STEP WHOSE OMISSION IS SILENT — see § 2.2.2. Sub-step (e) is what
#     catches it.
cp letsencrypt/acme.json /usr/local/atrium-ddns/letsencrypt/acme.json
chmod 600 /usr/local/atrium-ddns/letsencrypt/acme.json

# (c) refresh the fallback, so the insurance policy is current rather than
#     whatever was extracted last. Seconds, and § 4 explains when it matters.
cd /usr/local/atrium-ddns
make tls-extract

# (d) start the new proxy on 80/443 — the hand-over itself.
make acme-up                          # == docker compose -f compose.yaml \
                                      #      -f compose.acme.yaml --profile tls up -d proxy

# (e) VERIFY WHICH CERTIFICATE IS ON THE WIRE. Not that TLS works — which
#     certificate. See § 2.2.2; this is the whole reason the step exists.
make acme-verify HOST=<TRAEFIK_HOSTNAME>
```

`make acme-verify` prints both fingerprints and one of two verdicts:

```
  store  : <sha256>  (expires <date>)
  wire   : <sha256>  (expires <date>)
  MATCH — the certificate on the wire is the one in this stack's own ACME
  store. The hand-over took: renewal will run from that store.
```

or

```
  MISMATCH — the wire is NOT serving this stack's ACME store.
  It is serving the extracted FALLBACK. That is the 'store was never
  copied' failure: TLS looks perfect and nothing will ever renew.
  Re-do runbook step 5.4(b) and restart the proxy.
```

**A `MISMATCH` here is not an emergency and does not need a rollback.** TLS is
terminating on the fallback, which is valid until § 1.6's date. Fix (b), then
`make acme-up` again — the container restarts in well under a second — and
re-run (e).

Measured on the workstation: proxy cold start **0.71 s**, restart **0.29 s**,
first TLS request through it answering `/api/healthz` **200 in 0.062 s**.
Stopping a container is **0.73 s**. Sub-step (b) is a 16 KB file copy.
`make acme-verify` is two decodes and one handshake.

**The borrowed arrangement ends here, and it ends in the right order.** The old
Traefik — the store's sole writer — is stopped *before* the copy is taken, so
there is never a moment with two writers. From (c) onward the new stack owns
ACME, renews into its own store, and the old store is inert. The extracted
`cert.pem` demoted to a fallback (§ 2.2) is the only thing still "borrowed", and
it is now insurance rather than infrastructure.

Between (a) and (c) the hostname is **down** — not wrong, down. That gap is the
sum of two container operations and a file copy: **under ten seconds**, against a
fleet that sends a request every 3.2 minutes.

*Rollback at (a):* `docker compose start traefik` in the old directory. Nothing
has changed.
*Rollback at (b):* delete the copy. Nothing has changed — the copy has no
writer.
*Rollback at (c):* nothing to undo; `make tls-extract` only reads the old
store and rewrites `certs/`, which nothing else consumes.
*Rollback at (d):* `make acme-down` on the new stack (== `docker compose -f
compose.yaml -f compose.acme.yaml --profile tls stop proxy`), then
`docker compose start traefik` on the old. The old Traefik resumes its store,
which it has never stopped owning. **Do this before step 5.5 and the cutover
has left no trace.**

**One asymmetry to know about before you need it.** Once the new proxy has
successfully renewed — which § 2.2.3 says can be seconds after (d), not weeks —
the copy in `/usr/local/atrium-ddns/letsencrypt/acme.json` has moved on and the
old service's own store has not. Rolling back still works: the old Traefik's
store still holds a certificate valid until § 1.6's date and it resumes serving
it. What is lost is that the two stores now hold *different* certificates, so a
second cutover attempt must re-copy rather than assume. That is bookkeeping,
not an outage, and it is the same shape as § 7.1's argument about `last_ip_*`.

---

### Step 5.5 — Unfreeze · ~1 s · **this is the last free rollback**

The new stack is already up; nothing needs starting. What changes is that real
clients now reach it.

Confirm the path end to end before declaring the step done:

```bash
curl -sS -o /dev/null -w '%{http_code} %{ssl_verify_result}\n' https://<HOSTNAME>/api/healthz
# expect: 200 0
```

`%{ssl_verify_result} 0` is the second instrument on § 4 — it says the chain
validated, which a `200` alone does not.

**Leave the old `web` container stopped, not removed.** Its volume holds the
source of truth, and starting it plus its Traefik is the entire rollback for the
next hour. Do not `docker compose down` the old project until § 8 says so.

*Rollback:* `docker compose --profile tls stop proxy` (new) →
`docker compose start web traefik` (old). Measured shape: two stops and two
starts, **under five seconds**. The old service resumes on data that is still
correct, because nothing has written to it since 5.2.

**From the moment a router successfully authenticates against the new stack,
this rollback stops being free.** § 7 says exactly what it costs and why the
usual phrasing of that sentence is slightly wrong.

---

### Step 5.6 — Verify, on a clock · **see § 6, which is the point of the exercise**

---

## 6. Verification — "a real client completed an update"

**The failure this section exists to catch: a router that cannot update is
silent.** Nobody notices until a dynamic address changes and a hostname stops
resolving, hours later. So "the containers are healthy" is not verification, and
neither is "the endpoint answers 200". Both are true of a stack that answers
`badauth` to every router in the fleet.

Three instruments, of three different shapes. Use all three; each one is blind
to something the others see.

### 6.1 The three instruments

| # | instrument | what it proves | what it cannot see |
|---|---|---|---|
| **A** | `ddns_device.last_seen_at` | a device **authenticated** — the column is written by `_touch_device`, which runs only after auth succeeds | whether the update itself worked |
| **B** | `ddns_event` filtered to `event_type='update'` | the wire answer each request got, per response code | a device that never called |
| **C** | `atrium_ddns.health_check.done` in the worker log | what **public DNS** says, resolved from outside the request path | anything faster than its 60 s tick |

Instrument C is the one that is not the stack's own opinion of itself. It runs
every 60 s, sweeps hostnames due per `health_check_interval_minutes` (default
15), and its first sweep after a worker start was measured at **exactly 60 s**.

### 6.2 The query, and why the obvious one is wrong

```sql
-- A: did anything authenticate?
SELECT COUNT(*) FROM ddns_device WHERE last_seen_at IS NOT NULL;

-- B: what did the wire say — and note the filter
SELECT event_type, response_code, COUNT(*)
FROM ddns_event
GROUP BY 1, 2;
```

**Do not use `SELECT COUNT(*) FROM ddns_event` as a liveness signal.** Measured
on a freshly imported database, before any client had authenticated:

```
BREAKDOWN: [('auth', 'badauth', 18)]
```

Eighteen rows already, every one an `auth`/`badauth` from unknown usernames, none
of them a client being served. A bare row count reads 18 and climbing and looks
exactly like success. The `event_type='update'` filter is what makes it an
instrument.

**Demonstrated that the instrument bites**, by driving one authenticated request
that provably contacted no provider (`nohost` — reachable only *after* the
credential verified):

| | before | after one authenticated `nohost` |
|---|---|---|
| devices with `last_seen_at` | 0 | **1** |
| `ddns_event` where `event_type='update'` | 0 | **1** |
| breakdown | `auth/badauth 18` | `auth/badauth 18`, **`update/nohost 1`** |

The 18 `badauth` rows did not move. Both instruments moved. That is the check
that the check works.

### 6.3 ⚠️ `last_updated_at` is not a liveness signal — and the code says so

The natural thing to look at after a cutover is "when did this hostname last
update", and it is the wrong column. `_persist_updates` writes
`last_ip_*` and `last_updated_at` **on `good` only**; a `nochg` leaves all three
untouched. The frozen compat table has a case named for it:
`tracked-ip-moves-only-on-good-so-last-updated-at-is-not-a-liveness-signal`.

Since § 1.3 says 96% of production traffic is `nochg`, an operator watching
`last_updated_at` after cutover sees **nothing move for hours on a perfectly
healthy fleet**, and concludes the opposite of the truth.

Watch `last_seen_at` (moves on every authenticated call, including a
rate-limited one) and `ddns_event`. Not `last_updated_at`.

### 6.4 What "silence" means, per device — and why waiting is not a plan

§ 1.2 and § 1.4, restated as the thing the operator actually needs:

| population | of | behaviour in 24 h before cutover | can waiting verify it? |
|---|---|---|---|
| devices that called | 3 | at least once | **yes** |
| devices that did not call at all | 3 | silent | **no** |
| hostnames that appeared | 4 | of 11 | yes |
| hostnames that did not | 7 | of 11, oldest update 148 days ago | **no** |

**For half the fleet, silence is the normal state and carries no information.**
No amount of waiting distinguishes "this device works and has nothing to say"
from "this device has been answering `badauth` since Tuesday". So the runbook
does not ask the operator to wait for them. It asks for two things instead:

1. **Verification by construction, already done.** #49 and #50 proved the six
   stored hashes are byte-identical to the legacy rows (`6 of 6`, digest
   `238bc46b15349ea1` both sides), that `auth_device`'s own hasher *identifies*
   all six rather than raising, and — the control that makes it meaningful —
   that `PasswordHash.recommended()` raises `UnknownHashError` on 6 of 6 of the
   same bytes. That is the whole chain except the plaintext, which does not
   exist anywhere and cannot be obtained (§ 9, gap G3).
2. **A deliberate probe for the quiet ones**, if the operator wants better than
   that: temporarily rotate one quiet device's secret through the UI and drive
   one request from a workstation. That is a real client completing a real
   update, on a device chosen rather than on whichever one happened to call.

### 6.5 The clock — what to expect, and when to worry

Derived from § 1.3's 18.7 requests/hour and § 1.2's cadence. Two hostnames run a
four-hour refresh (observed at `01:10, 05:11, 09:12, 13:13, 17:08, 21:09` and
`22:05, 02:05, 06:05, 10:06, 14:06, 18:07`); one device polls every ~3.3 minutes
and always answers `nochg`.

| elapsed | expect | if you do not see it |
|---|---|---|
| **60 s** | first `atrium_ddns.health_check.done` in the worker log | the worker is not running its jobs — check `ddns.init_worker.jobs_registered` |
| **~3 min** | first row in `ddns_event` with `event_type='update'`; at least one `last_seen_at` non-NULL | § 6.6 |
| **10 min** | ≥ 2 update rows; `last_seen_at` on ≥ 1 device | § 6.6 — three missed intervals is no longer noise |
| **~4 h** | the first `good`, from one of the two four-hourly hostnames — **this is the first proof of a real zone write** | § 6.6, and re-read § 2.1 |
| **24 h** | 3 of 6 devices seen, 4 of 11 hostnames in `ddns_event`, ~448 update rows | compare against the "before" copy of `events.db` from step 5.1 — same shape, same rate |

**The four-hour figure is why this runbook does not claim the cutover is
verified at the end of the window.** Authentication is provable in three minutes.
An actual zone write is not provable for about four hours, and the honest thing
to write in the change log at the end of the session is *"authentication and
provider-credential verified; first `good` pending, expected by <time>"*.

### 6.6 If it goes quiet — the three-way diagnosis

`badauth` is the loud version of the silent failure, and at 18.7 requests/hour
it becomes visible within minutes rather than hours. Read the two together:

| `last_seen_at` moving? | `badauth` climbing? | diagnosis |
|---|---|---|
| yes | no | **healthy.** Clients are authenticating and being answered. |
| no | **yes** | **the migration broke the credentials** — the epic's fleet-wide `badauth`. Roll back (§ 5.5) and re-read § 6.4's control. |
| no | no | **nothing is reaching the stack at all.** This is a routing or TLS fault, not an application one — check `%{ssl_verify_result}`, the proxy's access log, and that 443 is actually bound by the new proxy. |
| yes | some | normal. Scanners produce `badauth` against a public endpoint continuously; 18 arrived on a workstation stack within four minutes of it existing. |

**`ddns_event` cannot tell a wrong password from an unknown username** — both
write `device_id` NULL, because `auth` is `None` on the badauth path. The
container log can, and that distinction is the whole diagnosis:

```bash
docker compose logs api --since 15m | grep -o 'ddns\.auth\.[a-z_]*' | sort | uniq -c
```

| line | meaning |
|---|---|
| `ddns.auth.bad_password` | **the migrated row was found and its hash refused** — the fleet-wide failure |
| `ddns.auth.unknown_device` | no such row — a scanner, or a device that was never migrated |
| `ddns.auth.unrecognised_stored_hash` | the hasher cannot identify a stored hash — **badauth forever** for that device |
| `ddns.auth.password_rehashed` | a successful verify, opportunistically upgraded bcrypt → argon2id |

`ddns.auth.password_rehashed` is worth watching for its own sake: it is
**positive proof that a migrated bcrypt hash was verified by the running
service**, and #50 measured it as `6 x $2b$12$ -> 6 x $argon2`.

**The new stack's logs are readable with `docker compose logs`; the old
service's are not.** The old project ships both its containers' logs to Loki
through the `loki` log driver, so `docker compose logs` against
`dyndns-route53` returns nothing and that emptiness is not a measurement. The
new stack declares no logging driver, so its logs are local. Do not carry a
habit from one to the other.

---

## 7. What rolls back, what does not, and the deadline nobody sets

The issue says there is one point of no return: *the moment routers start
authenticating against the new database and its `last_ip_*` diverges from the
old.* That is the right place to look and it is not quite what happens there.
The honest version:

**Service rolls back at every step. Evidence does not.**

### 7.1 Why the service keeps rolling back

Rolling back after divergence does **not** break DNS. The decision to write is
not made from `last_ip_*` — it is made by `check_hostnameon_server`, which asks
public DNS. So a legacy service restarted with a stale `last_ip_v6` re-derives
the right answer from DNS on the next call and writes if it needs to. What is
stale is the *bookkeeping*: `last_updated_at`, `last_ip_*`, and anything the
legacy UI renders from them, wrong until each device next sends a `good` —
measured at about four hours for the two dynamic hostnames and **up to 148 days**
for the five that have never updated.

So the cost of a late rollback is *a wrong "last changed" column for up to
five months on rows nobody is watching*. Real, worth writing down, not an
outage.

### 7.2 The thing that genuinely cannot be undone, and it has a clock

**The legacy `events.db` retains 24 hours and prunes continuously** (§ 1.3:
448 rows spanning exactly 24 h). It is pruned by the old service on its write
path, so it prunes whenever the old service runs.

That means the window in which the two systems' histories can be reconciled —
"did we lose an update during the switch?", "was that device already failing
before we touched it?" — **closes 24 hours after cutover, by itself, whether
anyone looks or not.** After that the evidence is gone and no rollback recovers
it, because the rows were deleted rather than superseded.

This is the real one-way door and it is why step 5.1 copies `events.db` and not
just `dyndns.db`. With that copy the door stays open indefinitely. Without it,
the deadline is 24 hours and unsettable.

### 7.3 Marked irreversible, explicitly

| | irreversible? | note |
|---|---|---|
| 5.1 snapshot | no | read-only |
| 5.2 freeze | no | `docker compose start web` |
| 5.3 import | no | wipe and redo, ≈ 17 s (§ 5.3) |
| 5.4 (a) stop old Traefik | no | `start traefik` |
| 5.4 (b) copy ACME store | no | delete the copy; it has no writer |
| 5.4 (c) new proxy on 80/443 | no | stop it, start the old Traefik |
| 5.5 unfreeze | no | ≈ 5 s, four container operations |
| **first successful device auth** | **partially** | the device's stored hash is rewritten bcrypt → argon2id **in the new database**. The legacy database is untouched, so rollback works; but re-importing into the new database is refused, so "roll back, run a while, cut over again" requires a **fresh empty database and a fresh copy**, discarding whatever the new stack recorded. At 17 s, that is a price rather than a barrier. |
| **legacy `events.db` ages out** | **YES — 24 h after cutover** | § 7.2. Prevented only by the step-5.1 copy. |
| **new stack renews ACME** | no | the old store keeps a certificate valid to 23 Sep; rollback serves it |

---

## 8. The old service's data — what it is worth and how long it is kept

### 8.1 Do not delete the old stack on cutover night

Keep `dyndns-route53` **stopped, not removed**, for at least 24 hours — long
enough for § 6.5's four-hour `good` and a full daily cycle. `docker compose down`
on that project is the step that makes the rollback in § 5.5 stop existing.

### 8.2 What each artefact is worth

| artefact | size | what it is | keep |
|---|---|---|---|
| `dyndns.db` copy (step 5.1) | 65 KB + WAL | **the only record of the fleet's bcrypt hashes.** The new database rewrites each to argon2id on first successful auth (§ 7.3), so within hours of cutover this copy is the sole surviving copy of the credentials as the routers know them. Also the migration's provenance. | **indefinitely**, off the host |
| `events.db` copy (step 5.1) | 217 KB + WAL | 24 h of pre-cutover telemetry; the only thing the post-cutover `ddns_event` can be compared against | **90 days** |
| the live volume `dyndns-route53_dyndns-data` | — | the same two files, still attached to a stopped container | **30 days**, then remove the project |
| the legacy `FERNET_KEY` | 45 B | decrypts the Route 53 credential inside `dyndns.db` | keep, **stored apart from the database copy** |

**Store the database copy and the Fernet key in different places.** Together
they are the plaintext Route 53 credential; apart, neither is. This is the same
argument the plan already makes about `SECRET_ENCRYPTION_KEY` and the database
dump — *a dump plus the key in the same place defeats encryption at rest
entirely* — applied to the artefact this runbook creates.

### 8.3 The 30/90-day numbers, and why they are not round for the sake of it

* **90 days for `events.db`**: the new stack's own log retention is ~30 days
  (plan § 3.4), so 90 gives two full retention cycles in which a
  "was this happening before the migration?" question can still be answered
  against the old data. After that, the new stack's own history covers the same
  ground.
* **30 days for the live volume**: long enough that every device in § 6.4's
  quiet half has had a plausible opportunity to be exercised, short enough that
  a stopped container holding production credentials does not become permanent
  furniture.
* **Indefinitely for the `dyndns.db` copy**: it is 65 KB, and § 8.2 explains why
  destroying it destroys something that cannot be reconstructed.

### 8.4 Delete the working copies

Everything the import touched, on the day:

```bash
docker compose exec -T -u 0 api rm -f /tmp/final.db /tmp/fernet.key
docker compose exec -T api ls /tmp                 # confirm
rm -rf /tmp/cutover                                # workstation
```

The copies carry real bcrypt hashes and Fernet ciphertext. They are not the
archive; § 8.2's are. Verified empty on both sides before the session ends.

And the checkout itself, which § 2.7 gated going in:

```bash
git status --porcelain --untracked-files=all       # expect: nothing
```

`.gitignore` covers the loose key, database-copy and dump files this
sequence produces (#76), so the expected output is empty. **Empty because
the files are gone is not the same result as empty because they are
ignored** — `ls` the directory as well, since § 8.2 says the legacy copy is
kept indefinitely and only § 8.4's *working* copies are deleted. If
something untracked does appear, it is a file family nobody anticipated:
add it to `.gitignore` so the next host inherits the answer rather than the
discovery.

---

## 9. Named gaps — the questions a dry read-through would still ask

The acceptance criterion asks for a document that raises no questions, and says
to name the gaps rather than paper over them. These are the gaps.

**G1 — ~~the ACME hand-over configuration is not in the repo~~ CLOSED (#60).**
It is now `compose.acme.yaml` + `infra/traefik/dynamic-acme.yml` + three
variables in `.env.example`, gate-tested on a throwaway stack by
`make test-acme-handover` (60 checks, 0 failures). It went in as an **overlay**
rather than an edit to `compose.yaml`, because editing the deployed `proxy`
service in place would have let a routine `docker compose up -d` perform the
hand-over by accident — § 2.2.1.

Two things the gate test found that this section did not know:

* the fallback certificate **suppresses first issuance** (§ 2.2.2), which makes
  a skipped step 5.4(b) invisible to every check an operator would normally
  run — hence step 5.4(e), `make acme-verify`;
* a renewal can fire **seconds** after the hand-over rather than weeks later
  (§ 2.2.3), which puts `LETSENCRYPT_CASERVER` and port 80 on the critical path
  after all.

What remains open is narrower and is listed in § 2.2.4: no phase registered or
issued against Let's Encrypt itself (staging was reached and answered;
production was never contacted), and nothing has met the real hostname.

**G2 — the AAAA gate has been measured on the workstation, not on the deploy
host.** § 2.1 gives the exact one-line probe and both possible outcomes. This is
the highest-consequence unknown in the document: two thirds of production
traffic depends on the answer, and the failure is silent in the direction that
generates 288 unnecessary Route 53 writes a day. *Effort: one command. Do it
first.*

**G3 — no device's plaintext password exists, and none can be obtained.** The
legacy service stores bcrypt only (`bcrypt.hashpw(...)` in `web_routes.py`,
verified through `bcrypt.checkpw` in `auth.py`). There is no plaintext, no
reversible copy, no recovery path. So *"log in as a real device and update"* is
unachievable as a pre-cutover check, and #50 corrected its own acceptance
criterion for exactly this reason. What replaces it is § 6.4's two halves: the
byte-identity evidence, and the first real client after cutover. **Anyone who
later writes a check claiming to present an original device credential is
presenting one it minted.**

**G4 — the first real zone write cannot be observed before cutover.** § 1.5
shows `nochg` proves everything up to and including the Route 53 credential
while writing nothing, which is as far as it is possible to go. The last link,
`change_resource_record_sets`, is proved in-process behind two guards (#50) and
in production by the first `good` — about four hours after cutover. **There is
no arrangement in which that link is verified against the live zone before the
live zone is the thing being served.** That is a property of the problem, not an
omission.

**G5 — timings are measured on the development workstation.** Every wall-clock
figure in § 5 came from a real run against the real population, on a laptop with
a locally built image and a warm docker cache. The deploy host is a smaller,
busier box (2.9 G RAM available, three other containers). The *ratios* transfer;
the absolutes are lower bounds. The number most likely to be wrong is the 11.4 s
cold `up -d`, which is dominated by MySQL initialising an empty volume.

**G6 — a mid-window abort between 5.4(a) and 5.4(c) has no measured timing.**
The rollback is `docker compose start traefik` and it is one container start,
so ~1 s by analogy with every other container operation here. It has not been
timed against the old stack's Traefik specifically, because that means starting
and stopping a production container.

---

## Appendix A — measurements, and where each came from

Every wall-clock figure in this document, with its instrument. Nothing here is
estimated.

| operation | measured | instrument |
|---|---|---|
| `copy-legacy-db.sh` (`dyndns.db`, 65 536 B) | **1.35 s** | `time`, workstation → deploy host over ssh |
| `copy-legacy-db.sh` (`events.db`, 217 088 B) | **1.61 s** | same |
| `import_legacy --dry-run` | **1.39 s** | `date` either side, in-container |
| `import_legacy` (adopt existing owner) | **1.38 s** | same |
| `import_legacy --create-owner` | **1.37 s** | same |
| `import_legacy`, second run (refusal, exit 2) | **1.35 s** | same |
| `docker compose down -v` | **1.98 s** | `date` either side |
| `docker compose up -d` (built image, empty volume) | **11.39 s** | same |
| `make migrate`, both chains, fresh database | **2.55 s** | same |
| `docker compose stop api worker` | **0.73 s** | same |
| `docker compose start api worker` (HTTP 200 immediately after) | **1.24 s** | same + `curl` |
| `docker compose --profile tls up -d proxy` (cold) | **0.71 s** | same |
| `docker compose --profile tls restart proxy` | **0.29 s** | same |
| TLS request through the proxy to `/api/healthz` | **200 in 0.062 s** | `curl -w '%{time_total}'` |
| first `health_check.done` after worker start | **60 s** | worker log timestamps, 21:24:07 → 21:25:07 |
| fleet request rate | **18.7 / h** (448 rows / 24 h) | legacy `events.db` copy |
| fleet `good` rate | **17 / 24 h** ≈ one per 85 min | same |
| dynamic-hostname cadence | **4 h**, two hostnames | same, timestamps in § 6.5 |

## Appendix B — related documents

* `docs/ops/refactor-plan.md` § 5b — deploying beside the old service
* `docs/ops/refactor-plan.md` § 5c — behaviour changes accepted at cutover
* `scripts/copy-legacy-db.sh` — the only sanctioned reader of the live database
* `scripts/extract-acme-cert.sh` — why the certificate is borrowed and what that costs
* `compose.acme.yaml` — the hand-over overlay, and why it is not an edit to `compose.yaml`
* `infra/traefik/dynamic-acme.yml` — the router after the hand-over, and the fallback's side effect
* `scripts/test-acme-handover.sh` — the gate test behind § 2.2.4 (`make test-acme-handover`)
* `scripts/verify-acme-handover.sh` — step 5.4(e) (`make acme-verify`)
* `backend/src/atrium_ddns/scripts/import_legacy.py` — the importer and its ten refusals
* `backend/src/atrium_ddns/scripts/rehearse_migration.py` — the rehearsal harness (#50)
