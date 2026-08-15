"""atrium_ddns initial schema + brand seed

Revision ID: 0001_init
Revises:
Create Date: 2026-04-28
"""
from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.auth.rbac_seed import seed_permissions_sync
from sqlalchemy.dialects.mysql import DATETIME as MysqlDATETIME

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Demo singleton table — delete this and `atrium_ddns/models.py`
    # once you've added your real domain models. The router and the
    # frontend widget reference it; remove those callsites at the
    # same time.
    op.create_table(
        "atrium_ddns_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column(
            "message",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("'Welcome to Atrium Ddns'"),
        ),
        sa.Column(
            "counter",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            MysqlDATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
    )

    # Singleton row so the demo route doesn't have to lazy-create.
    op.execute(
        "INSERT INTO atrium_ddns_state (id, message, counter) "
        "VALUES (1, 'Welcome to Atrium Ddns', 0)"
    )

    # Permissions belong with the schema. The runtime form
    # (`seed_permissions`) exists for hosts that discover permissions
    # at startup; for static codes, the migration form is the natural
    # fit. Atrium auto-grants every seeded permission to super_admin;
    # `grants` adds extra role bindings.
    seed_permissions_sync(
        op.get_bind(),
        ["atrium_ddns.read", "atrium_ddns.write"],
        grants={"admin": ["atrium_ddns.write"]},
    )

    # Materialise an initial BrandConfig so the SPA renders with the
    # host's name + primary colour from the first page load. Atrium
    # exposes this via /app-config and re-reads it within the 2 s
    # cache TTL when the admin tweaks branding live.
    brand = json.dumps(
        {
            "name": "Atrium Ddns",
            "preset": "default",
            "overrides": {"primaryColor": "blue"},
        }
    )
    op.execute(
        sa.text(
            "INSERT INTO app_settings (`key`, value) VALUES ('brand', :v) "
            "ON DUPLICATE KEY UPDATE value = :v"
        ).bindparams(v=brand)
    )


def downgrade() -> None:
    # Permissions intentionally left in place — they're cheap to keep
    # and removing them would orphan any UI that still references the
    # code. Brand seed left alone for the same reason.
    op.drop_table("atrium_ddns_state")
