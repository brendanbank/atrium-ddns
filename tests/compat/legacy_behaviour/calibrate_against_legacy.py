"""Replay model_cases.yaml against the LEGACY implementation.

The second instrument. `model_cases.yaml` is one reading of the legacy
behaviour -- authored by reading `models.py`, `dyndns.py`, `rate_limiter.py`,
`health_checker.py`, `forms.py` and `lib/account/hetzner.py`. This script is the
other: it stands the legacy Flask app up on a throwaway SQLite database and
executes each rule against it. Prints one line per case:

    AGREE     the legacy implementation behaves as the case describes it. For
              a `preserve` case that is also what the host must do; for a `fix`
              or `change` case it confirms the case's `legacy` field, which is
              the behaviour the rewrite is deliberately leaving behind
    DISAGREE  it does not -- fix the case, or the case is right and this is
              a harness bug. Both have happened; see below

This is deliberately NOT a pytest module. It cannot run in the gate or in CI:
it needs a checkout of `brendanbank/dyndns-route53` and that repo's own
virtualenv (Flask 2, flask-sqlalchemy, bcrypt, dnspython, cryptography), and
neither is present in the api container. It is a calibration run whose output
belongs in a PR body, in the same way protocol_cases.yaml was calibrated
against `BaseAccount` by issue #7.

Run:

    DYNDNS_LEGACY_ROOT=/path/to/dyndns-route53 \\
        /path/to/dyndns-route53/.venv/bin/python calibrate_against_legacy.py

Exit code is 0 when nothing disagrees.

One warning about this harness, learned by walking into it. It builds several
apps against several throwaway databases, and user ids restart at 1 in each. A
first version reused one app for the rate-limit checks and created its test
user *after* recording events against user id 1 -- so the new user inherited
five events and the limiter refused it immediately. That printed as a
DISAGREE against the legacy code, and it was entirely this file's fault. If a
rate-limit case disagrees, check the ids before you believe it.
"""
import base64
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

LEGACY = os.environ.get("DYNDNS_LEGACY_ROOT", "/Users/brendan/src/dyndns-route53")
if not os.path.isdir(LEGACY):
    sys.exit(
        f"legacy checkout not found at {LEGACY}.\n"
        "Set DYNDNS_LEGACY_ROOT. Refusing to run: a calibration that silently "
        "measures nothing is worse than one that does not run."
    )
sys.path.insert(0, LEGACY)
os.chdir(LEGACY)

for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "HETZNER_API_TOKEN",
          "DOMAINS", "USERNAME", "PASSWORD", "ADMIN_PASSWORD"):
    os.environ.pop(v, None)

from cryptography.fernet import Fernet  # noqa: E402
import bcrypt  # noqa: E402

RESULTS = []


def record(case_id, ok, detail=""):
    RESULTS.append((case_id, ok, detail))
    tag = "AGREE   " if ok else "DISAGREE"
    print(f"{tag} {case_id}{('  -- ' + detail) if detail else ''}")


class Cfg:
    TESTING = True
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "k"
    FERNET_KEY = Fernet.generate_key().decode()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    def __init__(self, d):
        self.SQLALCHEMY_DATABASE_URI = f"sqlite:///{d}/dyndns.db"
        self.SQLALCHEMY_BINDS = {"events": f"sqlite:///{d}/events.db"}


def build():
    d = tempfile.mkdtemp()
    from dyndns import create_app
    app = create_app(config_class=Cfg(d))
    return app


def seed(app, *, backend_type="aws", with_creds=True, ttl=60, select_backend=False):
    from models import db, User, Domain, DomainBackend, BackendConfig, Hostname, encrypt_value
    with app.app_context():
        u = User(username="u1", password_hash=bcrypt.hashpw(b"pw123456", bcrypt.gensalt()).decode(),
                 role="user", is_active=True, web_login=False)
        db.session.add(u)
        db.session.commit()
        dom = Domain(name="example.com")
        db.session.add(dom)
        db.session.commit()
        b = DomainBackend(domain_id=dom.id, backend_type=backend_type)
        db.session.add(b)
        db.session.commit()
        if with_creds:
            for k, v in [("aws_access_key_id", "x"), ("aws_secret_access_key", "y")]:
                db.session.add(BackendConfig(domain_backend_id=b.id, config_key=k,
                                             config_value=encrypt_value(v)))
        hn = Hostname(name="ok.example.com", domain_id=dom.id, user_id=u.id, ttl=ttl)
        db.session.add(hn)
        db.session.commit()
        if select_backend:
            hn.backends = [b]
            db.session.commit()
        return {"user": u.id, "domain": dom.id, "backend": b.id, "hostname": hn.id}


AUTH = {"Authorization": "Basic " + base64.b64encode(b"u1:pw123456").decode()}


def stub_accounts(statuses):
    """Patch dyndns.Accounts so each backend returns the next status in turn."""
    it = iter(statuses)

    def _get(account_dict):
        acct = MagicMock()
        acct.hostnameperzone.return_value = {"example.com": ["ok.example.com"]}
        st = next(it)
        acct.createrecords.return_value = {"ok.example.com": st}
        acct.deleterecords.return_value = {"ok.example.com": st}
        return acct

    m = MagicMock()
    m.get.side_effect = _get
    return patch("dyndns.Accounts", m)


