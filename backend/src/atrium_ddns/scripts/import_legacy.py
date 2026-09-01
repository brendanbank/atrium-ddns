"""Import the legacy ``dyndns-route53`` SQLite database into this schema.

    python -m atrium_ddns.scripts.import_legacy \
        --source /tmp/dyndns-live-copy.db \
        --owner-email you@example.com \
        --dry-run

    LEGACY_FERNET_KEY=... python -m atrium_ddns.scripts.import_legacy \
        --source /tmp/dyndns-live-copy.db --owner-email you@example.com

The mapping is plan §3.3.1's, and the one fact it turns on is that the
**six non-admin legacy rows are devices, not people**. The legacy
service had no device concept, so it used its ``users`` table for one:
each of those rows is a router with a username and a password that
updates DNS. So one legacy ``admin`` row becomes one atrium user, the
six non-admin rows become six :class:`~atrium_ddns.models.Device` rows
owned by it, and every hostname follows the row that already owned it.

===========================  =================================================
legacy                       becomes
===========================  =================================================
``users`` where role=admin   the owner — an existing atrium ``User``, adopted
                             by ``--owner-email``, or created by
                             ``--create-owner`` carrying the legacy bcrypt
                             hash verbatim
``users`` where role<>admin  one ``ddns_device`` each, ``password_hash``
                             copied byte-for-byte
``domains``                  ``ddns_domain``, owned by the owner
``domain_backends`` +        one ``ddns_domain_backend``: the two
``backend_configs``          Fernet-encrypted config rows are decrypted under
                             the legacy key and re-encrypted per-user through
                             ``SecretBlob`` + ``UserSecret``
``hostnames``                ``ddns_hostname`` on the device that owned it,
                             carrying ``last_ip_v4`` / ``last_ip_v6`` /
                             ``last_updated_at``
===========================  =================================================

Six properties, each of which exists because getting it wrong is
silent
-------------------------------------------------------------------

**The bcrypt hashes survive byte-for-byte.** They are the credential
every router in the field is configured with, and
:mod:`atrium_ddns.auth_device` verifies both shapes and re-hashes to
argon2id opportunistically. Re-hashing here is impossible (there is no
plaintext) and *replacing* them would answer ``badauth`` to the whole
fleet at cutover — a wire response indistinguishable from a wrong
password, which is why it would read as "the migration lost the
credentials" rather than as a code defect. Every stored hash is
therefore checked against ``auth_device``'s own hasher tuple *before*
anything is written, so a shape that tuple cannot identify is a refusal
rather than a row.

**The Fernet key is proved before any write.** Every
``backend_configs.config_value`` is decrypted up front. A missing key,
a wrong key, a corrupt token or an empty plaintext stops the run with
nothing written. Writing a credential that decrypts to ``""`` is the
failure that survives review: the row looks migrated, the UI shows a
configured backend, and the first update answers ``911``.

**``backend_type`` is resolved through the provider registry, never by
string match.** The stored spelling is ``aws``; #15 registered
``route53`` canonical with an ``aws`` alias. A name the registry does
not claim is a refusal, because the alternative is a migrated backend
that resolves to nothing and answers a perfectly well-formed ``911``.
The **canonical** name is what gets stored.

**One transaction.** A half-migrated database is worse than none: the
fleet would be split between two services with no way to tell which
router is on which.

**It refuses to run twice rather than being idempotent** — see
:func:`assert_target_is_empty`. Idempotence here would mean reconciling
a device password an operator may have rotated in the new UI and a
provider credential they may have replaced, against a legacy database
that still holds the old ones; "merge" would silently revert both. A
second run is a mistake, and the useful behaviour for a mistake is a
refusal that names the rows already present. Rehearse with
``--dry-run``, or against a fresh database.

**Two instruments on every count.** :func:`read_snapshot` counts the
source; :func:`verify` re-reads the *target* through the ORM in a fresh
session after the commit, and the two are printed side by side. The
hostname → device mapping is compared as a whole set, not sampled, and
the provider credential is compared by SHA-256 of its canonical JSON —
which never prints the value and still fails if a single byte moved.
Since #83 the pair also compares **where each name would publish and
at what TTL**: :attr:`Plan.publication` derives it from the legacy
columns through the legacy service's own rules, and :func:`verify`
derives it from the target through
:func:`atrium_ddns.models.resolve_backends` and
:func:`~atrium_ddns.models.resolve_ttl` — the functions ``/nic/update``
itself calls. A column copied correctly into a row that resolves
differently is the failure that reading columns cannot see.

**The legacy TTL and the per-name backend choice both survive** —
since #83, and only since ``0004_hostname_backends_and_ttl``
-------------------------------------------------------------------

Both of these were refusals before, and both were the *right* refusal
at the time: under ``0002`` this schema hung TTL and backend choice off
the zone, so migrating a per-name value would have changed its meaning
without saying so. ``0004`` added ``ddns_hostname.ttl`` and
``ddns_hostname_backend``, which made them expressible, and #83 turned
each refusal into a mapping.

* **``hostnames.ttl``.** A zone whose hostnames **agree** behaves
  exactly as it did before: the one value goes into
  ``ddns_domain_backend.config['ttl']`` and every
  ``ddns_hostname.ttl`` stays NULL, so the names keep tracking their
  binding. Production is uniformly 60, which is also ``DEFAULT_TTL`` —
  but it is written explicitly rather than left to a default, so a
  later change to the default cannot silently retune migrated zones. A
  zone whose hostnames **disagree** pins every name in it on
  ``ddns_hostname.ttl`` and puts the zone's most common value in the
  binding for whatever is created next.
* **``hostname_backends``.** Plan §3.3.1 recorded it empty on
  2026-08-15; it is not — it holds one row per hostname, every one of
  them naming the zone's only backend. That is the degenerate case,
  and **both** schemas spell it as an *absent* selection (legacy
  ``Hostname.get_backends()``,
  :func:`atrium_ddns.models.resolve_backends`), so it still writes no
  row and the name still tracks its zone. A *strict subset* now
  becomes ``ddns_hostname_backend`` rows.
  The one shape still refused is a binding naming a backend that is
  not on the hostname's own zone: ``resolve_backends`` filters a
  selection against ``domain.backends``, so such a row would resolve
  to nothing and leave the name publishing nowhere.

  The cost, stated because it is real: a degenerate legacy binding is
  not carried across as an explicit one, so a name that legacy had
  pinned-to-everything will pick up a backend added to its zone later,
  where legacy would not have. Nothing in the measured population can
  tell the two readings apart, and ``0004``'s and the frozen table's
  reading is the one that keeps the zone live.

**Whether either branch has ever run against real data: no.** Measured
on the 2026-08-15 WAL-safe copy (``refactor-plan.md`` §3.3.1) both
counts are **zero** — TTL uniformly 60, every binding degenerate — and
this run had no access to a legacy database to re-take that reading.
Both branches are exercised by ``backend/tests/test_import_legacy.py``
against synthetic sources built for them, end to end through
:func:`apply` and :func:`verify`, and the counts are printed by every
run so a re-import that does reach them says so out loud.

What this deliberately does not carry
-------------------------------------

* **``rate_limit_configs``.** The global row becomes the namespace
  default, which #17 already registers. A per-user override on a row
  that becomes a *device* would change behaviour silently, so it is a
  refusal; one on the admin row has no device to land on and is
  reported and dropped.
* **``users.totp_secret``.** Atrium owns TOTP and its own enrolment;
  the legacy secret is Fernet-encrypted under a key atrium does not
  have. Reported loudly so the operator re-enrols rather than
  discovering it at the first login.
* **``events`` / ``health_checks``.** A different database
  (``events.db``), pruned to 24 hours, and not the migration's subject.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import sqlalchemy as sa

#: Environment variable holding the legacy ``FERNET_KEY``. A different
#: name from the legacy service's own, deliberately: this process may
#: run inside a container that also has atrium's settings in its
#: environment, and two different key materials under one name is how a
#: run decrypts with the wrong one and reports success.
FERNET_KEY_ENV = "LEGACY_FERNET_KEY"

#: Every column this importer reads, per table. Asserted as a
#: **subset** of what the source actually has, before a single row is
#: read.
#:
#: The stale ``~/dyndns.db`` snapshot is two schema migrations behind
#: live — its ``users`` has no ``web_login`` and its ``hostnames`` no
#: ``ttl`` / ``last_ip_v4`` / ``last_ip_v6`` / ``last_updated_at``. The
#: legacy app adds those with one-time ``ALTER TABLE`` at boot, so a
#: snapshot taken before a deploy is silently a *different schema*
#: holding plausible rows, which is a worse failure than a missing
#: file. Naming the columns here is what turns it into a refusal.
REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "users": frozenset(
        {
            "id",
            "username",
            "password_hash",
            "role",
            "is_active",
            "web_login",
            # Read only to say *which* rows have one, never for the
            # value: atrium owns TOTP and the legacy secret is
            # encrypted under a key it does not have.
            "totp_secret",
        }
    ),
    "domains": frozenset({"id", "name"}),
    "domain_backends": frozenset({"id", "domain_id", "backend_type"}),
    "backend_configs": frozenset(
        {"id", "domain_backend_id", "config_key", "config_value"}
    ),
    "hostnames": frozenset(
        {
            "id",
            "name",
            "domain_id",
            "user_id",
            "ttl",
            "last_ip_v4",
            "last_ip_v6",
            "last_updated_at",
        }
    ),
    "hostname_backends": frozenset({"hostname_id", "domain_backend_id"}),
    "rate_limit_configs": frozenset(
        {"id", "user_id", "requests_per_minute", "requests_per_hour", "is_global"}
    ),
}

#: The legacy role that marks the one real person.
ADMIN_ROLE = "admin"

#: ``ddns_device.password_hash`` is ``String(255)``. A stored hash
#: longer than that would be silently truncated by MySQL in
#: non-strict mode — and a truncated bcrypt hash verifies nothing and
#: raises nothing, which is the same ``badauth`` the whole file is
#: written to avoid.
PASSWORD_HASH_MAX = 255

#: Role granted to an owner created by ``--create-owner``. The legacy
#: row is an admin, and ``models.PERMISSION_GRANTS`` gives ``admin``
#: the two cross-tenant ddns codes on top of the three ``.manage``
#: ones. An adopted owner's roles are left exactly as atrium has them.
OWNER_ROLE = "admin"


class MigrationRefused(RuntimeError):
    """Stop, with nothing written and a reason a human can act on."""


def normalise_dns_name(name: str) -> str:
    """Lower-case, strip a trailing dot — the shape this schema stores.

    ``Domain.name`` and ``Hostname.name`` both say so in as many words,
    and their ``UNIQUE`` indexes are the only thing stopping
    ``Example.com`` and ``example.com`` from becoming two zones. The
    legacy schema has the same ``UNIQUE`` and no such rule, so the
    normalisation happens here rather than being assumed.

    ``/nic/update?hostname=…`` lower-cases before it looks a name up
    (``router_nic``), so a migrated row that kept an upper-case letter
    would be **unreachable** — a router answered ``nohost`` for a name
    that is visibly present in the database.
    """
    return name.strip().rstrip(".").lower()


# --------------------------------------------------------------------- #
# The source, read-only
# --------------------------------------------------------------------- #


@contextlib.contextmanager
def open_source(path: Path) -> Iterator[sqlite3.Connection]:
    """Read a **copy** of the legacy database, creating nothing beside it.

    Two refusals first, both about pointing this at the wrong file:

    * a path that does not exist, named;
    * a path with a ``-wal`` sibling. SQLite in WAL mode keeps recent
      commits in the write-ahead log, so the main file alone is stale —
      on production it was 65 536 bytes of database beside 4 MB of WAL.
      A ``-wal`` sibling therefore means one of two things and both are
      wrong: this is the live database (which must never be opened by
      this process at all), or it is a naive ``cp`` of one, which is a
      copy of a *prefix* of the data. ``scripts/backup.sh`` in the
      legacy repo does ``sqlite3 … ".backup"``, whose output has no WAL,
      and that is the input this expects.

    **Then the bytes are copied into a private directory and SQLite is
    pointed at the copy, never at the caller's path.** ``mode=ro`` is
    not enough on its own and finding out why is the reason this
    function is shaped like this: a ``.backup`` output inherits
    ``journal_mode=wal`` in its header, and opening a WAL database
    *even read-only* makes SQLite create a zero-length ``-wal`` and a
    32 KB ``-shm`` next to it, because read-only WAL access needs the
    shared-memory file. Measured here — the first ``--dry-run`` against
    a fresh copy left ``…-wal`` and ``…-shm`` beside it, and the second
    run refused its own artefact. Aimed at a production volume that is
    a write into the live service's data directory, which is precisely
    what this importer promises not to do.

    ``immutable=1`` would also stop the creation, and is rejected: it
    tells SQLite the file cannot change and to **ignore the WAL
    entirely**, so pointing it at a live database would read the stale
    main file and report a plausible, wrong population instead of
    refusing. A guard that turns a loud failure into a quiet one is
    worse than none.

    ``PRAGMA integrity_check`` runs on the copy — a second instrument
    on the transfer, distinct from any digest the operator compared.
    """
    if not path.is_file():
        raise MigrationRefused(
            f"source database not found: {path}\n"
            "  Take a WAL-safe copy first — the legacy repo's "
            "scripts/backup.sh does\n"
            '  `sqlite3 <db> ".backup <out>"` inside the running '
            "container, which is\n"
            "  consistent while the service is serving."
        )
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        raise MigrationRefused(
            f"{path} has a write-ahead log beside it ({wal.name}).\n"
            "  That means it is either the LIVE database or a plain `cp` of "
            "one. Both are\n"
            "  refused: the live file is never opened by this process, and a "
            "`cp` copies\n"
            "  the main file without the commits still sitting in the WAL — "
            "silently a\n"
            "  different, older database. Use `sqlite3 <db> \".backup <out>\"`."
        )

    with tempfile.TemporaryDirectory(prefix="import-legacy-") as scratch:
        private = Path(scratch) / "source.db"
        shutil.copyfile(path, private)
        conn = sqlite3.connect(f"file:{private}?mode=ro", uri=True)
        try:
            verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if verdict != "ok":
                raise MigrationRefused(
                    f"{path} fails SQLite's integrity check: {verdict}. "
                    "Take the copy again."
                )
            yield conn
        finally:
            conn.close()


def assert_columns(conn: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    """Prove the source has the schema this importer was written for.

    Called **before any row is read**, and that ordering is the point.
    A snapshot missing ``web_login`` still answers ``SELECT id, username
    FROM users`` perfectly happily; the run only goes wrong later, in a
    way that looks like a data problem.

    Returns the observed columns per table so the caller can print them
    — a refusal that says "missing ``ttl``" is actionable and one that
    says "schema mismatch" is not.
    """
    observed: dict[str, tuple[str, ...]] = {}
    missing_tables: list[str] = []
    missing_columns: dict[str, list[str]] = {}

    present = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for table, required in REQUIRED_COLUMNS.items():
        if table not in present:
            missing_tables.append(table)
            continue
        cols = tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))
        observed[table] = cols
        absent = sorted(required - set(cols))
        if absent:
            missing_columns[table] = absent

    if missing_tables or missing_columns:
        lines = ["the source database is not the schema this importer reads."]
        for table in sorted(missing_tables):
            lines.append(f"  table {table!r}: ABSENT")
        for table, absent in sorted(missing_columns.items()):
            lines.append(
                f"  table {table!r}: missing {', '.join(absent)} "
                f"(has {', '.join(observed[table])})"
            )
        lines.append(
            "  The legacy app adds `users.web_login` and the four "
            "`hostnames` columns with"
        )
        lines.append(
            "  one-time ALTER TABLE at boot, so a snapshot taken before a "
            "deploy is a"
        )
        lines.append(
            "  DIFFERENT SCHEMA holding plausible rows. Copy the LIVE "
            "database instead."
        )
        raise MigrationRefused("\n".join(lines))
    return observed


# --------------------------------------------------------------------- #
# What was read
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class LegacyUser:
    id: int
    username: str
    password_hash: str
    role: str
    is_active: bool
    web_login: bool

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN_ROLE


@dataclass(frozen=True)
class LegacyHostname:
    id: int
    name: str
    domain_id: int
    user_id: int
    ttl: int
    last_ip_v4: str | None
    last_ip_v6: str | None
    last_updated_at: datetime | None


@dataclass(frozen=True)
class LegacyBackend:
    id: int
    domain_id: int
    backend_type: str
    #: ``config_key`` -> Fernet token, exactly as stored.
    ciphertext: Mapping[str, str]


@dataclass(frozen=True)
class LegacyRateLimit:
    id: int
    user_id: int | None
    requests_per_minute: int
    requests_per_hour: int
    is_global: bool


@dataclass(frozen=True)
class Snapshot:
    """Everything read from the source, and the counts as read.

    The counts are a *separate* field rather than ``len(...)`` of the
    lists on demand: they are the source-side instrument, taken with
    ``SELECT COUNT(*)`` over the table rather than by measuring the
    objects this module built from it. Two readings of the same table
    by two mechanisms, so a row dropped in the mapping loop is visible.
    """

    users: tuple[LegacyUser, ...]
    domains: Mapping[int, str]
    backends: tuple[LegacyBackend, ...]
    hostnames: tuple[LegacyHostname, ...]
    rate_limits: tuple[LegacyRateLimit, ...]
    hostname_backends: tuple[tuple[int, int], ...]
    counts: Mapping[str, int]
    #: *Which* rows hold a legacy TOTP secret — never the secret. It is
    #: not a field on :class:`LegacyUser` because a secret carried on a
    #: dataclass gets printed by somebody's ``repr`` eventually, and
    #: nothing here needs the value: atrium owns TOTP enrolment and the
    #: only useful output is "re-enrol".
    totp_user_ids: frozenset[int] = frozenset()

    @property
    def admins(self) -> tuple[LegacyUser, ...]:
        return tuple(u for u in self.users if u.is_admin)

    @property
    def device_rows(self) -> tuple[LegacyUser, ...]:
        return tuple(u for u in self.users if not u.is_admin)


def _parse_dt(value: Any) -> datetime | None:
    """SQLite's ``DATETIME`` text -> naive ``datetime``, or ``None``.

    Refuses rather than dropping. A timestamp this cannot parse means
    the source wrote a shape this was not written for, and silently
    storing ``NULL`` for it turns "last updated three minutes ago" into
    "never called", which is precisely the ``n/a`` is not ``0`` failure
    the status board is built to avoid.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError as exc:
        raise MigrationRefused(
            f"unparseable timestamp in the source: {value!r} ({exc}). "
            "Storing NULL for it would turn a device that called in "
            "minutes ago into one that has never called."
        ) from None


