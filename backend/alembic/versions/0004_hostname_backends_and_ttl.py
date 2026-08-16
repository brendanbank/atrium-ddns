"""per-hostname backend selection and TTL

Revision ID: 0004_hostname_backends_and_ttl
Revises: 0003_ddns_event_backend_type
Create Date: 2026-08-16

Closes the last two legacy routes of the hostname group (#74). Both of
them needed a schema change rather than a page: under ``0002`` a
hostname publishes to *every* backend bound to its zone, with no column
and no join table to say otherwise, and its TTL lives on the binding.

**This revision writes no rows, and that is the decision, not an
omission.**

Introducing a selection table makes explicit something that is
currently implicit — "publishes to all of the domain's backends" — and
there are exactly two ways to keep the existing fleet publishing:

1. **Backfill.** Insert one ``ddns_hostname_backend`` row per (existing
   hostname × its domain's backends) here, and read an empty selection
   as "publish nowhere".
2. **Inherit.** Write nothing, and read an empty selection as "every
   backend on the domain".

**Inherit is what this takes**, for three reasons and not merely
because it is less work:

* It is what the legacy service does. ``models.py``'s
  ``Hostname.get_backends()`` is ``if self.backends: return
  self.backends`` / ``return self.domain.backends``, and the frozen
  case ``backends-empty-selection-resolves-to-all-of-the-domains-
  backends`` records it as ``preserve``. The migration source of truth
  already answers this question; disagreeing with it would be a
  behaviour change smuggled in as a schema change.
* **Backfill freezes the answer at migration time.** Under it, a
  backend added to a zone *after* the cutover would be published to by
  new hostnames and not by old ones, with nothing anywhere saying so —
  the same silence this revision exists to avoid, moved six months into
  the future.  ``backends-empty-selection-tracks-the-domain-live`` is
  ``preserve`` and is precisely this property.
* A backfill of every (hostname × backend) pair is a write against
  production data taken during a migration, and it cannot be undone by
  ``downgrade`` — dropping the table afterwards is indistinguishable
  from never having populated it.

The cost is stated where it is paid: "publish nowhere" becomes
unspellable (see :class:`~atrium_ddns.models.HostnameBackend`), because
an empty selection is now a sentence about the domain rather than about
the name.

The assertion is in ``backend/tests/test_router_hostname_backends.py``
§0, against a hostname row inserted naming only the columns that
existed at ``0003`` — which is *exactly* the state an existing row is
left in by the two statements below, since ``ttl`` is added NULL with
no server default and ``ddns_hostname_backend`` is created empty. That
test asserts both halves: that the reconstruction is faithful (read out
of ``information_schema``, and out of this module's own AST for the
absence of any data-writing statement) and that such a row publishes to
every backend of its zone.

The independent second reading is the frozen 124-case wire table, whose
fixture seeds twelve hostnames through
``atrium_ddns.scripts.seed_compat_fixture`` — a script that knows
nothing about this table and writes no row into it. Three of its cases
(``allnochg``, ``mixed``, ``firsterr``) aggregate across two or three
backends, so "empty means nowhere" would take them from ``nochg`` /
``good`` / ``dnserr`` to ``911``. Green there is a statement about rows
created by something that has never heard of the feature.

``ALTER TABLE ... ADD COLUMN`` for a nullable column with no default is
INSTANT on MySQL 8, so the ``ddns_hostname`` half does not rebuild the
table.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_hostname_backends_and_ttl"
down_revision: str | None = "0003_ddns_event_backend_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `DEFAULT CURRENT_TIMESTAMP(6)`, matching every other host table's
# `created_at`. No `ON UPDATE`: a selection row is inserted and deleted,
# never edited (see the model), so there is nothing for it to track.
_CREATED = sa.text("CURRENT_TIMESTAMP(6)")


def upgrade() -> None:
    # NULL means *inherit the binding's* `config['ttl']`, which itself
    # falls back to providers.DEFAULT_TTL. Deliberately not `NOT NULL
    # DEFAULT 60`: that would be indistinguishable from an operator
    # having chosen 60 for every name, and would detach every existing
    # hostname from its zone's setting on the day this ran.
    op.add_column(
        "ddns_hostname",
        sa.Column("ttl", sa.Integer(), nullable=True),
    )

    op.create_table(
        "ddns_hostname_backend",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("hostname_id", sa.BigInteger(), nullable=False),
        sa.Column("backend_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.dialects.mysql.DATETIME(fsp=6),
            server_default=_CREATED,
            nullable=False,
        ),
        # Both CASCADE: a selection row has no meaning without either
        # end. Unbinding a backend from a zone therefore withdraws it
        # from every hostname that had selected it, rather than leaving
        # a dangling id that would have to be filtered out at read time.
        sa.ForeignKeyConstraint(
            ["hostname_id"], ["ddns_hostname.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["backend_id"], ["ddns_domain_backend.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "hostname_id", "backend_id", name="uq_ddns_hostname_backend_pair"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    # "which names use this backend" — the direction the UNIQUE above
    # cannot serve, and the one the cascade and the editor both take.
    op.create_index(
        op.f("ix_ddns_hostname_backend_backend_id"),
        "ddns_hostname_backend",
        ["backend_id"],
        unique=False,
    )


def downgrade() -> None:
    # Dropping the table discards every selection, which is not
    # reversible and is the honest shape of undoing this: at `0003` the
    # schema has nowhere to hold one. A hostname that had been narrowed
    # to a subset goes back to publishing to all of its zone's
    # backends, which is the `0003` behaviour by definition.
    op.drop_index(
        op.f("ix_ddns_hostname_backend_backend_id"),
        table_name="ddns_hostname_backend",
    )
    op.drop_table("ddns_hostname_backend")
    op.drop_column("ddns_hostname", "ttl")