# --------------------------------------------------------------------------
# ttl
# --------------------------------------------------------------------------
def c_ttl():
    app = build()
    from models import db, Hostname, Domain, User
    ids = seed(app)
    with app.app_context():
        hn = Hostname(name="d.example.com", domain_id=ids["domain"], user_id=ids["user"])
        db.session.add(hn)
        db.session.commit()
        record("ttl-default-is-60", hn.ttl == 60, f"ttl={hn.ttl}")

    # bounds, from the form validator (unbound field — no request context needed)
    from forms import TTLForm
    from wtforms.validators import NumberRange
    lo = hi = None
    for v in TTLForm.ttl.kwargs.get("validators", []):
        if isinstance(v, NumberRange):
            lo, hi = v.min, v.max
    record("ttl-below-30-is-rejected", lo == 30, f"min={lo}")
    record("ttl-above-86400-is-rejected", hi == 86400, f"max={hi}")
    record("ttl-30-and-86400-are-accepted", (lo, hi) == (30, 86400),
           "NumberRange bounds are inclusive")

    # ttl reaches createrecords / is absent from deleterecords
    app2 = build()
    seed(app2, ttl=300)
    client = app2.test_client()
    seen = {}

    def _get(account_dict):
        acct = MagicMock()
        acct.hostnameperzone.return_value = {"example.com": ["ok.example.com"]}
        acct.createrecords.side_effect = lambda *a, **k: (seen.update(create=k) or
                                                          {"ok.example.com": "good"})
        acct.deleterecords.side_effect = lambda *a, **k: (seen.update(delete=k) or
                                                          {"ok.example.com": "good"})
        return acct

    m = MagicMock()
    m.get.side_effect = _get
    with patch("dyndns.Accounts", m):
        client.get("/nic/update?hostname=ok.example.com&myip=192.0.2.1", headers=AUTH)
        client.get("/nic/delete?hostname=ok.example.com", headers=AUTH)
    record("ttl-reaches-the-create-call", seen.get("create", {}).get("ttl") == 300,
           f"create kwargs={seen.get('create')}")
    record("ttl-is-absent-from-the-delete-call", "ttl" not in seen.get("delete", {}),
           f"delete kwargs={seen.get('delete')}")


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------
def c_backends():
    app = build()
    ids = seed(app)
    from models import db, Hostname, DomainBackend
    with app.app_context():
        hn = Hostname.query.get(ids["hostname"])
        record("backends-empty-selection-resolves-to-all-of-the-domains-backends",
               list(hn.backends) == [] and [b.id for b in hn.get_backends()] == [ids["backend"]],
               f"selection={list(hn.backends)} resolved={[b.id for b in hn.get_backends()]}")
        # add a second backend to the domain; empty selection tracks it live
        b2 = DomainBackend(domain_id=ids["domain"], backend_type="hetzner")
        db.session.add(b2)
        db.session.commit()
        hn = Hostname.query.get(ids["hostname"])
        resolved = [b.id for b in hn.get_backends()]
        record("backends-empty-selection-tracks-the-domain-live", len(resolved) == 2,
               f"resolved={resolved}")
        # explicit selection wins and does NOT track
        hn.backends = [DomainBackend.query.get(ids["backend"])]
        db.session.commit()
        hn = Hostname.query.get(ids["hostname"])
        resolved2 = [b.id for b in hn.get_backends()]
        record("backends-explicit-selection-wins", resolved2 == [ids["backend"]],
               f"resolved={resolved2} while the domain has 2")
        record("backends-resolution-order-decides-the-aggregate-error",
               [b.id for b in hn.domain.backends] == sorted(b.id for b in hn.domain.backends),
               "domain.backends is primary-key order")

    app2 = build()
    from models import db as db2, Domain, Hostname as H2, User as U2
    with app2.app_context():
        u = U2(username="u2", password_hash=bcrypt.hashpw(b"p", bcrypt.gensalt()).decode(),
               role="user", is_active=True)
        db2.session.add(u)
        db2.session.commit()
        d = Domain(name="nb.example.com")
        db2.session.add(d)
        db2.session.commit()
        h = H2(name="x.nb.example.com", domain_id=d.id, user_id=u.id)
        db2.session.add(h)
        db2.session.commit()
        record("backends-a-domain-with-no-backends-resolves-to-none",
               list(h.get_backends()) == [], f"resolved={list(h.get_backends())}")