def read_snapshot(conn: sqlite3.Connection) -> Snapshot:
    """Read every row this importer maps, plus the source-side counts."""
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in REQUIRED_COLUMNS
    }

    users = tuple(
        LegacyUser(
            id=row[0],
            username=row[1],
            password_hash=row[2],
            role=row[3],
            is_active=bool(row[4]),
            web_login=bool(row[5]),
        )
        for row in conn.execute(
            "SELECT id, username, password_hash, role, is_active, web_login "
            "FROM users ORDER BY id"
        )
    )

    domains = {
        row[0]: row[1] for row in conn.execute("SELECT id, name FROM domains")
    }

    ciphertext: dict[int, dict[str, str]] = {}
    for backend_id, key, value in conn.execute(
        "SELECT domain_backend_id, config_key, config_value FROM backend_configs "
        "ORDER BY domain_backend_id, config_key"
    ):
        ciphertext.setdefault(backend_id, {})[key] = value

    backends = tuple(
        LegacyBackend(
            id=row[0],
            domain_id=row[1],
            backend_type=row[2],
            ciphertext=dict(ciphertext.get(row[0], {})),
        )
        for row in conn.execute(
            "SELECT id, domain_id, backend_type FROM domain_backends ORDER BY id"
        )
    )

    hostnames = tuple(
        LegacyHostname(
            id=row[0],
            name=row[1],
            domain_id=row[2],
            user_id=row[3],
            ttl=int(row[4]),
            last_ip_v4=row[5],
            last_ip_v6=row[6],
            last_updated_at=_parse_dt(row[7]),
        )
        for row in conn.execute(
            "SELECT id, name, domain_id, user_id, ttl, last_ip_v4, "
            "last_ip_v6, last_updated_at FROM hostnames ORDER BY id"
        )
    )

    rate_limits = tuple(
        LegacyRateLimit(
            id=row[0],
            user_id=row[1],
            requests_per_minute=int(row[2]),
            requests_per_hour=int(row[3]),
            is_global=bool(row[4]),
        )
        for row in conn.execute(
            "SELECT id, user_id, requests_per_minute, requests_per_hour, "
            "is_global FROM rate_limit_configs ORDER BY id"
        )
    )

    hostname_backends = tuple(
        (row[0], row[1])
        for row in conn.execute(
            "SELECT hostname_id, domain_backend_id FROM hostname_backends "
            "ORDER BY hostname_id, domain_backend_id"
        )
    )

    totp_user_ids = frozenset(
        row[0]
        for row in conn.execute("SELECT id FROM users WHERE totp_secret IS NOT NULL")
    )

    return Snapshot(
        users=users,
        domains=domains,
        backends=backends,
        hostnames=hostnames,
        rate_limits=rate_limits,
        hostname_backends=hostname_backends,
        counts=counts,
        totp_user_ids=totp_user_ids,
    )


