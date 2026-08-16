"""The legacy importer — and specifically the ways it must refuse.

``atrium_ddns.scripts.import_legacy`` runs once, against production, at
a cutover, with the old service stopped. There is no second attempt
that costs nothing: every router in the field is configured with a
credential that lives in one column of one legacy table, and a bad
import is discovered by a fleet answering ``badauth`` hours later. So
most of this file is about the refusals rather than the happy path —
the happy path is one test and the reasons not to write a row are
twenty.

Three shapes of evidence here, deliberately different from each other:

* **Pure guards** over a synthetic legacy SQLite built in a temp
  directory, one per refusal, each asserting the *message names the
  thing* rather than that something was raised. A guard that fails
  uninformatively gets deleted rather than investigated.
* **A database round trip** — import, then read the target back
  through the ORM and compare every count, the whole hostname → device
  mapping, the credential by digest, and every password hash byte for
  byte.
* **The wire.** A migrated device authenticating over ``/nic/update``
  with its **original** password, and every one of the eleven
  hostnames reached from the device that owned it and refused
  ``nohost`` from one that did not. The acceptance criterion says "not
  by inspecting the hash column", and a column read is exactly what the
  round trip above is; this is the half that cannot be satisfied by a
  correct-looking column.

**The fixture's bcrypt is the legacy service's own recipe.**
``dyndns-route53``'s ``web_routes.py`` writes
``bcrypt.hashpw(password.encode('utf8'), bcrypt.gensalt())`` — ``$2b$``
at cost 12, 60 characters, which is what the live database holds, read
2026-08-15. One fixture device is hashed at cost **12** so the
production shape is exercised end to end; the rest are cost **4**,
because 17 verifies at cost 12 is ~4 s of pure key stretching and the
thing under test is the mapping, not bcrypt. Both are ``$2b$`` and both
go down the identical branch of
:func:`atrium_ddns.auth_device._verify_sync`.

Requires a live database, so it runs inside the api container via
``make test-backend``. Everything it creates is namespaced by
``PYTEST_XDIST_WORKER``: device usernames, domain names and hostnames
are globally unique by constraint, so ten workers sharing one MySQL
collide on every one of them.
"""
from __future__ import annotations

import base64
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import bcrypt
import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from app.db import get_session_factory
from app.models.auth import User
from conftest import fixture_writes, purge_tenants, unusable_password_hash
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport

from atrium_ddns import compat_stub, router_nic
from atrium_ddns.compat_stub import CALLS
from atrium_ddns.models import Device, Domain, DomainBackend, Hostname
from atrium_ddns.scripts import import_legacy as il

W = os.environ.get("PYTEST_XDIST_WORKER", "serial")
EMAIL = f"legacy-owner-{W}@example.invalid"

#: ``label -> plaintext``. The importer never sees these; they exist so
#: the wire half can present the *original* password.
PASSWORDS = {
    "router-a": "router-a-secret-1",
    "router-b": "router-b-secret-2",
    "router-c": "router-c-secret-3",
    "router-d": "router-d-secret-4",
    "router-e": "router-e-secret-5",
    "router-f": "router-f-secret-6",
}

#: Which fixture device is hashed at production cost. ``router-a`` owns
#: four hostnames — the same 4/2/2/1/1/1 spread the live database has —
#: so the expensive path is also the busiest one.
PRODUCTION_COST_DEVICE = "router-a"

#: ``label -> how many hostnames``, mirroring production's spread.
SPREAD = {
    "router-a": 4,
    "router-b": 2,
    "router-c": 2,
    "router-d": 1,
    "router-e": 1,
    "router-f": 1,
}

ADMIN_PASSWORD = "the-one-real-account"
TTL = 60

#: The scripted slot the migrated backend resolves to. A real
#: ``route53`` row would contact AWS; this one contacts nothing and
#: still runs every check above ``createrecords`` — the zone check, the
#: credential check and the factory lookup.
STUB_SERVICE = "stub1"

# At import, not inside a fixture. `_resolve_backend_type` goes through
# the provider registry — that is the whole point of it — so a plan
# built for a `stub1` backend refuses unless the slot is registered,
# and most of this file's pure guards build a plan. `register` is a
# no-op when the slot is already claimed, so this is safe beside
# `test_router_nic.py`, which registers the same slots in a fixture.
compat_stub.register_stub_providers(force=True)


def legacy_hash(password: str, *, rounds: int) -> str:
    """A hash in exactly the shape the legacy service writes.

    ``bcrypt.hashpw(pw.encode('utf8'), bcrypt.gensalt())`` is
    ``dyndns-route53/web_routes.py`` verbatim; only the cost is a
    parameter here, and both values produce ``$2b$``.
    """
    return bcrypt.hashpw(
        password.encode("utf8"), bcrypt.gensalt(rounds=rounds)
    ).decode()


# ===================================================================== #
# A synthetic legacy database
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

#: The **stale snapshot's** shape, for the column-set guard: no
#: ``users.web_login``, and none of the four ``hostnames`` columns the
#: legacy app adds by ALTER TABLE at boot. Written out rather than
#: derived from :data:`LEGACY_DDL` so it is a second statement of the
#: schema and not the same one with a hole punched in it.
STALE_DDL = """
CREATE TABLE users (
    id INTEGER NOT NULL PRIMARY KEY, username VARCHAR(80) NOT NULL UNIQUE,
    password_hash VARCHAR(128) NOT NULL, role VARCHAR(10) NOT NULL,
    is_active BOOLEAN NOT NULL, created_at DATETIME, updated_at DATETIME,
    totp_secret TEXT
);
CREATE TABLE domains (id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(255));
CREATE TABLE domain_backends (
    id INTEGER NOT NULL PRIMARY KEY, domain_id INTEGER, backend_type VARCHAR(20)
);
CREATE TABLE backend_configs (
    id INTEGER NOT NULL PRIMARY KEY, domain_backend_id INTEGER,
    config_key VARCHAR(80), config_value TEXT
);
CREATE TABLE hostnames (
    id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(255), domain_id INTEGER,
    user_id INTEGER, created_at DATETIME
);
CREATE TABLE hostname_backends (
    hostname_id INTEGER NOT NULL, domain_backend_id INTEGER NOT NULL,
    PRIMARY KEY (hostname_id, domain_backend_id)
);
CREATE TABLE rate_limit_configs (
    id INTEGER NOT NULL PRIMARY KEY, user_id INTEGER,
    requests_per_minute INTEGER, requests_per_hour INTEGER, is_global BOOLEAN
);
"""