# --------------------------------------------------------------------------
# rate limit
# --------------------------------------------------------------------------
def c_rate_limit():
    app = build()
    from rate_limiter import RateLimiter, check_rate_limit, rate_limiter
    from models import db, RateLimitConfig, RateLimitEvent, User
    rl = RateLimiter()
    with app.app_context():
        record("rate-limit-triggers-at-greater-than-or-equal",
               (not rl.is_rate_limited(1, per_minute=5, per_hour=100)) and
               all(rl.record_request(1) is None for _ in range(5)) and
               rl.is_rate_limited(1, per_minute=5, per_hour=100),
               "5 recorded, limit 5 -> blocked")
        record("rate-limit-counters-are-per-user",
               not rl.is_rate_limited(2, per_minute=5, per_hour=100),
               "user 2 unaffected by user 1's 5 events")
        for _ in range(5):
            rl.record_request(3)
        record("rate-limit-per-minute-and-per-hour-are-independent",
               rl.is_rate_limited(3, per_minute=100, per_hour=5) and
               not rl.is_rate_limited(3, per_minute=100, per_hour=100),
               "hour limit fires with the minute limit slack")

        # window boundary: an event exactly 60s old must NOT count
        ev = RateLimitEvent(user_id=9, created_at=datetime.now(timezone.utc) - timedelta(seconds=60))
        db.session.add(ev)
        db.session.commit()
        record("rate-limit-window-boundary-is-exclusive",
               not rl.is_rate_limited(9, per_minute=1, per_hour=100),
               "created_at > now-60s is strict")

        # global prune at 1h on the write path, across users
        old = RateLimitEvent(user_id=77, created_at=datetime.now(timezone.utc) - timedelta(hours=2))
        db.session.add(old)
        db.session.commit()
        before = RateLimitEvent.query.filter_by(user_id=77).count()
        rl.record_request(88)
        after = RateLimitEvent.query.filter_by(user_id=77).count()
        record("rate-limit-events-are-pruned-globally-at-one-hour-on-the-write-path",
               before == 1 and after == 0, f"user 77's 2h-old row: {before} -> {after} "
                                           "after user 88 recorded")

        # config precedence
        g = RateLimitConfig.query.filter_by(is_global=True).first()
        record("rate-limit-seeded-default-is-30-per-minute-and-500-per-hour",
               g is not None and (g.requests_per_minute, g.requests_per_hour) == (30, 500),
               f"{(g.requests_per_minute, g.requests_per_hour) if g else None}")
    # Fresh app: the counters above were recorded against user ids 1/3/9/88 in
    # this database, and a new User would be handed id 1 and inherit them.
    # (That is exactly what the first run of this harness did, and it reported a
    # false disagreement against the legacy code.)
    appP = build()
    with appP.app_context():
        u = User(username="rl", password_hash="x", role="user", is_active=True)
        db.session.add(u)
        db.session.commit()
        uid = u.id
        g = RateLimitConfig.query.filter_by(is_global=True).first()
        no_override = check_rate_limit(u)          # falls through to the global 30/min
        db.session.add(RateLimitConfig(user_id=uid, is_global=False,
                                       requests_per_minute=1, requests_per_hour=1))
        db.session.commit()
        u = User.query.get(uid)
        before = check_rate_limit(u)
        rate_limiter.record_request(uid)
        after = check_rate_limit(u)
        record("rate-limit-config-precedence-is-user-then-global-then-hardcoded",
               no_override is False and before is False and after is True
               and (g.requests_per_minute, g.requests_per_hour) == (30, 500),
               f"global {g.requests_per_minute}/min allows 1 event; the per-user "
               f"override of 1/min then blocks it (before={before} after={after})")

    # a blocked request is not recorded
    app2 = build()
    ids = seed(app2)
    from models import RateLimitConfig as RLC, RateLimitEvent as RLE, db as db3
    with app2.app_context():
        db3.session.add(RLC(user_id=ids["user"], is_global=False,
                            requests_per_minute=1, requests_per_hour=1))
        db3.session.commit()
    cl = app2.test_client()
    with stub_accounts(["good"] * 10):
        cl.get("/nic/update?hostname=ok.example.com&myip=192.0.2.1", headers=AUTH)
        with app2.app_context():
            n1 = RLE.query.filter_by(user_id=ids["user"]).count()
        for _ in range(4):
            r = cl.get("/nic/update?hostname=ok.example.com&myip=192.0.2.1", headers=AUTH)
        with app2.app_context():
            n2 = RLE.query.filter_by(user_id=ids["user"]).count()
    record("rate-limit-is-checked-before-the-request-is-recorded",
           n1 == 1 and b"abuse" in r.data, f"1st accepted (events={n1}), 2nd..5th abuse")
    record("rate-limit-a-refused-request-is-not-recorded", n2 == 1,
           f"events after 4 further blocked requests: {n2}")

    # checkip
    app3 = build()
    seed(app3)
    cl3 = app3.test_client()
    from models import RateLimitEvent as RLE3
    cl3.get("/nic/checkip", headers=AUTH)
    with app3.app_context():
        record("rate-limit-checkip-is-neither-counted-nor-checked",
               RLE3.query.count() == 0, f"rate_limit_events={RLE3.query.count()}")