# --------------------------------------------------------------------- #
# The plan — every refusal lives here, before anything is written
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlannedDevice:
    legacy_user_id: int
    username: str
    password_hash: str
    name: str


@dataclass(frozen=True)
class PlannedBackend:
    legacy_backend_id: int
    legacy_domain_id: int
    #: Canonical service name from the registry, not the stored alias.
    backend_type: str
    #: Stored spelling, kept so the report can show the resolution.
    stored_as: str
    credentials: Mapping[str, str]
    config: Mapping[str, Any]


@dataclass(frozen=True)
class PlannedHostname:
    legacy_id: int
    name: str
    legacy_domain_id: int
    legacy_user_id: int
    last_ip_v4: str | None
    last_ip_v6: str | None
    last_updated_at: datetime | None
    #: What the legacy row published at. Always an int — the legacy
    #: column is NOT NULL — and kept separately from :attr:`ttl`
    #: because it is the *expectation* the second instrument compares
    #: against, not a thing that is written anywhere.
    legacy_ttl: int = 0
    #: ``ddns_hostname.ttl``, or ``None`` to inherit the binding's
    #: ``config['ttl']``. Set only for a zone whose hostnames disagree
    #: (see :func:`build_plan`); NULL everywhere else, so a zone that
    #: agreed keeps tracking its binding exactly as it did before #83.
    ttl: int | None = None
    #: Legacy ``domain_backends.id`` values this name is pinned to.
    #: Empty means *inherit every backend on the zone*, which is what
    #: both schemas mean by an absent selection — so a degenerate
    #: legacy binding lands here as empty, not as "all of them".
    selected_legacy_backend_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Plan:
    owner: LegacyUser
    devices: tuple[PlannedDevice, ...]
    domains: Mapping[int, str]
    backends: tuple[PlannedBackend, ...]
    hostnames: tuple[PlannedHostname, ...]
    snapshot: Snapshot
    notes: tuple[str, ...] = field(default=())

    @property
    def hostname_to_device(self) -> dict[str, str]:
        """``hostname -> device username``, the whole mapping.

        This is the thing the acceptance criterion asserts in full
        rather than sampling, so it is derived once here and compared
        against the target's own version of it in :func:`verify`.
        """
        by_user = {d.legacy_user_id: d.username for d in self.devices}
        return {h.name: by_user[h.legacy_user_id] for h in self.hostnames}

    @property
    def publication(self) -> dict[str, tuple[tuple[str, int], ...]]:
        """``hostname -> ((backend type, TTL), …)`` **as legacy answered it**.

        The source-side half of #83's assertion, and deliberately not a
        restatement of what :func:`apply` is about to write. It is
        derived from the legacy columns only — ``hostnames.ttl`` and
        ``hostname_backends`` — through the legacy service's own two
        rules:

        * ``Hostname.get_backends()`` is *the rows if there are any,
          otherwise the domain's* — so an empty selection here means
          every backend on the zone, in the zone's order;
        * ``hostnames.ttl`` is per name and applies to every backend
          that name publishes through. The legacy schema has no
          per-backend TTL at all.

        :func:`verify` builds the same mapping out of the **target**,
        through :func:`atrium_ddns.models.resolve_backends` and
        :func:`~atrium_ddns.models.resolve_ttl` — the functions
        ``/nic/update`` itself calls — and :func:`compare` refuses when
        the two differ. So the claim being checked is not "the columns
        were copied" but *"this name would be published to the same
        places at the same TTL as before"*, which is the only form of
        it a router in the field can tell apart.

        Keyed on ``backend_type`` rather than on an id because
        ``UNIQUE(domain_id, backend_type)`` makes it unique within a
        zone and it survives the renumbering the import performs; the
        tuple is ordered, because the order decides the aggregate
        status ``/nic/update`` returns.
        """
        by_domain: dict[int, list[PlannedBackend]] = {}
        for backend in self.backends:
            by_domain.setdefault(backend.legacy_domain_id, []).append(backend)
        out: dict[str, tuple[tuple[str, int], ...]] = {}
        for hostname in self.hostnames:
            available = by_domain.get(hostname.legacy_domain_id) or []
            chosen = set(hostname.selected_legacy_backend_ids)
            used = [
                b
                for b in available
                if not chosen or b.legacy_backend_id in chosen
            ]
            out[hostname.name] = tuple(
                (b.backend_type, hostname.legacy_ttl) for b in used
            )
        return out


def _identifiable(password_hash: str) -> bool:
    """Would ``auth_device`` recognise this stored hash?

    Asked through :data:`atrium_ddns.auth_device._PASSWORD_HASH` itself
    rather than by prefix-matching ``$2b$`` here. A second spelling of
    "which hashes do we accept" is a second thing to keep in step, and
    the one that matters is the tuple the running service verifies
    against — ``PasswordHash.recommended()`` holds Argon2 alone and
    *raises* on bcrypt, so a copy of this rule written from memory is
    exactly how the fleet ends up answering ``badauth``.
    """
    from ..auth_device import _PASSWORD_HASH

    return any(hasher.identify(password_hash) for hasher in _PASSWORD_HASH.hashers)