def zone_for(tag: str) -> str:
    """The fixture zone, namespaced per worker **and per world**.

    ``tag`` exists because several tests import a *second* world into
    the same MySQL, and the importer refuses to run twice against
    colliding usernames and hostnames — correctly. Without a tag those
    tests would be measuring that refusal instead of the thing they
    name.
    """
    return f"zone{tag}-{W}.legacy.invalid"


def device_username(label: str, tag: str) -> str:
    return f"{label}{tag}-{W}"


#: The default world's zone, derived rather than restated so it cannot
#: drift from :func:`zone_for`.
ZONE = zone_for("")


def hostname_labels(tag: str = "") -> list[tuple[str, str]]:
    """``(hostname, owning device label)`` for the whole fixture world.

    Derived from :data:`SPREAD` so the eleven names and the mapping
    cannot drift apart, and so a change to the spread changes both.
    """
    out: list[tuple[str, str]] = []
    for label, count in SPREAD.items():
        for index in range(count):
            out.append((f"{label}-{index}.{zone_for(tag)}", label))
    return out


def build_legacy_db(
    path: Path,
    *,
    fernet_key: str,
    ddl: str = LEGACY_DDL,
    rows: dict[str, Any] | None = None,
    tag: str = "",
) -> None:
    """Write a synthetic legacy database mirroring production's shape.

    Seven users (one ``admin`` with ``web_login=1`` and no hostnames,
    six devices with ``web_login=0``), one domain, one backend with two
    Fernet-encrypted config rows, eleven hostnames spread 4/2/2/1/1/1,
    and a ``hostname_backends`` row per hostname — which is what the
    live database actually holds, contrary to plan §3.3.1's note that
    the table is empty.
    """
    overrides = rows or {}
    fernet = Fernet(fernet_key.encode())
    conn = sqlite3.connect(path)
    conn.executescript(ddl)

    conn.execute(
        "INSERT INTO users (id, username, password_hash, role, is_active, "
        "web_login, totp_secret) VALUES (1, ?, ?, 'admin', 1, 1, ?)",
        (
            device_username("admin", tag),
            legacy_hash(ADMIN_PASSWORD, rounds=4),
            fernet.encrypt(b"JBSWY3DPEHPK3PXP").decode(),
        ),
    )
    for index, label in enumerate(SPREAD, start=2):
        rounds = 12 if label == PRODUCTION_COST_DEVICE else 4
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, is_active, "
            "web_login) VALUES (?, ?, ?, 'user', 1, 0)",
            (
                index,
                device_username(label, tag),
                legacy_hash(PASSWORDS[label], rounds=rounds),
            ),
        )

    conn.execute(
        "INSERT INTO domains (id, name) VALUES (1, ?)", (zone_for(tag),)
    )
    conn.execute(
        "INSERT INTO domain_backends (id, domain_id, backend_type) "
        "VALUES (1, 1, ?)",
        (overrides.get("backend_type", STUB_SERVICE),),
    )
    for key, value in (
        ("aws_access_key_id", "AKIAFIXTURENOTREAL01"),
        ("aws_secret_access_key", "fixture-secret-not-real-0123456789"),
    ):
        conn.execute(
            "INSERT INTO backend_configs (domain_backend_id, config_key, "
            "config_value) VALUES (1, ?, ?)",
            (key, fernet.encrypt(value.encode()).decode()),
        )

    user_ids = {label: index for index, label in enumerate(SPREAD, start=2)}
    for hostname_id, (name, label) in enumerate(hostname_labels(tag), start=1):
        conn.execute(
            "INSERT INTO hostnames (id, name, domain_id, user_id, ttl, "
            "last_ip_v4, last_ip_v6, last_updated_at) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
            (
                hostname_id,
                name,
                user_ids[label],
                overrides.get("ttl", {}).get(hostname_id, TTL)
                if isinstance(overrides.get("ttl"), dict)
                else TTL,
                f"203.0.113.{hostname_id}",
                f"2001:db8:113::{hostname_id}",
                "2026-08-15 18:07:00.123456",
            ),
        )
        conn.execute(
            "INSERT INTO hostname_backends (hostname_id, domain_backend_id) "
            "VALUES (?, 1)",
            (hostname_id,),
        )

    conn.execute(
        "INSERT INTO rate_limit_configs (id, user_id, requests_per_minute, "
        "requests_per_hour, is_global) VALUES (1, NULL, 30, 500, 1)"
    )
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture(scope="module")
def legacy_db(tmp_path_factory, fernet_key: str) -> Path:
    path = tmp_path_factory.mktemp("legacy") / "dyndns.db"
    build_legacy_db(path, fernet_key=fernet_key)
    return path


def plan_for(path: Path, key: str | None) -> il.Plan:
    with il.open_source(path) as conn:
        il.assert_columns(conn)
        snapshot = il.read_snapshot(conn)
    return il.build_plan(snapshot, fernet_key=key)


# ===================================================================== #
# 1. The source — opening it, and never touching it
# ===================================================================== #


def test_a_missing_source_names_the_path_and_the_backup_recipe(tmp_path) -> None:
    with pytest.raises(il.MigrationRefused) as excinfo:
        with il.open_source(tmp_path / "nope.db"):
            pass
    message = str(excinfo.value)
    assert "nope.db" in message
    assert ".backup" in message