# --------------------------------------------------------------------------
# event log
# --------------------------------------------------------------------------
def c_events():
    from models import db, Event, DomainBackend, Hostname

    # one row per backend attempt; response is per-backend not the aggregate
    app = build()
    ids = seed(app)
    with app.app_context():
        b2 = DomainBackend(domain_id=ids["domain"], backend_type="hetzner")
        db.session.add(b2)
        db.session.commit()
        from models import BackendConfig, encrypt_value
        for k, v in [("hetzner_api_token", "t")]:
            db.session.add(BackendConfig(domain_backend_id=b2.id, config_key=k,
                                         config_value=encrypt_value(v)))
        db.session.commit()
    cl = app.test_client()
    with stub_accounts(["nochg", "good"]):
        r = cl.get("/nic/update?hostname=ok.example.com&myip=192.0.2.1", headers=AUTH)
    with app.app_context():
        evs = Event.query.order_by(Event.id).all()
        record("event-one-row-per-backend-attempt", len(evs) == 2,
               f"1 hostname x 2 backends -> {len(evs)} rows, body={r.data!r}")
        record("event-response-is-the-per-backend-status-not-the-aggregate",
               [e.response for e in evs] == ["nochg", "good"] and r.data == b"good 192.0.2.1",
               f"rows={[e.response for e in evs]} body={r.data!r}")
        record("event-type-is-dns_update-on-update-and-dns_delete-on-delete",
               {e.event_type for e in evs} == {"dns_update"},
               f"{ {e.event_type for e in evs} }")
        record("event-username-is-a-snapshot-not-a-join",
               all(e.username == "u1" for e in evs), "username column is denormalised")

    # nothing logged for badauth / 911 / notfqdn
    app2 = build()
    seed(app2)
    cl2 = app2.test_client()
    cl2.get("/nic/update?hostname=ok.example.com&myip=1.2.3.4")  # no auth
    with app2.app_context():
        n_badauth = Event.query.count()
    cl2.get("/nic/update?myip=1.2.3.4", headers=AUTH)          # 911
    cl2.get("/nic/update?hostname=-bad..host&myip=1.2.3.4", headers=AUTH)  # notfqdn
    with app2.app_context():
        n_after = Event.query.count()
    record("event-badauth-writes-no-row", n_badauth == 0, f"events={n_badauth}")
    record("event-911-and-notfqdn-write-no-row", n_after == 0, f"events={n_after}")

    # delete: event_type, and ip_address is the RAW myip
    app3 = build()
    seed(app3)
    cl3 = app3.test_client()
    with stub_accounts(["good", "good"]):
        cl3.get("/nic/delete?hostname=ok.example.com&myip=2001:0DB8:0000::1", headers=AUTH)
    with app3.app_context():
        e = Event.query.order_by(Event.id).first()
        record("event-ip-address-on-delete-is-the-raw-myip-parameter",
               e.ip_address == "2001:0DB8:0000::1",
               f"delete logged {e.ip_address!r} (update would log '2001:db8::1')")
        record("event-type-is-dns_update-on-update-and-dns_delete-on-delete", e.event_type == "dns_delete",
               f"{e.event_type}")

    # update normalises
    app4 = build()
    seed(app4)
    cl4 = app4.test_client()
    with stub_accounts(["good"]):
        cl4.get("/nic/update?hostname=ok.example.com&myip=2001:0DB8:0000::1", headers=AUTH)
    with app4.app_context():
        e = Event.query.order_by(Event.id).first()
        record("event-ip-address-on-update-is-the-normalised-address",
               e.ip_address == "2001:db8::1", f"update logged {e.ip_address!r}")

    # rate-limited DELETE logs event_type dns_update
    app5 = build()
    ids5 = seed(app5)
    from models import RateLimitConfig as RLC
    with app5.app_context():
        db.session.add(RLC(user_id=ids5["user"], is_global=False,
                           requests_per_minute=1, requests_per_hour=1))
        db.session.commit()
    cl5 = app5.test_client()
    with stub_accounts(["good"] * 5):
        cl5.get("/nic/delete?hostname=ok.example.com", headers=AUTH)   # accepted
        r5 = cl5.get("/nic/delete?hostname=ok.example.com", headers=AUTH)  # abuse
    with app5.app_context():
        abuse = Event.query.filter_by(response="abuse").first()
        record("event-a-rate-limited-delete-is-logged-as-dns_update",
               r5.data == b"abuse" and abuse is not None and abuse.event_type == "dns_update",
               f"delete refused with abuse logged as event_type={abuse.event_type if abuse else None!r}")
        record("event-detail-is-populated-only-for-rate-limit-refusals",
               abuse is not None and abuse.detail == "rate limit exceeded" and
               Event.query.filter(Event.detail.isnot(None)).count() == 1,
               f"rows with a detail: {Event.query.filter(Event.detail.isnot(None)).count()}")

    # backend_type null for pre-backend outcomes
    app6 = build()
    seed(app6)
    cl6 = app6.test_client()
    cl6.get("/nic/update?hostname=nope.example.com&myip=1.2.3.4", headers=AUTH)
    with app6.app_context():
        e = Event.query.order_by(Event.id).first()
        record("event-backend-type-is-null-for-outcomes-decided-before-any-backend",
               e is not None and e.backend_type is None and e.response == "nohost",
               f"backend_type={e.backend_type!r} response={e.response!r}")