def decrypt_credentials(
    backend: LegacyBackend, fernet_key: str | None
) -> dict[str, str]:
    """Fernet-decrypt one backend's config rows, or refuse.

    Every failure stops the run **before anything is written**, and
    each is spelled separately because they need different fixes:

    * no key at all — the operator has not passed it;
    * a key Fernet will not accept — wrong length or not urlsafe-b64;
    * a token that will not decrypt — right shape, wrong key, and the
      one that would otherwise be discovered at cutover;
    * a plaintext that is empty — a row that looks migrated, a backend
      the UI shows as configured, and ``911`` on the first update.

    The plaintext never reaches the log, the exception text or stdout.
    """
    from cryptography.fernet import Fernet, InvalidToken

    if not backend.ciphertext:
        raise MigrationRefused(
            f"legacy domain_backend {backend.id} ({backend.backend_type}) has "
            "no backend_configs rows. Migrating it would produce a backend "
            "with no credentials, which answers 911 on every update while "
            "looking configured."
        )
    if not fernet_key:
        raise MigrationRefused(
            f"{FERNET_KEY_ENV} is not set, and {len(backend.ciphertext)} "
            f"provider credential(s) on legacy domain_backend {backend.id} "
            "are Fernet-encrypted under it.\n"
            "  Nothing has been written. Read the key off the legacy "
            "service's environment\n"
            "  (it is the `FERNET_KEY` the old container runs with) and pass "
            "it in as\n"
            f"  {FERNET_KEY_ENV}. Writing an empty credential instead would "
            "produce rows\n"
            "  that look migrated and answer 911 forever."
        )
    try:
        fernet = Fernet(
            fernet_key.encode() if isinstance(fernet_key, str) else fernet_key
        )
    except (ValueError, TypeError) as exc:
        raise MigrationRefused(
            f"{FERNET_KEY_ENV} is not a valid Fernet key ({exc}). "
            "A Fernet key is 32 urlsafe-base64 bytes — 44 characters. "
            "Value withheld."
        ) from None

    out: dict[str, str] = {}
    for key, token in sorted(backend.ciphertext.items()):
        try:
            plaintext = fernet.decrypt(
                token.encode() if isinstance(token, str) else token
            ).decode()
        except InvalidToken:
            raise MigrationRefused(
                f"{FERNET_KEY_ENV} does not decrypt "
                f"{backend.backend_type}.{key} (legacy domain_backend "
                f"{backend.id}).\n"
                "  The token is well-formed, so this is the WRONG KEY rather "
                "than corrupt data.\n"
                "  Nothing has been written. A run that continued here would "
                "store an empty\n"
                "  credential that looks migrated — the failure that survives "
                "review."
            ) from None
        except (ValueError, TypeError) as exc:
            raise MigrationRefused(
                f"{backend.backend_type}.{key} (legacy domain_backend "
                f"{backend.id}) is not a Fernet token ({type(exc).__name__}). "
                "Ciphertext withheld."
            ) from None
        if not plaintext:
            raise MigrationRefused(
                f"{backend.backend_type}.{key} (legacy domain_backend "
                f"{backend.id}) decrypts to an EMPTY string. Migrating it "
                "would produce a backend the UI shows as configured and "
                "which answers 911 on every update."
            )
        out[key] = plaintext
    return out


def _resolve_backend_type(stored: str) -> str:
    """Stored spelling -> canonical service name, through the registry.

    ``aws`` is what the legacy database holds and ``route53`` is what
    #15 registered it as, with ``aws`` an alias. Resolving here rather
    than matching the string means a provider renamed upstream takes
    its alias with it, and a spelling nobody claims is a refusal
    instead of a row that resolves to nothing and answers ``911``.
    """
    from ..providers import known_services, provider_class, resolvable_services

    cls = provider_class(stored)
    if cls is None:
        raise MigrationRefused(
            f"no provider is registered for backend_type {stored!r}.\n"
            f"  resolvable here: {', '.join(resolvable_services())}\n"
            f"  canonical: {', '.join(known_services())}\n"
            "  Nothing has been written. A migrated backend whose service "
            "nobody claims\n"
            "  resolves to nothing and answers a perfectly well-formed 911."
        )
    return cls.SERVICE


