"""The host schema, checked against the database rather than against itself.

Every claim in this file is made twice where it can be: once against
what ``atrium_ddns.models`` declares, and once against what MySQL
actually holds after ``alembic upgrade head``. A model and a migration
generated from that model share an author, so a test that compares
them to each other can only find typos.

The pairs, and what each one would miss on its own:

===========================================  ==========================================
instrument A (the models / the ORM)          instrument B (the live database)
===========================================  ==========================================
``HostForeignKey`` markers on ``Column.info``  ``information_schema.REFERENTIAL_CONSTRAINTS``
``server_onupdate=`` on the mixin              ``information_schema.COLUMNS.EXTRA``
``models.PERMISSIONS``                         rows in ``permissions`` / ``role_permissions``
``UserSecret`` descriptor registry             a ciphertext round trip through a real row
===========================================  ==========================================

Requires a live database, so it runs inside the api container via
``make test-backend``.

**Everything created here is namespaced by ``PYTEST_XDIST_WORKER``.**
Zone names, device usernames and user emails are all globally unique by
construction, so ten workers sharing one MySQL collide on every one of
them, and the collisions read as flakiness.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from app.db import get_session_factory
from app.host_sdk.crypto import (
    SecretBlob,
    SecretDecryptError,
    unlock_user_secrets,
)
from app.host_sdk.crypto import _USER_SECRETS  # noqa: PLC2701 — the registry is the point
from app.host_sdk.db import INFO_KEY
from app.models.auth import User

from atrium_ddns import models as m

pytestmark = pytest.mark.asyncio

CREDS_A = {"access_key": "AKIA-tenant-A", "secret_key": "s3cret-A", "region": "eu-west-1"}
CREDS_B = {"token": "hetzner-token-for-tenant-B"}


def _worker_id() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER", "serial")


W = _worker_id()

# Host tables this issue owns, as the migration creates them. Derived
# from the metadata rather than typed out, minus the scaffold's demo
# table which predates this revision.
HOST_TABLES = frozenset(m.HostBase.metadata.tables) - {"atrium_ddns_state"}


def _password_hash() -> str:
    from fastapi_users.password import PasswordHelper

    return PasswordHelper().hash("unusable-" + "x" * 24)


@pytest_asyncio.fixture
async def tenants():
    """Two users with a domain, a backend, a device and a hostname each.

    Creates its own users and destroys them afterwards. It must never
    adopt an existing row: one of the tests below hard-deletes a user
    to prove the ``ON DELETE CASCADE``, and on a developer's own stack
    the two rows a ``LIMIT 2`` returns are real accounts.
    """
    factory = get_session_factory()
    emails = [f"ddns-model-a-{W}@example.invalid", f"ddns-model-b-{W}@example.invalid"]

    await _purge(emails)

    ids: list[int] = []
    async with factory() as s:
        for email in emails:
            u = User(
                email=email,
                hashed_password=_password_hash(),
                is_active=True,
                is_verified=True,
                full_name=f"DDNS model probe {W}",
                preferred_language="en",
            )
            s.add(u)
            await s.flush()
            ids.append(u.id)
        await s.commit()

    built: dict[str, dict] = {}
    async with factory() as s:
        for tag, uid, creds in (("a", ids[0], CREDS_A), ("b", ids[1], CREDS_B)):
            await unlock_user_secrets(s, uid, create=True)
            domain = m.Domain(user_id=uid, name=f"{tag}-{W}.example.invalid")
            s.add(domain)
            await s.flush()
            backend = m.DomainBackend(
                domain_id=domain.id, user_id=uid, backend_type="stub"
            )
            backend.credentials = creds
            device = m.Device(
                user_id=uid,
                username=f"ddns-model-{tag}-{W}",
                password_hash="$argon2id$v=19$m=65536,t=3,p=4$fake$fake",
                name=f"router-{tag}",
            )
            s.add_all([backend, device])
            await s.flush()
            hostname = m.Hostname(
                domain_id=domain.id,
                device_id=device.id,
                name=f"home.{tag}-{W}.example.invalid",
            )
            s.add(hostname)
            await s.flush()
            built[tag] = {
                "user_id": uid,
                "email": emails[0 if tag == "a" else 1],
                "domain_id": domain.id,
                "backend_id": backend.id,
                "device_id": device.id,
                "hostname_id": hostname.id,
            }
        await s.commit()

    yield built

    await _purge(emails)


async def _purge(emails: list[str]) -> None:
    """Remove anything a previous run left behind.

    MySQL DDL and a killed worker do not cooperate, so teardown is not
    guaranteed to have run. Deleting the users cascades to their
    domains, backends and devices; the event rows only get their FKs
    nulled, so they are cleared by their denormalised email.

    **The orphan sweep of ``user_secret_keys`` was removed** (#14). It
    read ``DELETE FROM user_secret_keys WHERE user_id NOT IN (SELECT id
    FROM users)`` and could never delete a row: that table's foreign key
    is ``ON DELETE CASCADE`` to ``users``, which is asserted by
    ``test_deleting_a_user_destroys_their_domains_devices_and_credentials``
    — so the orphan state it swept up is one the database does not
    permit. What it *could* do is take a scan-wide lock over every
    worker's key rows, and two workers running it at once deadlock
    (``(1213, 'Deadlock found when trying to get lock')``). Harmless
    with two test files sharing the database, 3 not-green runs in 20
    with three.
    """
    factory = get_session_factory()
    async with factory() as s:
        await s.execute(
            sa.text("DELETE FROM ddns_event WHERE user_email IN :e").bindparams(
                sa.bindparam("e", expanding=True)
            ),
            {"e": emails},
        )
        for email in emails:
            await s.execute(
                sa.text("DELETE FROM users WHERE email = :e"), {"e": email}
            )
        await s.commit()


# --------------------------------------------------------------------- #
# 1. Metadata isolation and the autogenerate filter
# --------------------------------------------------------------------- #


async def test_host_tables_are_not_on_atriums_metadata():
    """Stronger than ``test_smoke``'s identity check.

    ``HostBase.metadata is not Base.metadata`` is necessary and not
    sufficient — a model can be declared on the wrong base and still
    satisfy it, because the *other* base is the one that grew a table.
    Check the table names on both sides.
    """
    from app.db import Base as AtriumBase

    assert m.HostBase.metadata is not AtriumBase.metadata
    assert HOST_TABLES, "no host tables declared — the rest of this file is vacuous"
    overlap = HOST_TABLES & set(AtriumBase.metadata.tables)
    assert not overlap, f"host tables declared on atrium's metadata: {sorted(overlap)}"


#: Prefix of the throwaway tables other modules in this suite create and
#: drop while this one runs. ``test_user_scope_secrets.py`` builds
#: ``test_user_secret_probe_<worker>`` per fixture, and the suite is
#: parallel by default.
SCRATCH_TABLE_PREFIX = "test_"


def _include_name(name, type_: str, parent_names) -> bool:  # noqa: ANN001
    """Keep autogenerate's *reflection* off other workers' scratch tables.

    ``include_object`` cannot do this: alembic applies it **after**
    reflecting the object, and reflection is where the race is.
    ``include_name`` is applied to the name, before the ``SHOW CREATE
    TABLE``.

    The race, measured rather than guessed:
    ``test_the_include_object_filter_bites`` reflects the *whole* schema
    on purpose — that is how it sees atrium's tables — so it lists
    ``test_user_secret_probe_gwN`` while another worker is dropping it,
    and the follow-up ``SHOW CREATE TABLE`` fails with
    ``(1146, "Table … doesn't exist")``. It presents as a flaky
    autogenerate test with a message about a table this module has never
    heard of. Swept on this box: **0 failures in 20 runs** of the three
    pre-#14 files, **5 in 34 runs** once
    ``test_tenant_isolation.py`` was added — the third DB-heavy file
    widens the window rather than introducing the defect. Deterministic
    since this filter went in: 0 in 30.
    """
    if type_ == "table" and name and name.startswith(SCRATCH_TABLE_PREFIX):
        return False
    return True


async def test_no_host_table_is_hidden_by_the_scratch_table_filter():
    """The exclusion above must not be able to hide a real regression."""
    hidden = {t for t in m.HostBase.metadata.tables if t.startswith(SCRATCH_TABLE_PREFIX)}
    assert not hidden, (
        f"host tables named like scratch tables are invisible to every "
        f"autogenerate check in this file: {sorted(hidden)}"
    )


async def _autogen_diffs(include_object) -> list:
    factory = get_session_factory()
    async with factory() as s:

        def _compare(sync_session):
            ctx = MigrationContext.configure(
                connection=sync_session.connection(),
                opts={
                    "target_metadata": m.HostBase.metadata,
                    "compare_type": True,
                    "include_object": include_object,
                    "include_name": _include_name,
                },
            )
            return compare_metadata(ctx, m.HostBase.metadata)

        return await s.run_sync(_compare)


async def test_autogenerate_proposes_nothing_against_the_migrated_database():
    """The migration's second instrument.

    ``upgrade -> downgrade -> upgrade`` proves the revision runs. It
    says nothing about whether what it built is what the models
    declare — a column with the wrong width, a missing index or a
    forgotten ``ondelete`` all survive it. This asks alembic to
    difference the live schema against the metadata and demands the
    answer be empty.
    """
    diffs = await _autogen_diffs(m.include_object)
    assert diffs == [], f"schema and models disagree: {diffs}"


async def test_the_include_object_filter_bites():
    """Prove the guard is load-bearing by removing it.

    Two distinct destructive proposals hide behind an empty-looking
    diff, and the filter is the only thing stopping either:

    1. ``remove_table`` for every atrium table, because
       ``target_metadata`` is the host's and atrium's tables are in the
       database without being in it.
    2. ``remove_fk`` for every cross-base foreign key, because
       ``HostForeignKey`` registers nothing with the mapper. Applying
       that one drops the four constraints tying host rows to their
       owner, and it does not show up until the *second* autogenerate —
       on the revision that creates the tables they are ``add_table``
       and their constraints are never compared.

    This test fails if either proposal is absent, so it cannot pass by
    the filter being unnecessary.
    """
    unfiltered = await _autogen_diffs(None)

    dropped = {
        d[1].name
        for d in unfiltered
        if isinstance(d, tuple) and d and d[0] == "remove_table"
    }
    assert "users" in dropped, (
        "removing the include_object filter did not propose dropping atrium's "
        f"tables, so the filter is not what is protecting them. Saw: {sorted(dropped)}"
    )
    assert not (dropped & HOST_TABLES), (
        "host tables were proposed for removal even unfiltered — the schema is "
        f"behind the models: {sorted(dropped & HOST_TABLES)}"
    )

    dropped_fks = {
        d[1].name
        for d in unfiltered
        if isinstance(d, tuple) and d and d[0] == "remove_fk"
    }
    assert dropped_fks, (
        "unfiltered autogenerate proposed no remove_fk, so the second half of "
        "this filter is guarding nothing — check HostForeignKey is still what "
        "declares the cross-base keys"
    )

    filtered = await _autogen_diffs(m.include_object)
    assert filtered == [], f"filtered autogenerate is not empty: {filtered}"


# --------------------------------------------------------------------- #
# 2. Cross-base foreign keys
# --------------------------------------------------------------------- #


def _declared_host_fks() -> set[tuple[str, str, str, str]]:
    """``(table, column, target_table, ondelete)`` from the model markers."""
    out = set()
    for table in m.HostBase.metadata.tables.values():
        for col in table.columns:
            for spec in col.info.get(INFO_KEY, ()):
                target_table, _, _ = spec.target.partition(".")
                out.add((table.name, col.name, target_table, spec.ondelete or ""))
    return out


async def test_cross_base_foreign_keys_reached_the_database():
    """Model markers vs ``information_schema``.

    ``HostForeignKey`` registers nothing with the mapper — it is a dict
    entry on ``Column.info`` that only ``emit_host_foreign_keys`` reads.
    So "the model says there is an FK" and "the database has one" are
    genuinely independent readings, and the whole mechanism exists in
    the gap between them.
    """
    declared = _declared_host_fks()
    assert declared, "no HostForeignKey markers found — this test cannot fail"

    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                sa.text(
                    """
                    SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME,
                           kcu.REFERENCED_TABLE_NAME, rc.DELETE_RULE
                      FROM information_schema.KEY_COLUMN_USAGE kcu
                      JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
                        ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
                       AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
                     WHERE kcu.TABLE_SCHEMA = DATABASE()
                       AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
                       AND kcu.TABLE_NAME LIKE 'ddns\\_%'
                    """
                )
            )
        ).all()

    actual = {(r[0], r[1], r[2], r[3]) for r in rows}
    missing = declared - actual
    assert not missing, (
        f"HostForeignKey declared but not in the database: {sorted(missing)}. "
        f"Database has: {sorted(actual)}"
    )


async def test_every_users_id_reference_is_an_int_column():
    """``users.id`` is MySQL ``int``.

    A ``bigint`` child column makes ``ADD FOREIGN KEY`` fail with
    errno 150, whose message names neither the column nor the width.
    The host's own primary keys are ``bigint``, so getting this wrong
    is a matter of copying the line above.
    """
    factory = get_session_factory()
    async with factory() as s:
        parent = (
            await s.execute(
                sa.text(
                    "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' "
                    "AND COLUMN_NAME = 'id'"
                )
            )
        ).scalar_one()
        children = (
            await s.execute(
                sa.text(
                    """
                    SELECT c.TABLE_NAME, c.COLUMN_NAME, c.COLUMN_TYPE
                      FROM information_schema.COLUMNS c
                      JOIN information_schema.KEY_COLUMN_USAGE kcu
                        ON kcu.TABLE_SCHEMA = c.TABLE_SCHEMA
                       AND kcu.TABLE_NAME = c.TABLE_NAME
                       AND kcu.COLUMN_NAME = c.COLUMN_NAME
                     WHERE c.TABLE_SCHEMA = DATABASE()
                       AND kcu.REFERENCED_TABLE_NAME = 'users'
                       AND c.TABLE_NAME LIKE 'ddns\\_%'
                    """
                )
            )
        ).all()

    assert children, "no host columns reference users.id — nothing to check"
    mismatched = [r for r in children if r[2].split("(")[0] != parent.split("(")[0]]
    assert not mismatched, (
        f"users.id is {parent!r} but these reference it with a different width: "
        f"{[(r[0], r[1], r[2]) for r in mismatched]}"
    )


# --------------------------------------------------------------------- #
# 3. updated_at actually updates
# --------------------------------------------------------------------- #


async def test_updated_at_carries_on_update_in_the_database():
    """The scaffold's ``atrium_ddns_state.updated_at`` does not, and never has.

    ``server_onupdate=`` emits no DDL. The column ends up as a plain
    ``DEFAULT CURRENT_TIMESTAMP(6)`` and its value is frozen at insert
    — an ``updated_at`` that is always the created time is worse than
    no column, because it is read as one. This is the schema reading;
    the test below is the behavioural one.
    """
    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                sa.text(
                    "SELECT TABLE_NAME, EXTRA FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND COLUMN_NAME = 'updated_at' "
                    "AND TABLE_NAME LIKE 'ddns\\_%'"
                )
            )
        ).all()

    assert rows, "no ddns_* table has an updated_at column"
    stale = [r[0] for r in rows if "on update" not in (r[1] or "").lower()]
    assert not stale, (
        f"updated_at has no ON UPDATE clause on {sorted(stale)} — it will hold "
        "the insert time forever, which is how the scaffolded "
        "atrium_ddns_state.updated_at behaves today"
    )


async def test_updated_at_moves_when_the_row_changes(tenants):
    """The behavioural half. A DDL reading can be right and inert."""
    domain_id = tenants["a"]["domain_id"]
    factory = get_session_factory()
    async with factory() as s:
        before = (
            await s.execute(
                sa.text("SELECT updated_at FROM ddns_domain WHERE id = :i"),
                {"i": domain_id},
            )
        ).scalar_one()
        await s.execute(
            sa.text(
                "UPDATE ddns_domain SET name = CONCAT('x-', name) WHERE id = :i"
            ),
            {"i": domain_id},
        )
        await s.commit()
        after = (
            await s.execute(
                sa.text("SELECT updated_at FROM ddns_domain WHERE id = :i"),
                {"i": domain_id},
            )
        ).scalar_one()

    assert after > before, f"updated_at did not move: {before} -> {after}"


# --------------------------------------------------------------------- #
# 4. Permissions
# --------------------------------------------------------------------- #


async def test_seeded_permissions_match_the_model_constant():
    """The migration spells the codes literally; the models export them.

    They are two files and they are allowed to disagree — which is why
    the check reads neither, and asks the database what the migration
    put there.
    """
    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                sa.text(
                    "SELECT code FROM permissions WHERE code LIKE 'atrium\\_ddns.%'"
                )
            )
        ).scalars().all()

    seeded = set(rows)
    expected = set(m.PERMISSIONS)
    assert expected, "models.PERMISSIONS is empty — this test cannot fail"
    assert expected <= seeded, f"declared but not seeded: {sorted(expected - seeded)}"


async def test_permission_grants_landed_on_the_right_roles():
    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                sa.text(
                    "SELECT r.code, rp.permission_code FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "WHERE rp.permission_code LIKE 'atrium\\_ddns.%'"
                )
            )
        ).all()

    granted: dict[str, set[str]] = {}
    for role_code, perm in rows:
        granted.setdefault(role_code, set()).add(perm)

    for role_code, perms in m.PERMISSION_GRANTS.items():
        missing = set(perms) - granted.get(role_code, set())
        assert not missing, f"role {role_code!r} is missing {sorted(missing)}"

    # super_admin is auto-granted by atrium, never listed in GRANTS.
    assert set(m.PERMISSIONS) <= granted.get("super_admin", set())

    # And the negative half: `user` must NOT hold the cross-tenant codes.
    # Without this the previous assertions pass on a seed that granted
    # everything to everyone.
    over_granted = granted.get("user", set()) & {
        "atrium_ddns.admin",
        "atrium_ddns.events.read.all",
    }
    assert not over_granted, (
        f"the 'user' role holds cross-tenant permissions: {sorted(over_granted)}"
    )


# --------------------------------------------------------------------- #
# 5. Secrets — the two kinds, and which is which
# --------------------------------------------------------------------- #


async def test_provider_credentials_round_trip_with_no_authenticated_user(tenants):
    """The ``/nic/update`` shape, on the real table.

    No request, no cookie, no PAT, no ContextVar. The owner comes off
    the row being decrypted. ``test_user_scope_secrets.py`` asserts the
    same property against a throwaway probe table; this one asserts it
    against ``ddns_domain_backend`` as declared, which is what actually
    ships.
    """
    backend_id = tenants["a"]["backend_id"]
    async with get_session_factory()() as s:
        row = await s.get(m.DomainBackend, backend_id)
        await unlock_user_secrets(s, row.user_id)  # owner read from the ROW
        assert row.credentials.reveal() == CREDS_A


async def test_credentials_are_ciphertext_on_disk(tenants):
    """A round trip through one code path can be a no-op both ways."""
    backend_id = tenants["a"]["backend_id"]
    async with get_session_factory()() as s:
        blob = (
            await s.execute(
                sa.text(
                    "SELECT credentials_ct FROM ddns_domain_backend WHERE id = :i"
                ),
                {"i": backend_id},
            )
        ).scalar_one()

    assert blob, "nothing stored — the round-trip test above is vacuous"
    assert blob.startswith(b"ATR"), "not an atrium secret blob"
    assert b"AKIA-tenant-A" not in blob
    assert b"s3cret-A" not in blob


async def test_one_tenants_credentials_do_not_decrypt_in_anothers_row(tenants):
    """Bad import script, or write access to the database."""
    factory = get_session_factory()
    id_a, id_b = tenants["a"]["backend_id"], tenants["b"]["backend_id"]

    async with factory() as s:
        ct_a = (
            await s.execute(
                sa.text(
                    "SELECT credentials_ct FROM ddns_domain_backend WHERE id = :i"
                ),
                {"i": id_a},
            )
        ).scalar_one()
        await s.execute(
            sa.text("UPDATE ddns_domain_backend SET credentials_ct = :c WHERE id = :i"),
            {"c": ct_a, "i": id_b},
        )
        await s.commit()

    async with factory() as s:
        row = await s.get(m.DomainBackend, id_b)
        await unlock_user_secrets(s, row.user_id)
        with pytest.raises(SecretDecryptError):
            row.credentials.reveal()


async def test_the_ciphertext_column_is_a_mediumblob():
    """``SecretBlob`` does not widen on its own — an upstream defect.

    Its docstring says it "widens to MEDIUMBLOB on MySQL", and
    ``load_dialect_impl`` is a ``TypeDecorator`` hook that a plain
    ``LargeBinary`` subclass never gets called on. Left alone the
    column is ``BLOB``: 64 KB, which is the exact ceiling atrium's own
    comment records atrium-pa hitting in production. The model adds an
    explicit ``.with_variant()``; this reads the width back off the
    live table, so the day upstream fixes the primitive nothing here
    changes, and the day somebody drops the variant this fails.
    """
    factory = get_session_factory()
    async with factory() as s:
        col_type = (
            await s.execute(
                sa.text(
                    "SELECT DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'ddns_domain_backend' "
                    "AND COLUMN_NAME = 'credentials_ct'"
                )
            )
        ).scalar_one()

    assert col_type == "mediumblob", (
        f"credentials_ct is {col_type!r}, not mediumblob. A bare SecretBlob() "
        "compiles to BLOB (64 KB) — see the note in models.py."
    )


async def test_the_device_secret_is_hashed_and_not_encrypted():
    """Hashing is for secrets we only compare; encryption for ones we return.

    Two readings, because "there is no UserSecret on Device" is the
    kind of claim that stays true after somebody adds one under a
    different name: check the descriptor registry *and* the column
    types.
    """
    assert not _USER_SECRETS.get(m.Device), (
        "Device grew a UserSecret descriptor. The device secret is compared, "
        "never handed back — hashing it keeps SECRET_ENCRYPTION_KEY plus a "
        "database copy from yielding every tenant's DNS-update credential."
    )
    # Vacuity guard: the registry is populated at all, so the assertion
    # above is not passing because nothing ever registers.
    assert _USER_SECRETS.get(m.DomainBackend), (
        "no UserSecret registered on DomainBackend — the check above proves "
        "nothing"
    )

    hash_col = m.Device.__table__.c.password_hash
    assert isinstance(hash_col.type, sa.String)
    assert not isinstance(hash_col.type, SecretBlob)
    assert not any(
        isinstance(c.type, SecretBlob) for c in m.Device.__table__.columns
    ), "ddns_device has an encrypted column; it should have none"


# --------------------------------------------------------------------- #
# 6. Tenancy and referential behaviour
# --------------------------------------------------------------------- #


async def test_zone_name_uniqueness_is_global_across_tenants(tenants):
    """Ownership is the multi-tenancy change; uniqueness is not.

    Two tenants claiming one zone is a conflict the database refuses,
    not a state the UI has to explain.
    """
    from sqlalchemy.exc import IntegrityError

    name = f"shared-{W}.example.invalid"
    factory = get_session_factory()
    async with factory() as s:
        s.add(m.Domain(user_id=tenants["a"]["user_id"], name=name))
        await s.commit()

    try:
        with pytest.raises(IntegrityError):
            async with factory() as s:
                s.add(m.Domain(user_id=tenants["b"]["user_id"], name=name))
                await s.commit()
    finally:
        async with factory() as s:
            await s.execute(
                sa.text("DELETE FROM ddns_domain WHERE name = :n"), {"n": name}
            )
            await s.commit()


async def test_hostname_outlives_its_device(tenants):
    """``device_id`` is nullable and ``ON DELETE SET NULL``.

    A hostname exists before it is assigned and survives being
    unassigned; deleting the router must not delete the name it was
    maintaining.
    """
    factory = get_session_factory()
    hostname_id = tenants["a"]["hostname_id"]
    device_id = tenants["a"]["device_id"]

    async with factory() as s:
        assert (
            await s.execute(
                sa.text("SELECT device_id FROM ddns_hostname WHERE id = :i"),
                {"i": hostname_id},
            )
        ).scalar_one() == device_id
        await s.execute(
            sa.text("DELETE FROM ddns_device WHERE id = :i"), {"i": device_id}
        )
        await s.commit()

    async with factory() as s:
        row = (
            await s.execute(
                sa.text(
                    "SELECT name, device_id FROM ddns_hostname WHERE id = :i"
                ),
                {"i": hostname_id},
            )
        ).first()

    assert row is not None, "the hostname was deleted with its device"
    assert row[1] is None


async def test_an_event_stays_readable_after_its_device_is_deleted(tenants):
    """Why ``dns_event`` carries both FKs and denormalised strings.

    ``ON DELETE SET NULL`` on its own turns the history of a deleted
    device into a wall of blanks — and a deleted device is precisely
    when somebody goes looking. The strings are captured at write time
    for exactly this.
    """
    factory = get_session_factory()
    t = tenants["a"]

    async with factory() as s:
        s.add(
            m.DnsEvent(
                user_id=t["user_id"],
                device_id=t["device_id"],
                domain_id=t["domain_id"],
                hostname_id=t["hostname_id"],
                user_email=t["email"],
                device_name="router-a",
                domain_name=f"a-{W}.example.invalid",
                hostname=f"home.a-{W}.example.invalid",
                event_type="update",
                response_code="good",
                client_ip="203.0.113.10",
                ip="203.0.113.10",
            )
        )
        await s.commit()

    async with factory() as s:
        await s.execute(
            sa.text("DELETE FROM ddns_device WHERE id = :i"), {"i": t["device_id"]}
        )
        await s.commit()

    async with factory() as s:
        row = (
            await s.execute(
                sa.text(
                    "SELECT device_id, device_name, response_code FROM ddns_event "
                    "WHERE user_email = :e"
                ),
                {"e": t["email"]},
            )
        ).first()

    assert row is not None, "the event was deleted with the device it described"
    assert row[0] is None, "device_id should be SET NULL, not left dangling"
    assert row[1] == "router-a", (
        "the denormalised device name did not survive — the entry is now "
        "unreadable at the one moment it gets read"
    )
    assert row[2] == "good"


async def test_deleting_a_user_destroys_their_domains_devices_and_credentials(
    tenants,
):
    """Hard delete, the ``CASCADE`` half of "it should die with their account".

    Atrium's own ``ON DELETE CASCADE`` on ``user_secret_keys`` destroys
    the key in the same statement, so the ciphertext would be
    unreadable even if a row survived. Both halves are checked, because
    "the rows are gone" and "the plaintext is gone" are different
    claims and only the second one is the promise.
    """
    factory = get_session_factory()
    t = tenants["b"]

    async with factory() as s:
        assert (
            await s.execute(
                sa.text("SELECT COUNT(*) FROM user_secret_keys WHERE user_id = :u"),
                {"u": t["user_id"]},
            )
        ).scalar_one() == 1
        await s.execute(
            sa.text("DELETE FROM users WHERE id = :u"), {"u": t["user_id"]}
        )
        await s.commit()

    async with factory() as s:
        for table in ("ddns_domain", "ddns_device", "ddns_domain_backend"):
            left = (
                await s.execute(
                    sa.text(
                        f"SELECT COUNT(*) FROM {table} WHERE user_id = :u"  # noqa: S608
                    ),
                    {"u": t["user_id"]},
                )
            ).scalar_one()
            assert left == 0, f"{table} kept {left} row(s) for a deleted user"

        # The hostname hung off the domain, not off the user directly.
        assert (
            await s.execute(
                sa.text("SELECT COUNT(*) FROM ddns_hostname WHERE id = :i"),
                {"i": t["hostname_id"]},
            )
        ).scalar_one() == 0

        assert (
            await s.execute(
                sa.text("SELECT COUNT(*) FROM user_secret_keys WHERE user_id = :u"),
                {"u": t["user_id"]},
            )
        ).scalar_one() == 0