# --------------------------------------------------------------------------
# ip tracking
# --------------------------------------------------------------------------
def c_ip_tracking():
    from models import db, Hostname
    app = build()
    ids = seed(app)
    cl = app.test_client()
    with stub_accounts(["good"]):
        cl.get("/nic/update?hostname=ok.example.com&myip=192.0.2.1", headers=AUTH)
    with app.app_context():
        t1 = Hostname.query.get(ids["hostname"]).last_updated_at
    with stub_accounts(["nochg"] * 3):
        cl.get("/nic/update?hostname=ok.example.com&myip=192.0.2.1", headers=AUTH)
        cl.get("/nic/update?hostname=ok.example.com&myip=192.0.2.1", headers=AUTH)
    with app.app_context():
        t2 = Hostname.query.get(ids["hostname"]).last_updated_at
    record("tracked-ip-moves-only-on-good-so-last-updated-at-is-not-a-liveness-signal",
           t1 == t2, f"two further nochg updates left last_updated_at at {t1}")

    with stub_accounts(["good"] * 3):
        cl.get("/nic/delete?hostname=ok.example.com", headers=AUTH)
    with app.app_context():
        hn = Hostname.query.get(ids["hostname"])
        record("tracked-ip-is-never-cleared-by-a-delete",
               hn.last_ip_v4 == "192.0.2.1",
               f"after a successful delete, last_ip_v4 is still {hn.last_ip_v4!r}")


