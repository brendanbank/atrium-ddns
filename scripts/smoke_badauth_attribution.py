"""#64 smoke — three differently-shaped instruments on one number.

Exit-criterion clause 2 of V1M7 (#109): *a tenant filtering their log
for ``badauth`` sees their own failures, with the count agreeing across
two differently-shaped instruments.* This is the demonstration, and it
lives in the repository rather than in a transcript so close-out can
**re-run** it instead of quoting a reading on file.

Run it inside the api container, against a stack raised by
``make e2e-up`` (it needs ``ATRIUM_DDNS_COMPAT_STUB=1`` for ``stub1``)::

    docker compose -p <project> cp scripts/smoke_badauth_attribution.py \
        api:/tmp/smoke_badauth_attribution.py
    docker compose -p <project> exec -T \
        -e SMOKE_ADMIN_EMAIL=admin@example.com \
        -e SMOKE_ADMIN_PASSWORD=e2e-pw-12345 \
        -e SMOKE_ADMIN_TOTP=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP \
        api /opt/venv/bin/python /tmp/smoke_badauth_attribution.py

It mints its own ``user``-role tenant through atrium's invite flow, its
own zone, device and name, and namespaces every row it writes by a
``client_ip`` unique to the run — so it is repeatable and it reads none
of anyone else's traffic.

**It does not clean up after itself**, deliberately: the rows are the
evidence and deleting them would leave nothing to check by hand. Run
``make e2e-down`` afterwards if the same stack is going to carry the
Playwright specs again — the volume is the reset, not this script.

  A. the WIRE      — how many `/nic/update` calls came back `badauth`,
                     counted from HTTP response bodies. Never touches
                     the database.
  B. the LOG API   — `GET /api/atrium_ddns/events?response_code=badauth`
                     under the tenant's own session cookie. Never sees
                     the wire.
  C. raw SQL       — `SELECT ... GROUP BY user_id IS NULL` straight at
                     MySQL. Never sees the app.

The whole point is that A and B are computed by different machinery
from different inputs, so agreement is evidence rather than tautology.
Exit code is non-zero on any disagreement.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import sys
import time

import httpx
import sqlalchemy as sa

BASE = "http://localhost:8000"
API = f"{BASE}/api"
ADMIN_EMAIL = os.environ["SMOKE_ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["SMOKE_ADMIN_PASSWORD"]
ADMIN_TOTP = os.environ["SMOKE_ADMIN_TOTP"]

#: How many of each kind of failure to send. Deliberately different, so
#: a reading that confused the two would be visibly wrong rather than
#: coincidentally right.
N_WRONG_PASSWORD = 3
N_UNKNOWN_USERNAME = 5


def totp(secret: str) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = struct.pack(">Q", int(time.time()) // 30)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


def must(resp: httpx.Response, what: str, *ok: int) -> httpx.Response:
    if resp.status_code not in ok:
        raise SystemExit(f"{what}: {resp.status_code} {resp.text[:400]}")
    return resp


def main() -> int:
    suffix = f"{int(time.time())}-{os.getpid()}"

    # ---------------- admin: mint a plain `user`-role tenant ----------
    admin = httpx.Client(base_url=API, timeout=30)
    must(
        admin.post(
            "/auth/jwt/login",
            data={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        ),
        "admin login",
        200,
        204,
    )
    must(
        admin.post("/auth/totp/verify", json={"code": totp(ADMIN_TOTP)}),
        "admin totp",
        200,
        204,
    )

    email = f"badauth-{suffix}@example.com"
    password = "Tenant-Pw-12345!"
    invite = must(
        admin.post(
            "/invites",
            json={
                "email": email,
                "full_name": "badauth smoke",
                "role_codes": ["user"],
            },
        ),
        "invite",
        201,
    ).json()

    # ---------------- the tenant, on its own cookie jar ---------------
    tenant = httpx.Client(base_url=API, timeout=30)
    must(
        tenant.post(
            "/invites/accept", json={"token": invite["token"], "password": password}
        ),
        "accept",
        200,
        201,
        204,
    )
    must(
        tenant.post(
            "/auth/jwt/login", data={"username": email, "password": password}
        ),
        "tenant login",
        200,
        204,
    )
    secret = must(tenant.post("/auth/totp/setup"), "totp setup", 200).json()["secret"]
    must(
        tenant.post("/auth/totp/confirm", json={"code": totp(secret)}),
        "totp confirm",
        200,
        204,
    )

    # ---------------- a zone, a device, a name ------------------------
    zone_name = f"badauth-{suffix}.example.invalid"
    zone = must(
        tenant.post(
            "/atrium_ddns/domains",
            json={
                "name": zone_name,
                "backend": {
                    "backend_type": "stub1",
                    "config": {"result": "good", "ttl": 300},
                    "credentials": {"stub_token": "smoke-fixture-not-a-secret"},
                },
            },
        ),
        "zone",
        200,
        201,
    ).json()
    device = must(
        tenant.post("/atrium_ddns/devices", json={"name": f"router-{suffix}"}),
        "device",
        200,
        201,
    ).json()
    ddns_username = device["device"]["username"]
    device_id = device["device"]["id"]
    # The real secret, so the CONTROL below is a real success
    # rather than another failure: a run in which nothing ever
    # authenticated would agree at 0 == 0 and prove nothing.
    ddns_secret = device["secret"]
    hostname = f"home.{zone_name}"
    must(
        tenant.post(
            "/atrium_ddns/hostnames",
            json={
                "domain_id": zone["id"],
                "device_id": device_id,
                "name": hostname,
            },
        ),
        "hostname",
        200,
        201,
    )

    # ================= INSTRUMENT A — the wire ========================
    # No database, no session. Just what a router would have seen.
    unknown_username = f"no-such-router-{suffix}"
    # Per RUN, not a constant. The unattributable rows are owned by
    # nobody, so nothing scopes them away between runs: with a fixed
    # address the second run of this script reads its own rows plus the
    # first run's and reports 10 where the wire says 5. Found by asking
    # what this instrument does the second time, which is the question
    # a one-shot smoke usually forgets to ask.
    client_ip = f"2001:db8:64::{os.getpid():x}:{int(time.time()) & 0xFFFF:x}"
    wire = httpx.Client(base_url=BASE, timeout=30)
    wire_badauth_wrong_password = 0
    wire_badauth_unknown_username = 0

    for _ in range(N_WRONG_PASSWORD):
        r = wire.get(
            "/nic/update",
            params={"hostname": hostname, "myip": "198.51.100.1"},
            auth=(ddns_username, "definitely-not-the-secret"),
            headers={"X-Forwarded-For": client_ip},
        )
        if r.status_code == 200 and r.text.strip() == "badauth":
            wire_badauth_wrong_password += 1

    for _ in range(N_UNKNOWN_USERNAME):
        r = wire.get(
            "/nic/update",
            params={"hostname": hostname, "myip": "198.51.100.1"},
            auth=(unknown_username, "irrelevant"),
            headers={"X-Forwarded-For": client_ip},
        )
        if r.status_code == 200 and r.text.strip() == "badauth":
            wire_badauth_unknown_username += 1

    # THE CONTROL. One call with the *right* credential, from the same
    # address. Without it every number below could be zero and the whole
    # run would still report five agreements — the vacuous pass this
    # repository catalogues as "the probe that could not fail".
    control = wire.get(
        "/nic/update",
        params={"hostname": hostname, "myip": "198.51.100.1"},
        auth=(ddns_username, ddns_secret),
        headers={"X-Forwarded-For": client_ip},
    )
    control_ok = control.status_code == 200 and control.text.strip().startswith(
        ("good", "nochg")
    )

    wire_total = wire_badauth_wrong_password + wire_badauth_unknown_username

    # ================= INSTRUMENT B — the log API =====================
    # The tenant's own read, through DdnsScope. It has never seen a
    # response body from `/nic/*`.
    page = must(
        tenant.get(
            "/atrium_ddns/events",
            params={"response_code": "badauth", "client_ip": client_ip},
        ),
        "events",
        200,
    ).json()
    api_rows = len(page["rows"])
    tally = page["unattributable"]

    # ================= INSTRUMENT C — raw SQL =========================
    # Neither the wire nor the app. Straight at the table.
    engine = sa.create_engine(
        os.environ["DATABASE_URL"].replace("mysql+aiomysql", "mysql+pymysql")
    )
    with engine.connect() as conn:
        sql_rows = conn.execute(
            sa.text(
                "SELECT user_id IS NULL AS orphan, COUNT(*) AS n "
                "FROM ddns_event WHERE response_code='badauth' "
                "AND client_ip=:ip GROUP BY orphan"
            ),
            {"ip": client_ip},
        ).all()
    sql = {int(r.orphan): int(r.n) for r in sql_rows}
    sql_attributed = sql.get(0, 0)
    sql_orphan = sql.get(1, 0)

    # ---------------- report ------------------------------------------
    print()
    print("=" * 68)
    print("#64 — badauth attribution, three instruments on one number")
    print("=" * 68)
    print(f"  tenant           {email}")
    print(f"  ddns username    {ddns_username}")
    print(f"  client_ip        {client_ip}  (the whole namespace of this run)")
    print()
    print("A. THE WIRE — HTTP response bodies from /nic/update, no DB")
    print(f"     sent with a WRONG PASSWORD on a real username : {N_WRONG_PASSWORD}")
    print(f"       answered 'badauth'                          : {wire_badauth_wrong_password}")
    print(f"     sent with a username NOBODY HOLDS             : {N_UNKNOWN_USERNAME}")
    print(f"       answered 'badauth'                          : {wire_badauth_unknown_username}")
    print(f"     total badauth on the wire                     : {wire_total}")
    print()
    print("B. THE LOG API — GET /events?response_code=badauth, tenant cookie")
    print(f"     rows the tenant can see                       : {api_rows}")
    print(f"     unattributable tally                          : {tally}")
    print()
    print("C. RAW SQL — SELECT ... GROUP BY user_id IS NULL")
    print(f"     rows with an owner                            : {sql_attributed}")
    print(f"     rows with user_id IS NULL                     : {sql_orphan}")
    print()

    failures: list[str] = []

    def check(label: str, left: object, right: object) -> None:
        verdict = "AGREE" if left == right else "DISAGREE"
        print(f"  [{verdict:8}] {label}: {left!r} vs {right!r}")
        if left != right:
            failures.append(label)

    print("Control — the same credential, correct:")
    print(f"     wire answered                                 : {control.text.strip()!r}")
    print(f"     a successful update happened                  : {control_ok}")
    print()
    print("Agreement:")
    check(
        "A(wrong password) vs B(rows the tenant sees)",
        wire_badauth_wrong_password,
        api_rows,
    )
    check(
        "A(wrong password) vs C(rows with an owner)",
        wire_badauth_wrong_password,
        sql_attributed,
    )
    check(
        "A(unknown username) vs B(unattributable tally)",
        wire_badauth_unknown_username,
        tally["rows"] if tally else None,
    )
    check(
        "A(unknown username) vs C(rows with user_id IS NULL)",
        wire_badauth_unknown_username,
        sql_orphan,
    )
    check("A(total) vs B(rows) + B(tally)", wire_total, api_rows + (tally or {}).get("rows", 0))
    # Vacuity: every figure above is zero unless the wire actually
    # refused, and a run that never authenticated successfully is not
    # a run against a working service.
    check("vacuity — the wire refused at all", wire_total > 0, True)
    check("vacuity — the credential works when correct", control_ok, True)

    print()
    print("What this would print if attribution were absent — the probe question:")
    print("  B(rows the tenant sees) would be 0 while A(wrong password) is")
    print(f"  {N_WRONG_PASSWORD}, and the first two checks would read DISAGREE. That is the")
    print("  state this repository shipped before #64, and it is why 'rows: 0'")
    print("  on its own is not evidence of anything.")
    print()

    if failures:
        print(f"FAILED: {len(failures)} disagreement(s): {failures}")
        return 1
    print("PASSED: 7 of 7 agreements, across three differently-shaped instruments.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
