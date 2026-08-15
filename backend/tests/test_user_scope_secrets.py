"""Per-user secrets (atrium >= 0.28) — the four properties this host depends on.

Atrium has its own suite for `app.host_sdk.crypto`, and it passes. This file
exists anyway, because a feature tested only by its author is tested on its
author's terms. These assert the properties *atrium-ddns* relies on, in the
shape atrium-ddns uses them:

  1. A round trip with **no authenticated user anywhere in the flow**. This is
     the whole reason atrium#227 was filed: `/nic/update` is a home router on
     HTTP Basic, authenticated as a device, and the domain rows it touches carry
     provider credentials that must decrypt with no session, no cookie, no PAT.
     If this test ever fails, the DDNS hot path is dead.
  2. Touching a locked value raises loudly rather than hanging or returning
     junk. Unlocking is a database read in an async app; the failure has to be
     legible.
  3. One tenant's ciphertext will not decrypt in another tenant's row — the
     bad-import-script and database-write-attacker case.
  4. Shredding actually destroys, asserted against ciphertext captured
     **before** the shred. A test that shreds and then reads the row proves
     only that the row changed. The stand-in for a backup tape is the only
     version of this claim worth making, and it is the property the whole
     `scope="user"` decision was taken for.

Requires a live database, so it runs inside the api container via
`make test-backend`.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db import get_session_factory
from app.host_sdk.crypto import (
    SecretBlob,
    SecretDecryptError,
    SecretLockedError,
    SecretShreddedError,
    UserSecret,
    shred_user_key,
    unlock_user_secrets,
)
from app.host_sdk.db import HostForeignKey
from app.models.auth import User

pytestmark = pytest.mark.asyncio

SECRET_A = "route53-key-for-tenant-A"
SECRET_B = "hetzner-token-for-tenant-B"


def _worker_id() -> str:
    """xdist worker name (`gw0`, `gw1`, …), or `serial` under `-n 0`.

    Everything this module creates in the database is namespaced by it. The
    suite runs `-n auto` by default, and two workers sharing a table name or a
    user row produce failures that look like flakiness and are actually
    collisions.
    """
    return os.environ.get("PYTEST_XDIST_WORKER", "serial")


PROBE_TABLE = f"test_user_secret_probe_{_worker_id()}"


class _ProbeBase(DeclarativeBase):
    """Own metadata — never atrium's Base, never the host's HostBase.

    This is a throwaway fixture table; putting it on HostBase would make it
    visible to `alembic revision --autogenerate` and it would land in a real
    migration.
    """


class Probe(_ProbeBase):
    __tablename__ = PROBE_TABLE

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(HostForeignKey("users.id"))
    secret_ct: Mapped[bytes | None] = mapped_column(SecretBlob(), nullable=True)

    # Two lines, not one: SQLAlchemy column types never see the row they
    # belong to, and the row is where the owner lives.
    secret = UserSecret(
        purpose="probe.secret", owner_attr="user_id", column="secret_ct"
    )


def _password_hash() -> str:
    from fastapi_users.password import PasswordHelper

    return PasswordHelper().hash("unusable-" + "x" * 24)


@pytest_asyncio.fixture
async def tenants():
    """Two throwaway users and a probe table, both namespaced per worker.

    **Always creates its own users; never reuses an existing one.** An earlier
    version picked up whatever two rows `SELECT * FROM users LIMIT 2` returned,
    which meant `test_shredding_...` destroyed the *seeded admin's* encryption
    key — permanently, by design, that being the property under test. On a
    developer's own stack that is real data. A test that needs to destroy
    something must create the thing it destroys.
    """
    factory = get_session_factory()
    w = _worker_id()
    emails = [f"probe-tenant-a-{w}@example.invalid", f"probe-tenant-b-{w}@example.invalid"]

    async with factory() as s:
        # Left over from a run that died mid-test; MySQL DDL is not
        # transactional, so teardown is not guaranteed to have happened.
        await s.execute(sa.text(f"DROP TABLE IF EXISTS {PROBE_TABLE}"))
        for email in emails:
            await s.execute(sa.text("DELETE FROM users WHERE email = :e"), {"e": email})
        await s.commit()

    ids: list[int] = []
    async with factory() as s:
        for email in emails:
            u = User(
                email=email,
                hashed_password=_password_hash(),
                is_active=True,
                is_verified=True,
                full_name=f"Probe tenant {w}",
                preferred_language="en",
            )
            s.add(u)
            await s.flush()
            ids.append(u.id)
        await s.run_sync(
            lambda c: Probe.__table__.create(c.connection(), checkfirst=True)
        )
        await s.commit()

    yield ids[0], ids[1]

    async with factory() as s:
        await s.execute(sa.text(f"DROP TABLE IF EXISTS {PROBE_TABLE}"))
        await s.execute(
            sa.text("DELETE FROM user_secret_keys WHERE user_id IN (:a, :b)"),
            {"a": ids[0], "b": ids[1]},
        )
        for email in emails:
            await s.execute(sa.text("DELETE FROM users WHERE email = :e"), {"e": email})
        await s.commit()


async def _seed(uid_a: int, uid_b: int) -> tuple[int, int]:
    factory = get_session_factory()
    async with factory() as s:
        await unlock_user_secrets(s, uid_a, create=True)
        await unlock_user_secrets(s, uid_b, create=True)
        a, b = Probe(user_id=uid_a), Probe(user_id=uid_b)
        s.add_all([a, b])
        a.secret = SECRET_A
        b.secret = SECRET_B
        await s.commit()
        return a.id, b.id


async def test_round_trip_without_authenticated_user(tenants):
    """The requirement that shaped atrium#227.

    A bare coroutine: no request, no cookie, no PAT, no ContextVar set by an
    auth dependency. The owner comes from the row being decrypted. This is the
    `/nic/update` shape exactly.
    """
    uid_a, uid_b = tenants
    id_a, _ = await _seed(uid_a, uid_b)

    async with get_session_factory()() as s:
        row = await s.get(Probe, id_a)
        await unlock_user_secrets(s, row.user_id)  # owner read from the ROW
        assert row.secret.reveal() == SECRET_A


async def test_locked_access_raises_and_names_the_fix(tenants):
    uid_a, uid_b = tenants
    id_a, _ = await _seed(uid_a, uid_b)

    async with get_session_factory()() as s:
        row = await s.get(Probe, id_a)
        with pytest.raises(SecretLockedError) as exc:
            row.secret.reveal()
        # A mystery here costs an afternoon; the message must say what to call.
        assert "unlock_user_secrets" in str(exc.value)


async def test_ciphertext_does_not_decrypt_in_another_tenants_row(tenants):
    """Bad import script, or someone with write access to the database."""
    uid_a, uid_b = tenants
    id_a, id_b = await _seed(uid_a, uid_b)

    factory = get_session_factory()
    async with factory() as s:
        ct_a = (
            await s.execute(
                sa.text(f"SELECT secret_ct FROM {PROBE_TABLE} WHERE id = :i"),
                {"i": id_a},
            )
        ).scalar_one()
        await s.execute(
            sa.text(f"UPDATE {PROBE_TABLE} SET secret_ct = :c WHERE id = :i"),
            {"c": ct_a, "i": id_b},
        )
        await s.commit()

    async with factory() as s:
        row = await s.get(Probe, id_b)
        await unlock_user_secrets(s, row.user_id)
        with pytest.raises(SecretDecryptError):
            row.secret.reveal()


async def test_shredding_destroys_ciphertext_captured_before_the_shred(tenants):
    """The property the whole scope="user" decision was taken for.

    Capture the bytes first — that copy stands in for a backup taken while the
    account still existed. Reading the row *after* a shred proves only that the
    row changed.
    """
    uid_a, uid_b = tenants
    id_a, _ = await _seed(uid_a, uid_b)
    factory = get_session_factory()

    async with factory() as s:
        backup_ct = (
            await s.execute(
                sa.text(f"SELECT secret_ct FROM {PROBE_TABLE} WHERE id = :i"),
                {"i": id_a},
            )
        ).scalar_one()
        assert backup_ct, "nothing was stored — the rest of this test is vacuous"
        assert await shred_user_key(s, uid_a) is True
        await s.commit()

    # Restore the pre-shred bytes, then try to read them.
    async with factory() as s:
        await s.execute(
            sa.text(f"UPDATE {PROBE_TABLE} SET secret_ct = :c WHERE id = :i"),
            {"c": backup_ct, "i": id_a},
        )
        await s.commit()

    async with factory() as s:
        row = await s.get(Probe, id_a)
        with pytest.raises((SecretShreddedError, SecretDecryptError, SecretLockedError)):
            await unlock_user_secrets(s, row.user_id)
            row.secret.reveal()