def test_a_source_with_a_wal_beside_it_is_refused(legacy_db: Path, tmp_path) -> None:
    """A ``-wal`` sibling is the live database, or a naive ``cp`` of one.

    Both are refused and for the same reason: production's main file was
    65 536 bytes beside a 4 MB write-ahead log, so the main file alone
    is a stale prefix — plausible, wrong, and silent.
    """
    copy = tmp_path / "with-wal.db"
    copy.write_bytes(legacy_db.read_bytes())
    (tmp_path / "with-wal.db-wal").write_bytes(b"")

    with pytest.raises(il.MigrationRefused) as excinfo:
        with il.open_source(copy):
            pass
    assert "write-ahead log" in str(excinfo.value)
    assert "with-wal.db-wal" in str(excinfo.value)


def test_opening_the_source_creates_nothing_beside_it(tmp_path) -> None:
    """The regression guard for a trap this importer walked into.

    A ``sqlite3 … ".backup"`` output inherits ``journal_mode=wal`` in
    its header, and opening a WAL database **even with ``mode=ro``**
    makes SQLite create a zero-length ``-wal`` and a 32 KB ``-shm``
    next to it — read-only WAL access needs the shared-memory file.
    Measured: the first ``--dry-run`` against a fresh copy left both
    beside it, and the *second* run then refused its own artefact.

    Pointed at a production volume that is a write into the live
    service's data directory, which is the one thing this importer
    promises not to do. So the second half of this test demonstrates
    the natural implementation doing exactly that, in the same
    directory, on the same file — a guard against a defect that has
    never been shown against it is a guard that is believed rather
    than checked.
    """
    source = tmp_path / "wal-mode.db"
    conn = sqlite3.connect(source)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(LEGACY_DDL)
    conn.commit()
    conn.close()
    for stray in tmp_path.glob("wal-mode.db-*"):
        stray.unlink()

    before = sorted(p.name for p in tmp_path.iterdir())
    with il.open_source(source) as opened:
        assert opened.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert sorted(p.name for p in tmp_path.iterdir()) == before

    # The natural implementation, for comparison. If this ever stops
    # creating files the guard above has become vacuous and should be
    # deleted rather than kept as decoration.
    naive = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    # A real table read, not `SELECT 1`: a constant expression never
    # touches the pager, so it never asks for the shared-memory file and
    # the comparison below would be measuring nothing.
    naive.execute("SELECT COUNT(*) FROM users").fetchone()
    naive.close()
    after = sorted(p.name for p in tmp_path.iterdir())
    assert after != before, (
        "opening a WAL database read-only in place no longer creates "
        "sidecar files; test_opening_the_source_creates_nothing_beside_it "
        "is now vacuous"
    )
    assert "wal-mode.db-shm" in after


def test_a_stale_snapshot_is_refused_by_name(tmp_path, fernet_key: str) -> None:
    """The column set is asserted **before a single row is read**.

    A snapshot missing ``web_login`` answers ``SELECT id, username FROM
    users`` perfectly happily; the run only goes wrong later, in a way
    that reads as a data problem. So the refusal names every column,
    and this asserts the names rather than the raise.
    """
    path = tmp_path / "stale.db"
    conn = sqlite3.connect(path)
    conn.executescript(STALE_DDL)
    conn.commit()
    conn.close()

    with pytest.raises(il.MigrationRefused) as excinfo:
        with il.open_source(path) as opened:
            il.assert_columns(opened)
    message = str(excinfo.value)
    for column in ("web_login", "ttl", "last_ip_v4", "last_ip_v6",
                   "last_updated_at"):
        assert column in message, column
    assert "DIFFERENT SCHEMA" in message


def test_the_live_schema_is_accepted(legacy_db: Path) -> None:
    with il.open_source(legacy_db) as conn:
        observed = il.assert_columns(conn)
    assert "web_login" in observed["users"]
    assert "ttl" in observed["hostnames"]


def test_a_missing_table_is_named_rather_than_read_past(tmp_path) -> None:
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY);")
    conn.commit()
    conn.close()
    with pytest.raises(il.MigrationRefused) as excinfo:
        with il.open_source(path) as opened:
            il.assert_columns(opened)
    assert "'domains': ABSENT" in str(excinfo.value)


# ===================================================================== #
# 2. The Fernet key — four failures, four messages, nothing written
# ===================================================================== #


def test_a_missing_fernet_key_stops_before_any_write(legacy_db: Path) -> None:
    with pytest.raises(il.MigrationRefused) as excinfo:
        plan_for(legacy_db, None)
    message = str(excinfo.value)
    assert il.FERNET_KEY_ENV in message
    assert "Nothing has been written" in message


def test_a_wrong_fernet_key_says_wrong_key_not_corrupt(
    legacy_db: Path,
) -> None:
    """Well-formed token, wrong key. The distinction is the diagnosis.

    ``InvalidToken`` covers both "somebody handed me the wrong key" and
    "this column is not a Fernet token at all", and they need different
    fixes. Only the first can be reached with a valid key of the wrong
    value, so it is the one this asserts.
    """
    with pytest.raises(il.MigrationRefused) as excinfo:
        plan_for(legacy_db, Fernet.generate_key().decode())
    message = str(excinfo.value)
    assert "WRONG KEY" in message
    assert "Nothing has been written" in message


def test_a_malformed_fernet_key_is_refused_without_echoing_it() -> None:
    backend = il.LegacyBackend(
        id=1, domain_id=1, backend_type="aws", ciphertext={"k": "gAAAA"}
    )
    with pytest.raises(il.MigrationRefused) as excinfo:
        il.decrypt_credentials(backend, "not-a-fernet-key")
    message = str(excinfo.value)
    assert "not a valid Fernet key" in message
    assert "not-a-fernet-key" not in message, "the key value leaked"


