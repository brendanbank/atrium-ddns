"""Host-side ORM models.

The host owns its own ``DeclarativeBase`` (and therefore its own
``MetaData``) so atrium's alembic chain never sees these tables. The
host's alembic chain manages them under a separate version table
(``alembic_version_app``); see ``alembic/env.py``.

Rules of engagement:

- **Never** parent host tables on ``app.db.Base``. The next atrium
  upgrade may collide with whatever you added.
- Cross-base foreign keys (a host column referencing ``users.id`` or
  any other atrium table) need ``HostForeignKey`` from
  ``app.host_sdk.db`` plus ``emit_host_foreign_keys`` wired into
  ``alembic/env.py`` — both already configured in the scaffolded
  files. See ``docs/host-models.md`` in the atrium repo for the full
  rationale.

Three conventions this module holds to, each of which has a reason
that is not obvious from the code:

**Table names carry a ``ddns_`` prefix.** Atrium's tables and the
host's live in one MySQL schema. ``domain``, ``device`` and
``hostname`` are exactly the names a future atrium release is most
likely to want, and the collision would be discovered at
``alembic upgrade`` on somebody's production database. The scaffold
already set the precedent with ``atrium_ddns_state``.

**Every column that references ``users.id`` is ``Integer``, not
``BigInteger``.** ``users.id`` is MySQL ``int``; a ``bigint`` child
column makes ``ALTER TABLE ... ADD FOREIGN KEY`` fail with errno 150
and a message that does not mention the width. Host tables' own
primary keys are ``BigInteger`` because they are ours to size.

**Ownership is stored once, except where the crypto forces a second
copy.** A hostname's owner is its domain's owner and is not
duplicated onto the row — a second source of truth for tenancy is a
tenancy bug waiting to be written. ``ddns_domain_backend`` is the one
exception: :class:`~app.host_sdk.crypto.UserSecret` reads the owner
off the row holding the ciphertext, so that row has to carry it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.host_sdk.crypto import SecretBlob, UserSecret
from app.host_sdk.db import HostForeignKey
from sqlalchemy import (
    JSON,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import DATETIME as MysqlDATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Longest legal FQDN is 253 octets; 255 leaves room for a stored
# trailing dot without a migration. utf8mb4 makes that a 1020-byte
# index prefix, comfortably under InnoDB's 3072-byte limit, so these
# columns can carry UNIQUE indexes directly.
DNS_NAME_LEN = 255

# ``str(ipaddress.ip_address(...))`` output. 15 for IPv4, 45 for the
# longest IPv6 spelling (an IPv4-mapped address with a zone index).
IPV4_LEN = 15
IPV6_LEN = 45

# atrium's ``users.email`` is VARCHAR(320); the denormalised copy in
# the event log has to be able to hold any of them.
EMAIL_LEN = 320


def _utcnow_col(**kwargs: Any) -> Any:
    """A ``DATETIME(6)`` defaulting to the server clock.

    Microsecond precision rather than atrium's whole-second
    ``DateTime``: ``ddns_event`` is paged by ``(user_id, created_at)``
    and a router bursting several hostnames writes several rows inside
    one second. At ``fsp=0`` their order is not recoverable and
    keyset pagination over the compound index double-counts.
    """
    return mapped_column(
        MysqlDATETIME(fsp=6),
        server_default=text("CURRENT_TIMESTAMP(6)"),
        **kwargs,
    )


class HostBase(DeclarativeBase):
    """Host metadata, separate from atrium's ``app.db.Base``."""


class TimestampMixin:
    """``created_at`` / ``updated_at``, host-flavoured.

    Deliberately not ``app.models.mixins.TimestampMixin``: that one is
    ``DATETIME`` at whole-second precision, and these tables are
    ordered and paged at sub-second resolution.

    ``server_onupdate=`` below is **documentation and nothing else** —
    SQLAlchemy emits no DDL for it. The scaffolded
    ``atrium_ddns_state.updated_at`` declares it and the resulting
    column is a plain ``datetime(6) NOT NULL DEFAULT
    CURRENT_TIMESTAMP(6)`` with no ``ON UPDATE`` clause, so it has
    never once changed after insert. The real ``ON UPDATE
    CURRENT_TIMESTAMP(6)`` is spelled into the migration's
    ``server_default`` text, which is the only form that reaches
    MySQL; ``tests/test_host_models.py`` reads it back off the live
    table so this cannot silently regress to the scaffold's shape.
    """

    created_at: Mapped[datetime] = _utcnow_col(nullable=False)
    updated_at: Mapped[datetime] = _utcnow_col(
        nullable=False, server_onupdate=text("CURRENT_TIMESTAMP(6)")
    )