def build_plan(snapshot: Snapshot, *, fernet_key: str | None) -> Plan:
    """Apply the mapping, refusing anything it cannot represent.

    Everything that could change meaning without saying so is a
    refusal here rather than a best guess downstream:

    * not exactly one ``admin`` row — the mapping has one owner;
    * an admin that owns hostnames, or a non-admin with ``web_login``
      — either one contradicts "the six non-admin rows are devices",
      and which way to resolve it is the operator's call, not this
      script's;
    * an inactive non-admin row. ``ddns_device`` has no active flag —
      :func:`atrium_ddns.auth_device.authenticate_device` checks the
      *owner's*, and the owner here is active by construction. A
      disabled legacy router would come back to life at cutover;
    * a stored hash ``auth_device`` cannot identify, or one longer than
      the column;
    * a hostname whose domain or owner does not exist;
    * a ``hostname_backends`` row naming a backend that is not on that
      hostname's own zone, or a hostname that does not exist;
    * a per-user rate limit on a row that becomes a device.

    **Two refusals were removed by #83, and neither was a bug.** Until
    ``0004_hostname_backends_and_ttl`` this schema could not hold a
    per-name TTL or a per-name backend selection, so both were refusals
    — the right failure, since the alternative was migrating a row
    whose meaning had quietly changed. ``0004`` made both
    representable, so they are now mappings; what remains a refusal is
    the one case ``0004`` did *not* make representable, above.
    """
    notes: list[str] = []

    admins = snapshot.admins
    if len(admins) != 1:
        raise MigrationRefused(
            f"expected exactly one legacy row with role={ADMIN_ROLE!r}; found "
            f"{len(admins)} ({', '.join(u.username for u in admins) or 'none'}). "
            "The mapping is one owner and N devices; which of several admins "
            "owns the migrated estate is not this script's decision."
        )
    owner = admins[0]

    hostnames_by_user: dict[int, list[LegacyHostname]] = {}
    for row in snapshot.hostnames:
        hostnames_by_user.setdefault(row.user_id, []).append(row)

    if hostnames_by_user.get(owner.id):
        raise MigrationRefused(
            f"the admin row {owner.username!r} owns "
            f"{len(hostnames_by_user[owner.id])} hostname(s). It becomes the "
            "atrium USER, and a user cannot own a hostname here — hostnames "
            "belong to devices. Give it a device in the legacy service first, "
            "or decide which device inherits them."
        )
    if not owner.web_login:
        raise MigrationRefused(
            f"the admin row {owner.username!r} has web_login=0, so it has "
            "never been able to log in. It is the row that becomes the one "
            "real atrium account; a row that cannot log in is a device, and "
            "the roles are the wrong way round."
        )

    device_rows = snapshot.device_rows
    if not device_rows:
        raise MigrationRefused(
            "no non-admin legacy rows: there is nothing to become a device, "
            "and every hostname would land unassigned."
        )

    per_user_limits = {
        rl.user_id: rl for rl in snapshot.rate_limits if not rl.is_global
    }
    globals_ = [rl for rl in snapshot.rate_limits if rl.is_global]
    if globals_:
        notes.append(
            f"legacy global rate limit {globals_[0].requests_per_minute}/min, "
            f"{globals_[0].requests_per_hour}/hour is NOT migrated — the "
            "per-minute limit is the `atrium_ddns` namespace default (#17) "
            "and the per-hour limit has no equivalent."
        )

    devices: list[PlannedDevice] = []
    for row in device_rows:
        if row.web_login:
            raise MigrationRefused(
                f"legacy row {row.username!r} is role={row.role!r} but has "
                "web_login=1. The six non-admin rows are devices (plan "
                "§3.3.1) and a device has no web login; this row is a person "
                "and the mapping does not cover it."
            )
        if not row.is_active:
            raise MigrationRefused(
                f"legacy row {row.username!r} is disabled (is_active=0). "
                "`ddns_device` has no active flag — auth checks the OWNER's, "
                "and the owner is active — so migrating it would bring a "
                "deliberately disabled router back to life at cutover."
            )
        if not _identifiable(row.password_hash):
            raise MigrationRefused(
                f"legacy row {row.username!r} holds a password hash that "
                "atrium_ddns.auth_device cannot identify (prefix "
                f"{row.password_hash[:7]!r}). Migrating it produces a device "
                "that answers badauth to its router forever, which on the "
                "wire is indistinguishable from a wrong password."
            )
        if len(row.password_hash) > PASSWORD_HASH_MAX:
            raise MigrationRefused(
                f"legacy row {row.username!r} holds a "
                f"{len(row.password_hash)}-character password hash; "
                f"ddns_device.password_hash is String({PASSWORD_HASH_MAX}). "
                "A truncated hash verifies nothing and raises nothing."
            )
        if row.id in per_user_limits:
            limit = per_user_limits[row.id]
            raise MigrationRefused(
                f"legacy row {row.username!r} has a per-user rate limit "
                f"({limit.requests_per_minute}/min, "
                f"{limit.requests_per_hour}/hour) and becomes a device. "
                "Migrating it silently would drop the override onto the "
                "namespace default; set ddns_device.rate_limit_per_minute by "
                "hand after the import, or clear the legacy override first."
            )
        devices.append(
            PlannedDevice(
                legacy_user_id=row.id,
                username=row.username,
                password_hash=row.password_hash,
                name=row.username,
            )
        )

    if owner.id in per_user_limits:
        limit = per_user_limits[owner.id]
        notes.append(
            f"the admin row's per-user rate limit "
            f"({limit.requests_per_minute}/min, {limit.requests_per_hour}/hour) "
            "is dropped: it becomes the atrium user, and rate limits are "
            "per device here. No device inherits it."
        )

    known_domains = set(snapshot.domains)
    device_user_ids = {d.legacy_user_id for d in devices}
    orphans = [
        h
        for h in snapshot.hostnames
        if h.domain_id not in known_domains or h.user_id not in device_user_ids
    ]
    if orphans:
        raise MigrationRefused(
            f"{len(orphans)} of {len(snapshot.hostnames)} hostname(s) "
            "reference a domain that does not exist, or an owner that does "
            "not become a device. Every hostname must land on the device "
            "that owned it, so there is no correct row to write for these. "
            f"Legacy hostname ids {[h.id for h in orphans]} (names withheld)."
        )

    ttl_by_domain: dict[int, list[int]] = {}
    for row in snapshot.hostnames:
        ttl_by_domain.setdefault(row.domain_id, []).append(row.ttl)
    zone_ttl = {d: _modal_ttl(t) for d, t in ttl_by_domain.items()}
    disagreeing = {d for d, t in ttl_by_domain.items() if len(set(t)) > 1}
    ttl_override: dict[int, int | None] = {}
    for row in snapshot.hostnames:
        # A zone that agrees writes NOTHING per name, exactly as it did
        # before #83: `ddns_hostname.ttl` stays NULL and the name keeps
        # tracking its binding's `config['ttl']`. That is the whole
        # measured production population (`refactor-plan.md` §3.3.1,
        # "ttl | uniformly 60"), so the rehearsed path is unchanged and
        # the branch below has never run against real data.
        ttl_override[row.id] = row.ttl if row.domain_id in disagreeing else None
    if disagreeing:
        notes.append(
            f"{len(disagreeing)} zone(s) hold hostnames that DISAGREE about "
            f"TTL, so {sum(1 for h in snapshot.hostnames if h.domain_id in disagreeing)} "
            "name(s) in them are pinned individually on ddns_hostname.ttl and "
            "the binding's config['ttl'] carries the zone's most common value "
            "(ties resolve to the lowest). Every name in such a zone is "
            "pinned, including the ones that happen to equal the zone value: "
            "where the source never said 'this zone has a TTL', letting some "
            "names inherit one would invent an answer that a later edit to "
            "the binding would silently apply to a subset."
        )

    backends_by_domain: dict[int, list[LegacyBackend]] = {}
    for backend in snapshot.backends:
        backends_by_domain.setdefault(backend.domain_id, []).append(backend)

    selection: dict[int, tuple[int, ...]] = {}
    if snapshot.hostname_backends:
        unrepresentable = _bindings_off_the_domain(snapshot, backends_by_domain)
        if unrepresentable:
            # NOT widened, and this is the one that stays a refusal.
            # A binding naming a backend that is not on the hostname's
            # own zone has no honest target row: `resolve_backends`
            # filters the selection against `domain.backends`, so
            # writing it would produce a row that resolves to nothing
            # and a name that publishes nowhere — "publish nowhere" by
            # accident, which is precisely the state `0004` made
            # unspellable on purpose.
            raise MigrationRefused(
                f"{len(unrepresentable)} hostname_backends row(s) bind a "
                "hostname to a backend that is not on that hostname's own "
                f"domain (legacy hostname ids {sorted(unrepresentable)}). "
                "resolve_backends() filters a selection against the zone's "
                "own bindings, so migrating these would leave those names "
                "publishing nowhere. Fix the source binding first."
            )
        selection = _selected_bindings(snapshot, backends_by_domain)
        pinned = {h: ids for h, ids in selection.items() if ids}
        notes.append(
            f"hostname_backends holds {len(snapshot.hostname_backends)} row(s); "
            f"{len(pinned)} hostname(s) select a strict SUBSET of their zone's "
            f"backends and become {sum(len(v) for v in pinned.values())} "
            "ddns_hostname_backend row(s). The rest bind a name to every "
            "backend its own domain has — the degenerate case, which both "
            "schemas spell as an ABSENT selection (legacy "
            "`Hostname.get_backends()`, `models.resolve_backends`), so no row "
            "is written for it and the name keeps tracking its zone. "
            "(Plan §3.3.1 recorded this table EMPTY on 2026-08-15; it is not, "
            "and on the measured population every row is degenerate.)"
        )

    planned_backends: list[PlannedBackend] = []
    for backend in snapshot.backends:
        canonical = _resolve_backend_type(backend.backend_type)
        credentials = decrypt_credentials(backend, fernet_key)
        config: dict[str, Any] = {}
        if backend.domain_id in zone_ttl:
            config["ttl"] = zone_ttl[backend.domain_id]
        planned_backends.append(
            PlannedBackend(
                legacy_backend_id=backend.id,
                legacy_domain_id=backend.domain_id,
                backend_type=canonical,
                stored_as=backend.backend_type,
                credentials=credentials,
                config=config,
            )
        )
        if canonical != backend.backend_type:
            notes.append(
                f"backend_type {backend.backend_type!r} resolved through the "
                f"provider registry to canonical {canonical!r}."
            )

    without_backends = sorted(set(snapshot.domains) - set(backends_by_domain))
    if without_backends:
        notes.append(
            f"{len(without_backends)} legacy domain(s) have no backend at all; "
            "they migrate as zones with nothing to write to, which answers "
            "911 — the same thing the legacy service does."
        )

    if owner.id in snapshot.totp_user_ids:
        notes.append(
            "the admin row has a legacy TOTP secret. It is NOT migrated: it "
            "is Fernet-encrypted under the legacy key and atrium owns its own "
            "enrolment, with its own table. Re-enrol in atrium before "
            "relying on two-factor on the new stack."
        )
    stray_totp = sorted(snapshot.totp_user_ids - {owner.id})
    if stray_totp:
        notes.append(
            f"{len(stray_totp)} non-admin row(s) hold a legacy TOTP secret "
            "even though they cannot log in. Not migrated; devices have no "
            "second factor."
        )

    planned_hostnames = tuple(
        PlannedHostname(
            legacy_id=h.id,
            name=normalise_dns_name(h.name),
            legacy_domain_id=h.domain_id,
            legacy_user_id=h.user_id,
            last_ip_v4=h.last_ip_v4,
            last_ip_v6=h.last_ip_v6,
            last_updated_at=h.last_updated_at,
            legacy_ttl=h.ttl,
            ttl=ttl_override[h.id],
            selected_legacy_backend_ids=selection.get(h.id, ()),
        )
        for h in snapshot.hostnames
    )
    planned_domains = {
        legacy_id: normalise_dns_name(name)
        for legacy_id, name in snapshot.domains.items()
    }

    renamed = sum(
        1
        for h, p in zip(snapshot.hostnames, planned_hostnames)
        if h.name != p.name
    ) + sum(
        1
        for legacy_id, name in snapshot.domains.items()
        if name != planned_domains[legacy_id]
    )
    if renamed:
        notes.append(
            f"{renamed} name(s) were normalised (lower-cased / trailing dot "
            "stripped) on the way in. `/nic/update` lower-cases before it "
            "looks a hostname up, so a row that kept an upper-case letter "
            "would answer nohost for a name plainly present in the database."
        )
    collapsed = len(planned_hostnames) - len({p.name for p in planned_hostnames})
    if collapsed:
        raise MigrationRefused(
            f"{collapsed} hostname(s) collide once normalised — the legacy "
            "UNIQUE index is case-sensitive and this schema's lookup is not. "
            "Rename them in the legacy service first; merging them here would "
            "silently drop one device's name onto another's."
        )

    return Plan(
        owner=owner,
        devices=tuple(devices),
        domains=planned_domains,
        backends=tuple(planned_backends),
        hostnames=planned_hostnames,
        snapshot=snapshot,
        notes=tuple(notes),
    )


def _modal_ttl(values: Sequence[int]) -> int:
    """The most common TTL in a zone; ties resolve to the lowest.

    Only ever consulted for a zone whose hostnames disagree — where
    every name is pinned individually, so this decides nothing about
    the migrated rows. What it decides is what a hostname **added
    afterwards** inherits, which is why it is the mode of the zone
    rather than, say, ``values[0]``: the commonest answer is the least
    surprising default, and an arbitrary one would be a number nobody
    chose sitting in the place an operator reads the zone's TTL from.

    Lowest-on-tie is arbitrary but *fixed*: an unstable tie-break makes
    the same source database import to two different configs, and the
    second instrument would then be comparing against a coin toss.
    """
    counts: dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return min(counts, key=lambda ttl: (-counts[ttl], ttl))


def _bound_backends(snapshot: Snapshot) -> dict[int, set[int]]:
    """``legacy hostname id -> the backend ids it names``, as stored."""
    bound: dict[int, set[int]] = {}
    for hostname_id, backend_id in snapshot.hostname_backends:
        bound.setdefault(hostname_id, set()).add(backend_id)
    return bound


def _bindings_off_the_domain(
    snapshot: Snapshot, backends_by_domain: Mapping[int, list[LegacyBackend]]
) -> set[int]:
    """Hostname ids naming a backend their own zone does not have.

    Includes a binding for a hostname row that does not exist, which
    lands here for the same reason: there is no zone to check it
    against and therefore no target row to write.

    This is the part of ``hostname_backends`` that #83 did **not**
    widen. Every other shape is now migrated; this one still refuses,
    because :func:`atrium_ddns.models.resolve_backends` filters a
    selection against ``domain.backends``, so a row surviving this
    check would resolve to the empty set and leave the name publishing
    nowhere. Refusing beats writing a row whose only effect is silence.
    """
    domain_of = {h.id: h.domain_id for h in snapshot.hostnames}
    stray: set[int] = set()
    for hostname_id, chosen in _bound_backends(snapshot).items():
        domain_id = domain_of.get(hostname_id)
        available = {b.id for b in (backends_by_domain.get(domain_id) or ())}
        if chosen - available:
            stray.add(hostname_id)
    return stray