def test_a_credential_that_decrypts_to_empty_is_refused(
    fernet_key: str,
) -> None:
    """The failure that survives review, stated as a test.

    An empty plaintext writes a row that looks migrated: the UI shows a
    configured backend and every update answers ``911``. Nothing about
    the database says why.
    """
    fernet = Fernet(fernet_key.encode())
    backend = il.LegacyBackend(
        id=1,
        domain_id=1,
        backend_type="aws",
        ciphertext={"aws_access_key_id": fernet.encrypt(b"").decode()},
    )
    with pytest.raises(il.MigrationRefused) as excinfo:
        il.decrypt_credentials(backend, fernet_key)
    assert "EMPTY string" in str(excinfo.value)


def test_a_backend_with_no_config_rows_is_refused(fernet_key: str) -> None:
    backend = il.LegacyBackend(
        id=7, domain_id=1, backend_type="aws", ciphertext={}
    )
    with pytest.raises(il.MigrationRefused) as excinfo:
        il.decrypt_credentials(backend, fernet_key)
    assert "no backend_configs rows" in str(excinfo.value)


def test_a_correct_key_returns_the_plaintext(
    legacy_db: Path, fernet_key: str
) -> None:
    plan = plan_for(legacy_db, fernet_key)
    (backend,) = plan.backends
    assert backend.credentials == {
        "aws_access_key_id": "AKIAFIXTURENOTREAL01",
        "aws_secret_access_key": "fixture-secret-not-real-0123456789",
    }


# ===================================================================== #
# 3. backend_type resolves through the registry, never by string match
# ===================================================================== #


def test_the_stored_aws_spelling_resolves_to_canonical_route53() -> None:
    """``aws`` is what the live database holds; ``route53`` is the name.

    #15 registered ``route53`` canonical with an ``aws`` alias, so this
    is a registry lookup and not a rename table. Asserting the
    *canonical* name is what stops the stored alias reaching a UI that
    then offers a service name the registry does not advertise.
    """
    assert il._resolve_backend_type("aws") == "route53"
    assert il._resolve_backend_type("route53") == "route53"


def test_an_unknown_backend_type_is_refused_with_the_known_set() -> None:
    with pytest.raises(il.MigrationRefused) as excinfo:
        il._resolve_backend_type("no-such-provider")
    message = str(excinfo.value)
    assert "no-such-provider" in message
    assert "route53" in message
    assert "911" in message


def test_a_legacy_database_naming_an_unknown_backend_is_refused(
    tmp_path, fernet_key: str
) -> None:
    path = tmp_path / "unknown-backend.db"
    build_legacy_db(
        path, fernet_key=fernet_key, rows={"backend_type": "no-such-provider"}
    )
    with pytest.raises(il.MigrationRefused) as excinfo:
        plan_for(path, fernet_key)
    assert "no provider is registered" in str(excinfo.value)


# ===================================================================== #
# 4. The mapping, and everything it will not guess at
# ===================================================================== #


def test_the_plan_is_one_owner_six_devices_and_eleven_hostnames(
    legacy_db: Path, fernet_key: str
) -> None:
    plan = plan_for(legacy_db, fernet_key)
    assert plan.owner.username == f"admin-{W}"
    assert len(plan.devices) == 6
    assert len(plan.domains) == 1
    assert len(plan.backends) == 1
    assert len(plan.hostnames) == 11
    assert plan.snapshot.counts["users"] == 7


def test_the_whole_hostname_to_device_mapping_is_carried(
    legacy_db: Path, fernet_key: str
) -> None:
    """All eleven, compared as a set. Not a sample.

    Derived from :func:`hostname_labels`, which is also what built the
    database — so this asserts the mapping survived the mapping code,
    not that two hand-written lists agree.
    """
    plan = plan_for(legacy_db, fernet_key)
    expected = {name: f"{label}-{W}" for name, label in hostname_labels()}
    assert plan.hostname_to_device == expected
    assert len(expected) == 11


def test_every_password_hash_is_carried_byte_for_byte(
    legacy_db: Path, fernet_key: str
) -> None:
    conn = sqlite3.connect(legacy_db)
    source = dict(
        conn.execute(
            "SELECT username, password_hash FROM users WHERE role <> 'admin'"
        )
    )
    conn.close()
    plan = plan_for(legacy_db, fernet_key)
    assert {d.username: d.password_hash for d in plan.devices} == source
    assert all(h.startswith("$2b$") for h in source.values())


def test_the_hashes_are_the_ones_auth_device_can_verify(
    legacy_db: Path, fernet_key: str
) -> None:
    """The shape check, asked of the hasher the router actually uses.

    ``pwdlib.PasswordHash.recommended()`` holds Argon2 alone and
    *raises* ``UnknownHashError`` on a bcrypt hash; every one of these
    devices would answer ``badauth`` for ever under it. Asserting
    against the tuple in :mod:`atrium_ddns.auth_device` rather than a
    prefix keeps the two from drifting.
    """
    from atrium_ddns.auth_device import _PASSWORD_HASH

    plan = plan_for(legacy_db, fernet_key)
    for device in plan.devices:
        assert any(
            hasher.identify(device.password_hash)
            for hasher in _PASSWORD_HASH.hashers
        ), device.username


def test_the_legacy_ttl_lands_on_the_backend_not_the_hostname(
    legacy_db: Path, fernet_key: str
) -> None:
    """A correction to the issue body, made explicit.

    The issue asks for ``ttl`` to be "preserved" on ``ddns_hostname``.
    That column does not exist: TTL is per **backend** in this schema
    (``ddns_domain_backend.config['ttl']``, read by
    ``router_nic._backend_plan``), because a zone is written with one
    TTL by one provider. The value is carried; where it lands is
    different from where the issue expected it.
    """
    plan = plan_for(legacy_db, fernet_key)
    assert not hasattr(plan.hostnames[0], "ttl")
    assert plan.backends[0].config["ttl"] == TTL


def test_disagreeing_ttls_in_one_zone_are_refused(
    tmp_path, fernet_key: str
) -> None:
    path = tmp_path / "ttl-clash.db"
    build_legacy_db(path, fernet_key=fernet_key)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE hostnames SET ttl = 300 WHERE id = 1")
    conn.commit()
    conn.close()
    with pytest.raises(il.MigrationRefused) as excinfo:
        plan_for(path, fernet_key)
    message = str(excinfo.value)
    assert "per hostname" in message
    assert "300" in message


