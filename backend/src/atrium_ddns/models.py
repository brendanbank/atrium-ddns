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
    DateTime,
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
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

# The legacy TTL form's ``NumberRange(min=30, max=86400)``, inclusive at
# both ends — frozen as ``ttl-below-30-is-rejected``,
# ``ttl-above-86400-is-rejected`` and ``ttl-30-and-86400-are-accepted``,
# all ``preserve``. They bound what the *editing surface* accepts, not
# what the write path sends: ``ttl-is-not-validated-on-the-ddns-write-
# path`` is also ``preserve``, so a row written by something other than
# the form (the importer, a hand-typed UPDATE) reaches the provider
# unchanged. Enforced in the request model, deliberately not in the
# column.
TTL_MIN = 30
TTL_MAX = 86400


#: ``DATETIME(6)`` on MySQL, plain ``DATETIME`` elsewhere.
#:
#: The suite builds its schema from this metadata on in-memory SQLite, so
#: a bare ``mysql.DATETIME`` made the models unloadable there and forced
#: every backend test through a real MySQL in a container — which is what
#: made ten xdist workers share one database, which is what produced the
#: races in #117 and #149. Production DDL is unaffected: alembic owns it
#: and still emits ``DATETIME(6)`` verbatim.
#:
#: The same `with_variant` shape `SecretBlob` already uses below.
UTC_DATETIME = DateTime().with_variant(MysqlDATETIME(fsp=6), "mysql")


