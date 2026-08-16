"""The rehearsal harness — and specifically, that its checks can fail.

``rehearse_migration`` is an instrument, and the defect an instrument
has is that it reports agreement it did not observe. Every rehearsal it
ran against the live copy came back ``0 discrepancies``, which is either
a result or a report that could not have said anything else, and only
this file tells the two apart.

So almost none of what follows tests the happy path. It tests that:

* a check whose population went to zero reports ``NOT MEASURED`` and is
  **not** counted as a pass — the ``n/a`` is never ``0`` rule, as a
  guard rather than as a convention;
* every data check reports the disagreement it exists to find, one
  mutation at a time, against a real import into a real database;
* the two guards that stand between a rehearsal on migrated production
  credentials and a **write to the operator's live DNS** each hold, and
  the second one still holds when the first is removed;
* the surrogate hash really does carry the production row's salt and
  cost, and is really not the production hash;
* reading the source creates nothing beside it, with the natural
  implementation demonstrated creating the sidecars on the same file.

Everything is namespaced by ``PYTEST_XDIST_WORKER``: device usernames,
domain names and hostnames are globally unique by constraint, so ten
workers sharing one MySQL collide on all three.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import bcrypt
import pytest
import pytest_asyncio
from app.db import get_session_factory
from app.models.auth import User
from conftest import fixture_writes, purge_tenants
from cryptography.fernet import Fernet, InvalidToken

from atrium_ddns import compat_stub
from atrium_ddns.scripts import import_legacy as il
from atrium_ddns.scripts import rehearse_migration as rm

W = os.environ.get("PYTEST_XDIST_WORKER", "serial")
EMAIL = f"rehearsal-owner-{W}@example.invalid"
ZONE = f"rehearse-{W}.example.invalid"
TTL = 60

#: ``label -> plaintext``. Two devices, three hostnames — the smallest
#: world in which "a hostname landed on the wrong device" is a
#: distinguishable failure at all.
PASSWORDS = {"router-x": "router-x-secret", "router-y": "router-y-secret"}
SPREAD = {"router-x": 2, "router-y": 1}

ADMIN_PASSWORD = "the-admin-password"

#: The scripted slot the migrated backend resolves to. A ``route53``
#: row would reach AWS; this one reaches nothing.
STUB_SERVICE = "stub1"

compat_stub.register_stub_providers(force=True)


# ===================================================================== #
# A synthetic legacy database — a SECOND statement of the legacy schema
# ===================================================================== #

LEGACY_DDL = """
CREATE TABLE users (
    id INTEGER NOT NULL PRIMARY KEY, username VARCHAR(80) NOT NULL UNIQUE,
    password_hash VARCHAR(128) NOT NULL, role VARCHAR(10) NOT NULL,
    is_active BOOLEAN NOT NULL, created_at DATETIME, updated_at DATETIME,
    totp_secret TEXT, web_login BOOLEAN NOT NULL DEFAULT 0
);
CREATE TABLE domains (
    id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(255) NOT NULL UNIQUE,
    created_at DATETIME
);
CREATE TABLE domain_backends (
    id INTEGER NOT NULL PRIMARY KEY, domain_id INTEGER NOT NULL,
    backend_type VARCHAR(20) NOT NULL, UNIQUE (domain_id, backend_type)
);
CREATE TABLE backend_configs (
    id INTEGER NOT NULL PRIMARY KEY, domain_backend_id INTEGER NOT NULL,
    config_key VARCHAR(80) NOT NULL, config_value TEXT NOT NULL,
    UNIQUE (domain_backend_id, config_key)
);
CREATE TABLE hostnames (
    id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(255) NOT NULL UNIQUE,
    domain_id INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at DATETIME,
    ttl INTEGER NOT NULL DEFAULT 60, last_ip_v4 VARCHAR(45),
    last_ip_v6 VARCHAR(45), last_updated_at DATETIME
);
CREATE TABLE hostname_backends (
    hostname_id INTEGER NOT NULL, domain_backend_id INTEGER NOT NULL,
    PRIMARY KEY (hostname_id, domain_backend_id)
);
CREATE TABLE rate_limit_configs (
    id INTEGER NOT NULL PRIMARY KEY, user_id INTEGER,
    requests_per_minute INTEGER NOT NULL, requests_per_hour INTEGER NOT NULL,
    is_global BOOLEAN NOT NULL, updated_at DATETIME
);
"""


def hostname_labels() -> list[tuple[str, str]]:
    """``(fqdn, owning device label)`` for the whole fixture world."""
    out: list[tuple[str, str]] = []
    for label, count in SPREAD.items():
        for index in range(count):
            out.append((f"{label}-{index}.{ZONE}", label))
    return out


@pytest.fixture(scope="module")
def fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture(scope="module")
def legacy_db(tmp_path_factory, fernet_key: str) -> Path:
    """A legacy database of the production *shape*, at production cost."""
    path = tmp_path_factory.mktemp("legacy") / "dyndns.db"
    fernet = Fernet(fernet_key.encode())
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_DDL)
    conn.execute(
        "INSERT INTO users VALUES (1,?,?,'admin',1,NULL,NULL,NULL,1)",
        (
            f"admin-{W}",
            bcrypt.hashpw(
                ADMIN_PASSWORD.encode("utf8"), bcrypt.gensalt(rounds=4)
            ).decode(),
        ),
    )
    for index, (label, password) in enumerate(sorted(PASSWORDS.items()), start=2):
        conn.execute(
            "INSERT INTO users VALUES (?,?,?,'user',1,NULL,NULL,NULL,0)",
            (
                index,
                f"{label}-{W}",
                # Cost 12 is what the live database holds, measured
                # 2026-08-15: `6 x $2b$12$`. Two devices at cost 12 is
                # ~0.5 s, which is the price of exercising the real
                # shape rather than one that merely starts with `$2b$`.
                bcrypt.hashpw(
                    password.encode("utf8"), bcrypt.gensalt(rounds=12)
                ).decode(),
            ),
        )
    conn.execute("INSERT INTO domains VALUES (1,?,NULL)", (ZONE,))
    conn.execute("INSERT INTO domain_backends VALUES (1,1,?)", (STUB_SERVICE,))
    conn.execute(
        "INSERT INTO backend_configs VALUES (1,1,'api_key',?)",
        (fernet.encrypt(b"the-api-key").decode(),),
    )
    conn.execute(
        "INSERT INTO backend_configs VALUES (2,1,'api_secret',?)",
        (fernet.encrypt(b"the-api-secret").decode(),),
    )
    user_ids = {label: index for index, label in enumerate(sorted(PASSWORDS), start=2)}
    for index, (name, label) in enumerate(hostname_labels(), start=1):
        conn.execute(
            "INSERT INTO hostnames VALUES (?,?,1,?,NULL,?,?,?,?)",
            (
                index,
                name,
                user_ids[label],
                TTL,
                f"198.51.100.{index}",
                f"2001:db8::{index}",
                "2026-08-15 18:07:13.224559",
            ),
        )
        conn.execute("INSERT INTO hostname_backends VALUES (?,1)", (index,))
    conn.execute("INSERT INTO rate_limit_configs VALUES (1,NULL,30,500,1,NULL)")
    conn.commit()
    conn.close()
    return path


#: Who this module's guarded regions say they are, in the message
#: :func:`conftest.fixture_writes` prints when a nesting bug or a
#: timeout stops the run. A bare ``"?"`` names nothing.
LOCK_OWNER = "test_rehearse_migration.imported"


@pytest_asyncio.fixture(scope="module")
async def imported(legacy_db: Path, fernet_key: str) -> Any:
    """One import into the real database; every check reads it back.

    Teardown is :func:`conftest.purge_tenants`, not a copy of it kept
    here. Removing the owner is enough on its own — domains, devices,
    hostnames and backends all cascade off it — but the shared helper
    also clears the tenant's ``ddns_event`` rows by ``user_id`` and its
    ``user_secret_keys`` row, and does the two in the order that leaves
    the cascade nothing to do. Called at **both** ends, which is the
    shared helper's own documented contract: a worker killed mid-module
    leaves this fixture's owner behind, and the next run's insert
    collides on ``ix_users_email`` rather than reporting the leak.

    The writes are inside :func:`conftest.fixture_writes` for the reason
    ``conftest``'s docstring gives: what deadlocked this suite was never
    a teardown, it was two fixtures inserting into
    ``ix_users_email`` and ``uq_ddns_domain_name`` in opposite gap
    order. This fixture does exactly that pair — an owner, then a
    zone-and-hostnames import — so guarding only its teardown would be
    guarding the statement that was never the problem. Measured at
    0.35 s per worker per module, against a 30 s lock timeout.
    """
    factory = get_session_factory()
    await purge_tenants([EMAIL], owner=LOCK_OWNER)

    async with fixture_writes(LOCK_OWNER) as session:
        session.add(
            User(
                email=EMAIL,
                hashed_password="$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$x" * 1,
                is_active=True,
                is_verified=True,
                full_name="Rehearsal Owner",
            )
        )

    with il.open_source(legacy_db) as conn:
        il.assert_columns(conn)
        snapshot = il.read_snapshot(conn)
    plan = il.build_plan(snapshot, fernet_key=fernet_key)

    async with fixture_writes(LOCK_OWNER) as session:
        written = await il.apply(
            session, plan, owner_email=EMAIL, create_owner=False
        )
    async with factory() as session:
        reading = await il.verify(session, written.owner_id)

    yield {
        "plan": plan,
        "written": written,
        "reading": reading,
        "factory": factory,
        "source": rm.count_source(legacy_db),
        "fernet_key": fernet_key,
    }

    await purge_tenants([EMAIL], owner=LOCK_OWNER)


# ===================================================================== #
# 1. `n/a` is never `0` — the reporting type itself
# ===================================================================== #


def test_an_empty_population_is_reported_as_not_measured_and_is_not_a_pass() -> None:
    """The whole reason ``Finding`` carries a denominator.

    A check that stopped having anything to compare must not render in
    the same type as one that compared eleven things and found nothing
    wrong. Both of the properties below are load-bearing: the text, so
    a reader sees it, and ``ok``, so the exit status does.
    """
    empty = rm.Finding("something", population=0)
    assert not empty.measured
    assert not empty.ok, "an unmeasured check must not count as a pass"
    assert "NOT MEASURED" in empty.line()
    assert "0 discrepancies" not in empty.line()


def test_a_measured_check_prints_its_denominator_beside_its_numerator() -> None:
    finding = rm.Finding("something", population=11)
    assert "11 compared" in finding.line()
    assert "0 discrepancies" in finding.line()
    assert finding.ok


def test_a_check_with_discrepancies_is_not_ok_and_names_them() -> None:
    finding = rm.Finding("something", population=3, discrepancies=["a", "b"])
    assert not finding.ok
    assert "2 discrepancies" in finding.line()


def test_the_digest_changes_when_one_byte_moves() -> None:
    """Set comparisons are reported by digest; a digest that did not
    move for a changed set would make every one of them vacuous."""
    base = {"a.example": "device-1", "b.example": "device-2"}
    moved = {"a.example": "device-1", "b.example": "device-3"}
    assert rm.set_digest(base) == rm.set_digest(dict(reversed(list(base.items()))))
    assert rm.set_digest(base) != rm.set_digest(moved)


# ===================================================================== #
# 2. Reading the source creates nothing beside it
# ===================================================================== #


def test_count_source_creates_nothing_beside_the_source(tmp_path) -> None:
    """And the natural implementation is shown creating the sidecars.

    ``count_source`` goes through ``import_legacy.open_source``, so this
    asserts the inheritance rather than a second copy of the rule — but
    an inherited guard is exactly the kind that stops applying without
    anyone noticing, so it is measured here on its own file.
    """
    path = tmp_path / "wal.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(LEGACY_DDL)
    conn.execute(
        "INSERT INTO users VALUES (1,'a','$2b$12$x','admin',1,NULL,NULL,NULL,1)"
    )
    conn.commit()
    conn.close()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)

    before = sorted(p.name for p in tmp_path.iterdir())
    rm.count_source(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == before, (
        "count_source created a file beside its source. Pointed at the live "
        "volume that is a write into the running service's data directory."
    )

    # The failure demonstration. If this ever stops creating sidecars,
    # the assertion above has gone vacuous and says so rather than
    # quietly continuing to pass.
    naive = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    naive.execute("SELECT COUNT(*) FROM users").fetchone()
    naive.close()
    after_naive = sorted(p.name for p in tmp_path.iterdir())
    assert after_naive != before, (
        "a plain mode=ro open no longer creates -wal/-shm on this SQLite "
        "build, so the guard above is no longer distinguishing anything. "
        f"before={before} after={after_naive}"
    )
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def test_count_source_and_read_snapshot_agree(legacy_db: Path) -> None:
    """Two differently-shaped readers over one file.

    ``count_source`` is hand-written SQL against the legacy column
    names; ``read_snapshot`` builds the dataclasses the plan is made
    from. Agreement here is what makes the counts in a rehearsal two
    readings rather than one printed twice.
    """
    source = rm.count_source(legacy_db)
    with il.open_source(legacy_db) as conn:
        snapshot = il.read_snapshot(conn)
    assert source.counts["users"] == len(snapshot.users)
    assert source.counts["hostnames"] == len(snapshot.hostnames)
    assert source.counts["domains"] == len(snapshot.domains)
    assert source.devices == sum(1 for u in snapshot.users if u.role != "admin")
    assert source.admins == sum(1 for u in snapshot.users if u.role == "admin")


# ===================================================================== #
# 3. The legacy decryption, as a second instrument
# ===================================================================== #


def test_legacy_decrypt_agrees_with_the_importers_own(
    legacy_db: Path, fernet_key: str
) -> None:
    """``dyndns-route53/models.py::decrypt_value`` vs the importer's.

    They must agree on the plaintext, because the rehearsal's credential
    check compares one against what the *target* reveals and the whole
    claim is "the same value the legacy service reads".
    """
    source = rm.count_source(legacy_db)
    with il.open_source(legacy_db) as conn:
        snapshot = il.read_snapshot(conn)
    theirs = il.decrypt_credentials(snapshot.backends[0], fernet_key)
    ours = rm.legacy_decrypt(source.ciphertext, fernet_key)
    assert ours == theirs
    assert ours == {"api_key": "the-api-key", "api_secret": "the-api-secret"}


def test_legacy_decrypt_under_the_wrong_key_refuses(legacy_db: Path) -> None:
    """Otherwise the agreement above would hold for any key at all."""
    source = rm.count_source(legacy_db)
    with pytest.raises(InvalidToken):
        rm.legacy_decrypt(source.ciphertext, Fernet.generate_key().decode())


# ===================================================================== #
# 4. The surrogate hash
# ===================================================================== #


def test_a_surrogate_carries_the_production_salt_and_cost() -> None:
    """The point of the construction, stated as three assertions.

    Same 29-character ``$2b$NN$<salt>`` prefix, a different digest, and
    the known password verifying against the surrogate while *not*
    verifying against the original. Without the last one the surrogate
    could be the production hash and nobody would know.
    """
    production = bcrypt.hashpw(b"the-real-password", bcrypt.gensalt(rounds=12)).decode()
    surrogate = rm.surrogate_hash(production, "a-password-we-chose")

    assert surrogate[:29] == production[:29]
    assert surrogate[29:] != production[29:]
    assert surrogate.startswith("$2b$12$")
    assert bcrypt.checkpw(b"a-password-we-chose", surrogate.encode())
    assert not bcrypt.checkpw(b"a-password-we-chose", production.encode())
    assert not bcrypt.checkpw(b"the-real-password", surrogate.encode())


def test_a_surrogate_of_something_that_is_not_bcrypt_is_refused() -> None:
    with pytest.raises(rm.RehearsalFailed) as excinfo:
        rm.surrogate_hash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$ZGlnZXN0", "pw")
    assert "bcrypt" in str(excinfo.value)
    assert "$arg" in str(excinfo.value), "the refusal must name what it saw"


def test_build_surrogate_copy_changes_only_the_device_hashes(
    legacy_db: Path, tmp_path
) -> None:
    """Everything else in the copy is production, and is asserted to be.

    A surrogate build that quietly altered a hostname, a TTL or a
    credential would turn the wire half into a test of a world this
    process invented — which is the thing the whole issue is trying not
    to do.
    """
    derived = tmp_path / "surrogate.db"
    passwords = rm.build_surrogate_copy(legacy_db, derived)
    assert set(passwords) == {f"{label}-{W}" for label in PASSWORDS}

    original = rm.count_source(legacy_db)
    changed = rm.count_source(derived)
    assert changed.counts == original.counts
    assert changed.hostname_to_user == original.hostname_to_user
    assert changed.hostname_ips == original.hostname_ips
    assert changed.hostname_updated == original.hostname_updated
    assert changed.ttls == original.ttls
    assert changed.ciphertext == original.ciphertext
    assert changed.backend_types == original.backend_types

    # The one thing that did change, and it changed everywhere.
    assert set(changed.password_hashes) == set(original.password_hashes)
    for username, minted in changed.password_hashes.items():
        assert minted != original.password_hashes[username]
        assert minted[:29] == original.password_hashes[username][:29]
        assert bcrypt.checkpw(passwords[username].encode(), minted.encode())


def test_a_surrogate_build_over_an_empty_device_set_is_refused(tmp_path) -> None:
    """A surrogate run with no devices would drive zero requests and
    report no failures — the probe that cannot fail, applied to the
    wire half."""
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_DDL)
    conn.execute(
        "INSERT INTO users VALUES (1,'a','$2b$12$x','admin',1,NULL,NULL,NULL,1)"
    )
    conn.commit()
    conn.close()
    with pytest.raises(rm.RehearsalFailed) as excinfo:
        rm.build_surrogate_copy(path, tmp_path / "out.db")
    assert "zero requests" in str(excinfo.value)


# ===================================================================== #
# 5. The two guards between a rehearsal and a live DNS write
# ===================================================================== #


def _account(service: str) -> Any:
    from atrium_ddns.providers import account_from_mapping

    return account_from_mapping(
        {
            "service": service,
            "domains": [ZONE],
            "credentials": {"aws_access_key_id": "x", "aws_secret_access_key": "y"},
            "config": {"ttl": TTL},
            "id": "1",
        }
    )


def test_the_provider_guard_records_instead_of_reaching_the_provider() -> None:
    """Guard 1: the registry entry is swapped for a recorder."""
    from atrium_ddns.providers import get_provider, provider_class
    from atrium_ddns.providers.base import STATUS_GOOD

    original = provider_class("route53")
    with rm.no_provider_can_reach_the_internet("route53") as calls:
        assert provider_class("route53") is not original
        provider = get_provider(_account("route53"))
        assert provider is not None
        grouped = provider.hostnameperzone([f"a.{ZONE}"])
        assert grouped, "the zone map must survive; otherwise everything is nohost"
        result = provider.createrecords(
            "203.0.113.51", grouped, rtype="A", ttl=TTL
        )
        assert result == {f"a.{ZONE}": STATUS_GOOD}
    assert len(calls) == 1
    assert calls[0].op == "create"
    assert calls[0].ttl == TTL
    assert calls[0].rtype == "A"
    assert calls[0].hostnames == (f"a.{ZONE}",)


def test_the_second_guard_holds_when_the_first_is_removed() -> None:
    """Guard 2, demonstrated with guard 1 deliberately bypassed.

    This is the mutation that matters: if the registry swap ever stops
    applying — a provider registered under a name the guard was not
    told about, a code path that constructs the adapter directly — the
    run must still not reach AWS. So the *real* Route 53 adapter is
    constructed and driven **inside** the context, and the reading is
    that ``boto3.client`` refused.

    ``createrecords`` catches its own client failure and degrades to
    ``dnserr`` (that is the documented contract), so the assertion is
    on the status rather than on the raise — and ``dnserr`` here is the
    shape of "we did not talk to AWS".
    """
    from atrium_ddns.providers.base import STATUS_DNSERR
    from atrium_ddns.providers.route53 import Route53Provider

    with rm.no_provider_can_reach_the_internet("route53"):
        provider = Route53Provider(_account("route53"))
        grouped = provider.hostnameperzone([f"a.{ZONE}"])
        result = provider.createrecords("203.0.113.51", grouped, rtype="A", ttl=TTL)
    assert result == {f"a.{ZONE}": STATUS_DNSERR}, (
        "the real adapter reached something. With guard 1 bypassed, guard 2 "
        "is the only thing between a rehearsal and the operator's live zone."
    )


def test_boto3_client_raises_inside_the_guard_and_works_outside() -> None:
    """Both halves, because a guard that never lifts is also a defect."""
    import boto3

    before = boto3.client
    with rm.no_provider_can_reach_the_internet("route53"):
        with pytest.raises(rm.RehearsalFailed) as excinfo:
            boto3.client("route53")
        assert "one API call away" in str(excinfo.value)
    assert boto3.client is before


def test_the_guard_restores_the_registry_even_when_the_body_raises() -> None:
    from atrium_ddns.providers import provider_class

    original = provider_class("route53")
    with pytest.raises(ValueError):
        with rm.no_provider_can_reach_the_internet("route53"):
            raise ValueError("something went wrong mid-rehearsal")
    assert provider_class("route53") is original


def test_neutralising_a_service_nobody_registered_is_refused() -> None:
    """Otherwise a typo would produce a rehearsal with no guard at all
    and no complaint about it."""
    with pytest.raises(rm.RehearsalFailed) as excinfo:
        with rm.no_provider_can_reach_the_internet("no-such-provider"):
            pass
    assert "no-such-provider" in str(excinfo.value)


# ===================================================================== #
# 6. The data checks report what they exist to find
# ===================================================================== #


@pytest.mark.asyncio
async def test_the_unmutated_world_is_clean(imported) -> None:
    """The vacuity guard for every mutation below.

    If this failed, each mutation could be passing because the fixture
    is broken rather than because the check works.
    """
    findings = await rm.run_checks(
        imported["source"],
        imported["plan"],
        imported["written"],
        imported["reading"],
        fernet_key=imported["fernet_key"],
        session_factory=imported["factory"],
    )
    problems = {f.name: f.discrepancies for f in findings if f.discrepancies}
    assert problems == {}, problems
    assert all(f.measured for f in findings), [f.name for f in findings if not f.measured]
    # Every check compared something, and the total is derived from the
    # fixture's own size rather than typed here as a constant.
    hostnames = len(hostname_labels())
    assert sum(f.population for f in findings) == (
        4  # the four count pairs
        + hostnames  # hostname -> device
        + hostnames  # last_ip pairs
        + hostnames  # last_updated_at
        + 1  # ttl, on the one backend
        + 2  # two credential keys
    )


#: ``mutation -> the check that must report it``. Naming the check is
#: the difference between "something noticed" and "the thing that
#: exists to notice this noticed it" — a single over-broad check would
#: satisfy the weaker assertion for all six.
MUTATIONS = {
    "hostname on the wrong device": "hostname -> device, whole set",
    "a hostname that is not there": "hostname -> device, whole set",
    "a moved last_ip": "last_ip_v4 / last_ip_v6",
    "a moved last_updated_at": "last_updated_at (to the second)",
    "a disagreeing ttl": "ttl -> ddns_domain_backend.config",
    "a different credential plaintext": "provider credentials (plaintext)",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
async def test_each_check_reports_its_own_disagreement(imported, mutation) -> None:
    """One mutation at a time, each asserted to be *reported*.

    The mutations are applied to the **source reading**, which is the
    cheap side: the target is a real import into a real database and
    stays untouched, so what is being tested is that the comparison
    notices a difference rather than that a mutated writer produces one.
    """
    source = imported["source"]
    names = sorted(source.hostname_to_user)
    key = names[0]

    if mutation == "hostname on the wrong device":
        other = next(
            v for v in source.hostname_to_user.values()
            if v != source.hostname_to_user[key]
        )
        source = replace(
            source, hostname_to_user={**source.hostname_to_user, key: other}
        )
    elif mutation == "a hostname that is not there":
        remaining = {k: v for k, v in source.hostname_to_user.items() if k != key}
        source = replace(source, hostname_to_user=remaining)
    elif mutation == "a moved last_ip":
        source = replace(
            source,
            hostname_ips={**source.hostname_ips, key: ("192.0.2.99", None)},
        )
    elif mutation == "a moved last_updated_at":
        source = replace(
            source,
            hostname_updated={
                **source.hostname_updated,
                key: "2000-01-01 00:00:00.000000",
            },
        )
    elif mutation == "a disagreeing ttl":
        source = replace(source, ttls=(TTL + 1,))
    elif mutation == "a different credential plaintext":
        fernet = Fernet(imported["fernet_key"].encode())
        source = replace(
            source,
            ciphertext={
                **source.ciphertext,
                "api_key": fernet.encrypt(b"a-different-value").decode(),
            },
        )
    else:  # pragma: no cover - the parametrisation is closed
        raise AssertionError(mutation)

    findings = await rm.run_checks(
        source,
        imported["plan"],
        imported["written"],
        imported["reading"],
        fernet_key=imported["fernet_key"],
        session_factory=imported["factory"],
    )
    reported = {f.name for f in findings if f.discrepancies}
    assert MUTATIONS[mutation] in reported, (
        f"the mutation {mutation!r} was not reported by "
        f"{MUTATIONS[mutation]!r}. A comparison that cannot see this "
        "difference cannot see it in production either. "
        f"Checks that did report: {sorted(reported) or 'none'}"
    )


# ===================================================================== #
# 7. The hash checks
# ===================================================================== #


@pytest.mark.asyncio
async def test_the_hash_checks_are_clean_on_the_unmutated_world(imported) -> None:
    findings = await rm.check_hashes(
        imported["source"], imported["reading"], expect_identical=True
    )
    assert [f.name for f in findings if f.discrepancies] == []
    names = [f.name for f in findings]
    assert "password hashes, byte-identical" in names
    assert "control: recommended() refuses" in names
    assert all(f.measured for f in findings)


@pytest.mark.asyncio
async def test_a_hash_that_did_not_survive_byte_for_byte_is_reported(
    imported,
) -> None:
    """The byte-identity check, mutated. Without this it is a claim that
    two dictionaries were built from the same place."""
    source = imported["source"]
    username = sorted(source.password_hashes)[0]
    mutated = replace(
        source,
        password_hashes={
            **source.password_hashes,
            username: bcrypt.hashpw(b"something-else", bcrypt.gensalt(rounds=4)).decode(),
        },
    )
    findings = await rm.check_hashes(
        mutated, imported["reading"], expect_identical=True
    )
    byte_check = next(
        f for f in findings if f.name == "password hashes, byte-identical"
    )
    assert byte_check.discrepancies
    assert not byte_check.ok


@pytest.mark.asyncio
async def test_a_hash_the_hasher_cannot_identify_is_reported(imported) -> None:
    """The epic's failure mode, injected.

    A device whose stored hash ``auth_device`` cannot identify answers
    ``badauth`` forever, and on the wire that is indistinguishable from
    a wrong password. This is the check that would have caught it, so
    it is the check that has to be shown catching it.
    """
    reading = imported["reading"]
    username = sorted(reading.password_hashes)[0]
    mutated = replace(
        reading,
        password_hashes={
            **reading.password_hashes,
            username: "$md5$not-a-hash-any-of-them-know",
        },
    )
    findings = await rm.check_hashes(
        imported["source"], mutated, expect_identical=False
    )
    identify = next(
        f for f in findings if f.name == "hashes auth_device can identify"
    )
    assert identify.discrepancies
    assert "badauth forever" in identify.discrepancies[0]


@pytest.mark.asyncio
async def test_the_control_notices_if_recommended_stops_refusing(imported) -> None:
    """The control's own control.

    ``PasswordHash.recommended()`` raising on every migrated hash is
    what makes "auth_device can identify them" a distinction rather
    than a tautology. If an upstream release ever adds bcrypt to
    ``recommended()``, the check above stops distinguishing anything —
    and this asserts the harness would say so rather than staying
    green.
    """
    reading = replace(
        imported["reading"],
        password_hashes={
            name: "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$ZGlnZXN0"
            for name in imported["reading"].password_hashes
        },
    )
    findings = await rm.check_hashes(
        imported["source"], reading, expect_identical=False
    )
    control = next(
        f for f in findings if f.name == "control: recommended() refuses"
    )
    assert control.discrepancies, (
        "recommended() accepted every stored hash and the control did not "
        "notice. The identification check is a tautology in that state."
    )
    assert "no longer distinguishes" in control.discrepancies[0]