@pytest.mark.parametrize(
    "sql, expected",
    [
        pytest.param(
            "UPDATE users SET role = 'admin' WHERE id = 2",
            "found 2",
            id="two-admins",
        ),
        pytest.param(
            "UPDATE users SET role = 'user' WHERE id = 1",
            "found 0",
            id="no-admin",
        ),
        pytest.param(
            "UPDATE hostnames SET user_id = 1 WHERE id = 1",
            "owns 1 hostname",
            id="admin-owns-a-hostname",
        ),
        pytest.param(
            "UPDATE users SET web_login = 0 WHERE id = 1",
            "wrong way round",
            id="admin-cannot-log-in",
        ),
        pytest.param(
            "UPDATE users SET web_login = 1 WHERE id = 2",
            "this row is a person",
            id="device-can-log-in",
        ),
        pytest.param(
            "UPDATE users SET is_active = 0 WHERE id = 2",
            "back to life at cutover",
            id="disabled-device",
        ),
        pytest.param(
            "UPDATE users SET password_hash = 'plaintext' WHERE id = 2",
            "cannot identify",
            id="unrecognisable-hash",
        ),
        pytest.param(
            "INSERT INTO rate_limit_configs (user_id, requests_per_minute, "
            "requests_per_hour, is_global) VALUES (2, 5, 60, 0)",
            "per-user rate limit",
            id="device-with-a-rate-limit-override",
        ),
        pytest.param(
            "UPDATE hostnames SET domain_id = 99 WHERE id = 1",
            "does not exist",
            id="orphan-hostname",
        ),
        # A second backend on the zone that no hostname is bound to.
        # Deleting a binding would NOT do it: the legacy model reads an
        # empty association as "all of this domain's backends", so an
        # absent row and a complete one mean the same thing. Only a
        # strict subset is unrepresentable here.
        pytest.param(
            "INSERT INTO domain_backends (id, domain_id, backend_type) "
            "VALUES (2, 1, 'hetzner')",
            "SUBSET",
            id="selective-backend-binding",
        ),
    ],
)
def test_a_world_the_mapping_cannot_represent_is_refused(
    tmp_path, fernet_key: str, sql: str, expected: str
) -> None:
    """Ten mutations of a good world, each refused, each named.

    Every one of these is a state the mapping *could* have guessed at,
    and every guess changes behaviour without saying so — a disabled
    router coming back to life, a person becoming a router, an override
    silently dropped onto the installation default. The refusal is the
    feature.
    """
    path = tmp_path / f"mutant-{abs(hash(sql))}.db"
    build_legacy_db(path, fernet_key=fernet_key)
    conn = sqlite3.connect(path)
    conn.execute(sql)
    conn.commit()
    conn.close()

    with pytest.raises(il.MigrationRefused) as excinfo:
        plan_for(path, fernet_key)
    assert expected in str(excinfo.value)


def test_the_mutation_harness_is_not_vacuous(
    tmp_path, fernet_key: str
) -> None:
    """The unmutated world plans cleanly.

    Without this, every case above would pass against a fixture that
    was broken for some other reason, and the parametrised test would
    be asserting that a database is unreadable rather than that a rule
    bites.
    """
    path = tmp_path / "control.db"
    build_legacy_db(path, fernet_key=fernet_key)
    plan = plan_for(path, fernet_key)
    assert len(plan.devices) == 6 and len(plan.hostnames) == 11


def test_a_selective_binding_is_only_refused_when_it_is_selective(
    tmp_path, fernet_key: str
) -> None:
    """Eleven rows binding every hostname to the only backend is *not* it.

    The legacy model's ``get_backends()`` returns the domain's backends
    when the association is empty, so "all of them" and "none listed"
    mean the same thing — and this schema expresses both. Plan §3.3.1
    recorded the table empty on 2026-08-15 and it holds eleven rows;
    the note in the output says so, and the target is unaffected.
    """
    path = tmp_path / "fully-bound.db"
    build_legacy_db(path, fernet_key=fernet_key)
    plan = plan_for(path, fernet_key)
    assert len(plan.snapshot.hostname_backends) == 11
    assert any("degenerate" in note for note in plan.notes)


def test_names_are_normalised_and_a_collision_is_refused(
    tmp_path, fernet_key: str
) -> None:
    """``/nic/update`` lower-cases before it looks a name up.

    A migrated row that kept an upper-case letter is unreachable: the
    router gets ``nohost`` for a name plainly present in the database.
    So names are lower-cased on the way in — and two legacy names that
    differ only in case become one row, which is a refusal rather than
    a silent merge.
    """
    path = tmp_path / "mixed-case.db"
    build_legacy_db(path, fernet_key=fernet_key)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE hostnames SET name = UPPER(name) WHERE id = 1")
    conn.commit()
    conn.close()
    plan = plan_for(path, fernet_key)
    assert all(h.name == h.name.lower() for h in plan.hostnames)
    assert any("normalised" in note for note in plan.notes)

    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE hostnames SET name = (SELECT UPPER(name) FROM hostnames "
        "WHERE id = 2) WHERE id = 3"
    )
    conn.commit()
    conn.close()
    with pytest.raises(il.MigrationRefused) as excinfo:
        plan_for(path, fernet_key)
    assert "collide once normalised" in str(excinfo.value)


def test_the_totp_secret_is_reported_and_never_carried(
    legacy_db: Path, fernet_key: str
) -> None:
    plan = plan_for(legacy_db, fernet_key)
    assert any("TOTP" in note for note in plan.notes)
    assert not any(
        hasattr(user, "totp_secret") for user in plan.snapshot.users
    ), "the TOTP ciphertext must not reach a dataclass repr"


# ===================================================================== #
# 5. The database round trip
# ===================================================================== #


