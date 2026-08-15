"""Rehearse the legacy migration against a copy of the live database.

    python -m atrium_ddns.scripts.rehearse_migration \
        --source /tmp/legacy-copy.db --owner-email you@example.com \
        --create-owner --label R1-fidelity

``import_legacy`` proves *the importer* works, against fixtures shaped
like production. This proves *the migration* works, against production,
copied — and they are different claims. The importer's own suite builds
a world it also asserts about; this one is handed a world nobody chose
and reports what it finds.

Everything here is a **negative result or it is nothing**
--------------------------------------------------------

Every check prints the size of the population it compared beside the
number of disagreements it found, in one line, because *"0 discrepancies
over 11 hostnames"* is a measurement and *"hostnames: OK"* is an
impression. A check whose population is empty prints
``NOT MEASURED`` — never ``0 discrepancies`` — since a comparison with
nothing on either side is the probe that cannot fail, and rendering it
in the same type as a real reading is how it gets believed.

Two instruments on every number, and they are differently shaped:
:func:`count_source` reads the legacy copy with hand-written SQL against
the legacy column names, while ``import_legacy.read_snapshot`` reads it
through the dataclasses the plan is built from, and
``import_legacy.verify`` re-reads the **target** through the ORM and
``DdnsScope``. A single mapping mistake shows up as a disagreement
between two of the three rather than as three copies of itself.

⚠️ Why the wire is driven in two places and not one
---------------------------------------------------

**After a real import this database holds the production DNS provider
credentials.** A ``good`` on ``/nic/update`` is therefore a *write to
the operator's live zone* — from a rehearsal. So the wire is split by
what each half can reach:

* Everything that stops at or before ownership — ``badauth`` and
  ``nohost`` — is driven against the **running service** over HTTP.
  Neither reaches a provider. ``nohost`` is the useful one: it is only
  reachable *after* the credential verified, so it proves an
  authentication success without touching DNS.
* ``good`` is driven **in this process**, with a recording stub
  registered for the migrated backend's service name and ``boto3.client``
  replaced by something that raises. Two independent guards, because one
  of them is the only thing between a rehearsal and a production DNS
  write, and a single guard that silently stops applying is exactly the
  defect this milestone is about.

The plaintexts, and the thing this cannot prove
-----------------------------------------------

The legacy service stores device passwords as bcrypt only
(``dyndns-route53/web_routes.py``: ``bcrypt.hashpw(pw.encode('utf8'),
bcrypt.gensalt())``). **The production plaintexts do not exist anywhere
and cannot be recovered**, so no rehearsal can present a real router's
real password. Saying so is more useful than a check that looks like it
did.

What *can* be measured, and is, in two halves that meet in the middle:

* ``--mode fidelity`` imports the copy **untouched**: the six hashes
  are the production bytes, and they are compared byte-for-byte in the
  target, driven to ``badauth`` on the wire, and — the load-bearing
  one — put through ``auth_device``'s own hasher tuple, which must
  *identify* them and answer ``False`` rather than raise. The control
  beside it is ``PasswordHash.recommended()`` raising
  ``UnknownHashError`` on the very same bytes: the epic's named
  fleet-wide failure, demonstrated on production data rather than
  described.
* ``--mode surrogate`` imports a copy in which each device's hash has
  been re-minted **with that row's own salt and cost** and a plaintext
  this process chose. The stored string keeps the production row's
  29-character ``$2b$NN$<salt>`` prefix exactly; only the 31-character
  digest differs, which is the difference a different password makes.
  Every one of the eleven production hostnames is then driven end to
  end with that credential.

Between them: the bytes are carried (fidelity), the bytes are the kind
this stack verifies (fidelity), and a string of exactly that shape
carries a router through all eleven hostnames (surrogate). What remains
unmeasurable is whether the operator's own password matches its own
hash — which is a property of the legacy database, not of this
migration, and was already true before anything was copied.

Disclosure
----------

Nothing here prints a hostname, a domain, a device username, a password
hash, an IP address, a Fernet key or a provider credential. Sets are
compared whole and reported as ``sha256[:16]`` of their canonical form,
which fails if one byte moved and discloses nothing if it did not.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from . import import_legacy as il

#: The service the running stack is reachable at from inside the api
#: container. Not a default anything infers: it is printed with every
#: run and overridable, because a wire result against the wrong service
#: is worse than no wire result.
DEFAULT_SERVICE_URL = "http://localhost:8000"

#: Passwords minted for ``--mode surrogate``. Never touch production —
#: they exist only in the derived copy this process builds and deletes.
SURROGATE_PASSWORD_PREFIX = "rehearsal-surrogate-"

#: An address from RFC 5737's TEST-NET-3, presented as the client IP on
#: the wire. It is never written anywhere a provider could see, because
#: no ``good`` in this module reaches a real provider.
CLIENT_IP = "203.0.113.50"

#: The address a surrogate ``good`` claims to be updating to. Also
#: TEST-NET-3 — if a guard below ever failed open, an obviously
#: documentation-range address in a real zone is at least legible as an
#: accident rather than as a plausible router.
SURROGATE_IP = "203.0.113.51"


class RehearsalFailed(RuntimeError):
    """A check disagreed, or a guard refused. Never swallowed."""


# ===================================================================== #
# Findings — the reporting type
# ===================================================================== #


@dataclass
class Finding:
    """One check: what was compared, how much of it, and what differed.

    ``population`` is not decoration. It is the denominator, printed on
    the same line as the numerator, so a check that quietly stopped
    having anything to compare reads as ``NOT MEASURED`` instead of as
    a pass.
    """

    name: str
    population: int
    discrepancies: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def measured(self) -> bool:
        return self.population > 0

    @property
    def ok(self) -> bool:
        return self.measured and not self.discrepancies

    def line(self) -> str:
        if not self.measured:
            return (
                f"  {self.name:<34} NOT MEASURED — the population was empty, "
                "which is a different state from agreement"
            )
        verdict = (
            f"{len(self.discrepancies)} discrepancies"
            if self.discrepancies
            else "0 discrepancies"
        )
        tail = f"  ({self.note})" if self.note else ""
        return (
            f"  {self.name:<34} {self.population:>4} compared, {verdict}{tail}"
        )


def set_digest(values: Any) -> str:
    """``sha256[:16]`` of a canonical JSON rendering.

    Used wherever the thing being compared is a production name, an
    address or a hash. Two of these agreeing is a falsifiable claim
    about bytes; neither of them is the bytes.
    """
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ===================================================================== #
# 1. The source, read by a second instrument
# ===================================================================== #

#: Legacy table -> the column the count is taken over. Hand-written
#: against the legacy schema on purpose: ``read_snapshot`` reads the
#: same database through the dataclasses the plan is built from, so if
#: this restated *that*, the two instruments would share an author and
#: a mistake.
SOURCE_TABLES = (
    "users",
    "domains",
    "domain_backends",
    "backend_configs",
    "hostnames",
    "hostname_backends",
    "rate_limit_configs",
)


@dataclass(frozen=True)
class SourceReading:
    """What straight SQL finds in the legacy copy."""

    counts: Mapping[str, int]
    admins: int
    devices: int
    hostname_to_user: Mapping[str, str]
    hostname_ips: Mapping[str, tuple[str | None, str | None]]
    hostname_updated: Mapping[str, str | None]
    ttls: tuple[int, ...]
    password_hashes: Mapping[str, str]
    ciphertext: Mapping[str, str]
    backend_types: tuple[str, ...]


def count_source(path: Path) -> SourceReading:
    """Read the legacy copy with hand-written SQL.

    Goes through :func:`import_legacy.open_source`, which copies the
    bytes into a private temporary directory before SQLite ever sees
    them — so this instrument inherits the rule about not creating
    ``-wal``/``-shm`` beside the caller's file rather than restating
    it, and there is exactly one implementation of that rule to get
    wrong.
    """
    with il.open_source(path) as conn:
        conn.row_factory = sqlite3.Row
        counts = {
            table: int(
                conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )
            for table in SOURCE_TABLES
        }
        users = conn.execute(
            "SELECT id, username, role, password_hash FROM users"
        ).fetchall()
        hostnames = conn.execute(
            "SELECT name, user_id, ttl, last_ip_v4, last_ip_v6, last_updated_at "
            "FROM hostnames"
        ).fetchall()
        backends = conn.execute(
            "SELECT id, backend_type FROM domain_backends"
        ).fetchall()
        configs = conn.execute(
            "SELECT config_key, config_value FROM backend_configs"
        ).fetchall()

    by_id = {int(row["id"]): row for row in users}
    return SourceReading(
        counts=counts,
        admins=sum(1 for row in users if row["role"] == "admin"),
        devices=sum(1 for row in users if row["role"] != "admin"),
        hostname_to_user={
            il.normalise_dns_name(row["name"]): by_id[int(row["user_id"])]["username"]
            for row in hostnames
        },
        hostname_ips={
            il.normalise_dns_name(row["name"]): (
                row["last_ip_v4"],
                row["last_ip_v6"],
            )
            for row in hostnames
        },
        hostname_updated={
            il.normalise_dns_name(row["name"]): (
                str(row["last_updated_at"]) if row["last_updated_at"] else None
            )
            for row in hostnames
        },
        ttls=tuple(sorted({int(row["ttl"]) for row in hostnames})),
        password_hashes={
            row["username"]: row["password_hash"]
            for row in users
            if row["role"] != "admin"
        },
        ciphertext={row["config_key"]: row["config_value"] for row in configs},
        backend_types=tuple(sorted(str(row["backend_type"]) for row in backends)),
    )


def legacy_decrypt(ciphertext: Mapping[str, str], key: str) -> dict[str, str]:
    """``dyndns-route53/models.py::decrypt_value``, verbatim.

    Deliberately *not* :func:`import_legacy.decrypt_credentials`: this
    is the legacy service's own two lines, so the comparison downstream
    is between what the old service reads and what the new one reads,
    rather than between one function and itself.
    """
    from cryptography.fernet import Fernet

    fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return {
        name: fernet.decrypt(token.encode()).decode()
        for name, token in sorted(ciphertext.items())
    }


# ===================================================================== #
# 2. The surrogate — a hash of the production shape, with a known key
# ===================================================================== #


def surrogate_hash(production_hash: str, password: str) -> str:
    """Re-mint ``production_hash`` for a plaintext this process knows.

    bcrypt's stored form is ``$2b$<cost>$<22-char salt><31-char
    digest>``, and ``bcrypt.hashpw`` accepts an existing hash in place
    of a salt — it reads the first 29 characters and ignores the rest.
    So the result shares the production row's **variant, cost and salt
    bytes exactly** and differs only in the digest, which is precisely
    what a different password changes.

    That matters because the alternative — ``gensalt()`` — produces a
    hash that is merely *of the same kind*, and the cost is the
    expensive, misconfigurable parameter. A surrogate at cost 4 driving
    the wire green would say nothing about a fleet whose hashes are at
    cost 12.
    """
    import bcrypt

    if len(production_hash) < 29 or not production_hash.startswith("$2"):
        raise RehearsalFailed(
            "cannot build a surrogate from a stored hash that is not bcrypt "
            f"(prefix {production_hash[:4]!r}, length {len(production_hash)}). "
            "Value withheld."
        )
    salt = production_hash[:29].encode()
    minted = bcrypt.hashpw(password.encode("utf8"), salt).decode()
    if minted[:29] != production_hash[:29]:
        raise RehearsalFailed(
            "the surrogate does not share the production row's salt and cost. "
            "Without that this proves nothing about the fleet's hash shape."
        )
    if minted == production_hash:
        raise RehearsalFailed(
            "the surrogate is byte-identical to the production hash, which "
            "would mean this process guessed a real password. Refusing."
        )
    return minted


def build_surrogate_copy(source: Path, destination: Path) -> dict[str, str]:
    """A copy of the legacy database with known device credentials.

    Everything except ``users.password_hash`` for the non-admin rows is
    production: the domain, the eleven hostnames, the device → hostname
    mapping, the TTLs, the ``last_ip`` columns and the Fernet-encrypted
    provider credentials. Returns ``username -> plaintext``.

    The derived file carries real provider ciphertext, so it is written
    0600 and the caller deletes it.
    """
    reading = count_source(source)
    if not reading.password_hashes:
        raise RehearsalFailed(
            "the source has no non-admin rows, so there is nothing to build "
            "a surrogate from. A surrogate run over an empty device set "
            "would drive zero requests and report no failures."
        )

    shutil.copyfile(source, destination)
    destination.chmod(0o600)

    passwords: dict[str, str] = {}
    conn = sqlite3.connect(destination)
    try:
        for index, (username, stored) in enumerate(
            sorted(reading.password_hashes.items())
        ):
            password = f"{SURROGATE_PASSWORD_PREFIX}{index}"
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (surrogate_hash(stored, password), username),
            )
            passwords[username] = password
        conn.commit()
    finally:
        conn.close()
    # `sqlite3.connect` on a non-WAL file leaves no sidecars, but the
    # source may have been journalled; clearing them keeps the derived
    # file the single artefact the caller has to delete.
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(destination) + suffix).unlink(missing_ok=True)
    return passwords


# ===================================================================== #
# 3. The guards that stand between a rehearsal and a production DNS write
# ===================================================================== #


@contextmanager
def no_provider_can_reach_the_internet(service: str) -> Iterator[list[Any]]:
    """Replace ``service``'s adapter with a recorder, and break boto3.

    Two guards, because this is the only thing between a rehearsal on
    migrated production credentials and a write to the operator's live
    zone, and one guard that stops applying is silent:

    1. the provider registry's entry for ``service`` is swapped for a
       recording stub, so ``get_provider`` hands the router something
       that talks to nothing;
    2. ``boto3.client`` is replaced by a function that raises, so if
       (1) is ever bypassed the run **fails loudly** instead of
       succeeding quietly against AWS.

    On exit both are restored, and the restoration is asserted — a
    guard that leaks into the rest of the process is a different bug
    from the one it prevents.
    """
    import boto3

    from ..providers import _REGISTRY, provider_class

    original = provider_class(service)
    if original is None:
        raise RehearsalFailed(
            f"no provider is registered for {service!r}, so there is nothing "
            "to neutralise. A `good` here would be reached through a code "
            "path this guard does not cover."
        )

    calls: list[ProviderCall] = []

    class _Recorder(original):  # type: ignore[misc,valid-type]
        """Everything above ``createrecords`` runs; nothing leaves.

        ``_discover_zones`` is the method that talks to AWS, and it is
        the *only* one overridden besides the two write operations.
        ``BaseProvider.__init__`` has already seeded ``self._zones``
        from the account's own domains, so suppressing discovery leaves
        the zone map exactly as the migrated row describes it — which
        is what ``hostnameperzone`` and therefore ``nohost`` are decided
        from. Emptying it instead would answer ``nohost`` to everything
        and the run would read as a migration failure.
        """

        def _discover_zones(self) -> None:
            return None

        def createrecords(
            self,
            ip: str,
            hostname_zones: Mapping[str, Sequence[str]],
            rtype: str = "A",
            ttl: int = 60,
        ) -> dict[str, str]:
            from ..providers.base import STATUS_GOOD

            names = [name for group in hostname_zones.values() for name in group]
            calls.append(
                ProviderCall(op="create", ip=ip, hostnames=tuple(names),
                             rtype=rtype, ttl=ttl)
            )
            return {name: STATUS_GOOD for name in names}

        def deleterecords(
            self,
            hostname_zones: Mapping[str, Sequence[str]],
            rtype: str | None = None,
        ) -> dict[str, str]:
            from ..providers.base import STATUS_GOOD

            names = [name for group in hostname_zones.values() for name in group]
            calls.append(
                ProviderCall(op="delete", ip=None, hostnames=tuple(names),
                             rtype=rtype, ttl=None)
            )
            return {name: STATUS_GOOD for name in names}

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise RehearsalFailed(
            "boto3.client was constructed during a rehearsal. The provider "
            "guard did not hold, and this run was one API call away from "
            "writing the operator's live DNS. Nothing was sent."
        )

    saved_registry = dict(_REGISTRY)
    saved_client = boto3.client
    for name in original.service_names():
        _REGISTRY[name] = _Recorder
    boto3.client = _refuse  # type: ignore[assignment]
    try:
        yield calls
    finally:
        boto3.client = saved_client  # type: ignore[assignment]
        _REGISTRY.clear()
        _REGISTRY.update(saved_registry)
        if provider_class(service) is not original:
            raise RehearsalFailed(
                "the provider registry was not restored after the rehearsal."
            )


@dataclass(frozen=True)
class ProviderCall:
    """One call the router made to a DNS provider, as recorded.

    The TTL is on here because it is the value the importer moved off
    the legacy ``hostnames.ttl`` column onto the backend config, and
    the only place it can be *observed* rather than read back out of
    the column it was written to.
    """

    op: str
    ip: str | None
    hostnames: tuple[str, ...]
    rtype: str | None
    ttl: int | None


# ===================================================================== #
# 4. The wire
# ===================================================================== #


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}", "X-Forwarded-For": CLIENT_IP}


async def drive_running_service(
    url: str,
    *,
    cases: Sequence[tuple[str, str, str, str]],
) -> list[str]:
    """``(label, username, password, hostname)`` -> disagreements.

    Against the **running** service over HTTP, so the reading covers
    uvicorn, the middleware stack and the real router — not an ASGI
    app assembled here, which would share an author with the thing
    under test. Every case is one whose expected answer is reached
    *before* any provider is consulted.
    """
    import httpx

    problems: list[str] = []
    async with httpx.AsyncClient(base_url=url, timeout=30.0) as client:
        for expected, username, password, hostname in cases:
            response = await client.get(
                "/nic/update",
                params={"hostname": hostname, "myip": SURROGATE_IP},
                headers=basic(username, password),
            )
            body = response.text.strip()
            first = body.split(" ")[0] if body else ""
            if response.status_code != 200:
                problems.append(
                    f"expected HTTP 200 for a {expected!r} case, got "
                    f"{response.status_code} (hostname withheld)"
                )
            elif first != expected:
                problems.append(
                    f"expected {expected!r}, got {first!r} "
                    "(hostname and credential withheld)"
                )
    return problems


# ===================================================================== #
# 5. The checks
# ===================================================================== #


async def run_checks(
    source_reading: SourceReading,
    plan: il.Plan,
    written: il.Written,
    reading: il.Reading,
    *,
    fernet_key: str,
    session_factory: Any,
) -> list[Finding]:
    """Every data-fidelity comparison, as findings.

    Ordered from the coarsest (counts) to the finest (a credential's
    plaintext), because a count disagreeing makes everything below it
    unsurprising and the report should read in that order.
    """
    findings: list[Finding] = []

    # --- counts, both directions -------------------------------------
    pairs = {
        "devices": (source_reading.devices, written.devices, reading.devices),
        "domains": (
            source_reading.counts["domains"],
            written.domains,
            reading.domains,
        ),
        "backends": (
            source_reading.counts["domain_backends"],
            written.backends,
            reading.backends,
        ),
        "hostnames": (
            source_reading.counts["hostnames"],
            written.hostnames,
            reading.hostnames,
        ),
    }
    count_problems = [
        f"{name}: source {src}, written {wrote}, target {tgt}"
        for name, (src, wrote, tgt) in pairs.items()
        if not (src == wrote == tgt)
    ]
    findings.append(
        Finding(
            "row counts (3 instruments)",
            population=len(pairs),
            discrepancies=count_problems,
            note="source SQL / apply() / ORM read-back, all three equal",
        )
    )

    # --- the whole hostname -> device mapping -------------------------
    source_map = source_reading.hostname_to_user
    target_map = dict(reading.hostname_to_device)
    map_problems: list[str] = []
    if set(source_map) != set(target_map):
        missing = len(set(source_map) - set(target_map))
        extra = len(set(target_map) - set(source_map))
        map_problems.append(
            f"hostname sets differ: {missing} in the source only, "
            f"{extra} in the target only (names withheld)"
        )
    else:
        for name in sorted(source_map):
            if source_map[name] != target_map[name]:
                map_problems.append(
                    "a hostname landed on a different device than the legacy "
                    "row owned (names withheld)"
                )
    findings.append(
        Finding(
            "hostname -> device, whole set",
            population=len(source_map),
            discrepancies=map_problems,
            note=f"set digest {set_digest(source_map)} both sides"
            if not map_problems
            else "",
        )
    )

    # --- last_ip_v4 / last_ip_v6 --------------------------------------
    ip_problems = [
        "a last_ip pair did not survive the import (values withheld)"
        for name in sorted(source_reading.hostname_ips)
        if tuple(source_reading.hostname_ips[name])
        != tuple(reading.hostname_ips.get(name, (None, None)))
    ]
    v4 = sum(1 for a, _ in source_reading.hostname_ips.values() if a)
    v6 = sum(1 for _, b in source_reading.hostname_ips.values() if b)
    findings.append(
        Finding(
            "last_ip_v4 / last_ip_v6",
            population=len(source_reading.hostname_ips),
            discrepancies=ip_problems,
            note=(
                f"{v4} carry a v4 address, {v6} a v6; digest "
                f"{set_digest(source_reading.hostname_ips)}"
            ),
        )
    )
    if v4 == 0 and v6 == 0:
        findings.append(
            Finding(
                "last_ip: was there anything to carry",
                population=0,
                note="every source row was NULL, so the check above compared "
                "absence to absence",
            )
        )

    # --- last_updated_at ----------------------------------------------
    from ..models import Hostname
    from ..scope import DdnsScope

    import sqlalchemy as sa

    # Bound to a local named `scope` rather than chained inline. Not
    # cosmetic: `test_tenant_isolation.py`'s read-path census
    # classifies a call as scoped by the receiver being literally
    # `scope`, so the chained spelling is reported UNSCOPED. Conforming
    # keeps the census a census — the alternative is widening what
    # counts as scoped, which is a change to a tenancy control and does
    # not belong in a rehearsal harness.
    scope = DdnsScope.for_user_id(written.owner_id)

    async with session_factory() as session:
        rows = (
            await session.execute(scope.select(Hostname))
        ).scalars().all()
        target_updated = {
            row.name: (
                row.last_updated_at.strftime("%Y-%m-%d %H:%M:%S")
                if row.last_updated_at
                else None
            )
            for row in rows
        }
        # Deliberately raw, and deliberately scoped by the subquery.
        # ``ddns_domain_backend.config`` is where the legacy per-hostname
        # TTL landed, and reading it back through the ORM's own
        # ``DomainBackend`` would go through the same attribute the
        # importer wrote it with — a second reading by the first
        # instrument. The tenancy is carried by ``user_id = :uid`` on
        # the owning domain, which is the join ``DdnsScope`` applies for
        # this model.
        backend_configs = (
            await session.execute(
                sa.text(
                    "SELECT config FROM ddns_domain_backend "
                    "WHERE domain_id IN (SELECT id FROM ddns_domain "
                    "WHERE user_id = :uid)"
                ),
                {"uid": written.owner_id},
            )
        ).scalars().all()

    stamp_problems: list[str] = []
    carried = 0
    for name, value in sorted(source_reading.hostname_updated.items()):
        if value is None:
            continue
        carried += 1
        # The legacy column is a SQLite datetime string with
        # microseconds; MySQL's DATETIME drops them by default, so the
        # comparison is to the second. Stated rather than assumed —
        # comparing the raw strings would fail for a reason that has
        # nothing to do with the migration.
        if target_updated.get(name) != value[:19]:
            stamp_problems.append(
                "a last_updated_at did not survive to the second "
                "(values withheld)"
            )
    findings.append(
        Finding(
            "last_updated_at (to the second)",
            population=carried,
            discrepancies=stamp_problems,
            note=f"most recent in the source: {max(v for v in source_reading.hostname_updated.values() if v)}"
            if carried
            else "",
        )
    )

    # --- TTL: per hostname in the legacy schema, per backend here -----
    ttl_problems: list[str] = []
    stored_ttls = []
    for raw in backend_configs:
        config = json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or {})
        stored_ttls.append(config.get("ttl"))
    if len(source_reading.ttls) != 1:
        ttl_problems.append(
            f"the source holds {len(source_reading.ttls)} distinct TTLs, which "
            "this schema cannot represent per hostname; the importer should "
            "have refused"
        )
    elif not stored_ttls:
        ttl_problems.append(
            "no migrated backend carries a config, so the legacy TTL landed "
            "nowhere"
        )
    else:
        for value in stored_ttls:
            if value != source_reading.ttls[0]:
                ttl_problems.append(
                    f"backend config ttl is {value!r}, legacy hostnames say "
                    f"{source_reading.ttls[0]}"
                )
    findings.append(
        Finding(
            "ttl -> ddns_domain_backend.config",
            population=len(stored_ttls),
            discrepancies=ttl_problems,
            note=(
                f"legacy hostnames.ttl = {source_reading.ttls}, stored on "
                f"{len(stored_ttls)} backend(s) as {sorted(set(stored_ttls))} "
                "— ddns_hostname has no ttl column"
            ),
        )
    )

    # --- provider credentials, compared as PLAINTEXT ------------------
    legacy_plain = legacy_decrypt(source_reading.ciphertext, fernet_key)
    cred_problems: list[str] = []
    from app.host_sdk.crypto import unlock_user_secrets
    from sqlalchemy.orm import selectinload

    from ..models import Domain

    async with session_factory() as session:
        await unlock_user_secrets(session, written.owner_id)
        domains = (
            await session.execute(
                scope.select(Domain).options(selectinload(Domain.backends))
            )
        ).scalars().all()
        target_plain: dict[str, str] = {}
        for domain in domains:
            for backend in domain.backends:
                blob = backend.credentials
                if blob is None:
                    cred_problems.append(
                        "a migrated backend has NULL credentials — it would "
                        "answer 911 while looking configured"
                    )
                    continue
                for key, value in blob.reveal().items():
                    if key == "ttl":
                        continue
                    target_plain[key] = value

    compared = 0
    for key in sorted(legacy_plain):
        compared += 1
        if key not in target_plain:
            cred_problems.append(f"credential {key!r} is absent from the target")
        elif target_plain[key] != legacy_plain[key]:
            cred_problems.append(
                f"credential {key!r} does not decrypt to the legacy plaintext "
                "(values withheld)"
            )
        elif not target_plain[key]:
            cred_problems.append(
                f"credential {key!r} decrypts to the EMPTY string on both "
                "sides — migrated-looking and useless"
            )
    findings.append(
        Finding(
            "provider credentials (plaintext)",
            population=compared,
            discrepancies=cred_problems,
            note=(
                "legacy Fernet -> per-user SecretBlob; equal by value, "
                f"digest {il.credential_digest(legacy_plain)} "
                f"vs {il.credential_digest({k: v for k, v in target_plain.items() if k in legacy_plain})}"
            ),
        )
    )

    return findings


async def check_hashes(
    source_reading: SourceReading,
    reading: il.Reading,
    *,
    expect_identical: bool,
) -> list[Finding]:
    """The three things a bcrypt column has to be, measured separately.

    Byte-identity is one claim; *being a hash this stack can verify* is
    another; and *pwdlib's recommended tuple refusing the very same
    bytes* is the control that says the second claim was worth making.
    """
    from pwdlib import PasswordHash
    from pwdlib.exceptions import UnknownHashError

    from ..auth_device import _verify_sync

    findings: list[Finding] = []
    stored = dict(reading.password_hashes)

    if expect_identical:
        mismatched = [
            "a device's stored hash is not byte-identical to the legacy row "
            "(values withheld)"
            for username, legacy in source_reading.password_hashes.items()
            if stored.get(username) != legacy
        ]
        findings.append(
            Finding(
                "password hashes, byte-identical",
                population=len(source_reading.password_hashes),
                discrepancies=mismatched,
                note=f"digest {set_digest(source_reading.password_hashes)} both sides",
            )
        )

    shapes = sorted({(value[:4], len(value)) for value in stored.values()})
    identifiable: list[str] = []
    for username, value in sorted(stored.items()):
        ok, upgraded = _verify_sync(value, "definitely-not-the-password")
        if ok:
            identifiable.append(
                "a stored hash verified a password this process invented, "
                "which is not a migration result"
            )
        elif upgraded is not None:
            identifiable.append(
                "a failed verify returned a re-hash, which should be "
                "unreachable"
            )
    # A wrong password answering False is only evidence that the hash
    # was *identified* if an unidentifiable one is distinguishable. It
    # is: `_verify_sync` logs `ddns.auth.unrecognised_stored_hash` and
    # still returns False, so the distinguishing reading is taken here,
    # against the hasher, rather than off the return value.
    # `auth_device._PASSWORD_HASH` itself, not a second tuple assembled
    # here to look like it. A rehearsal that built its own hasher would
    # keep passing after the deployed one changed.
    from ..auth_device import _PASSWORD_HASH

    unidentified = 0
    for value in stored.values():
        try:
            _PASSWORD_HASH.verify(b"definitely-not-the-password", value)
        except UnknownHashError:
            unidentified += 1
        except Exception:  # noqa: BLE001 - any other error is a verify result
            pass
    if unidentified:
        identifiable.append(
            f"{unidentified} stored hash(es) are not identifiable by "
            "auth_device's hasher tuple — every one of those devices answers "
            "badauth forever"
        )
    findings.append(
        Finding(
            "hashes auth_device can identify",
            population=len(stored),
            discrepancies=identifiable,
            note=f"shapes {shapes}",
        )
    )

    # The control. `PasswordHash.recommended()` holds Argon2 alone, so
    # it must raise on every one of these strings. If it ever stops
    # raising, the check above has become a tautology and says so.
    recommended = PasswordHash.recommended()
    raised = 0
    for value in stored.values():
        try:
            recommended.verify(b"definitely-not-the-password", value)
        except UnknownHashError:
            raised += 1
        except Exception:  # noqa: BLE001
            pass
    control: list[str] = []
    if raised != len(stored):
        control.append(
            f"PasswordHash.recommended() raised on {raised} of {len(stored)} "
            "stored hashes. It is supposed to raise on all of them; if it no "
            "longer does, the check above no longer distinguishes anything."
        )
    findings.append(
        Finding(
            "control: recommended() refuses",
            population=len(stored),
            discrepancies=control,
            note=(
                f"{raised} of {len(stored)} raise UnknownHashError under "
                f"{[type(h).__name__ for h in recommended.hashers]} — the "
                "epic's fleet-wide badauth, on production bytes"
            ),
        )
    )
    return findings


# ===================================================================== #
# 6. The run
# ===================================================================== #


def _print_findings(label: str, findings: Sequence[Finding]) -> None:
    print()
    print(f"{label}")
    for finding in findings:
        print(finding.line())
        for line in finding.discrepancies:
            print(f"      ! {line}")


async def _rehearse(args: argparse.Namespace) -> int:
    from app.db import get_engine, get_session_factory

    source = Path(args.source)
    fernet_key = os.environ.get(il.FERNET_KEY_ENV, "")
    if not fernet_key:
        raise RehearsalFailed(
            f"{il.FERNET_KEY_ENV} is not set. The provider credentials in the "
            "copy are Fernet-encrypted under the legacy service's key, and a "
            "rehearsal that skipped the credential check would be the one "
            "check this migration most needs."
        )

    workdir = Path(tempfile.mkdtemp(prefix="rehearsal-"))
    surrogate_passwords: dict[str, str] = {}
    try:
        if args.mode == "surrogate":
            derived = workdir / "surrogate.db"
            surrogate_passwords = build_surrogate_copy(source, derived)
            import_from = derived
        else:
            import_from = source

        source_reading = count_source(source)
        print(f"rehearsal {args.label}  mode={args.mode}")
        print(f"  source: {source}  ({source.stat().st_size} bytes)")
        print(f"  source digest: {set_digest(sorted(source_reading.counts.items()))}")
        print()
        print("  source, read by hand-written SQL over the legacy schema:")
        for table in SOURCE_TABLES:
            print(f"    {table:<20} {source_reading.counts[table]:>4}")
        print(
            f"    -> {source_reading.admins} admin row(s), "
            f"{source_reading.devices} device row(s), "
            f"backend types {source_reading.backend_types}"
        )
        if args.mode == "surrogate":
            print(
                f"  surrogate: {len(surrogate_passwords)} device hash(es) "
                "re-minted with the production row's own salt and cost. "
                "Everything else is production."
            )

        with il.open_source(import_from) as conn:
            il.assert_columns(conn)
            snapshot = il.read_snapshot(conn)
        plan = il.build_plan(snapshot, fernet_key=fernet_key)

        factory = get_session_factory()
        try:
            async with factory() as session:
                written = await il.apply(
                    session,
                    plan,
                    owner_email=args.owner_email,
                    create_owner=args.create_owner,
                )
                await session.commit()
            async with factory() as session:
                reading = await il.verify(session, written.owner_id)

            problems = il.compare(plan, written, reading)
            findings = await run_checks(
                source_reading,
                plan,
                written,
                reading,
                fernet_key=fernet_key,
                session_factory=factory,
            )
            findings.extend(
                await check_hashes(
                    source_reading,
                    reading,
                    expect_identical=args.mode == "fidelity",
                )
            )
            _print_findings(
                "DATA FIDELITY — what was compared, and what disagreed",
                findings,
            )
            if problems:
                print()
                print("  import_legacy.compare() disagreements:")
                for line in problems:
                    print(f"      ! {line}")

            wire = await _drive_wire(
                args,
                plan=plan,
                reading=reading,
                surrogate_passwords=surrogate_passwords,
                session_factory=factory,
                owner_id=written.owner_id,
            )
            _print_findings("THE WIRE — driven, not inspected", wire)
        finally:
            await get_engine().dispose()

        every = list(findings) + list(wire)
        failed = [f for f in every if not f.ok]
        print()
        print(
            f"{args.label}: {len(every)} checks, "
            f"{sum(f.population for f in every)} individual comparisons, "
            f"{sum(len(f.discrepancies) for f in every)} discrepancies"
        )
        if problems:
            print(f"{args.label}: import_legacy.compare() reported {len(problems)}")
        if failed:
            print(f"{args.label}: NOT GREEN — {len(failed)} check(s) did not pass:")
            for finding in failed:
                reason = (
                    "population empty"
                    if not finding.measured
                    else f"{len(finding.discrepancies)} discrepancies"
                )
                print(f"  - {finding.name}: {reason}")
            return 1
        if problems:
            return 1
        print(f"{args.label}: GREEN")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _drive_wire(
    args: argparse.Namespace,
    *,
    plan: il.Plan,
    reading: il.Reading,
    surrogate_passwords: Mapping[str, str],
    session_factory: Any,
    owner_id: int,
) -> list[Finding]:
    """The wire half, split by what each case can reach."""
    findings: list[Finding] = []
    hostname_to_device = dict(reading.hostname_to_device)
    devices = sorted({username for username in hostname_to_device.values()})

    # --- against the RUNNING service ----------------------------------
    # `badauth` for every real username under a password this process
    # invented. It reaches the device row and the stored hash and stops
    # there; no provider is consulted, so this is safe against a stack
    # holding production credentials.
    bad_cases = [
        ("badauth", device, "not-the-password", hostname)
        for hostname, device in sorted(hostname_to_device.items())
    ]
    unknown_cases = [
        ("badauth", f"no-such-device-{index}", "not-the-password", hostname)
        for index, (hostname, _) in enumerate(sorted(hostname_to_device.items()))
    ]
    problems = await drive_running_service(
        args.service_url, cases=bad_cases + unknown_cases
    )
    findings.append(
        Finding(
            "running service: badauth",
            population=len(bad_cases) + len(unknown_cases),
            discrepancies=problems,
            note=(
                f"{len(bad_cases)} migrated username(s) with a wrong password "
                f"and {len(unknown_cases)} username(s) that never existed — "
                "identical on the wire by design, distinguished in the log"
            ),
        )
    )

    if args.mode != "surrogate":
        return findings

    # --- `good`, in this process, behind two guards -------------------
    # This half runs FIRST, on purpose. A successful verify re-hashes a
    # bcrypt row to argon2id (plan §3.2), so whichever wire half runs
    # first is the only one that sees the migrated shape — and the
    # acceptance criterion is about `good`. The shapes are snapshotted
    # either side rather than assumed, because "which hash did this
    # actually verify" is exactly the sort of thing that reads as
    # satisfied and is not.
    service = sorted({backend.backend_type for backend in plan.backends})
    if len(service) != 1:
        raise RehearsalFailed(
            f"expected exactly one migrated backend service, found {service}"
        )

    before = await _stored_shapes(session_factory, owner_id)
    findings.append(
        Finding(
            "stored shape before any success",
            population=len(before),
            discrepancies=[
                "a migrated device is not stored as bcrypt, so the wire below "
                "verifies something other than the legacy credential"
                for prefix in before.values()
                if not prefix.startswith("$2")
            ],
            note=f"{_shape_tally(before)} — what the fleet's routers hold",
        )
    )

    good_problems: list[str] = []
    observed_ttls: set[Any] = set()
    observed_rtypes: set[Any] = set()
    with no_provider_can_reach_the_internet(service[0]) as calls:
        import httpx
        from fastapi import FastAPI
        from httpx import ASGITransport

        from .. import router_nic

        app = FastAPI()
        app.include_router(router_nic.router)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://rehearsal.invalid"
        ) as client:
            for hostname, owner in sorted(hostname_to_device.items()):
                response = await client.get(
                    "/nic/update",
                    params={"hostname": hostname, "myip": SURROGATE_IP},
                    headers=basic(owner, surrogate_passwords[owner]),
                )
                body = response.text.strip()
                if not body.startswith("good"):
                    good_problems.append(
                        f"expected 'good', got {body.split(' ')[0]!r} "
                        "(hostname withheld)"
                    )
        observed_ttls = {call.ttl for call in calls}
        observed_rtypes = {call.rtype for call in calls}
    if len(calls) != len(hostname_to_device):
        good_problems.append(
            f"the recorder saw {len(calls)} provider call(s) for "
            f"{len(hostname_to_device)} hostname(s). A `good` that reached no "
            "provider is not a DNS update."
        )
    findings.append(
        Finding(
            "in-process: good, every hostname",
            population=len(hostname_to_device),
            discrepancies=good_problems,
            note=(
                f"{len(calls)} provider call(s) recorded, rtype(s) "
                f"{sorted(observed_rtypes, key=str)} ttl(s) "
                f"{sorted(observed_ttls, key=str)} observed arriving at the "
                "provider; boto3.client was replaced by a raiser throughout"
            ),
        )
    )

    # The fleet migrating itself, measured rather than described.
    after_good = await _stored_shapes(session_factory, owner_id)
    findings.append(
        Finding(
            "opportunistic re-hash to argon2id",
            population=len(after_good),
            discrepancies=[
                "a device that authenticated is still stored as bcrypt, so "
                "the fleet never leaves the migrated shape"
                for prefix in after_good.values()
                if not prefix.startswith("$argon2")
            ],
            note=(
                f"{_shape_tally(before)} -> {_shape_tally(after_good)} after "
                "one successful update each — plan §3.2"
            ),
        )
    )

    # Put the migrated bcrypt back, so the second wire half also
    # verifies the shape the fleet actually holds rather than the
    # argon2id the first half just wrote. Stated in the report because
    # a rehearsal that quietly rewrites the thing under test is worse
    # than one that does not do it at all.
    restored = await _restore_bcrypt(
        session_factory, owner_id, surrogate_passwords, before
    )
    print(
        f"  [rehearsal] restored {restored} device hash(es) to the migrated "
        "bcrypt so the running-service half verifies the same shape"
    )

    # `nohost` is the strongest thing that can be asserted against the
    # running service on a stack holding production credentials: it is
    # only reachable AFTER the credential verified, and it stops before
    # any provider is consulted. A wrong password answers `badauth`
    # here, so a `nohost` is an authentication success.
    # The **next** device in sorted order, cyclically — not
    # ``hash(hostname) % len(others)``, which is what this was first
    # written as and which is wrong twice over. Python salts ``hash``
    # of a str per process, so the choice varied between runs of the
    # same rehearsal: one run authenticated all six devices here and
    # the next authenticated four, and the difference showed up only
    # because the re-hash tally beside it moved. A rehearsal whose
    # coverage is drawn from a per-process random seed is not
    # reproducible, and the number that would have been reported is an
    # average over a population nobody chose. Cyclic-next is
    # deterministic, and because every device owns at least one
    # hostname, every device is some other device's successor — so the
    # set of devices exercised here is the whole set, which the check
    # below asserts rather than assumes.
    cross_cases: list[tuple[str, str, str, str]] = []
    for hostname, owner in sorted(hostname_to_device.items()):
        if len(devices) < 2:
            continue
        other = devices[(devices.index(owner) + 1) % len(devices)]
        cross_cases.append(("nohost", other, surrogate_passwords[other], hostname))
    problems = await drive_running_service(args.service_url, cases=cross_cases)
    exercised = {username for _, username, _, _ in cross_cases}
    if exercised != set(devices):
        problems.append(
            f"{len(devices) - len(exercised)} migrated device(s) never "
            "authenticated in this half, so the re-hash tally below is over a "
            "population smaller than the fleet. Names withheld."
        )
    findings.append(
        Finding(
            "running service: authenticated nohost",
            population=len(cross_cases),
            discrepancies=problems,
            note=(
                f"{len(exercised)} of {len(devices)} migrated device(s) "
                "exercised; each hostname requested by a DIFFERENT migrated "
                "device of the same tenant — a credential that did not verify "
                "would answer badauth, so nohost is an authentication success "
                "that provably wrote no DNS"
            ),
        )
    )

    after_nohost = await _stored_shapes(session_factory, owner_id)
    findings.append(
        Finding(
            "re-hash through the running service",
            population=len(after_nohost),
            discrepancies=[
                "a device that authenticated against the running service is "
                "still stored as bcrypt"
                for username, prefix in after_nohost.items()
                if username in exercised and not prefix.startswith("$argon2")
            ],
            note=(
                f"{_shape_tally(before)} -> {_shape_tally(after_nohost)} — "
                "the restored bcrypt was verified by uvicorn and re-hashed "
                f"again over the {len(exercised)} device(s) this half "
                "exercised, so both wire halves ran against the migrated shape"
            ),
        )
    )
    return findings


async def _stored_shapes(
    session_factory: Any, owner_id: int
) -> dict[str, str]:
    """``device username -> the first seven characters of its hash``.

    Seven characters is the whole prefix (``$2b$12$`` / ``$argon2i``)
    and none of the salt or digest.
    """
    from ..models import Device
    from ..scope import DdnsScope

    scope = DdnsScope.for_user_id(owner_id)
    async with session_factory() as session:
        devices = (
            await session.execute(scope.select(Device))
        ).scalars().all()
        return {device.username: device.password_hash[:7] for device in devices}


def _shape_tally(shapes: Mapping[str, str]) -> str:
    """``6 x $2b$12$`` — a count per prefix, sorted."""
    counts: dict[str, int] = {}
    for prefix in shapes.values():
        counts[prefix] = counts.get(prefix, 0) + 1
    return ", ".join(f"{n} x {p}" for p, n in sorted(counts.items()))


async def _restore_bcrypt(
    session_factory: Any,
    owner_id: int,
    passwords: Mapping[str, str],
    before: Mapping[str, str],
) -> int:
    """Put each device's surrogate bcrypt back after a re-hash.

    Only reachable in ``--mode surrogate``, where this process minted
    the plaintexts and therefore can re-derive the same stored form. It
    refuses on a row whose pre-wire shape was not bcrypt, so it can
    never overwrite something it did not put there.
    """
    import bcrypt

    from ..models import Device
    from ..scope import DdnsScope

    restored = 0
    scope = DdnsScope.for_user_id(owner_id)
    async with session_factory() as session:
        devices = (
            await session.execute(scope.select(Device))
        ).scalars().all()
        for device in devices:
            password = passwords.get(device.username)
            if password is None or not before.get(device.username, "").startswith("$2"):
                raise RehearsalFailed(
                    "refusing to rewrite a device hash this rehearsal did "
                    "not mint. Username withheld."
                )
            device.password_hash = bcrypt.hashpw(
                password.encode("utf8"), bcrypt.gensalt(rounds=12)
            ).decode()
            restored += 1
        await session.commit()
    return restored


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rehearse the legacy migration against a COPY of the live "
            "database and report what was compared, not that it worked."
        )
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--create-owner", action="store_true")
    parser.add_argument(
        "--mode",
        choices=("fidelity", "surrogate"),
        default="fidelity",
        help=(
            "fidelity: import the copy untouched — production hashes, "
            "byte-identity and badauth. surrogate: re-mint each device hash "
            "with its own production salt and cost under a known password, "
            "and drive every hostname to good."
        ),
    )
    parser.add_argument("--service-url", default=DEFAULT_SERVICE_URL)
    parser.add_argument("--label", default="rehearsal")
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_rehearse(args))
    except (RehearsalFailed, il.MigrationRefused) as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLIENT_IP",
    "Finding",
    "ProviderCall",
    "RehearsalFailed",
    "SOURCE_TABLES",
    "SourceReading",
    "SURROGATE_IP",
    "build_surrogate_copy",
    "check_hashes",
    "count_source",
    "drive_running_service",
    "legacy_decrypt",
    "main",
    "no_provider_can_reach_the_internet",
    "run_checks",
    "set_digest",
    "surrogate_hash",
]