def _utcnow_col(**kwargs: Any) -> Any:
    """A ``DATETIME(6)`` defaulting to the server clock.

    Microsecond precision rather than atrium's whole-second
    ``DateTime``: ``ddns_event`` is paged by ``(user_id, created_at)``
    and a router bursting several hostnames writes several rows inside
    one second. At ``fsp=0`` their order is not recoverable and
    keyset pagination over the compound index double-counts.
    """
    return mapped_column(
        UTC_DATETIME,
        # `func.now()` rather than `CURRENT_TIMESTAMP(6)`: the literal is a
        # syntax error in SQLite DDL. Only `create_all` reads this — the
        # deployed default comes from the migration.
        server_default=func.now(),
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
        nullable=False, server_onupdate=func.now()
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
        UTC_DATETIME,
        nullable=False,
        server_default=func.now(),
        server_onupdate=func.now(),
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
        UTC_DATETIME, nullable=True
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

    # Per-hostname record TTL. The legacy schema had this column and the
    # first cut of this one did not, which is why `import_legacy` had to
    # fold it onto the *binding* and refuse a zone whose names disagreed.
    # `ttl-is-stored-per-hostname-not-per-domain` is `preserve`.
    #
    # **NULL means inherit**, and inherit resolves to
    # `ddns_domain_backend.config['ttl']`, which itself falls back to
    # `providers.DEFAULT_TTL` (60 — the same number as the legacy
    # column's default, `ttl-default-is-60`). NULL is not 0 and not 60:
    # a hostname that has never been given a TTL follows whatever the
    # binding says, including a later change to it, and a hostname set
    # explicitly to 60 does not. Collapsing the two would silently
    # detach every existing name from its zone's setting the first time
    # anyone opened the editor.
    ttl: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # What we last successfully wrote. Persisted only when the
    # aggregate is `good` — a `nochg` leaves all three untouched, and
    # the compat table asserts that.
    last_ip_v4: Mapped[str | None] = mapped_column(String(IPV4_LEN), nullable=True)
    last_ip_v6: Mapped[str | None] = mapped_column(String(IPV6_LEN), nullable=True)
    last_updated_at: Mapped[datetime | None] = mapped_column(
        UTC_DATETIME, nullable=True
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
        UTC_DATETIME, nullable=True
    )
    dns_check_error: Mapped[str | None] = mapped_column(String(255), nullable=True)

    domain: Mapped[Domain] = relationship(back_populates="hostnames")
    device: Mapped[Device | None] = relationship(back_populates="hostnames")

    #: The rows of :class:`HostnameBackend` for this name. Managed
    #: directly (the endpoint replaces the set wholesale), which is why
    #: :attr:`selected_backends` beside it is ``viewonly``: two writable
    #: relationships over one table is the ``overlaps`` warning and,
    #: worse, two flush orders for one set of rows.
    selections: Mapped[list[HostnameBackend]] = relationship(
        back_populates="hostname",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    #: The selected backends themselves, for reading. **Empty is not
    #: "none"** — see :func:`resolve_backends`, which is the only thing
    #: that should ever interpret it.
    #:
    #: ``order_by`` matches ``Domain.backends`` because
    #: ``backends-resolution-order-decides-the-aggregate-error`` is
    #: ``preserve`` and the frozen table's ``firsterr.example.com`` case
    #: fails if the order moves. :func:`resolve_backends` re-imposes the
    #: domain's own order anyway, so this is belt and braces rather than
    #: the mechanism.
    selected_backends: Mapped[list[DomainBackend]] = relationship(
        secondary="ddns_hostname_backend",
        order_by="DomainBackend.id",
        viewonly=True,
    )

    __table_args__ = (
        UniqueConstraint("name", name="uq_ddns_hostname_name"),
        # The health-check job sweeps "hostnames not checked since X".
        Index("ix_ddns_hostname_dns_checked_at", "dns_checked_at"),
    )


class HostnameBackend(HostBase):
    """One row of *this name publishes to that binding*.

    The legacy ``hostname_backends`` association table, brought across
    with its semantics intact rather than reinvented — and the semantics
    are the whole point of the table, because they are not the ones the
    shape suggests.

    **An empty selection means "every backend on the domain", not "no
    backends".** That is ``backends-empty-selection-resolves-to-all-of-
    the-domains-backends``, disposition ``preserve``, and it is also the
    only reading under which introducing this table is safe: every
    hostname that exists today has no row here, so the alternative
    reading would stop the entire migrated fleet publishing at the
    moment ``0004`` runs — silently, because a router that gets
    ``911`` logs it and keeps going and nothing on the board says
    "nobody is publishing this any more".

    The consequence, stated because it is a real cost and not a
    detail: **"publish nowhere" is not spellable.** Clearing the
    selection restores the inherit behaviour rather than muting the
    name. The way to stop publishing a name is to delete it, or to
    unbind the backend from the zone. The legacy service had the same
    property for the same reason, and a fourth state
    (selection-is-empty-and-that-means-nothing) would need a NOT NULL
    discriminator column on ``ddns_hostname`` and a migration that
    backfills it — which is precisely the backfill this reading avoids.

    Both foreign keys are ``ON DELETE CASCADE``, so unbinding a backend
    from a zone takes every hostname's selection of it with it — which
    is what makes :func:`resolve_backends`'s filter belt-and-braces
    rather than load-bearing.

    **A surrogate ``id`` with a UNIQUE on the pair, not a composite
    primary key.** The pair is what is unique and the composite key
    says so more directly, but every other table here is keyed on a
    ``BigInteger id`` and so is every generic guard written against
    them — ``DdnsScope.get`` takes ``pk_attr="id"``, and
    ``test_tenant_isolation`` parameterises one case per registered
    model over exactly that. A model that needs each of those guards
    special-cased is a model that quietly stops being covered by the
    next one somebody adds. The UNIQUE constraint keeps the property
    the composite key would have enforced.

    No ``user_id``: the owner is the hostname's domain's owner, two
    hops away (``scope.TENANT_PATHS``), and a second copy of ownership
    is a second source of truth for tenancy. No ``updated_at`` either —
    a selection row is created and destroyed, never edited, so the
    column could only ever equal ``created_at``. ``created_at`` itself
    stays, because *when* a name was pinned to a subset is the question
    somebody asks after a zone stops answering.
    """

    __tablename__ = "ddns_hostname_backend"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hostname_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ddns_hostname.id", ondelete="CASCADE"),
        nullable=False,
    )
    backend_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ddns_domain_backend.id", ondelete="CASCADE"),
        nullable=False,
        # The UNIQUE below leads with `hostname_id` and serves "which
        # backends does this name use". "Which names use this backend"
        # — what the FK's cascade does on unbind, and what the editor
        # wants before removing a binding — leads with the other column
        # and would be a scan without this.
        index=True,
    )
    created_at: Mapped[datetime] = _utcnow_col(nullable=False)

    hostname: Mapped[Hostname] = relationship(back_populates="selections")

    __table_args__ = (
        UniqueConstraint(
            "hostname_id", "backend_id", name="uq_ddns_hostname_backend_pair"
        ),
    )