#: Who this module's guarded regions say they are, in the message
#: :func:`conftest.fixture_writes` prints when a nesting bug or a
#: timeout stops the run. A bare ``"?"`` names nothing.
LOCK_OWNER = "test_import_legacy.imported"


async def _make_owner() -> int:
    """The account every migrated row hangs off.

    Inside :func:`conftest.fixture_writes` because this is one of the
    two inserts in ``conftest``'s logged deadlocks — ``ix_users_email``
    — and the zone import below is the other, ``uq_ddns_domain_name``.
    ``unusable_password_hash`` is the shared cached argon2 placeholder:
    the input is a constant no test reads, and hashing it per module
    per worker cost ~22 ms for nothing.
    """
    async with fixture_writes(LOCK_OWNER) as session:
        user = User(
            email=EMAIL,
            hashed_password=unusable_password_hash(),
            is_active=True,
            is_verified=True,
            full_name="Legacy owner",
            preferred_language="en",
        )
        session.add(user)
        await session.flush()
        return user.id


@pytest_asyncio.fixture(scope="module")
async def imported(legacy_db: Path, fernet_key: str) -> Any:
    """Run the importer for real, once, and yield what it wrote.

    Module-scoped: the import is the subject, and re-running it per
    test would be re-running the thing under test rather than testing
    it.

    Teardown is :func:`conftest.purge_tenants`, not the module-local
    copy of it this fixture used to own. Removing the owner is what
    does the work — domains, devices, hostnames and backends all
    cascade off it — and the shared helper additionally clears the
    tenant's ``ddns_event`` rows and its ``user_secret_keys`` row, in
    the order that leaves the cascade nothing to do.

    It is called at **both** ends, as it was before: the wire tests
    below drive ``/nic/update`` against the migrated world, and
    ``test_a_second_run_refuses_and_names_the_collisions`` asserts on
    rows this fixture wrote. A teardown that silently stopped running
    would surface as the *next* run's import refusing on 6 of 6 device
    usernames, not as an error here.

    Deliberately **no** ``unattributed_emails``: ``router_nic`` sets
    ``ddns_event.user_id`` and ``ddns_event.user_email`` from the same
    ``auth`` object, so a row carrying this owner's address always
    carries its id too. Passing it would buy a full scan of an
    unindexed column to look for rows that cannot exist — #14's defect,
    re-added.
    """
    compat_stub.register_stub_providers(force=True)
    await purge_tenants([EMAIL], owner=LOCK_OWNER)
    owner_id = await _make_owner()

    plan = plan_for(legacy_db, fernet_key)
    factory = get_session_factory()
    async with fixture_writes(LOCK_OWNER) as session:
        written = await il.apply(
            session, plan, owner_email=EMAIL, create_owner=False
        )
    async with factory() as session:
        reading = await il.verify(session, written.owner_id)

    # The stub reads its scripted result out of
    # `ddns_domain_backend.config`, and the legacy schema has no column
    # for one — every legacy config row is an encrypted credential. So
    # the migrated row is scripted here, AFTER the import and outside
    # it, rather than by teaching the importer about a fixture. The
    # `ttl` the import wrote is left exactly as it is, and asserted
    # further down against the value the provider is called with.
    async with factory() as session:
        row = (
            await session.execute(
                sa.select(DomainBackend).where(DomainBackend.user_id == owner_id)
            )
        ).scalar_one()
        row.config = {**(row.config or {}), "result": "good"}
        await session.commit()

    yield {
        "owner_id": owner_id,
        "plan": plan,
        "written": written,
        "reading": reading,
    }

    await purge_tenants([EMAIL], owner=LOCK_OWNER)


def test_both_instruments_agree_on_every_count(imported) -> None:
    """Rows read from the source, and rows present in the target.

    ``written`` is what :func:`~atrium_ddns.scripts.import_legacy.apply`
    asked for; ``reading`` is what a **fresh session** found by
    traversing the same relationships ``/nic/update`` does. Same
    numbers by two mechanisms; :func:`compare` is what refuses when
    they part.
    """
    plan, written, reading = (
        imported["plan"],
        imported["written"],
        imported["reading"],
    )
    assert il.compare(plan, written, reading) == []
    assert (len(plan.snapshot.device_rows), written.devices, reading.devices) == (
        6,
        6,
        6,
    )
    assert (plan.snapshot.counts["hostnames"], written.hostnames, reading.hostnames) == (
        11,
        11,
        11,
    )
    assert (plan.snapshot.counts["domains"], written.domains, reading.domains) == (
        1,
        1,
        1,
    )
    assert (
        plan.snapshot.counts["domain_backends"],
        written.backends,
        reading.backends,
    ) == (1, 1, 1)


def test_the_comparison_is_not_vacuous(imported) -> None:
    """Break each half of the comparison and watch it bite.

    :func:`compare` returning ``[]`` is only evidence if it can return
    something else. Four mutations, one per thing it claims to check.
    """
    plan, written, reading = (
        imported["plan"],
        imported["written"],
        imported["reading"],
    )
    assert il.compare(plan, replace(written, hostnames=10), reading)
    assert il.compare(
        plan,
        written,
        replace(reading, hostname_to_device={"nope." + ZONE: "wrong"}),
    )
    assert il.compare(
        plan, written, replace(reading, password_hashes={"a": "$2b$x"})
    )
    assert il.compare(
        plan, written, replace(reading, credential_digests={("z", "y"): "0" * 16})
    )


async def test_every_hostname_is_on_the_device_that_owned_it(imported) -> None:
    """All eleven, read out of the database through the FK, in full.

    Not through :class:`Reading`, which the importer built: this is a
    third reading, taken with a join the importer does not use, and
    compared against the mapping derived from the fixture builder.
    """
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                sa.select(Hostname.name, Device.username)
                .join(Device, Device.id == Hostname.device_id)
                .join(Domain, Domain.id == Hostname.domain_id)
                .where(Domain.user_id == imported["owner_id"])
            )
        ).all()
    assert dict(rows) == {name: f"{label}-{W}" for name, label in hostname_labels()}
    assert len(rows) == 11


