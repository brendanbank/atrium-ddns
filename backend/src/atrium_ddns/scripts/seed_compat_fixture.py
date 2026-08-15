"""Build the frozen compat table's ``fixture:`` world in this database.

    python -m atrium_ddns.scripts.seed_compat_fixture
    make test-compat TARGET=host BASE_URL=http://api:8000

``tests/compat/conftest.py`` says in as many words that it "does **not**
build the ``fixture:`` world … it assumes the world in the table already
exists behind ``--base-url``". This is the thing that makes that
assumption true for ``--target host``.

**Read from the table, not restated.** The users, backends and
hostnames are parsed out of ``protocol_cases.yaml`` itself, so a fixture
that drifts from the table is not expressible: adding a hostname to the
table and forgetting it here is impossible, and the freeze guard already
stops the table changing by accident. The only things written down here
are the four *mappings* from the table's schema to this one, and each is
a decision with a reason:

===========================  ==================================================
the table says               this schema needs
===========================  ==================================================
``users[]``                  an atrium ``User`` **and** a ``ddns_device``. The
                             legacy rows were devices wearing a users table
                             (plan §3.3.1); ``active: false`` maps onto
                             ``User.is_active``, which is what
                             ``update-badauth-inactive-user`` asserts
``backends[].zone``          the **hostname's domain name**. Legacy passed
                             ``hn.domain.name`` to the provider; here the
                             domain *is* the zone, so ``wrong-zone`` becomes a
                             hostname whose domain is not a suffix of it
``hostnames[].backends``     legacy had a ``hostname_backends`` association;
                             this schema hangs backends off the **domain**. So
                             each hostname gets its own domain, named after
                             itself, and the backend set belongs to that
``service: stub``            a slot name (``stub1``/``stub2``/``stub3``) with
                             the scripted ``result`` in
                             ``ddns_domain_backend.config`` —
                             ``UNIQUE(domain_id, backend_type)`` means three
                             differently-scripted backends on one domain
                             cannot share a name
===========================  ==================================================

**Backend order is load-bearing and this script is what carries it.**
``update-aggregate-first-non-good-nochg-status`` expects ``911`` from
``[nochg, 911, dnserr]`` and would expect ``dnserr`` from the same set in
a different order. Rows are inserted in the order the table lists them
and ``Domain.backends`` is ``order_by="DomainBackend.id"``; the script
prints the resolved order per hostname at the end, so it is a reading
rather than an assumption.

**Not idempotent-by-merge; it deletes and rebuilds.** Deleting the
fixture users cascades through domains, devices, hostnames, backends and
rate-limit events, and takes their ``user_secret_keys`` row with them —
which is the right behaviour and worth naming: the stored provider
ciphertext becomes permanently unreadable, so a partial re-seed that
kept the rows and replaced the key would produce backends that resolve
and cannot be decrypted. A throwaway fixture has no reason to be
incremental.

Refuses to run when the compat stub provider is not registrable, for
the reason the whole exercise exists: seeding twelve hostnames whose
backends nothing can resolve would leave a database that looks seeded
and answers ``911`` to everything.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import sqlalchemy as sa
from app.db import get_engine, get_session_factory
from app.host_sdk.crypto import unlock_user_secrets
from app.models.auth import User

from ..auth_device import hash_password
from ..compat_stub import SLOTS, scripted_config, stub_providers_allowed
from ..models import Device, Domain, DomainBackend, Hostname

#: Where the Dockerfile's ``dev`` stage puts the repo-root ``tests/``
#: tree. Mirrors the repo layout, so this is ``tests/compat/…``.
DEFAULT_TABLE = Path("/opt/compat_tests/compat/protocol_cases.yaml")

#: Email domain for the fixture's users. ``.invalid`` is reserved by
#: RFC 2606 and can never resolve, so nothing here can send mail to a
#: real address by accident.
EMAIL_DOMAIN = "compat.invalid"

#: The credential a ``credentials: present`` backend stores. Synthetic,
#: and it reaches a provider that contacts nothing.
STUB_CREDENTIALS: dict[str, str] = {"stub_token": "compat-fixture-not-a-secret"}

#: Which fixture user's device is stored as **bcrypt** rather than
#: argon2id. Not decoration: it is the migrated-row shape from plan
#: §3.2, and ``alice`` sends ~110 of the table's requests, so a run of
#: the frozen table exercises the bcrypt verify path *and* the
#: opportunistic re-hash on its first request. ``--verify-rehash``
#: reads the column back afterwards.
BCRYPT_SEEDED_USERS: frozenset[str] = frozenset({"alice"})

#: Raised so the table's ``rate_limited: false`` default is *true* —
#: it is a precondition ("the user is not over its limit"), and the
#: table is ~124 requests in a few seconds against a default of 30/min.
#: The three ``rate_limited: true`` cases are skipped by the runner and
#: are measured out of band.
FIXTURE_RATE_LIMIT = 100_000


@dataclass(frozen=True)
class SeededHostname:
    hostname: str
    domain: str
    owner: str
    backend_types: tuple[str, ...]


def load_fixture(path: Path) -> Mapping[str, Any]:
    import yaml

    with path.open(encoding="utf-8") as handle:
        table = yaml.safe_load(handle)
    fixture = table.get("fixture")
    if not fixture:
        raise SystemExit(f"{path}: no `fixture:` block")
    return fixture


def domain_for(hostname: str, backends: Sequence[Mapping[str, Any]]) -> str:
    """The zone this hostname's row lives in.

    Itself, unless the fixture deliberately points it at a zone it is
    not inside — which is the whole content of the ``wrong-zone``
    backend and of ``update-nohost-hostname-outside-backend-zone``.

    Derived from the data rather than special-cased on the name
    ``outofzone.example.com``: a second out-of-zone hostname added to
    the table would be handled without editing this file, and a
    hard-coded name is the same defect one table revision later.
    """
    zones = {str(backend["zone"]) for backend in backends}
    if len(zones) == 1:
        zone = next(iter(zones))
        if hostname != zone and not hostname.endswith("." + zone):
            return zone
    return hostname


def backend_type_for(index: int, spec: Mapping[str, Any]) -> str:
    """Slot name for a scripted backend; the literal for anything else.

    ``service: no-such-service`` must stay a name the factory does not
    know — it is how ``update-911-backend-service-unknown-to-factory``
    is produced — so it is passed through verbatim.
    """
    service = str(spec["service"])
    if service != "stub":
        return service
    if index >= len(SLOTS):
        raise SystemExit(
            f"a domain needs {index + 1} scripted backends but only "
            f"{len(SLOTS)} slots exist ({', '.join(SLOTS)}). Add a slot to "
            "atrium_ddns.compat_stub.SLOTS — UNIQUE(domain_id, "
            "backend_type) is why they cannot share a name."
        )
    return SLOTS[index]


async def _purge(emails: Sequence[str]) -> None:
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            sa.text("DELETE FROM ddns_event WHERE user_email IN :e").bindparams(
                sa.bindparam("e", expanding=True)
            ),
            {"e": list(emails)},
        )
        await session.execute(
            sa.text("DELETE FROM users WHERE email IN :e").bindparams(
                sa.bindparam("e", expanding=True)
            ),
            {"e": list(emails)},
        )
        await session.commit()


async def seed(path: Path) -> list[SeededHostname]:
    allowed, reason = stub_providers_allowed()
    if not allowed:
        raise SystemExit(
            "refusing to seed: the compat stub provider is not registrable "
            f"here ({reason}). Every scripted backend would resolve to "
            "nothing and the whole table would answer 911 against a "
            "database that looks correctly seeded."
        )

    fixture = load_fixture(path)
    users: list[Mapping[str, Any]] = list(fixture["users"])
    backend_specs = {str(b["name"]): b for b in fixture["backends"]}
    hostnames: list[Mapping[str, Any]] = list(fixture["hostnames"])

    emails = [f"{u['username']}@{EMAIL_DOMAIN}" for u in users]
    await _purge(emails)

    factory = get_session_factory()
    seeded: list[SeededHostname] = []

    # Hashes are computed off the session: argon2id is ~50 ms and
    # bcrypt ~250 ms, and holding a transaction open across them buys
    # nothing.
    from pwdlib.hashers.bcrypt import BcryptHasher

    hashes: dict[str, str] = {}
    for spec in users:
        username = str(spec["username"])
        password = str(spec["password"])
        if username in BCRYPT_SEEDED_USERS:
            hashes[username] = BcryptHasher().hash(password)
        else:
            hashes[username] = await hash_password(password)

    async with factory() as session:
        user_ids: dict[str, int] = {}
        device_ids: dict[str, int] = {}
        for spec in users:
            username = str(spec["username"])
            user = User(
                email=f"{username}@{EMAIL_DOMAIN}",
                # The atrium web password is a different credential from
                # the DDNS one and is deliberately unusable here: the
                # fixture is about `/nic/*`, and seeding a working web
                # login for it would be a login nobody asked for.
                hashed_password=await hash_password(
                    f"unusable-web-login-{username}-" + "x" * 32
                ),
                is_active=bool(spec["active"]),
                is_verified=True,
                full_name=str(spec.get("name", username)),
                preferred_language="en",
            )
            session.add(user)
            await session.flush()
            user_ids[username] = user.id

            device = Device(
                user_id=user.id,
                username=username,
                password_hash=hashes[username],
                name=f"{username}-router",
                rate_limit_per_minute=FIXTURE_RATE_LIMIT,
            )
            session.add(device)
            await session.flush()
            device_ids[username] = device.id

        # The write path mints a key when the tenant has none —
        # `create=True` is correct here and wrong on a read path, where
        # it would hand a shredded user a fresh key that decrypts
        # nothing.
        for username in user_ids:
            await unlock_user_secrets(session, user_ids[username], create=True)

        for entry in hostnames:
            hostname = str(entry["name"])
            owner = str(entry["owner"])
            specs = [backend_specs[str(n)] for n in (entry.get("backends") or [])]
            domain_name = domain_for(hostname, specs)

            domain = Domain(user_id=user_ids[owner], name=domain_name)
            session.add(domain)
            await session.flush()

            backend_types: list[str] = []
            for index, spec in enumerate(specs):
                backend_type = backend_type_for(index, spec)
                backend_types.append(backend_type)
                result = spec.get("result")
                row = DomainBackend(
                    domain_id=domain.id,
                    user_id=user_ids[owner],
                    backend_type=backend_type,
                    config=scripted_config(str(result)) if result else None,
                )
                if str(spec.get("credentials")) == "present":
                    # `credentials: absent` stores nothing at all, which
                    # is a real state and the one that contributes 911.
                    row.credentials = dict(STUB_CREDENTIALS)
                session.add(row)
                # Flushed inside the loop so `DomainBackend.id` — and
                # therefore `Domain.backends`' order — follows the
                # table's order rather than the session's flush order.
                await session.flush()

            session.add(
                Hostname(
                    domain_id=domain.id,
                    device_id=device_ids[owner],
                    name=hostname,
                )
            )
            seeded.append(
                SeededHostname(
                    hostname=hostname,
                    domain=domain_name,
                    owner=owner,
                    backend_types=tuple(backend_types),
                )
            )

        await session.commit()

    return seeded


async def read_back(path: Path) -> list[SeededHostname]:
    """Re-read the seeded world **through the ORM relationship**.

    A second instrument, and a differently shaped one: :func:`seed`
    reports what it asked for, this reports what ``Domain.backends``
    hands the router — which is the ordering the aggregate cases
    depend on and the one thing an insert-order assumption cannot
    check.
    """
    fixture = load_fixture(path)
    wanted = {str(entry["name"]) for entry in fixture["hostnames"]}

    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    sa.select(Hostname)
                    .where(Hostname.name.in_(wanted))
                    .options(
                        sa.orm.selectinload(Hostname.domain).selectinload(
                            Domain.backends
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        out = [
            SeededHostname(
                hostname=row.name,
                domain=row.domain.name,
                owner="?",
                backend_types=tuple(b.backend_type for b in row.domain.backends),
            )
            for row in rows
        ]
    return sorted(out, key=lambda entry: entry.hostname)


async def verify_rehash() -> int:
    """Print each fixture device's stored hash *shape*. Never the hash.

    The point of the reading: ``alice`` is seeded bcrypt, so after the
    frozen table has run her row must read ``argon2id`` — the
    opportunistic re-hash of plan §3.2, observed rather than asserted
    in a unit test alone. Returns the number of rows still on bcrypt.
    """
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    sa.select(Device)
                    .join(User, User.id == Device.user_id)
                    .where(User.email.like(f"%@{EMAIL_DOMAIN}"))
                    .order_by(Device.username)
                )
            )
            .scalars()
            .all()
        )
    remaining = 0
    for row in rows:
        shape = (
            "argon2id"
            if row.password_hash.startswith("$argon2")
            else "bcrypt"
            if row.password_hash[:4] in ("$2a$", "$2b$", "$2y$")
            else "unknown"
        )
        seeded_as = "bcrypt" if row.username in BCRYPT_SEEDED_USERS else "argon2id"
        if shape == "bcrypt":
            remaining += 1
        print(f"  {row.username:<8} seeded {seeded_as:<8} now {shape}")
    return remaining


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table",
        type=Path,
        default=DEFAULT_TABLE,
        help=f"path to protocol_cases.yaml (default: {DEFAULT_TABLE})",
    )
    parser.add_argument(
        "--verify-rehash",
        action="store_true",
        help="print each fixture device's stored hash shape and exit",
    )
    args = parser.parse_args()

    if args.verify_rehash:

        async def _verify() -> int:
            try:
                return await verify_rehash()
            finally:
                # Without this the pool's sockets are closed by
                # aiomysql's ``__del__`` *after* ``asyncio.run`` has
                # closed the loop, and the process prints an "Event loop
                # is closed" traceback over a run that succeeded. A
                # traceback on a successful run is how a green result
                # gets read as a failure.
                await get_engine().dispose()

        remaining = asyncio.run(_verify())
        print(f"devices still stored as bcrypt: {remaining}")
        return

    if not args.table.is_file():
        print(
            f"{args.table} not found. The compat table reaches the image "
            "through the Dockerfile's `dev` stage; pass --table if you are "
            "running from a checkout.",
            file=sys.stderr,
        )
        sys.exit(2)

    # ONE `asyncio.run` for both halves. `app.db` caches a
    # process-global engine whose pool holds sockets bound to whichever
    # loop first opened them, so a second `asyncio.run` pulls a pooled
    # connection created on the first loop — which is dead — and dies
    # with "got Future attached to a different loop". Measured here,
    # from the read-back rather than from the seed, so the seed had
    # already committed and the failure looked like a verification bug.
    # `backend/pyproject.toml` records the same trap for pytest.
    async def _both() -> tuple[list[SeededHostname], list[SeededHostname]]:
        try:
            return await seed(args.table), await read_back(args.table)
        finally:
            await get_engine().dispose()

    seeded, read = asyncio.run(_both())

    print(f"seeded {len(seeded)} hostnames from {args.table}")
    by_name = {entry.hostname: entry for entry in seeded}
    print(f"  {'hostname':<26} {'domain':<26} owner    backends (ORM order)")
    for entry in read:
        asked = by_name[entry.hostname]
        flag = "" if asked.backend_types == entry.backend_types else "  <-- ORDER DIFFERS"
        print(
            f"  {entry.hostname:<26} {entry.domain:<26} "
            f"{asked.owner:<8} {list(entry.backend_types)}{flag}"
        )

    mismatched = [
        entry.hostname
        for entry in read
        if by_name[entry.hostname].backend_types != entry.backend_types
    ]
    if len(read) != len(seeded) or mismatched:
        print(
            f"\nREFUSED: seeded {len(seeded)} hostnames, read back "
            f"{len(read)}; backend order differs for {mismatched}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        f"\nboth instruments agree: {len(seeded)} hostnames, identical "
        "backend order on every one"
    )


if __name__ == "__main__":
    main()