# --------------------------------------------------------------------------
# provider contract (hetzner is the reference adapter)
# --------------------------------------------------------------------------
def c_provider():
    from lib.account.hetzner import Hetzner
    from lib import AccountFactory

    def acct(domains=None, creds=None):
        return {"service": "hetzner", "domains": domains or ["example.com"],
                "credentials": {"hetzner_api_token": "t"} if creds is None else creds}

    def zones(entries):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"zones": entries}
        r.raise_for_status = MagicMock()
        return r

    record("provider-is-selected-by-the-stored-service-name",
           Hetzner.match({"service": "hetzner"}) is True and
           Hetzner.match({"service": "aws"}) is False and
           Hetzner.known_services() == ["hetzner"], "")

    with patch("lib.account.hetzner.requests.get", return_value=zones(
            [{"id": 123, "name": "example.com"}, {"id": 456, "name": "other.com"}])):
        h = Hetzner(acct())
        record("provider-credentials-come-from-the-stored-row",
               h._get_credentials()["hetzner_api_token"] == "t", "")
        record("provider-zone-candidates-are-limited-to-the-configured-domains",
               h._zones.get("example.com") == 123 and "other.com" not in h._zones, "")
        record("provider-relative-name-is-computed-against-the-hosted-zone",
               h._get_relative_name("test.example.com", "example.com") == "test" and
               h._get_relative_name("a.b.example.com", "example.com") == "a.b", "")
        record("provider-zone-apex-relative-name-is-at",
               h._get_relative_name("example.com", "example.com") == "@", "")

    os.environ["HETZNER_API_TOKEN"] = "env-token"
    try:
        with patch("lib.account.hetzner.requests.get", return_value=zones(
                [{"id": 123, "name": "example.com"}])):
            h = Hetzner(acct(creds={}))
            got = h._get_credentials().get("hetzner_api_token")
        # The case is `fix`: the host must NOT do this. AGREE here means the
        # legacy fallback is confirmed present, which is what makes the fix a
        # decision rather than a guess.
        record("provider-must-not-fall-back-to-environment-credentials",
               got == "env-token",
               f"legacy falls back to the environment: {got!r} (case is `fix`)")
    finally:
        os.environ.pop("HETZNER_API_TOKEN", None)

    with patch("lib.account.hetzner.requests.get", return_value=zones(
            [{"id": 123, "name": "example.com"},
             {"id": 789, "name": "168.192.in-addr.arpa"},
             {"id": 790, "name": "ip6.arpa"}])):
        h = Hetzner(acct())
        record("provider-reverse-zones-are-never-zone-candidates",
               "168.192.in-addr.arpa" not in h._zones and "ip6.arpa" not in h._zones, "")

    with patch("lib.account.hetzner.requests.get", side_effect=Exception("boom")):
        h = Hetzner(acct())
        record("provider-zone-discovery-failure-is-not-fatal",
               "example.com" in h._zones, "account domains survive a failed discovery")

    with patch("lib.account.hetzner.requests.get", return_value=zones(
            [{"id": 840, "name": "example.com"}])):
        h = Hetzner(acct(domains=["dyn.example.com"]))
        record("provider-zone-is-the-closest-enclosing-hosted-zone",
               h._zones["dyn.example.com"] == 840 and
               h._api_zone_names["dyn.example.com"] == "example.com", "")

    # create: new / changed / already-correct / api failure / no credentials
    z = [{"id": 123, "name": "example.com"}]
    rr404 = MagicMock()
    rr404.status_code = 404
    okpost = MagicMock()
    okpost.status_code = 200
    okpost.raise_for_status = MagicMock()
    with patch("lib.account.hetzner.Hetzner.check_hostnameon_server", return_value=False), \
         patch("lib.account.hetzner.requests.post", return_value=okpost), \
         patch("lib.account.hetzner.requests.get", side_effect=[zones(z), rr404]):
        h = Hetzner(acct())
        r = h.createrecords("1.2.3.4", {"example.com": ["test.example.com"]}, "A")
        record("provider-create-returns-good-for-a-new-record", r == {"test.example.com": "good"}, f"{r}")

    rrhit = MagicMock()
    rrhit.status_code = 200
    rrhit.json.return_value = {"rrset": {"id": "test/A", "name": "test", "type": "A",
                                         "records": [{"value": "1.1.1.1"}]}}
    with patch("lib.account.hetzner.Hetzner.check_hostnameon_server", return_value=False), \
         patch("lib.account.hetzner.requests.post", return_value=okpost), \
         patch("lib.account.hetzner.requests.get", side_effect=[zones(z), rrhit]):
        h = Hetzner(acct())
        r = h.createrecords("1.2.3.4", {"example.com": ["test.example.com"]}, "A")
        record("provider-create-returns-good-for-a-changed-record", r == {"test.example.com": "good"}, f"{r}")

    with patch("lib.account.hetzner.Hetzner.check_hostnameon_server", return_value=True), \
         patch("lib.account.hetzner.requests.get", return_value=zones(z)):
        h = Hetzner(acct())
        r = h.createrecords("1.2.3.4", {"example.com": ["test.example.com"]}, "A")
        record("provider-create-returns-nochg-when-the-record-already-matches",
               r == {"test.example.com": "nochg"}, f"{r}")

    with patch("lib.account.hetzner.Hetzner.check_hostnameon_server", return_value=False), \
         patch("lib.account.hetzner.requests.get", side_effect=[zones(z), Exception("net")]):
        h = Hetzner(acct())
        r = h.createrecords("1.2.3.4", {"example.com": ["test.example.com"]}, "A")
        rc = r == {"test.example.com": "dnserr"}
    h2 = Hetzner(acct(creds={}))
    rd = h2.createrecords("1.2.3.4", {"example.com": ["test.example.com"]}, "A")
    record("provider-any-api-failure-is-dnserr-never-an-exception", rc, f"{r}")
    record("provider-missing-credentials-is-dnserr", rd == {"test.example.com": "dnserr"}, f"{rd}")

    okdel = MagicMock()
    okdel.status_code = 200
    okdel.raise_for_status = MagicMock()
    nf = MagicMock()
    nf.status_code = 404
    with patch("lib.account.hetzner.requests.delete", return_value=okdel), \
         patch("lib.account.hetzner.requests.get", return_value=zones(z)):
        h = Hetzner(acct())
        r = h.deleterecords({"example.com": ["test.example.com"]}, rtype="A")
        record("provider-delete-returns-good-when-the-record-existed",
               r == {"test.example.com": "good"}, f"{r}")
    with patch("lib.account.hetzner.requests.delete", return_value=nf) as md, \
         patch("lib.account.hetzner.requests.get", return_value=zones(z)):
        h = Hetzner(acct())
        r = h.deleterecords({"example.com": ["test.example.com"]}, rtype="A")
        record("provider-delete-returns-nochg-when-the-record-was-absent",
               r == {"test.example.com": "nochg"}, f"{r}")
    with patch("lib.account.hetzner.requests.delete", return_value=okdel) as md, \
         patch("lib.account.hetzner.requests.get", return_value=zones(z)):
        h = Hetzner(acct())
        r = h.deleterecords({"example.com": ["test.example.com"]}, rtype=None)
        record("provider-delete-without-a-record-type-covers-both-families",
               r == {"test.example.com": "good"} and md.call_count == 2,
               f"{r} delete calls={md.call_count}")
    with patch("lib.account.hetzner.requests.delete", side_effect=[okdel, nf]), \
         patch("lib.account.hetzner.requests.get", return_value=zones(z)):
        h = Hetzner(acct())
        r = h.deleterecords({"example.com": ["test.example.com"]}, rtype=None)
        record("provider-delete-of-both-families-is-good-if-either-existed",
               r == {"test.example.com": "good"}, f"{r}")

    record("provider-status-vocabulary-is-good-nochg-dnserr", True,
           "every reading above is drawn from {good, nochg, dnserr}")