async def test_the_stored_hashes_are_byte_identical_to_the_legacy_rows(
    imported, legacy_db: Path
) -> None:
    """Read straight out of MySQL and compared against the SQLite bytes.

    Deliberately before anything authenticates: a successful bcrypt
    verify re-hashes the row to argon2id (plan §3.2), which is correct
    and would make this assertion untrue five milliseconds later. The
    ordering is enforced by
    ``test_a_successful_call_upgrades_the_stored_hash`` reading the same
    column afterwards.
    """
    conn = sqlite3.connect(legacy_db)
    source = dict(
        conn.execute(
            "SELECT username, password_hash FROM users WHERE role <> 'admin'"
        )
    )
    conn.close()

    factory = get_session_factory()
    async with factory() as session:
        stored = dict(
            (
                await session.execute(
                    sa.select(Device.username, Device.password_hash).where(
                        Device.user_id == imported["owner_id"]
                    )
                )
            ).all()
        )
    assert stored == source
    assert all(value.startswith("$2b$") for value in stored.values())


async def test_the_credential_round_trips_through_per_user_encryption(
    imported, fernet_key: str
) -> None:
    """Fernet in, ``SecretBlob`` + ``UserSecret`` out, same bytes.

    Compared by SHA-256 of the canonical JSON rather than by printing
    it: the production version of this value is an AWS secret access
    key, and a test that prints the fixture's teaches the habit of
    printing the real one.
    """
    from app.host_sdk.crypto import unlock_user_secrets

    factory = get_session_factory()
    async with factory() as session:
        await unlock_user_secrets(session, imported["owner_id"])
        row = (
            await session.execute(
                sa.select(DomainBackend).where(
                    DomainBackend.user_id == imported["owner_id"]
                )
            )
        ).scalar_one()
        revealed = row.credentials.reveal()
        assert row.credentials_ct is not None
        assert b"AKIAFIXTURENOTREAL01" not in row.credentials_ct

    assert il.credential_digest(revealed) == il.credential_digest(
        imported["plan"].backends[0].credentials
    )
    assert row.backend_type == STUB_SERVICE
    assert row.config["ttl"] == TTL


async def test_a_second_run_refuses_and_names_the_collisions(
    imported, legacy_db: Path, fernet_key: str
) -> None:
    """Refuses rather than merging — and says which of the two it is.

    Idempotence here would mean reconciling a device password an
    operator may have rotated in the new UI against a legacy database
    that still holds the old one, and "merge" would revert it silently.
    """
    plan = plan_for(legacy_db, fernet_key)
    factory = get_session_factory()
    async with factory() as session:
        with pytest.raises(il.MigrationRefused) as excinfo:
            await il.apply(session, plan, owner_email=EMAIL, create_owner=False)
        await session.rollback()
    message = str(excinfo.value)
    assert "6 of 6 device username(s) already exist" in message
    assert "11 of 11 hostname(s) already exist" in message
    assert "REFUSES to run twice" in message


async def test_a_dry_run_writes_nothing(
    tmp_path, fernet_key: str
) -> None:
    """The rollback half, measured by counting rows rather than trusting it.

    A different owner and a different zone from :func:`imported`, so
    this cannot be confused by rows that import left behind.
    """
    email = f"dry-run-{W}@example.invalid"
    path = tmp_path / "dry-run.db"
    build_legacy_db(path, fernet_key=fernet_key, tag="-dry")
    plan = plan_for(path, fernet_key)

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            sa.text("DELETE FROM users WHERE email = :e"), {"e": email}
        )
        await session.commit()

    from fastapi_users.password import PasswordHelper

    async with factory() as session:
        user = User(
            email=email,
            hashed_password=PasswordHelper().hash("unusable-" + "y" * 24),
            is_active=True,
            is_verified=True,
            full_name="Dry run",
            preferred_language="en",
        )
        session.add(user)
        await session.flush()
        owner_id = user.id
        await session.commit()

    async with factory() as session:
        written = await il.apply(
            session, plan, owner_email=email, create_owner=False
        )
        assert written.hostnames == 11
        await session.rollback()

    async with factory() as session:
        remaining = (
            await session.execute(
                sa.select(sa.func.count(Device.id)).where(
                    Device.user_id == owner_id
                )
            )
        ).scalar_one()
        await session.execute(
            sa.text("DELETE FROM users WHERE email = :e"), {"e": email}
        )
        await session.commit()
    assert remaining == 0


async def test_an_absent_owner_is_refused_rather_than_invented(
    tmp_path, fernet_key: str
) -> None:
    """Its own world: the collision check runs first, and correctly.

    Against the already-imported world this would refuse for the *other*
    reason and pass while asserting nothing about the owner.
    """
    path = tmp_path / "absent-owner.db"
    build_legacy_db(path, fernet_key=fernet_key, tag="-noowner")
    plan = plan_for(path, fernet_key)
    factory = get_session_factory()
    async with factory() as session:
        with pytest.raises(il.MigrationRefused) as excinfo:
            await il.apply(
                session,
                plan,
                owner_email=f"nobody-{W}@example.invalid",
                create_owner=False,
            )
        await session.rollback()
    assert "--create-owner" in str(excinfo.value)


