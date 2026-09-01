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
from atrium_ddns.models import (
    Device,
    Domain,
    DomainBackend,
    Hostname,
    HostnameBackend,
)
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

#: The TTL the widened world gives its one dissenting hostname. Chosen
#: to be neither :data:`TTL` nor ``providers.DEFAULT_TTL`` (both 60), so
#: a per-name TTL that was silently dropped cannot resolve to the right
#: answer by falling through to either fallback.
ODD_TTL = 300

#: The scripted slot the migrated backend resolves to. A real
#: ``route53`` row would contact AWS; this one contacts nothing and
#: still runs every check above ``createrecords`` — the zone check, the
#: credential check and the factory lookup.
STUB_SERVICE = "stub1"

#: The second slot the widened world adds beside :data:`STUB_SERVICE`.
#: A *strict subset* is not expressible on a zone with one backend —
#: "all of them" and "the only one" are the same set — so without a
#: second backend there is nothing for #83's second widening to be
#: about, and a fixture that forgot it would pass while asserting the
#: degenerate case twice.
SECOND_SLOT = "stub2"

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


#: The legacy hostname id the widened world singles out. ``1`` is
#: ``router-a-0``, owned by the production-cost device, so the pinned
#: TTL and the narrowed selection are exercised on the same row the
#: wire tests already drive.
WIDENED_HOSTNAME_ID = 1