def resolve_backends(hostname: Hostname) -> list[DomainBackend]:
    """The backends ``hostname`` publishes to. **The one resolver.**

    The rewrite of the legacy ``Hostname.get_backends()``, and it is a
    module-level function rather than a method for the same reason
    ``zone_contains`` is: there must be exactly one of it, reachable by
    identity from both the wire path (:mod:`atrium_ddns.router_nic`) and
    the editing path (:mod:`atrium_ddns.router`), so the two cannot
    answer differently about the same row. V1M3's clearest lesson was a
    second validator that agreed on the day it was written;
    ``test_router_hostname_backends.py`` asserts the two call sites hold
    the same function object.

    Three properties, each with a frozen case behind it:

    * **empty selection -> every backend on the domain**
      (``backends-empty-selection-resolves-to-all-of-the-domains-
      backends``). This is the production branch and the reason the
      ``0004`` migration needs no backfill.
    * **non-empty selection -> exactly those**
      (``backends-explicit-selection-wins``).
    * **the domain's order, either way**
      (``backends-resolution-order-decides-the-aggregate-error``). The
      selected set is filtered *out of* ``domain.backends`` rather than
      returned as loaded, so the aggregate's "first status that is
      neither good nor nochg" walks the same order in both branches —
      and a selection row naming a backend that is no longer on this
      domain (impossible through the API, reachable by a direct write)
      cannot widen the set.

    **Both relationships must already be loaded.** This is a pure
    function over ORM state: it issues no IO and it cannot, because a
    lazy load inside it would be a synchronous database call on the
    event loop (``MissingGreenlet``, not a slow query). Callers use
    ``selectinload(Hostname.domain).selectinload(Domain.backends)`` and
    ``selectinload(Hostname.selected_backends)``.
    """
    chosen = {backend.id for backend in hostname.selected_backends}
    if not chosen:
        return list(hostname.domain.backends)
    return [
        backend for backend in hostname.domain.backends if backend.id in chosen
    ]


def resolve_ttl(hostname: Hostname, backend: DomainBackend) -> Any:
    """The TTL to publish ``hostname`` at through ``backend``, or ``None``.

    Two levels and a default, in this order:

    1. ``ddns_hostname.ttl`` — the per-name override. NULL means
       *inherit*, and NULL is the state every row is in until somebody
       edits one.
    2. ``ddns_domain_backend.config['ttl']`` — where the rewrite put the
       TTL when it had no per-name column. Still the fallback rather
       than dead weight: it is what the importer writes, and it is the
       right place for "this provider wants a different TTL from that
       one".
    3. ``None``, meaning *the caller's default* — resolved to
       :data:`atrium_ddns.providers.DEFAULT_TTL` by ``router_nic``,
       which is the only place that knows what a missing TTL costs.

    Returns the raw stored value without range-checking or coercing it;
    ``ttl-is-not-validated-on-the-ddns-write-path`` is ``preserve`` and
    ``router_nic._backend_plan`` already owns the int coercion and the
    ``ddns.backend.bad_ttl`` warning for a value that is not one.
    """
    if hostname.ttl is not None:
        return hostname.ttl
    return (backend.config or {}).get("ttl")


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
    "TTL_MAX",
    "TTL_MIN",
    "AtriumDdnsState",
    "Device",
    "DnsEvent",
    "Domain",
    "DomainBackend",
    "HostBase",
    "Hostname",
    "HostnameBackend",
    "RateLimitEvent",
    "TimestampMixin",
    "include_object",
    "resolve_backends",
    "resolve_ttl",
]