async def test_create_owner_carries_the_legacy_admin_hash_verbatim(
    tmp_path, fernet_key: str
) -> None:
    """The one real account keeps its existing web password.

    Measured against this image rather than assumed:
    ``fastapi_users.password.PasswordHelper`` holds
    ``(Argon2Hasher, BcryptHasher)`` and verifies a legacy ``$2b$``
    hash, re-hashing to argon2id on the first successful login. That is
    **not** the same as ``pwdlib.PasswordHash.recommended()``, which
    holds Argon2 alone and raises — the distinction the epic draws for
    devices holds for the web login too, in the opposite direction.
    """
    from fastapi_users.password import PasswordHelper

    email = f"created-owner-{W}@example.invalid"
    path = tmp_path / "create-owner.db"
    build_legacy_db(path, fernet_key=fernet_key, tag="-created")
    plan = plan_for(path, fernet_key)

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            sa.text("DELETE FROM users WHERE email = :e"), {"e": email}
        )
        await session.commit()

    async with factory() as session:
        written = await il.apply(
            session, plan, owner_email=email, create_owner=True
        )
        await session.commit()
    assert written.owner_action == "created"

    async with factory() as session:
        user = (
            await session.execute(sa.select(User).where(User.email == email))
        ).scalar_one()
        stored = user.hashed_password
    assert stored == plan.owner.password_hash
    assert stored.startswith("$2b$")
    verified, upgraded = PasswordHelper().verify_and_update(
        ADMIN_PASSWORD, stored
    )
    assert verified
    assert upgraded is not None and upgraded.startswith("$argon2")

    async with factory() as session:
        await session.execute(
            sa.text("DELETE FROM users WHERE email = :e"), {"e": email}
        )
        await session.commit()


# ===================================================================== #
# 6. The wire — the half a column read cannot satisfy
# ===================================================================== #


@pytest.fixture(scope="module")
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(router_nic.router)
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> Any:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://migrated.test"
    ) as instance:
        yield instance


@pytest.fixture(autouse=True)
def clear_calls() -> Iterator[None]:
    CALLS.clear()
    yield
    CALLS.clear()


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}", "X-Forwarded-For": "203.0.113.9"}


async def test_a_migrated_device_updates_with_its_original_password(
    client, imported
) -> None:
    """End to end, over HTTP, with the password the router already has.

    The production-cost device (``$2b$12$``, the exact shape and cost
    the live database holds) presenting the plaintext nobody migrated,
    against a hash nobody re-hashed. ``good`` is the whole acceptance
    criterion, and the provider call log carries the second half of it:
    the record type, the address, and the **TTL the importer moved from
    the legacy hostname row onto the backend config**.
    """
    hostname = f"{PRODUCTION_COST_DEVICE}-0.{ZONE}"
    response = await client.get(
        "/nic/update",
        params={"hostname": hostname, "myip": "198.51.100.7"},
        headers=basic(
            f"{PRODUCTION_COST_DEVICE}-{W}", PASSWORDS[PRODUCTION_COST_DEVICE]
        ),
    )
    assert response.text == "good 198.51.100.7"
    calls = [call for call in CALLS if call.op == "create"]
    assert [(c.hostname, c.rtype, c.ip, c.ttl) for c in calls] == [
        (hostname, "A", "198.51.100.7", TTL)
    ]


async def test_a_wrong_password_is_badauth_on_a_migrated_device(
    client, imported
) -> None:
    """The guard bites — and it is the *hash* that refuses, not the row.

    Without this the test above passes against an importer that stored
    no hash at all, or one that stored a hash nothing checks.
    """
    response = await client.get(
        "/nic/update",
        params={
            "hostname": f"{PRODUCTION_COST_DEVICE}-0.{ZONE}",
            "myip": "198.51.100.8",
        },
        headers=basic(f"{PRODUCTION_COST_DEVICE}-{W}", "not-the-password"),
    )
    assert response.text == "badauth"
    assert [call for call in CALLS if call.op == "create"] == []


async def test_a_username_that_did_not_migrate_is_also_badauth(
    client, imported
) -> None:
    response = await client.get(
        "/nic/update",
        params={"hostname": f"{PRODUCTION_COST_DEVICE}-0.{ZONE}"},
        headers=basic(f"never-existed-{W}", PASSWORDS[PRODUCTION_COST_DEVICE]),
    )
    assert response.text == "badauth"


async def test_every_migrated_device_reaches_exactly_its_own_hostnames(
    client, imported
) -> None:
    """The whole mapping, over the wire. Eleven good and eleven nohost.

    The database half of this is asserted three files up from the FK;
    this is the half a correct-looking ``device_id`` cannot satisfy.
    Each hostname is fetched by its owner (``good``) and by the *next*
    device in the fixture (``nohost``) — the tenant matches in both
    cases, so ``nohost`` here is ownership being enforced per device
    rather than per account, which is the whole point of the model
    change (plan §3.3).

    Twenty-two requests spread over six devices is ~4 each against a
    30/minute namespace default, so the limiter stays out of the way
    without being disabled.
    """
    labels = list(SPREAD)
    good, refused = 0, 0
    for name, label in hostname_labels():
        response = await client.get(
            "/nic/update",
            params={"hostname": name, "myip": "198.51.100.20"},
            headers=basic(f"{label}-{W}", PASSWORDS[label]),
        )
        assert response.text == "good 198.51.100.20", (name, label)
        good += 1

        other = labels[(labels.index(label) + 1) % len(labels)]
        response = await client.get(
            "/nic/update",
            params={"hostname": name, "myip": "198.51.100.21"},
            headers=basic(f"{other}-{W}", PASSWORDS[other]),
        )
        # `nohost {ip}` rather than bare `nohost` — divergence 1 in the
        # frozen table (`update-nohost-hostname-owned-by-another-user`).
        # Update echoes the address, delete does not.
        assert response.text == "nohost 198.51.100.21", (name, other)
        refused += 1

    assert (good, refused) == (11, 11)


async def test_a_successful_call_upgrades_the_stored_hash(imported) -> None:
    """The fleet migrates itself as routers check in — plan §3.2.

    Runs after the wire tests above have authenticated every device, so
    every migrated bcrypt row has been through
    ``verify_and_update``. Ordered by name within the file, which is
    how ``--dist=loadfile`` runs it: this file is one worker, in
    declaration order.
    """
    factory = get_session_factory()
    async with factory() as session:
        shapes = dict(
            (
                await session.execute(
                    sa.select(Device.username, Device.password_hash).where(
                        Device.user_id == imported["owner_id"]
                    )
                )
            ).all()
        )
    assert len(shapes) == 6
    assert all(value.startswith("$argon2") for value in shapes.values()), {
        k: v[:7] for k, v in shapes.items()
    }