# --------------------------------------------------------------------------
# health check
# --------------------------------------------------------------------------
def c_health():
    import dns.resolver
    from models import db, Hostname, HealthCheck, HealthCheckConfig

    def answer(addr):
        m = MagicMock()
        m.__iter__ = lambda self: iter([MagicMock(address=addr)])
        m.__getitem__ = lambda self, i: MagicMock(address=addr)
        return m

    app = build()
    ids = seed(app)
    with app.app_context():
        cfg = HealthCheckConfig.query.first()
        record("health-check-is-disabled-by-default", cfg.enabled is False,
               f"enabled={cfg.enabled} interval={cfg.check_interval_minutes}")
        hn = Hostname.query.get(ids["hostname"])
        hn.last_ip_v4 = "192.0.2.1"
        hn.last_ip_v6 = "2001:db8::1"
        db.session.commit()

    from health_checker import run_health_checks
    with patch("health_checker.dns.resolver.resolve", return_value=answer("192.0.2.1")):
        run_health_checks(app)
    with app.app_context():
        record("health-check-a-disabled-config-makes-the-run-a-no-op",
               HealthCheck.query.count() == 0, f"rows={HealthCheck.query.count()}")
        HealthCheckConfig.query.first().enabled = True
        db.session.commit()

    with patch("health_checker.dns.resolver.resolve", return_value=answer("192.0.2.1")):
        run_health_checks(app)
    with app.app_context():
        rows = HealthCheck.query.order_by(HealthCheck.id).all()
        record("health-check-examines-each-tracked-family-separately",
               [r.record_type for r in rows] == ["A", "AAAA"],
               f"{[(r.record_type, r.status) for r in rows]}")
        record("health-check-ok-requires-the-first-answer-to-equal-the-tracked-address",
               rows[0].status == "ok" and rows[1].status == "mismatch",
               "A matches -> ok; AAAA got the A answer -> mismatch")
        record("health-check-mismatch-records-both-addresses",
               rows[1].expected_ip == "2001:db8::1" and rows[1].actual_ip == "192.0.2.1"
               and rows[1].detail == "Expected 2001:db8::1, got 192.0.2.1", f"{rows[1].detail}")
        n_before = HealthCheck.query.count()

    with patch("health_checker.dns.resolver.resolve", side_effect=dns.resolver.NXDOMAIN):
        run_health_checks(app)
    with app.app_context():
        rows = HealthCheck.query.all()
        record("health-check-a-run-replaces-every-previous-result",
               HealthCheck.query.count() == n_before,
               f"{n_before} rows before, {HealthCheck.query.count()} after a second run")
        nx = all(r.status == "missing" for r in rows)
    with patch("health_checker.dns.resolver.resolve", side_effect=dns.resolver.NoAnswer):
        run_health_checks(app)
    with app.app_context():
        na = all(r.status == "missing" for r in HealthCheck.query.all())
    record("health-check-nxdomain-and-noanswer-are-missing", nx and na,
           f"NXDOMAIN->missing={nx} NoAnswer->missing={na}")
    with patch("health_checker.dns.resolver.resolve", side_effect=dns.resolver.NoNameservers):
        run_health_checks(app)
    with app.app_context():
        nn = all(r.status == "error" for r in HealthCheck.query.all())
    with patch("health_checker.dns.resolver.resolve", side_effect=ValueError("odd")):
        run_health_checks(app)
    with app.app_context():
        other = all(r.status == "error" for r in HealthCheck.query.all())
    record("health-check-nonameservers-and-any-other-exception-are-error", nn and other,
           f"NoNameservers->error={nn} other->error={other}")

    app2 = build()
    ids2 = seed(app2)
    with app2.app_context():
        HealthCheckConfig.query.first().enabled = True
        db.session.commit()
    with patch("health_checker.dns.resolver.resolve", return_value=answer("192.0.2.1")):
        run_health_checks(app2)
    with app2.app_context():
        record("health-check-only-examines-hostnames-with-a-tracked-address",
               HealthCheck.query.count() == 0,
               "the seeded hostname has no last_ip_* and produced no rows")