def widen(path: Path, *, fernet_key: str) -> None:
    """Turn a built world into the two states #83 stopped refusing.

    **Both at once, in one zone**, because they interact and a fixture
    that exercised them separately would never ask the question that
    matters: a pinned ``ddns_hostname.ttl`` has to win over the
    ``config['ttl']`` of a binding the name did *not* select, and it
    has to keep winning for the names that selected nothing.

    Three mutations, each undoing one of the reasons the base fixture
    cannot express a subset:

    * a **second backend** on the only zone, with its own Fernet
      credentials — without it "all of this zone's backends" and "the
      one backend" are the same set;
    * every hostname **except** :data:`WIDENED_HOSTNAME_ID` gains a
      binding to that second backend, so it stays degenerate. Leaving
      them alone would make all eleven names a strict subset and the
      test would no longer be about one;
    * :data:`WIDENED_HOSTNAME_ID` moves to :data:`ODD_TTL`, so the zone
      disagrees.

    The result: one name pinned to one backend at 300, ten names
    inheriting both backends at 60, and a zone whose binding says 60.
    """
    fernet = Fernet(fernet_key.encode())
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO domain_backends (id, domain_id, backend_type) "
        "VALUES (2, 1, ?)",
        (SECOND_SLOT,),
    )
    for key, value in (
        ("aws_access_key_id", "AKIAFIXTURENOTREAL02"),
        ("aws_secret_access_key", "fixture-secret-not-real-9876543210"),
    ):
        conn.execute(
            "INSERT INTO backend_configs (domain_backend_id, config_key, "
            "config_value) VALUES (2, ?, ?)",
            (key, fernet.encrypt(value.encode()).decode()),
        )
    conn.execute(
        "INSERT INTO hostname_backends (hostname_id, domain_backend_id) "
        "SELECT id, 2 FROM hostnames WHERE id <> ?",
        (WIDENED_HOSTNAME_ID,),
    )
    conn.execute(
        "UPDATE hostnames SET ttl = ? WHERE id = ?",
        (ODD_TTL, WIDENED_HOSTNAME_ID),
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
    """A correction to #50's issue body, and the half of it #83 undid.

    #50 asked for ``ttl`` to be "preserved" on ``ddns_hostname``. At
    the time that column did not exist — TTL was per **backend**
    (``ddns_domain_backend.config['ttl']``, read by
    ``router_nic._backend_plan``) — so the value was carried, but not
    where the issue expected it.

    ``0004_hostname_backends_and_ttl`` has since added the column, and
    #83 writes it. What did **not** change is where an *agreeing*
    zone's TTL lands: still the binding, with every ``ddns_hostname.
    ttl`` NULL, because NULL is what lets a name keep following its
    zone. So both halves are asserted here — the column exists and it
    is deliberately empty for this world.
    """
    plan = plan_for(legacy_db, fernet_key)
    assert hasattr(plan.hostnames[0], "ttl")
    assert plan.hostnames[0].ttl is None
    assert plan.backends[0].config["ttl"] == TTL


def test_a_zone_that_agrees_about_ttl_still_pins_no_hostname(
    legacy_db: Path, fernet_key: str
) -> None:
    """#83 must not change the path #50 rehearsed. The regression half.

    The measured population is uniformly 60 (``refactor-plan.md``
    §3.3.1), so *this* is the branch a real re-import takes, and the
    only evidence that widening the other one cost nothing is that
    every ``ddns_hostname.ttl`` here is still NULL. A widening that
    also started pinning agreeing zones would detach every migrated
    name from its binding on the day it ran — silently, because
    resolution answers 60 either way until somebody edits the zone.
    """
    plan = plan_for(legacy_db, fernet_key)
    assert [h.ttl for h in plan.hostnames] == [None] * 11
    assert plan.backends[0].config["ttl"] == TTL
    assert not any("DISAGREE" in note for note in plan.notes)


def test_a_zone_whose_hostnames_disagree_about_ttl_is_carried_not_refused(
    tmp_path, fernet_key: str
) -> None:
    """The first of #83's two widenings, at the plan level.

    Refused before ``0004``, and correctly: there was no column to put
    a per-name TTL in, so the choice was between refusing and quietly
    republishing somebody's 300-second name at 60. ``0004`` added
    ``ddns_hostname.ttl``, so the choice is gone.

    Every name in the zone is pinned, not just the dissenting one —
    see ``_selected_bindings``' sibling argument in ``build_plan``: a
    zone whose names disagree has no zone TTL, and letting the majority
    inherit one would invent an answer that a later edit to the binding
    would apply to a subset of the names an operator thinks of as
    identical.
    """
    path = tmp_path / "ttl-clash.db"
    build_legacy_db(path, fernet_key=fernet_key)
    conn = sqlite3.connect(path)
    conn.execute(f"UPDATE hostnames SET ttl = {ODD_TTL} WHERE id = 1")
    conn.commit()
    conn.close()

    plan = plan_for(path, fernet_key)
    by_legacy_id = {h.legacy_id: h for h in plan.hostnames}
    assert by_legacy_id[1].ttl == ODD_TTL
    assert {h.ttl for h in plan.hostnames if h.legacy_id != 1} == {TTL}
    # The zone's value is the mode, so what gets created *next* in this
    # zone inherits the commonest answer rather than the loudest one.
    assert plan.backends[0].config["ttl"] == TTL
    assert any("DISAGREE" in note for note in plan.notes)


def test_the_zone_ttl_is_the_mode_and_ties_break_low(
    tmp_path, fernet_key: str
) -> None:
    """Two names at 300 against one at 60 puts 300 in the binding.

    Without this the mode is indistinguishable from "whatever the first
    row said", which is what the set-based version of this code did and
    which made the same source database import to two different configs
    depending on iteration order.
    """
    path = tmp_path / "ttl-mode.db"
    build_legacy_db(path, fernet_key=fernet_key)
    conn = sqlite3.connect(path)
    conn.execute(f"UPDATE hostnames SET ttl = {ODD_TTL}")
    conn.execute("UPDATE hostnames SET ttl = 60 WHERE id = 1")
    conn.commit()
    conn.close()
    assert plan_for(path, fernet_key).backends[0].config["ttl"] == ODD_TTL

    # A dead heat: 60 and 300 with nothing between them. Lowest wins,
    # and the point is that it is *fixed*, not that it is 60.
    conn = sqlite3.connect(path)
    conn.execute("UPDATE hostnames SET ttl = 60 WHERE id <= 5")
    conn.execute(f"UPDATE hostnames SET ttl = {ODD_TTL} WHERE id > 5")
    conn.execute("DELETE FROM hostnames WHERE id = 11")
    conn.execute("DELETE FROM hostname_backends WHERE hostname_id = 11")
    conn.commit()
    conn.close()
    assert plan_for(path, fernet_key).backends[0].config["ttl"] == TTL


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
        # The one `hostname_backends` shape #83 did NOT widen, and the
        # reason it did not: `resolve_backends` filters a selection
        # against the zone's own bindings, so a row naming a backend
        # that is not on this hostname's zone resolves to the empty
        # set — "publish nowhere", which `0004` made unspellable on
        # purpose. A strict subset, by contrast, is now migrated; that
        # is `test_a_selective_binding_is_carried_across`.
        pytest.param(
            "INSERT INTO hostname_backends (hostname_id, domain_backend_id) "
            "VALUES (1, 99)",
            "not on that hostname's own domain",
            id="binding-names-a-backend-off-the-zone",
        ),
        pytest.param(
            "INSERT INTO hostname_backends (hostname_id, domain_backend_id) "
            "VALUES (99, 1)",
            "not on that hostname's own domain",
            id="binding-names-a-hostname-that-does-not-exist",
        ),
    ],
)
def test_a_world_the_mapping_cannot_represent_is_refused(
    tmp_path, fernet_key: str, sql: str, expected: str
) -> None:
    """Eleven mutations of a good world, each refused, each named.

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


def test_a_degenerate_binding_writes_no_selection_row(
    tmp_path, fernet_key: str
) -> None:
    """Eleven rows binding every hostname to the only backend is *not* a subset.

    The legacy model's ``get_backends()`` returns the domain's backends
    when the association is empty, so "all of them" and "none listed"
    mean the same thing — and ``models.resolve_backends`` spells it the
    same way, which is why this maps to **no** ``ddns_hostname_backend``
    row rather than to eleven. Plan §3.3.1 recorded the table empty on
    2026-08-15 and it holds eleven rows; this is the entire measured
    population, so it is also the branch a real re-import takes.
    """
    path = tmp_path / "fully-bound.db"
    build_legacy_db(path, fernet_key=fernet_key)
    plan = plan_for(path, fernet_key)
    assert len(plan.snapshot.hostname_backends) == 11
    assert [h.selected_legacy_backend_ids for h in plan.hostnames] == [()] * 11
    assert any("degenerate" in note for note in plan.notes)


def test_a_selective_binding_is_carried_across(
    tmp_path, fernet_key: str
) -> None:
    """The second of #83's two widenings, at the plan level.

    A strict subset was a refusal until ``0004`` added
    ``ddns_hostname_backend`` — before it, this schema hung backends
    off the zone and migrating a per-name choice would have widened
    those names to every backend of their zone without saying so. The
    refusal was right and it is now unnecessary.

    Note what the assertion is on: the *other ten* names keep an empty
    selection. A widening that wrote a row per legacy binding would
    also pass "the subset survived" while quietly freezing ten names
    that ``0004`` deliberately left tracking their zone.
    """
    path = tmp_path / "selective.db"
    build_legacy_db(path, fernet_key=fernet_key)
    widen(path, fernet_key=fernet_key)
    plan = plan_for(path, fernet_key)

    by_legacy_id = {h.legacy_id: h for h in plan.hostnames}
    assert by_legacy_id[WIDENED_HOSTNAME_ID].selected_legacy_backend_ids == (1,)
    assert all(
        h.selected_legacy_backend_ids == ()
        for h in plan.hostnames
        if h.legacy_id != WIDENED_HOSTNAME_ID
    )
    assert [b.backend_type for b in plan.backends] == [STUB_SERVICE, SECOND_SLOT]
    assert any("strict SUBSET" in note for note in plan.notes)


def test_the_publication_the_plan_predicts_is_the_legacy_one(
    tmp_path, fernet_key: str
) -> None:
    """``Plan.publication`` — the source half of #83's assertion.

    Derived from ``hostnames.ttl`` and ``hostname_backends`` through
    the legacy service's own two rules, and asserted here on its own
    before anything is written, so a later disagreement with the target
    is attributable to one side rather than to both moving together.
    """
    path = tmp_path / "publication.db"
    build_legacy_db(path, fernet_key=fernet_key)
    widen(path, fernet_key=fernet_key)
    plan = plan_for(path, fernet_key)

    pinned = next(
        h.name for h in plan.hostnames if h.legacy_id == WIDENED_HOSTNAME_ID
    )
    publication = plan.publication
    assert publication[pinned] == ((STUB_SERVICE, ODD_TTL),)
    others = {v for k, v in publication.items() if k != pinned}
    assert others == {((STUB_SERVICE, TTL), (SECOND_SLOT, TTL))}


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


@pytest.mark.functional
# `--create-owner` calls atrium's `assign_role`, and
# `app/auth/rbac.py:222` writes `INSERT IGNORE` — MySQL-only syntax in
# atrium's own code, not this repo's. So this test cannot run on SQLite
# without changing atrium, which is a decision about a different
# repository. The thing under test — that the legacy bcrypt hash is
# carried verbatim so the fleet keeps authenticating — is still
# asserted, in the MySQL lane.
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


# ===================================================================== #
# 7. #83 — the two states the importer stopped refusing, end to end
# ===================================================================== #
#
# Everything above this line is the world the cutover rehearsal used:
# one backend, every binding degenerate, TTL uniformly 60. On that
# population both of #83's new branches are dead code — measured, not
# assumed: `refactor-plan.md` §3.3.1 reads "ttl | uniformly 60" and
# "hostname_backends | 11 rows … degenerate", taken by #49 from a
# WAL-safe copy on 2026-08-15, so the two counts this issue was opened
# to discover are **zero and zero**.
#
# That is exactly why this section exists. An importer whose new
# branches have never seen a row is an artefact with no writer: the
# code is there, the refusal is gone, and nothing anywhere has proved
# that what replaced the refusal writes a row a router would
# recognise. There is no legacy database in this run to point it at —
# the deploy host is not touched — so the fixture is built instead, and
# driven all the way to `/nic/update`.


WIDE_EMAIL = f"legacy-owner-wide-{W}@example.invalid"
WIDE_LOCK_OWNER = "test_import_legacy.imported_wide"


async def _make_named_owner(email: str) -> int:
    async with fixture_writes(WIDE_LOCK_OWNER) as session:
        user = User(
            email=email,
            hashed_password=unusable_password_hash(),
            is_active=True,
            is_verified=True,
            full_name="Legacy owner (widened)",
            preferred_language="en",
        )
        session.add(user)
        await session.flush()
        return user.id


@pytest_asyncio.fixture(scope="module")
async def imported_wide(tmp_path_factory, fernet_key: str) -> Any:
    """Import a world that exercises **both** widened branches.

    Its own owner, its own zone and its own device usernames: the
    importer refuses to run twice against colliding rows, correctly, so
    sharing :func:`imported`'s world would measure that refusal instead
    of this.

    Both migrated backends are scripted ``result: good`` afterwards and
    outside the import, for the reason :func:`imported` gives — the
    legacy schema has no column for a scripted result, every legacy
    config row is an encrypted credential, and teaching the importer
    about a fixture would be teaching it to write a row production
    never has. The ``ttl`` the import wrote is left exactly as it is;
    it is what the wire test below reads back.
    """
    compat_stub.register_stub_providers(force=True)
    await purge_tenants([WIDE_EMAIL], owner=WIDE_LOCK_OWNER)
    owner_id = await _make_named_owner(WIDE_EMAIL)

    path = tmp_path_factory.mktemp("legacy-wide") / "dyndns.db"
    build_legacy_db(path, fernet_key=fernet_key, tag="-wide")
    widen(path, fernet_key=fernet_key)
    plan = plan_for(path, fernet_key)

    factory = get_session_factory()
    async with fixture_writes(WIDE_LOCK_OWNER) as session:
        written = await il.apply(
            session, plan, owner_email=WIDE_EMAIL, create_owner=False
        )
    async with factory() as session:
        reading = await il.verify(session, written.owner_id)

    async with factory() as session:
        rows = (
            (
                await session.execute(
                    sa.select(DomainBackend).where(
                        DomainBackend.user_id == owner_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.config = {**(row.config or {}), "result": "good"}
        await session.commit()

    yield {
        "owner_id": owner_id,
        "plan": plan,
        "written": written,
        "reading": reading,
        "source": path,
    }

    await purge_tenants([WIDE_EMAIL], owner=WIDE_LOCK_OWNER)


def test_the_widened_world_writes_the_rows_it_claims_to(imported_wide) -> None:
    """One pinned TTL per name, one selection row, and both instruments agree.

    ``ttl_overrides == 11`` rather than ``1`` is the deliberate part: a
    zone whose names disagree has no zone TTL, so every name in it is
    pinned rather than only the dissenter. See ``build_plan``.
    """
    plan, written, reading = (
        imported_wide["plan"],
        imported_wide["written"],
        imported_wide["reading"],
    )
    assert il.compare(plan, written, reading) == []
    assert (written.ttl_overrides, written.selections) == (11, 1)
    assert (written.hostnames, reading.hostnames) == (11, 11)
    assert (written.backends, reading.backends) == (2, 2)


async def test_the_columns_0004_added_actually_hold_the_migrated_values(
    imported_wide,
) -> None:
    """Read straight out of MySQL, not through :class:`il.Reading`.

    A third instrument, and a deliberately dumb one: two ``SELECT``s
    over the two things ``0004`` created, compared against the SQLite
    source. :func:`il.verify` resolves; this counts.
    """
    conn = sqlite3.connect(imported_wide["source"])
    source_ttl = dict(conn.execute("SELECT name, ttl FROM hostnames"))
    conn.close()

    factory = get_session_factory()
    async with factory() as session:
        stored = dict(
            (
                await session.execute(
                    sa.select(Hostname.name, Hostname.ttl)
                    .join(Domain, Domain.id == Hostname.domain_id)
                    .where(Domain.user_id == imported_wide["owner_id"])
                )
            ).all()
        )
        selections = (
            await session.execute(
                sa.select(Hostname.name, DomainBackend.backend_type)
                .join(HostnameBackend, HostnameBackend.hostname_id == Hostname.id)
                .join(
                    DomainBackend,
                    DomainBackend.id == HostnameBackend.backend_id,
                )
                .join(Domain, Domain.id == Hostname.domain_id)
                .where(Domain.user_id == imported_wide["owner_id"])
            )
        ).all()

    assert stored == source_ttl
    assert sorted(stored.values()) == [TTL] * 10 + [ODD_TTL]
    pinned = f"{PRODUCTION_COST_DEVICE}-0.{zone_for('-wide')}"
    assert [tuple(row) for row in selections] == [(pinned, STUB_SERVICE)]


def test_the_publication_comparison_is_not_vacuous(imported_wide) -> None:
    """Break each half of #83's assertion and watch it bite.

    :func:`il.compare` returning ``[]`` above is only evidence if it can
    return something else about *these* two properties.

    Four mutations: the TTL alone, the backend set alone, a name the
    target does not hold, and a name it holds that the plan does not.
    """
    plan, written, reading = (
        imported_wide["plan"],
        imported_wide["written"],
        imported_wide["reading"],
    )
    pinned = f"{PRODUCTION_COST_DEVICE}-0.{zone_for('-wide')}"
    published = dict(reading.hostname_publication)
    assert published[pinned] == ((STUB_SERVICE, ODD_TTL),)

    wrong_ttl = {**published, pinned: ((STUB_SERVICE, TTL),)}
    problems = il.compare(
        plan, written, replace(reading, hostname_publication=wrong_ttl)
    )
    assert problems and any(f"{STUB_SERVICE}@{TTL}" in p for p in problems)

    widened = {
        **published,
        pinned: ((STUB_SERVICE, ODD_TTL), (SECOND_SLOT, ODD_TTL)),
    }
    assert il.compare(
        plan, written, replace(reading, hostname_publication=widened)
    )

    missing = {k: v for k, v in published.items() if k != pinned}
    problems = il.compare(
        plan, written, replace(reading, hostname_publication=missing)
    )
    assert problems and any("NOTHING" in p for p in problems)

    extra = {**published, "stray." + zone_for("-wide"): ()}
    problems = il.compare(
        plan, written, replace(reading, hostname_publication=extra)
    )
    assert problems and any("not in the plan" in p for p in problems)


async def test_a_pre_83_write_of_the_same_plan_is_caught(
    tmp_path, fernet_key: str
) -> None:
    """Mutate the implementation, and show the assertion fails.

    The mutation is the *old* importer. #83's write-side diff is
    exactly "put ``ddns_hostname.ttl`` and ``ddns_hostname_backend``
    rows in", so stripping both from the plan makes :func:`il.apply`
    write precisely the rows the pre-#83 code would have written had it
    not refused outright. Everything else — counts, hostname → device,
    hashes, credentials — is unchanged and still agrees, which is the
    point: those are the instruments that were already here, and
    **none of them notices**.

    The comparison is then made against the *true* plan, so what is
    asserted is that the new check, and only the new check, sees a
    world in which one name publishes to two providers at 60 where it
    used to publish to one at 300.
    """
    email = f"pre83-{W}@example.invalid"
    path = tmp_path / "pre83.db"
    build_legacy_db(path, fernet_key=fernet_key, tag="-pre83")
    widen(path, fernet_key=fernet_key)
    truth = plan_for(path, fernet_key)
    mutated = replace(
        truth,
        hostnames=tuple(
            replace(h, ttl=None, selected_legacy_backend_ids=())
            for h in truth.hostnames
        ),
    )

    await purge_tenants([email], owner=WIDE_LOCK_OWNER)
    owner_id = await _make_named_owner(email)
    factory = get_session_factory()
    try:
        async with fixture_writes(WIDE_LOCK_OWNER) as session:
            written = await il.apply(
                session, mutated, owner_email=email, create_owner=False
            )
        async with factory() as session:
            reading = await il.verify(session, owner_id)

        assert (written.ttl_overrides, written.selections) == (0, 0)

        problems = il.compare(truth, written, reading)
        assert problems, "the publication check did not bite"
        # **Only** the new check bites. Every problem reported is a
        # publication problem, so counts, hostname -> device, password
        # hashes, credential digests and last_ip_* — the whole
        # pre-#83 instrument set — are silent about a name that has
        # just been moved from one provider at 300 to two at 60.
        assert all("would publish through" in p for p in problems)
        assert any(f"{SECOND_SLOT}@{TTL}" in p for p in problems)
        assert any(f"{STUB_SERVICE}@{ODD_TTL}" in p for p in problems)

        # Said the other way round, without circularity: satisfy the
        # new check by construction and the rest of `compare` reports a
        # clean migration of this same defective world.
        assert (
            il.compare(
                truth,
                written,
                replace(reading, hostname_publication=truth.publication),
            )
            == []
        )
    finally:
        await purge_tenants([email], owner=WIDE_LOCK_OWNER)


async def test_the_narrowed_name_publishes_to_one_provider_at_its_own_ttl(
    client, imported_wide
) -> None:
    """The wire — the half a column read cannot satisfy.

    ``il.verify`` resolves through ``models.resolve_backends`` and
    ``models.resolve_ttl``, which is a strong reading but still this
    module's own. This is ``/nic/update`` doing it: the migrated device
    presenting its original password, and the provider call log saying
    which backend was called and at what TTL.

    Two names, because one proves nothing on its own. The pinned name
    must reach ``stub1`` alone at 300; its neighbour in the same zone,
    which selected nothing, must reach both at 60. A selection written
    for every legacy binding rather than only for the strict subset
    would pass the first assertion and fail the second.
    """
    zone = zone_for("-wide")
    username = device_username(PRODUCTION_COST_DEVICE, "-wide")
    password = PASSWORDS[PRODUCTION_COST_DEVICE]

    response = await client.get(
        "/nic/update",
        params={
            "hostname": f"{PRODUCTION_COST_DEVICE}-0.{zone}",
            "myip": "198.51.100.31",
        },
        headers=basic(username, password),
    )
    assert response.text == "good 198.51.100.31"
    assert [(c.service, c.ttl) for c in CALLS if c.op == "create"] == [
        (STUB_SERVICE, ODD_TTL)
    ]

    CALLS.clear()
    response = await client.get(
        "/nic/update",
        params={
            "hostname": f"{PRODUCTION_COST_DEVICE}-1.{zone}",
            "myip": "198.51.100.32",
        },
        headers=basic(username, password),
    )
    assert response.text == "good 198.51.100.32"
    assert [(c.service, c.ttl) for c in CALLS if c.op == "create"] == [
        (STUB_SERVICE, TTL),
        (SECOND_SLOT, TTL),
    ]