def _selected_bindings(
    snapshot: Snapshot, backends_by_domain: Mapping[int, list[LegacyBackend]]
) -> dict[int, tuple[int, ...]]:
    """``legacy hostname id -> the backends to pin it to``.

    ``hostname_backends`` empty for a hostname means "all of its
    domain's backends" (``Hostname.get_backends`` in the legacy model),
    and so does a binding that names all of them — and
    :func:`atrium_ddns.models.resolve_backends` spells the same thing
    the same way, as an absent selection. So the degenerate case maps
    to **no rows**, and only a strict subset becomes
    ``ddns_hostname_backend`` rows.

    The alternative — carrying every binding across verbatim, including
    the complete ones — is faithful in a narrow sense and wrong in a
    wider one. It agrees with legacy today and diverges from it the
    moment a backend is added to a zone: legacy would keep publishing
    the old set, atrium would too, and the eleven names in the measured
    population would silently stop tracking a zone they have tracked
    since ``0004`` argued for exactly that reading. The cost is stated
    rather than hidden: a *degenerate* legacy binding does not survive
    as an explicit one, so a name that had been pinned-to-everything
    picks up a backend added later. Nothing in the measured population
    distinguishes the two, and the reading that keeps the zone live is
    ``0004``'s and the frozen table's.
    """
    domain_of = {h.id: h.domain_id for h in snapshot.hostnames}
    selected: dict[int, tuple[int, ...]] = {}
    for hostname_id, chosen in _bound_backends(snapshot).items():
        domain_id = domain_of.get(hostname_id)
        available = {b.id for b in (backends_by_domain.get(domain_id) or ())}
        selected[hostname_id] = (
            () if chosen == available else tuple(sorted(chosen))
        )
    return selected


# --------------------------------------------------------------------- #
# The write — one transaction
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class Written:
    owner_id: int
    owner_email: str
    owner_action: str
    devices: int
    domains: int
    backends: int
    hostnames: int
    #: ``ddns_hostname.ttl`` values written — i.e. names in a zone whose
    #: hostnames disagreed. ``0`` on the measured population.
    ttl_overrides: int = 0
    #: ``ddns_hostname_backend`` rows written. ``0`` on the measured
    #: population, where every legacy binding is degenerate.
    selections: int = 0


async def assert_target_is_empty(session: Any, plan: Plan) -> None:
    """Refuse a second run, naming what is already there.

    Deliberately not idempotent. Making it so would mean reconciling a
    device password an operator may have rotated in the new UI, and a
    provider credential they may have replaced, against a legacy
    database that still holds the old ones — and "merge" would silently
    revert both, which is worse than the run this refuses. Rehearse
    with ``--dry-run`` or against a fresh database.

    Checked inside the same transaction as the writes, so nothing can
    land between the check and the insert.
    """
    from ..models import Device, Domain, Hostname

    collisions: list[str] = []

    usernames = [d.username for d in plan.devices]
    existing = (
        (
            await session.execute(
                sa.select(Device.username).where(Device.username.in_(usernames))
            )
        )
        .scalars()
        .all()
    )
    if existing:
        collisions.append(
            f"{len(existing)} of {len(usernames)} device username(s) already "
            "exist"
        )

    names = list(plan.domains.values())
    existing_domains = (
        (
            await session.execute(
                sa.select(Domain.name).where(Domain.name.in_(names))
            )
        )
        .scalars()
        .all()
    )
    if existing_domains:
        collisions.append(
            f"{len(existing_domains)} of {len(names)} domain name(s) already "
            "exist"
        )

    hostnames = [h.name for h in plan.hostnames]
    existing_hostnames = (
        (
            await session.execute(
                sa.select(Hostname.name).where(Hostname.name.in_(hostnames))
            )
        )
        .scalars()
        .all()
    )
    if existing_hostnames:
        collisions.append(
            f"{len(existing_hostnames)} of {len(hostnames)} hostname(s) "
            "already exist"
        )

    if collisions:
        raise MigrationRefused(
            "this database already holds rows this import would create:\n"
            + "\n".join(f"  - {line}" for line in collisions)
            + "\n  This importer REFUSES to run twice rather than merging. "
            "Names withheld.\n"
            "  Re-import against a fresh database, or delete the migrated "
            "tenant first."
        )


async def _resolve_owner(
    session: Any, plan: Plan, *, owner_email: str, create_owner: bool
) -> tuple[int, str]:
    """Adopt the named atrium user, or create one from the admin row.

    Adoption is the default and creation is opt-in, because an importer
    that mints an account is an authentication surface nobody reviewed.
    When it does create one it carries the legacy admin's **bcrypt hash
    verbatim**, so the operator's existing web password keeps working:
    measured against this image, ``fastapi_users.password.PasswordHelper``
    holds ``(Argon2Hasher, BcryptHasher)`` and re-hashes to argon2id on
    the first successful login — which is *not* the same as
    ``PasswordHash.recommended()``, which holds Argon2 alone and raises.
    """
    from app.models.auth import User

    existing = (
        await session.execute(sa.select(User).where(User.email == owner_email))
    ).scalar_one_or_none()
    if existing is not None:
        if not existing.is_active:
            raise MigrationRefused(
                f"the atrium user {owner_email} exists but is not active. "
                "Every migrated device authenticates through its owner's "
                "is_active flag, so the whole fleet would answer badauth."
            )
        return existing.id, "adopted"

    if not create_owner:
        raise MigrationRefused(
            f"no atrium user with email {owner_email}.\n"
            "  Create it first (`make seed-admin EMAIL=… PASSWORD=…`, or "
            "atrium's own signup),\n"
            "  or pass --create-owner to have this import create it carrying "
            "the legacy\n"
            f"  admin row's ({plan.owner.username!r}) password hash verbatim, "
            "so the existing\n"
            "  web password keeps working. Nothing has been written."
        )

    if not _identifiable(plan.owner.password_hash):
        raise MigrationRefused(
            "--create-owner was given, but the legacy admin row's password "
            f"hash is not one atrium can verify (prefix "
            f"{plan.owner.password_hash[:7]!r}). Creating the account with it "
            "would produce a user who can never log in."
        )

    from app.auth.rbac import assign_role
    from app.models.enums import Language

    user = User(
        email=owner_email,
        hashed_password=plan.owner.password_hash,
        is_active=True,
        is_verified=True,
        full_name=plan.owner.username,
        preferred_language=Language.EN.value,
    )
    session.add(user)
    await session.flush()
    await assign_role(session, user_id=user.id, role_code=OWNER_ROLE)
    return user.id, "created"


async def apply(
    session: Any, plan: Plan, *, owner_email: str, create_owner: bool
) -> Written:
    """Write the whole plan. Caller owns the transaction.

    Every insert in this function is in the caller's transaction and
    none of it is committed here — ``--dry-run`` is exactly this
    function followed by a rollback, so the rehearsal exercises the
    same code the real run does rather than a description of it.
    """
    from app.host_sdk.crypto import unlock_user_secrets

    from ..models import Device, Domain, DomainBackend, Hostname, HostnameBackend

    # Emptiness first, owner second: the collision check is the cheap
    # refusal and `_resolve_owner` may *create* a user. A rollback would
    # undo it either way, but a refusal that has already written
    # something is a refusal somebody has to reason about.
    await assert_target_is_empty(session, plan)
    owner_id, action = await _resolve_owner(
        session, plan, owner_email=owner_email, create_owner=create_owner
    )

    # `create=True` is correct on a write path and wrong on a read one,
    # where it would hand a shredded user a fresh key that decrypts
    # nothing.
    await unlock_user_secrets(session, owner_id, create=True)

    device_ids: dict[int, int] = {}
    for planned in plan.devices:
        device = Device(
            user_id=owner_id,
            username=planned.username,
            # Byte-for-byte. This is the credential the router in the
            # field is configured with, and `auth_device` verifies
            # bcrypt and re-hashes opportunistically on first call.
            password_hash=planned.password_hash,
            name=planned.name,
        )
        session.add(device)
        await session.flush()
        device_ids[planned.legacy_user_id] = device.id

    domain_ids: dict[int, int] = {}
    for legacy_id, name in sorted(plan.domains.items()):
        domain = Domain(user_id=owner_id, name=name)
        session.add(domain)
        await session.flush()
        domain_ids[legacy_id] = domain.id

    backend_ids: dict[int, int] = {}
    for backend in plan.backends:
        row = DomainBackend(
            domain_id=domain_ids[backend.legacy_domain_id],
            user_id=owner_id,
            backend_type=backend.backend_type,
            config=dict(backend.config) or None,
        )
        # Assignment, not `credentials_ct=`: the descriptor holds the
        # value in memory and encrypts it at flush, under the key
        # `unlock_user_secrets` put in `session.info` above.
        row.credentials = dict(backend.credentials)
        session.add(row)
        # Flushed inside the loop so `Domain.backends`' order follows
        # the legacy primary-key order rather than the session's flush
        # order — the aggregate status on `/nic/update` depends on it.
        await session.flush()
        backend_ids[backend.legacy_backend_id] = row.id

    selections = 0
    for hostname in plan.hostnames:
        record = Hostname(
            domain_id=domain_ids[hostname.legacy_domain_id],
            device_id=device_ids[hostname.legacy_user_id],
            name=hostname.name,
            last_ip_v4=hostname.last_ip_v4,
            last_ip_v6=hostname.last_ip_v6,
            last_updated_at=hostname.last_updated_at,
            # NULL unless this name's zone disagreed about TTL — see
            # `build_plan`. NULL is "inherit the binding", which is the
            # state every hostname in the measured population is in.
            ttl=hostname.ttl,
        )
        session.add(record)
        if hostname.selected_legacy_backend_ids:
            # Flushed here rather than in one batch at the end because
            # the selection row needs this hostname's id, and the id
            # does not exist until the insert has gone out. Names with
            # no selection — the whole measured population — cost no
            # extra round trip.
            await session.flush()
            for legacy_backend_id in hostname.selected_legacy_backend_ids:
                session.add(
                    HostnameBackend(
                        hostname_id=record.id,
                        backend_id=backend_ids[legacy_backend_id],
                    )
                )
                selections += 1
    await session.flush()

    return Written(
        owner_id=owner_id,
        owner_email=owner_email,
        owner_action=action,
        devices=len(plan.devices),
        domains=len(plan.domains),
        backends=len(plan.backends),
        hostnames=len(plan.hostnames),
        ttl_overrides=sum(1 for h in plan.hostnames if h.ttl is not None),
        selections=selections,
    )