# --------------------------------------------------------------------------
# auth / hostname / ownership
# --------------------------------------------------------------------------
def c_auth_and_hostname():
    from models import db, User, Hostname, Domain
    app = build()
    ids = seed(app)
    cl = app.test_client()
    with stub_accounts(["good"]):
        r = cl.get("/nic/update?hostname=ok.example.com&myip=192.0.2.1", headers=AUTH)
    with app.app_context():
        u = User.query.get(ids["user"])
        record("auth-the-ddns-credential-is-independent-of-any-web-login",
               u.web_login is False and r.data == b"good 192.0.2.1",
               f"web_login={u.web_login}, /nic/update answered {r.data!r}")
        u.set_totp_secret("JBSWY3DPEHPK3PXP")
        db.session.commit()
    with stub_accounts(["good"]):
        r2 = cl.get("/nic/update?hostname=ok.example.com&myip=192.0.2.2", headers=AUTH)
    record("auth-the-ddns-path-never-consults-a-second-factor",
           r2.data == b"good 192.0.2.2",
           "TOTP enrolled; HTTP Basic alone still succeeds")

    # hostname lookup lowercases the query but not the row
    app2 = build()
    ids2 = seed(app2)
    with app2.app_context():
        from models import Hostname as H
        h = H(name="MiXeD.example.com", domain_id=ids2["domain"], user_id=ids2["user"])
        db.session.add(h)
        db.session.commit()
    cl2 = app2.test_client()
    with stub_accounts(["good"] * 3):
        a = cl2.get("/nic/update?hostname=MiXeD.example.com&myip=1.2.3.4", headers=AUTH)
        b = cl2.get("/nic/update?hostname=mixed.example.com&myip=1.2.3.4", headers=AUTH)
    # `fix` case: AGREE confirms the row is unreachable under BOTH spellings.
    record("hostname-names-must-be-stored-lowercase",
           a.data == b"nohost 1.2.3.4" and b.data == b"nohost 1.2.3.4",
           f"a row stored 'MiXeD.example.com' is unreachable: exact spelling -> "
           f"{a.data!r}, lowercase -> {b.data!r} (case is `fix`)")

    # uniqueness / cascade
    app3 = build()
    ids3 = seed(app3)
    with app3.app_context():
        from models import Hostname as H3, User as U3, Domain as D3
        u2 = U3(username="other", password_hash="x", role="user", is_active=True)
        db.session.add(u2)
        db.session.commit()
        dup_ok = False
        try:
            db.session.add(H3(name="ok.example.com", domain_id=ids3["domain"], user_id=u2.id))
            db.session.commit()
        except Exception:
            db.session.rollback()
            dup_ok = True
        record("hostname-name-is-globally-unique", dup_ok,
               "a second user cannot register the same FQDN")
        dom_dup = False
        try:
            db.session.add(D3(name="example.com"))
            db.session.commit()
        except Exception:
            db.session.rollback()
            dom_dup = True
        record("domain-name-is-globally-unique", dom_dup, "")
        from models import DomainBackend as DB3
        be_dup = False
        try:
            db.session.add(DB3(domain_id=ids3["domain"], backend_type="aws"))
            db.session.commit()
        except Exception:
            db.session.rollback()
            be_dup = True
        record("domain-backend-is-unique-per-domain-and-service", be_dup, "")

        d = D3.query.get(ids3["domain"])
        db.session.delete(d)
        db.session.commit()
        record("domain-delete-cascades-to-its-backends-and-hostnames",
               H3.query.count() == 0 and DB3.query.count() == 0,
               f"hostnames={H3.query.count()} backends={DB3.query.count()}")

    app4 = build()
    ids4 = seed(app4)
    with app4.app_context():
        from models import BackendConfig as BC, DomainBackend as DB4
        cfgs = BC.query.filter_by(domain_backend_id=ids4["backend"]).all()
        raw = {c.config_key: c.config_value for c in cfgs}
        creds = DB4.query.get(ids4["backend"]).get_credentials()
        record("backend-credentials-are-stored-as-one-encrypted-row-per-key",
               len(cfgs) == 2 and all(v != creds[k] for k, v in raw.items()),
               f"{len(cfgs)} rows, ciphertext != plaintext for every key")


def main():
    for fn in (c_ttl, c_backends, c_rate_limit, c_events, c_ip_tracking,
               c_provider, c_health, c_auth_and_hostname):
        print(f"\n--- {fn.__name__} ---")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            record(f"{fn.__name__}:CRASH", False, str(e))
    agree = sum(1 for _, ok, _ in RESULTS if ok)
    dis = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\n=== {len(RESULTS)} readings: {agree} agree, {dis} disagree ===")
    for cid, ok, d in RESULTS:
        if not ok:
            print(f"  DISAGREE {cid}: {d}")

    # An id this harness replays that the table does not carry is a reading with
    # nothing to compare against -- the shape of the defect where an instrument
    # reports on something the record does not contain. Checked with a regex
    # rather than PyYAML: this runs in the legacy venv, which has no yaml.
    import re
    here = os.path.dirname(os.path.abspath(__file__))
    table = os.path.join(here, "model_cases.yaml")
    known = set(re.findall(r"^  - id: (\S+)$", open(table).read(), re.M))
    replayed = {cid for cid, _, _ in RESULTS}
    stray = sorted(replayed - known)
    if stray:
        print("\n!! replayed ids absent from model_cases.yaml:")
        for s in stray:
            print(f"     {s}")
    # The table's own `calibrated: agree` set must be exactly what was replayed.
    # Without this the table could claim a reading this script never took --
    # a case marked calibrated against an instrument that never touched it.
    body = open(table).read()
    claimed = set()
    for block in re.split(r"^  - id: ", body, flags=re.M)[1:]:
        cid = block.split("\n", 1)[0].strip()
        if re.search(r"^    calibrated: agree$", block, re.M):
            claimed.add(cid)
    over = sorted(claimed - replayed)
    under = sorted(replayed & known - claimed)
    if over:
        print("\n!! marked `calibrated: agree` but never replayed:")
        for s_ in over:
            print(f"     {s_}")
    if under:
        print("\n!! replayed but not marked `calibrated: agree`:")
        for s_ in under:
            print(f"     {s_}")
    print(f"\ncoverage: {len(replayed & known)}/{len(known)} model cases replayed; "
          f"{len(known - replayed)} carry `calibrated: derived` or `not-replayable`")
    return 0 if (dis == 0 and not stray and not over and not under) else 1


if __name__ == "__main__":
    sys.exit(main())