class AtriumDdnsState(HostBase):
    """Singleton row (id=1) for the demo widget.

    Left in place deliberately: ``router.py``, the frontend widget and
    ``scripts/smoke.sh`` all reference it, and none of those are in
    this issue's scope. It goes when the real HTTP surface lands.
    """

    __tablename__ = "atrium_ddns_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    message: Mapped[str] = mapped_column(
        String(255), nullable=False, default="Welcome to Atrium Ddns"
    )
    counter: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    updated_at: Mapped[datetime] = mapped_column(
        MysqlDATETIME(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        server_onupdate=text("CURRENT_TIMESTAMP(6)"),
    )


class Domain(HostBase, TimestampMixin):
    """A DNS zone, **owned by a user**.

    The multi-tenancy change in one line: the old service's domains
    were global and admin-managed. Uniqueness of ``name`` stays global
    — DNS is global, and two tenants claiming one zone is a conflict
    the database should refuse rather than a state the UI has to
    explain.

    ``ON DELETE CASCADE`` to ``users``: a tenant's zones, their
    provider credentials and their hostnames die with the account.
    That is atrium's *hard* delete, after ``auth.delete_grace_days``,
    not the soft delete — see plan §3.1.3.
    """

    __tablename__ = "ddns_domain"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        HostForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Stored lower-cased and without a trailing dot. DNS comparison is
    # case-insensitive, and the UNIQUE index below is the only thing
    # stopping `Example.com` and `example.com` becoming two zones.
    name: Mapped[str] = mapped_column(String(DNS_NAME_LEN), nullable=False)

    backends: Mapped[list[DomainBackend]] = relationship(
        back_populates="domain",
        cascade="all, delete-orphan",
        passive_deletes=True,
        # Order is load-bearing: `/nic/update` aggregates per-backend
        # results and answers with the *first* non-good/nochg status.
        # The legacy schema had no ordering column and fell back to
        # primary-key order, and the compat table's
        # `update-aggregate-first-non-good-nochg-status` case depends
        # on it. Say so here rather than inheriting it by accident.
        order_by="DomainBackend.id",
    )
    hostnames: Mapped[list[Hostname]] = relationship(
        back_populates="domain",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("name", name="uq_ddns_domain_name"),
    )


class DomainBackend(HostBase, TimestampMixin):
    """One provider binding for a domain — and the credentials for it.

    One row per ``(domain, backend_type)``, which is the legacy shape
    and the one the compat fixture is built against: four differently
    scripted stubs on one domain are four ``backend_type`` values, not
    four rows of one type.

    **The credentials are encrypted per-user**, atrium >= 0.28. Two
    declarations, not one: a SQLAlchemy column type never sees the row
    it belongs to, and the row is where the owner lives.
    ``EncryptedText(scope="user")`` raises permanently and names this
    replacement.

    Reading one is::

        row = await session.get(DomainBackend, backend_id)
        await unlock_user_secrets(session, row.user_id)   # owner from the ROW
        row.credentials.reveal()

    …and the ``await`` is not optional: unwrapping is a database read,
    so it cannot hide behind attribute access in an async app. On
    ``/nic/update`` it belongs in the same fetch that loads the domain
    rows, not lazily inside the per-backend loop.
    """

    __tablename__ = "ddns_domain_backend"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ddns_domain.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalised from `ddns_domain.user_id`, and the one place this
    # module duplicates ownership. `UserSecret` resolves the owner
    # from the row carrying the ciphertext; making it read through a
    # relationship would be a lazy load — i.e. IO — inside attribute
    # access, which is precisely what the two-declaration shape exists
    # to avoid. Keep the two in step on any re-parenting.
    user_id: Mapped[int] = mapped_column(
        Integer,
        HostForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 'route53' | 'hetzner' | 'nsupdate' | … — resolved by the provider
    # factory (#15). An unknown value is not a schema error: the
    # compat table has a case for it (`unknownsvc.example.com`
    # contributes 911), so the column stays a free string.
    backend_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # Non-secret provider settings: hosted-zone id, TTL, nameserver
    # address, TSIG algorithm. JSON because every provider wants a
    # different set and none of them wants a migration.
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Raw ciphertext. Never read this directly — read `credentials`.
    #
    # The `.with_variant()` is not decoration. `SecretBlob`'s docstring
    # says it "widens to MEDIUMBLOB on MySQL", and it does not:
    # `load_dialect_impl` is a `TypeDecorator` hook and `SecretBlob` is
    # a plain `LargeBinary` subclass, so the method is never called and
    # the column compiles to `BLOB` — 64 KB, the exact ceiling atrium's
    # own comment says atrium-pa hit in production. Measured two ways:
    # `SecretBlob().compile(dialect=mysql.dialect())` returns `BLOB`
    # (its sibling `EncryptedText`, a real `TypeDecorator`, returns
    # `MEDIUMBLOB`), and the first cut of the 0002 migration produced
    # `credentials_ct blob`. Upstream defect, reported with the PR; one
    # line takes the width upstream intended, and
    # `tests/test_host_models.py` reads it back off the live table.
    credentials_ct: Mapped[bytes | None] = mapped_column(
        SecretBlob().with_variant(mysql.MEDIUMBLOB(), "mysql"), nullable=True
    )
    # NULL ciphertext is a real state, not a missing one: the compat
    # fixture's `credentials: absent` backend contributes 911, and
    # this is how it is spelled.
    credentials = UserSecret(
        purpose="domain_backend.credentials",
        owner_attr="user_id",
        column="credentials_ct",
        json=True,
    )

    domain: Mapped[Domain] = relationship(back_populates="backends")

    __table_args__ = (
        UniqueConstraint(
            "domain_id", "backend_type", name="uq_ddns_domain_backend_type"
        ),
    )


class Device(HostBase, TimestampMixin):
    """The thing that makes the call — a router, a NAS, a script.

    The object the old model was missing. It carries the DDNS
    credential and it is the unit a hostname belongs to, which is what
    lets the log attribute an update to something more specific than
    "the account".

    **The secret is hashed, not encrypted, and that is a decision.**
    argon2id for anything newly issued, bcrypt verification retained
    so rows migrated from the old service keep working untouched
    (verify by stored prefix: ``$2a$``/``$2b$``/``$2y$`` -> bcrypt,
    ``$argon2`` -> argon2id; re-hash opportunistically on a successful
    bcrypt verify). Shown once at create and at rotate, never
    re-displayable.

    So ``password_hash`` is a plain ``String`` and deliberately does
    **not** go through ``app.host_sdk.crypto``: encryption is for
    secrets we have to hand back, hashing is for secrets we only have
    to compare. Atrium 0.28's release notes name a device password as
    an example ``UserSecret``; we decline it on purpose, because a
    reversible store of every tenant's DNS-update credential is a
    blast radius we do not need.
    """

    __tablename__ = "ddns_device"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        HostForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # HTTP Basic username. Globally unique and indexed, because it is
    # the lookup key on the hot path and there is no tenant context to
    # scope it by — the request has not been authenticated yet.
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    # argon2id (new) or bcrypt (migrated). 255 fits both with room to
    # spare; argon2id at atrium's parameters is ~100 characters.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # What the owner calls it. Denormalised into `ddns_event` at write
    # time so a log entry stays readable after the device is deleted.
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Operational signal — the answer to "which of my devices stopped
    # calling in". All nullable, and NULL means *never*, which is a
    # different thing from a failed call and a different thing again
    # from zero updates in a window. Three states, three renderings;
    # collapsing them into 0 is how a status board lies.
    last_seen_at: Mapped[datetime | None] = mapped_column(
        MysqlDATETIME(fsp=6), nullable=True
    )
    last_ip_v4: Mapped[str | None] = mapped_column(String(IPV4_LEN), nullable=True)
    last_ip_v6: Mapped[str | None] = mapped_column(String(IPV6_LEN), nullable=True)
    last_user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Per-device override of the namespace-level default (#17 registers
    # the namespace; #16 enforces the limit and answers `abuse`).
    # NULL means *inherit*, which is not the same as 0 — 0 is a device
    # that may never call. The compat fixture needs its users' limits
    # raised for the table's `rate_limited: false` precondition to
    # hold, and this is where that is spelled.
    rate_limit_per_minute: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    hostnames: Mapped[list[Hostname]] = relationship(
        back_populates="device",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("username", name="uq_ddns_device_username"),
        UniqueConstraint("user_id", "name", name="uq_ddns_device_user_name"),
    )


class Hostname(HostBase, TimestampMixin):
    """A name this service maintains, in one domain, for one device.

    ``device_id`` is **nullable** on purpose: a hostname can be
    registered before it is assigned to anything, and reassigned
    between devices without being deleted and recreated. The FK is
    ``ON DELETE SET NULL`` so deleting a device orphans its hostnames
    rather than destroying them.

    One device per hostname (1:N, not M:N) — settled. A failover pair
    sharing a name would need M:N, and M:N makes "which device last
    wrote this record" ambiguous, which is the question the whole UI
    is built around.

    No ``user_id`` column. The owner is ``domain.user_id`` and storing
    it twice would create a second source of truth for tenancy.
    """

    __tablename__ = "ddns_hostname"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    domain_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ddns_domain.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("ddns_device.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The fully-qualified name, lower-cased. Unique globally and
    # indexed because `/nic/update?hostname=…` looks it up by string
    # with no tenant context — ownership is checked *after* the row is
    # found, which is what makes `nohost` distinguishable from "no
    # such name".
    #
    # Not required to be a suffix of `domain.name`: the compat table
    # has a case for a hostname outside its zone
    # (`outofzone.example.com`, which contributes `nohost`), so the
    # database must be able to hold that row.
    name: Mapped[str] = mapped_column(String(DNS_NAME_LEN), nullable=False)

    # What we last successfully wrote. Persisted only when the
    # aggregate is `good` — a `nochg` leaves all three untouched, and
    # the compat table asserts that.
    last_ip_v4: Mapped[str | None] = mapped_column(String(IPV4_LEN), nullable=True)
    last_ip_v6: Mapped[str | None] = mapped_column(String(IPV6_LEN), nullable=True)
    last_updated_at: Mapped[datetime | None] = mapped_column(
        MysqlDATETIME(fsp=6), nullable=True
    )

    # What the authoritative nameserver answered, last time the health
    # check asked (#17). Three states rather than two, which is the
    # whole point of having four columns instead of two:
    #
    #   dns_checked_at IS NULL        -> never checked        (n/a)
    #   dns_check_error IS NOT NULL   -> the check failed     (error)
    #   checked, no error, ips NULL   -> no such record       (0)
    #
    # Rendering those three as one zero is the failure mode #17's
    # acceptance criteria call out by name.
    dns_ip_v4: Mapped[str | None] = mapped_column(String(IPV4_LEN), nullable=True)
    dns_ip_v6: Mapped[str | None] = mapped_column(String(IPV6_LEN), nullable=True)
    dns_checked_at: Mapped[datetime | None] = mapped_column(
        MysqlDATETIME(fsp=6), nullable=True
    )
    dns_check_error: Mapped[str | None] = mapped_column(String(255), nullable=True)

    domain: Mapped[Domain] = relationship(back_populates="hostnames")
    device: Mapped[Device | None] = relationship(back_populates="hostnames")

    __table_args__ = (
        UniqueConstraint("name", name="uq_ddns_hostname_name"),
        # The health-check job sweeps "hostnames not checked since X".
        Index("ix_ddns_hostname_dns_checked_at", "dns_checked_at"),
    )


class DnsEvent(HostBase):
    """The DNS-update audit trail — searchable by user, device, domain.

    Two halves, and both are needed for different reasons:

    - **Indexed, nullable foreign keys** to user, device, domain and
      hostname, every one ``ON DELETE SET NULL``. These are what the
      filters index on.
    - **Denormalised name columns captured at write time.** These are
      what keeps an entry readable after the row it describes is
      deleted — which is precisely when the log is being read. A
      ``SET NULL`` FK on its own turns the history of a deleted device
      into a wall of blanks.

    Atrium's ``audit_log`` covers authentication and admin actions and
    is not a substitute: this is domain data, written by a router over
    HTTP Basic with no atrium session anywhere in the request.

    No ``updated_at``: an event is written once and never edited, and
    a column that only ever equals ``created_at`` invites somebody to
    order by the wrong one.
    """

    __tablename__ = "ddns_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = _utcnow_col(nullable=False)

    # --- who / what, by reference ------------------------------------ #
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        HostForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    device_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("ddns_device.id", ondelete="SET NULL"),
        nullable=True,
    )
    domain_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("ddns_domain.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    hostname_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("ddns_hostname.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # --- who / what, by value ---------------------------------------- #
    # Captured at write time. These survive the SET NULL above.
    user_email: Mapped[str | None] = mapped_column(String(EMAIL_LEN), nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    domain_name: Mapped[str | None] = mapped_column(
        String(DNS_NAME_LEN), nullable=True
    )
    hostname: Mapped[str | None] = mapped_column(String(DNS_NAME_LEN), nullable=True)

    # --- the event itself -------------------------------------------- #
    # 'update' | 'delete' | 'checkip' | 'auth' | 'healthcheck' | …
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # The wire response for this line: good | nochg | nohost | badauth
    # | notfqdn | abuse | dnserr | 911. Nullable because not every
    # event answers on the wire (a health check does not).
    response_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The address the request came *from* — after X-Forwarded-For, the
    # way the legacy service resolves it.
    client_ip: Mapped[str | None] = mapped_column(String(IPV6_LEN), nullable=True)
    # The address the request was *about* — `myip`, normalised. Not
    # the same as client_ip, and the difference is the interesting
    # part of a NAT'd update.
    ip: Mapped[str | None] = mapped_column(String(IPV6_LEN), nullable=True)
    # Which provider this row is about, when the row is about one.
    #
    # NULL is a *meaning*, not a missing value: "decided before any
    # backend was contacted" — badauth, abuse, 911, notfqdn, nohost, and
    # a hostname whose domain has zero backends. The legacy service has
    # the same property (`event-backend-type-is-null-for-outcomes-
    # decided-before-any-backend`, disposition `preserve`), and it is
    # what makes `backend_type IS NULL` a filter rather than a
    # data-quality complaint.
    #
    # Deliberately not folded into `message`: that column is set on
    # exactly one kind of row, the rate-limit refusal
    # (`event-detail-is-populated-only-for-rate-limit-refusals`, also
    # `preserve`), and a free-text column that sometimes holds a
    # provider name and sometimes a refusal reason cannot be filtered
    # on. Same width as `ddns_domain_backend.backend_type`, which is
    # where the value comes from.
    backend_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The two views this table exists to serve: one tenant's
        # history, and one device's. Leading with the filter column
        # and trailing with the sort key means both are an index scan
        # rather than a filesort.
        Index("ix_ddns_event_user_created", "user_id", "created_at"),
        Index("ix_ddns_event_device_created", "device_id", "created_at"),
        # The retention prune (#17) deletes across every tenant:
        # `WHERE created_at < :cutoff LIMIT :batch`. Neither compound
        # above serves that — they lead with a column the prune does
        # not constrain — so it gets its own.
        Index("ix_ddns_event_created_at", "created_at"),
        # Named in the query surface (plan §3.4). Without it, "which
        # device is this address" is a full scan of the retention
        # window.
        Index("ix_ddns_event_client_ip", "client_ip"),
    )


class RateLimitEvent(HostBase):
    """One row per rate-limited request, keyed to the **device**.

    The legacy service's ``RateLimitEvent``, re-keyed from the user to
    the device (plan §2). It is a table rather than a process-local
    counter for a boring reason: api and worker are separate
    containers and api may be several processes, so an in-memory
    window is per-process, resets on every deploy, and lets a client
    multiply its allowance by the number of workers.

    It is deliberately *not* derived from :class:`DnsEvent`, which
    carries the same ``(device_id, created_at)`` index and would
    superficially do. ``ddns_event``'s retention is an operator-tunable
    setting; hanging the rate limiter off it makes "how long do we
    keep logs" silently also mean "how far back does the limiter
    look".

    **No writer until #16.** Declared here because #16 (`/nic/*` and
    device auth) has ``abuse`` in its acceptance criteria and declares
    no migration in its scope, and this chain admits one revision
    author at a time.
    """

    __tablename__ = "ddns_rate_limit_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ddns_device.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = _utcnow_col(nullable=False)

    __table_args__ = (
        # "how many in the last minute for this device"
        Index("ix_ddns_rate_limit_event_device_created", "device_id", "created_at"),
        # …and the sweep that keeps the table from growing forever.
        Index("ix_ddns_rate_limit_event_created_at", "created_at"),
    )


def include_object(
    obj: Any, name: str, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """``include_object`` for alembic autogenerate on the host chain.

    Atrium and the host write to one MySQL database. With
    ``target_metadata = HostBase.metadata`` alembic sees every atrium
    table as "in the database but not in the model" and proposes a
    ``drop_table`` for each — the most destructive failure mode of the
    host-extension pattern, and one that reads as a perfectly ordinary
    autogenerate diff.

    **It also has to filter cross-base foreign keys, and the scaffolded
    version did not.** ``HostForeignKey`` deliberately registers
    nothing with the mapper, so the four constraints pointing at
    ``users.id`` exist in the database and in no ``MetaData``.
    Alembic reads that as "in the database, not in the model" and
    proposes ``remove_fk`` for every one of them — on *every*
    autogenerate from now on, silently, in a diff that otherwise looks
    empty. Applying it would drop the tenancy foreign keys and leave
    every host row able to outlive its owner.

    This was invisible on the revision that created the tables: a
    brand-new table is an ``add_table`` and its constraints are never
    compared. It only appears from the *second* autogenerate onwards,
    which is the one nobody reviews as carefully.
    ``tests/test_host_models.py`` runs both directions.

    It lives here rather than inside ``alembic/env.py`` because
    ``env.py`` runs migrations at import time and therefore cannot be
    imported by a test. A filter no test can reach is a filter nobody
    finds out is wrong.
    """
    if type_ == "table":
        return name in HostBase.metadata.tables
    if type_ == "foreign_key_constraint":
        target = _fk_target_table(obj)
        # A constraint pointing outside HostBase's metadata is a
        # HostForeignKey. It is emitted from the column markers by
        # `emit_host_foreign_keys` at revision time, never by
        # comparison — so comparison must not have an opinion on it.
        if target is not None and target not in HostBase.metadata.tables:
            return False
    if reflected:
        parent = getattr(obj, "table", None)
        if parent is not None and parent.name not in HostBase.metadata.tables:
            return False
    return True


def _fk_target_table(constraint: Any) -> str | None:
    """Referred table name of a ``ForeignKeyConstraint``, reflected or not.

    ``referred_table`` needs a resolvable target and raises for a
    reflected constraint pointing at a table that is not in the same
    ``MetaData`` — which is every constraint this predicate exists to
    catch. ``target_fullname`` is the unresolved string, so it answers
    for both.
    """
    elements = getattr(constraint, "elements", ())
    for element in elements:
        target = getattr(element, "target_fullname", None)
        if target:
            return target.rsplit(".", 1)[0]
    return None


#: Permission codes seeded by the ``0002`` migration. Exported so the
#: router and the tests read the same list the migration writes,
#: rather than each spelling the strings again.
PERMISSIONS: tuple[str, ...] = (
    "atrium_ddns.domain.manage",
    "atrium_ddns.device.manage",
    "atrium_ddns.hostname.manage",
    "atrium_ddns.admin",
    "atrium_ddns.events.read.all",
)

#: Role code -> permission codes, as passed to ``seed_permissions_sync``.
#: ``super_admin`` is auto-granted every seeded permission by atrium and
#: is therefore absent here on purpose; an entry for it is skipped.
PERMISSION_GRANTS: dict[str, tuple[str, ...]] = {
    # The three `.manage` permissions are "own rows" — they are what an
    # ordinary tenant needs to run their own zones, and the row-level
    # filtering is the host Scope's job (#14), not the permission's.
    "user": (
        "atrium_ddns.domain.manage",
        "atrium_ddns.device.manage",
        "atrium_ddns.hostname.manage",
    ),
    # `admin` gets the same three plus the two cross-tenant codes.
    "admin": PERMISSIONS,
}


__all__ = [
    "DNS_NAME_LEN",
    "EMAIL_LEN",
    "IPV4_LEN",
    "IPV6_LEN",
    "PERMISSIONS",
    "PERMISSION_GRANTS",
    "AtriumDdnsState",
    "Device",
    "DnsEvent",
    "Domain",
    "DomainBackend",
    "HostBase",
    "Hostname",
    "RateLimitEvent",
    "TimestampMixin",
    "include_object",
]