# --------------------------------------------------------------------- #
# The second instrument
# --------------------------------------------------------------------- #


def credential_digest(credentials: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON, first 16 hex characters.

    A comparable reading that is not the value. Printing the credential
    to prove it round-tripped would put a production AWS secret in a
    terminal, a transcript and quite possibly a PR body; printing
    ``ok`` proves nothing. A digest fails if a single byte moved and
    discloses nothing if it did not.
    """
    canonical = json.dumps(
        {str(k): credentials[k] for k in sorted(credentials)},
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Reading:
    """What the **target** holds, read back after the commit."""

    devices: int
    domains: int
    backends: int
    hostnames: int
    hostname_to_device: Mapping[str, str]
    #: ``(domain name, backend type)`` -> sha256[:16] of the decrypted
    #: credential's canonical JSON. Keyed by the pair rather than by the
    #: type alone because ``UNIQUE(domain_id, backend_type)`` makes only
    #: the pair unique — two zones each with a ``route53`` backend would
    #: otherwise collapse onto one entry and one of the two digests
    #: would never be compared.
    credential_digests: Mapping[tuple[str, str], str]
    password_hashes: Mapping[str, str]
    hostname_ips: Mapping[str, tuple[str | None, str | None]]
    #: ``hostname -> ((backend type, TTL), …)`` — where each migrated
    #: name would publish, and at what TTL, resolved through
    #: :func:`atrium_ddns.models.resolve_backends` and
    #: :func:`~atrium_ddns.models.resolve_ttl` rather than by reading
    #: ``ddns_hostname.ttl`` and ``ddns_hostname_backend`` directly.
    #: Compared against :attr:`Plan.publication`, which is derived from
    #: the legacy columns alone. Default ``{}`` only so a hand-built
    #: :class:`Reading` in a test stays constructible; :func:`compare`
    #: treats a missing entry as a disagreement, not as "nothing to
    #: check".
    hostname_publication: Mapping[str, tuple[tuple[str, Any], ...]] = field(
        default_factory=dict
    )


async def verify(session: Any, owner_id: int) -> Reading:
    """Re-read everything the import wrote, through the ORM.

    A differently-shaped instrument from :func:`apply`'s own return
    value, which reports what it *asked for*. This reports what the
    database *holds*, resolved through the same relationships
    ``/nic/update`` traverses — so a hostname attached to the wrong
    device, a credential that did not encrypt, or a backend type that
    did not resolve shows up here and not in the first reading.
    """
    from app.host_sdk.crypto import unlock_user_secrets
    from sqlalchemy.orm import selectinload

    from ..models import Device, Domain, Hostname, resolve_backends, resolve_ttl
    from ..scope import DdnsScope

    await unlock_user_secrets(session, owner_id)

    # Through the scope, not a hand-written `user_id ==`. Two reasons:
    # it is the control this codebase applies to every host query
    # without exception, and `Hostname` has no `user_id` column — its
    # tenancy is its domain's, and `DdnsScope` knows the join while a
    # hand-written filter here would have to reinvent it and would be
    # the shape copied into the next query.
    scope = DdnsScope.for_user_id(owner_id)

    devices = (
        (
            await session.execute(
                scope.select(Device).order_by(Device.username)
            )
        )
        .scalars()
        .all()
    )
    domains = (
        (
            await session.execute(
                scope.select(Domain)
                .options(selectinload(Domain.backends))
                .order_by(Domain.name)
            )
        )
        .scalars()
        .all()
    )
    hostnames = (
        (
            await session.execute(
                scope.select(Hostname)
                .options(
                    selectinload(Hostname.device),
                    # Exactly the eager loads `router_nic` uses.
                    # `resolve_backends` is a pure function over ORM
                    # state and cannot lazy-load: a lazy load inside it
                    # is a synchronous database call on the event loop,
                    # which raises `MissingGreenlet` rather than being
                    # slow.
                    selectinload(Hostname.domain).selectinload(Domain.backends),
                    selectinload(Hostname.selected_backends),
                )
                .order_by(Hostname.name)
            )
        )
        .scalars()
        .all()
    )
    backends = [(d, b) for d in domains for b in d.backends]

    digests: dict[tuple[str, str], str] = {}
    for domain, backend in backends:
        revealed = backend.credentials
        digests[(domain.name, backend.backend_type)] = (
            # `ABSENT` rather than `""`: a NULL ciphertext is a real
            # state in this schema (the compat fixture's
            # `credentials: absent` backend), and rendering it as an
            # empty digest would make it compare equal to nothing in
            # particular.
            "ABSENT"
            if revealed is None
            else credential_digest(revealed.reveal())
        )

    return Reading(
        devices=len(devices),
        domains=len(domains),
        backends=len(backends),
        hostnames=len(hostnames),
        hostname_to_device={
            h.name: (h.device.username if h.device is not None else "UNASSIGNED")
            for h in hostnames
        },
        credential_digests=digests,
        password_hashes={d.username: d.password_hash for d in devices},
        hostname_ips={h.name: (h.last_ip_v4, h.last_ip_v6) for h in hostnames},
        hostname_publication={
            h.name: tuple(
                (b.backend_type, resolve_ttl(h, b)) for b in resolve_backends(h)
            )
            for h in hostnames
        },
    )


def compare(plan: Plan, written: Written, reading: Reading) -> list[str]:
    """Every disagreement between the two instruments, as text.

    An empty list is the result; it is never the *default*, because
    each check below is derived from data on both sides rather than
    from a constant. A plan with no devices produces ``0 == 0`` and the
    caller refuses that separately — a comparison that passes because
    there was nothing to compare is the probe that could not fail.
    """
    problems: list[str] = []

    source = plan.snapshot.counts
    pairs = [
        ("devices", len(plan.snapshot.device_rows), reading.devices, written.devices),
        ("domains", source["domains"], reading.domains, written.domains),
        (
            "backends",
            source["domain_backends"],
            reading.backends,
            written.backends,
        ),
        (
            "hostnames",
            source["hostnames"],
            reading.hostnames,
            written.hostnames,
        ),
    ]
    for label, in_source, in_target, asked in pairs:
        if not (in_source == in_target == asked):
            problems.append(
                f"{label}: source {in_source}, written {asked}, target "
                f"{in_target}"
            )

    expected = plan.hostname_to_device
    actual = dict(reading.hostname_to_device)
    if expected != actual:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        moved = sorted(
            name
            for name in set(expected) & set(actual)
            if expected[name] != actual[name]
        )
        problems.append(
            f"hostname -> device mapping differs: {len(missing)} missing, "
            f"{len(extra)} unexpected, {len(moved)} on the wrong device "
            "(names withheld)"
        )

    for device in plan.devices:
        stored = reading.password_hashes.get(device.username)
        if stored is None:
            problems.append(f"device {device.username!r} is not in the target")
        elif stored != device.password_hash:
            problems.append(
                f"device {device.username!r}: password hash is NOT "
                "byte-identical to the legacy row"
            )

    for backend in plan.backends:
        want = credential_digest(backend.credentials)
        key = (plan.domains[backend.legacy_domain_id], backend.backend_type)
        got = reading.credential_digests.get(key)
        if got != want:
            problems.append(
                f"backend {backend.backend_type!r} (legacy id "
                f"{backend.legacy_backend_id}): the credential read back out "
                f"of the target digests {got}, the credential decrypted from "
                f"the source digests {want}"
            )

    for hostname in plan.hostnames:
        got = reading.hostname_ips.get(hostname.name)
        if got != (hostname.last_ip_v4, hostname.last_ip_v6):
            problems.append(
                f"hostname (legacy id {hostname.legacy_id}): last_ip_* did "
                "not survive"
            )

    # #83's assertion, and the one that decides whether widening the
    # two refusals was safe. The left-hand side is derived from the
    # legacy columns through the legacy service's own rules; the
    # right-hand side is read out of MySQL through `resolve_backends`
    # and `resolve_ttl`, which is what `/nic/update` calls. Equal means
    # every migrated name publishes to the same providers at the same
    # TTL as before; unequal is the only failure a router in the field
    # could ever notice.
    expected_publication = plan.publication
    for name, want in expected_publication.items():
        got_pub = reading.hostname_publication.get(name)
        if got_pub != want:
            problems.append(
                f"hostname (legacy id "
                f"{next(h.legacy_id for h in plan.hostnames if h.name == name)}"
                f"): would publish through {_render_publication(got_pub)}, the "
                f"legacy row published through {_render_publication(want)} "
                "(names withheld)"
            )
    unexpected = sorted(set(reading.hostname_publication) - set(expected_publication))
    if unexpected:
        problems.append(
            f"{len(unexpected)} migrated hostname(s) are not in the plan at "
            "all (names withheld)"
        )

    return problems


def _render_publication(
    entry: tuple[tuple[str, Any], ...] | None,
) -> str:
    """``[route53@60, hetzner@300]`` — provider and TTL, never a name.

    ``None`` is rendered as ``NOTHING`` rather than as ``[]``: a
    hostname the target does not hold at all and one that publishes
    nowhere are different failures, and ``[]`` would read as the second
    while meaning the first.
    """
    if entry is None:
        return "NOTHING (the target has no such hostname)"
    if not entry:
        return "[] (no backend at all)"
    return "[" + ", ".join(f"{name}@{ttl}" for name, ttl in entry) + "]"


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #


def _print_plan(plan: Plan, *, source: Path) -> None:
    counts = plan.snapshot.counts
    print(f"source: {source}")
    print(
        "  read: "
        + ", ".join(f"{table} {counts[table]}" for table in sorted(counts))
    )
    print()
    print("mapping:")
    print(
        f"  1 admin row                -> 1 atrium user "
        f"({plan.owner.username!r}, legacy id {plan.owner.id})"
    )
    print(f"  {len(plan.devices)} non-admin rows            -> {len(plan.devices)} ddns_device")
    print(f"  {len(plan.domains)} domain(s)                -> {len(plan.domains)} ddns_domain")
    print(
        f"  {len(plan.backends)} backend(s)               -> "
        f"{len(plan.backends)} ddns_domain_backend "
        + ", ".join(
            f"[{b.stored_as} -> {b.backend_type}, "
            f"{len(b.credentials)} credential(s) decrypted, "
            f"ttl={b.config.get('ttl', 'unset')}]"
            for b in plan.backends
        )
    )
    print(f"  {len(plan.hostnames)} hostname(s)             -> {len(plan.hostnames)} ddns_hostname")
    # The two counts #83 exists for, printed on EVERY run including
    # `--dry-run`, and printed even when they are zero. Both were
    # refusals until `0004` made them representable, and both were
    # measured at zero on the 2026-08-15 population — so a later
    # re-import that is not zero here is doing something no rehearsal
    # has ever done, and this line is where that becomes visible
    # instead of silent. `docs/ops/cutover.md` § 5.3 records the
    # rehearsed values to compare against.
    pinned = sum(1 for h in plan.hostnames if h.ttl is not None)
    zones = len({h.legacy_domain_id for h in plan.hostnames if h.ttl is not None})
    rows = sum(len(h.selected_legacy_backend_ids) for h in plan.hostnames)
    names = sum(1 for h in plan.hostnames if h.selected_legacy_backend_ids)
    print(
        f"  per-name TTL              -> {pinned} of {len(plan.hostnames)} "
        f"ddns_hostname.ttl written ({zones} zone(s) disagreed)"
    )
    print(
        f"  per-name backend choice   -> {rows} ddns_hostname_backend row(s) "
        f"for {names} name(s), from "
        f"{len(plan.snapshot.hostname_backends)} legacy binding(s)"
    )
    print()
    spread: dict[str, int] = {}
    for device in plan.hostname_to_device.values():
        spread[device] = spread.get(device, 0) + 1
    print(
        "  hostnames per device: "
        + ", ".join(str(spread.get(d.username, 0)) for d in plan.devices)
        + f"  (total {sum(spread.values())})"
    )
    for note in plan.notes:
        print(f"\n  NOTE: {note}")


def _print_readings(plan: Plan, written: Written, reading: Reading) -> None:
    counts = plan.snapshot.counts
    print()
    print("two instruments — rows read from the source vs rows in the target:")
    print(f"  {'':<12} {'source':>8} {'written':>8} {'target':>8}")
    rows = [
        ("devices", len(plan.snapshot.device_rows), written.devices, reading.devices),
        ("domains", counts["domains"], written.domains, reading.domains),
        ("backends", counts["domain_backends"], written.backends, reading.backends),
        ("hostnames", counts["hostnames"], written.hostnames, reading.hostnames),
    ]
    for label, a, b, c in rows:
        flag = "" if a == b == c else "   <-- DISAGREE"
        print(f"  {label:<12} {a:>8} {b:>8} {c:>8}{flag}")


def _run(args: argparse.Namespace) -> int:
    source = Path(args.source)
    with open_source(source) as conn:
        assert_columns(conn)
        snapshot = read_snapshot(conn)

    plan = build_plan(snapshot, fernet_key=os.environ.get(FERNET_KEY_ENV))
    _print_plan(plan, source=source)

    if not plan.devices or not plan.hostnames:
        raise MigrationRefused(
            "the plan is empty on at least one side "
            f"({len(plan.devices)} device(s), {len(plan.hostnames)} "
            "hostname(s)). Every count below would agree at zero, which is a "
            "comparison that cannot fail rather than a migration."
        )

    async def _go() -> int:
        from app.db import get_engine, get_session_factory

        factory = get_session_factory()
        try:
            async with factory() as session:
                written = await apply(
                    session,
                    plan,
                    owner_email=args.owner_email,
                    create_owner=args.create_owner,
                )
                if args.dry_run:
                    await session.rollback()
                    print()
                    print(
                        "DRY RUN — the transaction was rolled back. It "
                        f"planned {written.devices} device(s), "
                        f"{written.domains} domain(s), {written.backends} "
                        f"backend(s), {written.hostnames} hostname(s) for "
                        f"owner {written.owner_email} ({written.owner_action})."
                    )
                    print(
                        "  Target counts: n/a — NOT MEASURED, because "
                        "nothing was committed. That is"
                    )
                    print(
                        "  a different state from zero and is deliberately "
                        "not rendered as one."
                    )
                    return 0
                await session.commit()

            # A fresh session on purpose: reading back through the one
            # that wrote would be served from its identity map, which
            # reports what the ORM was told rather than what MySQL
            # holds.
            async with factory() as session:
                reading = await verify(session, written.owner_id)
        finally:
            await get_engine().dispose()

        _print_readings(plan, written, reading)
        problems = compare(plan, written, reading)
        print()
        print(
            f"  owner: {written.owner_email} (atrium user {written.owner_id}, "
            f"{written.owner_action})"
        )
        print(
            "  hostname -> device: "
            f"{len(reading.hostname_to_device)} of "
            f"{len(plan.hostname_to_device)} compared as a whole set, "
            + ("IDENTICAL" if not problems else "see below")
        )
        # Zone names are withheld; the type and the digest are not
        # secrets and the digest is what makes the comparison
        # falsifiable.
        print(
            "  provider credentials: "
            + ", ".join(
                f"{backend_type}={digest}"
                for (_zone, backend_type), digest in sorted(
                    reading.credential_digests.items()
                )
            )
            + "  (sha256[:16] of the canonical JSON — decrypted from the "
            "source, and read back out of the target)"
        )
        print(
            "  device password hashes: "
            f"{sum(1 for d in plan.devices if reading.password_hashes.get(d.username) == d.password_hash)}"
            f" of {len(plan.devices)} byte-identical to the legacy rows"
        )

        if problems:
            print()
            print("REFUSED — the two instruments disagree:", file=sys.stderr)
            for line in problems:
                print(f"  - {line}", file=sys.stderr)
            return 1
        print()
        print("both instruments agree on every count.")
        return 0

    return asyncio.run(_go())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import the legacy dyndns-route53 SQLite database into the "
            "atrium-ddns schema. Reads a WAL-safe COPY, never the live file."
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        help=(
            "path to a COPY of the legacy database, taken with "
            '`sqlite3 <db> ".backup <out>"`. A path with a -wal sibling is '
            "refused."
        ),
    )
    parser.add_argument(
        "--owner-email",
        required=True,
        help=(
            "the atrium user that owns everything migrated. Must already "
            "exist unless --create-owner is given."
        ),
    )
    parser.add_argument(
        "--create-owner",
        action="store_true",
        help=(
            "create the owner from the legacy admin row, carrying its "
            "password hash verbatim so the existing web password works."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "do everything, including the decryption and the collision "
            "check, then roll back. Target counts are reported as n/a "
            "rather than 0."
        ),
    )
    args = parser.parse_args(argv)

    try:
        return _run(args)
    except MigrationRefused as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FERNET_KEY_ENV",
    "REQUIRED_COLUMNS",
    "LegacyBackend",
    "LegacyHostname",
    "LegacyRateLimit",
    "LegacyUser",
    "MigrationRefused",
    "Plan",
    "PlannedBackend",
    "PlannedDevice",
    "PlannedHostname",
    "Reading",
    "Snapshot",
    "Written",
    "apply",
    "assert_columns",
    "assert_target_is_empty",
    "build_plan",
    "compare",
    "credential_digest",
    "decrypt_credentials",
    "main",
    "normalise_dns_name",
    "open_source",
    "read_snapshot",
    "verify",
]
