"""Host-side ORM models.

The host owns its own ``DeclarativeBase`` (and therefore its own
``MetaData``) so atrium's alembic chain never sees this table. The
host's alembic chain manages it under a separate version table
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
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Integer, String, text
from sqlalchemy.dialects.mysql import DATETIME as MysqlDATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class HostBase(DeclarativeBase):
    """Host metadata, separate from atrium's ``app.db.Base``."""


class AtriumDdnsState(HostBase):
    """Singleton row (id=1) for the demo widget. Replace with your
    real domain models — and remove the corresponding migration row
    when you do."""

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
