"""ddns_event.backend_type — which provider a log row is about

Revision ID: 0003_ddns_event_backend_type
Revises: 0002_ddns_core
Create Date: 2026-08-15

Closes the one gap #16 named and could not close: ``ddns_event`` had no
``backend_type`` column, so the frozen model case
``event-backend-type-is-null-for-outcomes-decided-before-any-backend``
(disposition ``preserve``) was not expressible on this schema.

**The value already existed at the write site.** ``_record_hostname_events``
in ``router_nic.py`` iterates ``result.attempts``, which is a sequence of
``(backend_type, status)`` pairs — one row per backend attempt — and the
first element was being unpacked into ``_backend_type`` and thrown away
for want of somewhere to put it. So this is a column with a writer from
the same commit, not an artefact waiting for one.

**NULL is a meaning, not an absence.** ``backend_type IS NULL`` selects
the rows decided before any provider was contacted: ``badauth``,
``abuse``, ``911``, ``notfqdn``, ``nohost``, and a hostname whose domain
carries zero backends. That is the legacy service's own behaviour and
the case above records it as ``preserve``.

**Not folded into ``message``.** ``message`` is populated on exactly one
kind of row — the rate-limit refusal
(``event-detail-is-populated-only-for-rate-limit-refusals``, also
``preserve``) — and a free-text column that sometimes carries a provider
name and sometimes a refusal reason is not something a filter can index.

**No index.** Plan §3.4's query surface does not list the provider, and
the two compound indexes lead with ``user_id`` / ``device_id``; an index
whose only reader is a hypothetical future filter is the artefact this
project keeps catching. Add one with the query that needs it.

``ALTER TABLE ... ADD COLUMN`` on MySQL 8 is an INSTANT operation for a
nullable column with no default, so this does not rebuild the table.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_ddns_event_backend_type"
down_revision: str | None = "0002_ddns_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ddns_event",
        # Same width as ddns_domain_backend.backend_type, which is where
        # the value is read from. A narrower column here would truncate
        # a provider name that the row it came from stores in full.
        sa.Column("backend_type", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ddns_event", "backend_type")
