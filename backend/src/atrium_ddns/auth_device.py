"""HTTP Basic device authentication, and the per-device rate limiter.

Everything ``/nic/update`` and ``/nic/delete`` need before they are
allowed to look at a hostname. Split out of :mod:`atrium_ddns.router_nic`
because all of it is testable without a request and none of it is
about the DynDNS wire format.

Five things here are load-bearing and none of them are obvious from
the code.

**There is no atrium user on this path.** The caller is a router with
a username and a password, and the only thing it authenticates as is a
:class:`~atrium_ddns.models.Device`. No cookie, no session, no
``current_user`` dependency, no TOTP gate — plan §3.2. The tenant is
``device.user_id``, read off the row *after* the credential verifies,
and it is what every subsequent query is scoped by.

**The device lookup is the one query that cannot be scoped**, and
that is deliberate rather than an oversight. ``ddns_device.username``
is globally unique precisely because at the moment it is read there is
no tenant to scope by — the request has not been authenticated yet.
Everything downstream goes through :class:`~atrium_ddns.scope.DdnsScope`.

**Hashing is CPU-bound and blocks the event loop.** argon2id at
atrium's parameters is ~50 ms and bcrypt at cost 12 is ~250 ms *per
verify*. Awaiting one on the loop stalls every other request in the
worker, and a DDNS fleet checking in on a five-minute timer is a lot of
verifies. So every hash operation goes through
``anyio.to_thread.run_sync``, the same rule :mod:`atrium_ddns.providers`
states for boto3.

**A failed lookup still pays for a verify.** Without the dummy hash
below, "no such device" returns in microseconds and "wrong password"
returns in ~50 ms, so the response time enumerates the device list.
The dummy is computed once at import against a value nothing knows.

**A refusal names its reason and, when there is one, its owner.**
:func:`authenticate_device` answers :class:`DeviceAuth` or
:class:`FailedAuth` — never ``None``. Four refusals share one wire
answer and must not share one log row: a wrong password against a real
device belongs to that device's owner, and a username matching nothing
belongs to nobody. Collapsing both into an unattributed row is what
made ``response_code=badauth`` answer every tenant zero (#64). The
distinction costs no extra query — the ``Device``/``User`` pair is
already fetched below — and no extra disclosure, because the only
reader of the resulting row is the owner of the username tried.
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import anyio
import sqlalchemy as sa
from app.logging import log
from app.models.auth import User
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from .models import Device, RateLimitEvent
from .scope import DdnsScope

#: Argon2id **first**, bcrypt second — order is the whole mechanism.
#:
#: ``PasswordHash.verify_and_update`` identifies the stored hash by its
#: own prefix (``$2a$`` / ``$2b$`` / ``$2y$`` -> bcrypt, ``$argon2…`` ->
#: argon2id), verifies with that hasher, and returns a **re-hash under
#: the first hasher in this tuple** whenever the stored one is not it.
#: So "bcrypt for migrated rows, argon2id for new, re-hash
#: opportunistically on a successful bcrypt verify" (plan §3.2) is this
#: tuple's ordering and nothing else.
#:
#: ``PasswordHash.recommended()`` is deliberately not used: measured
#: against pwdlib 0.3.0 in this image it returns ``[Argon2Hasher]``
#: alone, so every migrated bcrypt row would raise ``UnknownHashError``
#: and answer ``badauth`` — the fleet would stop working at cutover,
#: with a wire response that looks exactly like a wrong password.
_PASSWORD_HASH = PasswordHash((Argon2Hasher(), BcryptHasher()))

#: bcrypt refuses a password longer than 72 bytes with a ``ValueError``
#: rather than truncating (bcrypt >= 4.1). An uncaught one is a 500 on
#: a path whose entire contract is "always answer 200 with a status
#: token", so the length is checked before the hasher sees it.
_BCRYPT_MAX_BYTES = 72

#: Verified against on every failed lookup so the timing of "no such
#: device" matches the timing of "wrong password". The plaintext is
#: never anything a caller can send: it is longer than the bcrypt limit
#: and is not stored anywhere.
_DUMMY_HASH = Argon2Hasher().hash("no-such-device-" + "x" * 64)

#: Read for the client address, and only this one. Divergence 9 in the
#: compat table: the legacy service sits behind ``ProxyFix(x_for=1)``
#: and ``Client-IP`` is never consulted. Honouring ``Client-IP`` would
#: let any caller nominate the address written into DNS through a
#: header nothing validates.
FORWARDED_FOR = "x-forwarded-for"


@dataclass(frozen=True)
class BasicCredentials:
    username: str
    password: str


def parse_basic_auth(header: str | None) -> BasicCredentials | None:
    """``Authorization: Basic <b64>`` -> credentials, or ``None``.

    ``None`` for every malformed shape — absent header, a non-Basic
    scheme, invalid base64, undecodable bytes, no colon. All of them
    end at ``badauth``, so distinguishing them on the wire would be a
    disclosure and nothing else; they are distinguished in the log
    instead.

    ``atr_pat_`` is not special-cased here on purpose. Atrium's
    ``PATAuthMiddleware`` reads ``Bearer`` only and refuses any request
    with a token in the path or query with ``400 token_in_url`` before
    the bearer parse, so a PAT can neither authenticate nor be smuggled
    past this function. Re-implementing either half here would be a
    second copy of a rule that is already enforced upstream.
    """
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token:
        log.debug("ddns.auth.non_basic_scheme", scheme=scheme[:16])
        return None
    try:
        raw = base64.b64decode(token.strip(), validate=True)
    except (binascii.Error, ValueError):
        log.debug("ddns.auth.basic_not_base64")
        return None
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        # latin-1 is what a router configured through a Windows dialog
        # is most likely to have sent. Not a security decision: both
        # spellings end at the same hash comparison.
        try:
            decoded = raw.decode("latin-1")
        except UnicodeDecodeError:  # pragma: no cover — latin-1 cannot fail
            return None
    username, sep, password = decoded.partition(":")
    if not sep:
        log.debug("ddns.auth.basic_without_colon")
        return None
    return BasicCredentials(username=username, password=password)


def _verify_sync(stored: str, presented: str) -> tuple[bool, str | None]:
    """``(verified, upgraded_hash_or_None)``. Blocking — never awaited.

    Every failure mode answers ``(False, None)``:

    * a stored hash no hasher recognises (a truncated column, a row
      migrated from something else) — ``UnknownHashError``;
    * a presented password bcrypt refuses to look at — over 72 bytes.

    Returning rather than raising is the point. This function sits
    behind a wire contract that answers HTTP 200 with a status token
    for *every* input; an exception escaping it is a 500 that no
    DynDNS client can parse.
    """
    if not stored or not presented:
        return False, None
    if len(presented.encode("utf-8")) > _BCRYPT_MAX_BYTES and not stored.startswith(
        "$argon2"
    ):
        log.info("ddns.auth.password_over_bcrypt_limit")
        return False, None
    try:
        return _PASSWORD_HASH.verify_and_update(presented, stored)
    except UnknownHashError:
        log.error("ddns.auth.unrecognised_stored_hash", prefix=stored[:7])
        return False, None
    except ValueError as exc:
        log.error("ddns.auth.verify_failed", error=str(exc))
        return False, None


async def verify_password(stored: str, presented: str) -> tuple[bool, str | None]:
    """:func:`_verify_sync` off the event loop."""
    return await anyio.to_thread.run_sync(_verify_sync, stored, presented)


async def hash_password(plaintext: str) -> str:
    """A fresh argon2id hash — what a newly issued device secret gets."""
    return await anyio.to_thread.run_sync(_PASSWORD_HASH.hash, plaintext)


async def _burn_dummy_verify(presented: str) -> None:
    """Spend a verify against a hash nothing matches.

    Called when the username does not resolve, so the two failures cost
    the same wall clock. Cheaper alternatives (a fixed ``sleep``) are
    worse: they are a constant an attacker can subtract, whereas this
    is the same distribution the real path draws from.
    """
    await anyio.to_thread.run_sync(_verify_sync, _DUMMY_HASH, presented or "-")


#: The four refusals :func:`authenticate_device` distinguishes.
#:
#: They are **one answer on the wire** — ``badauth``, always, because
#: telling them apart there tells an attacker which usernames exist.
#: They are four in the log, and two of them resolved a real device
#: before refusing, which is what makes the row attributable.
AUTH_FAILED_ABSENT = "absent_or_empty_credentials"
AUTH_FAILED_UNKNOWN_USERNAME = "unknown_username"
AUTH_FAILED_BAD_PASSWORD = "bad_password"
AUTH_FAILED_INACTIVE_OWNER = "inactive_owner"

#: The refusals that resolved a device, and therefore an owner.
#:
#: Derived from nothing — it is a claim, and
#: ``test_auth_device``/``test_router_nic`` check it against the
#: function's real behaviour rather than against this tuple.
ATTRIBUTABLE_AUTH_FAILURES = (
    AUTH_FAILED_BAD_PASSWORD,
    AUTH_FAILED_INACTIVE_OWNER,
)


@dataclass(frozen=True)
class FailedAuth:
    """A refused credential, and whom the attempt is attributable to.

    The counterpart to :class:`DeviceAuth`, and the reason
    :func:`authenticate_device` no longer answers ``None``. A bare
    ``None`` collapses four conditions into one, and the caller then
    writes a ``badauth`` row carrying no tenant **for all four** — which
    is how the log came to answer every tenant *zero* to
    ``response_code=badauth`` however many times their router failed.

    ``user_id`` is populated exactly when the submitted username
    resolved to a device: a wrong password against a real device, or a
    correct password held by a deactivated owner. It is ``None`` when
    the username matched nothing, because there is then nobody to
    attribute the attempt to — and that ``None`` is a *meaning*, not a
    missing value.

    **This costs no extra query and no extra disclosure.**
    :func:`authenticate_device` already fetches the ``Device``/``User``
    pair before it verifies anything; this class carries the row that
    was being thrown away. Nothing changes on the wire, nothing changes
    in the timing, and the only party who can read the resulting row is
    the owner of the username that was tried.
    """

    reason: str
    device: Device | None = None
    user_id: int | None = None
    user_email: str | None = None

    @property
    def attributable(self) -> bool:
        """Whether a log row written from this carries a tenant."""
        return self.user_id is not None


@dataclass(frozen=True)
class DeviceAuth:
    """A verified device, and the tenant it belongs to."""

    device: Device
    user_id: int
    user_email: str

    @property
    def scope(self) -> DdnsScope:
        """The tenant view every subsequent query goes through.

        Built from the id on the row rather than from a principal:
        there is no authenticated atrium user anywhere in this request,
        which is exactly the shape :meth:`DdnsScope.for_user_id`
        exists for. It carries no permissions, so it can never reach
        across tenants however the handler is later edited.
        """
        return DdnsScope.for_user_id(self.user_id)


async def authenticate_device(
    session: AsyncSession, credentials: BasicCredentials | None
) -> DeviceAuth | FailedAuth:
    """Resolve and verify a device, or say which refusal it was.

    **One answer on the wire, four in the log.** No credentials, no
    such device, wrong password, owner deactivated — the wire says
    ``badauth`` to all four, because telling them apart there tells an
    attacker which usernames exist. This function used to say ``None``
    to all four as well, and that is the defect #64 is about: the
    caller then wrote a ``badauth`` row with no ``user_id`` in every
    case, so a tenant filtering their log for ``badauth`` was served
    **zero** however many times their own router had failed — and zero
    is also what a healthy account looks like.

    So the refusal is now a :class:`FailedAuth` carrying the row this
    function had already fetched. Two of the four reasons resolved a
    real device and therefore a real owner (``bad_password``,
    ``inactive_owner``); the other two resolved nothing and stay
    unattributable, which is a state and not a gap.

    **Nothing about the wire, the timing or the disclosure changes.**
    The query below already ran before any of this; the dummy verify on
    an unknown username still burns the same distribution; every caller
    still answers ``badauth``. The only new reader of the resolution is
    the tenant who owns the username that was tried.

    **The owner's ``is_active`` is checked here**, not on the device.
    ``ddns_device`` has no active flag; the fixture's ``dave`` is an
    *inactive user* holding correct device credentials, and
    ``update-badauth-inactive-user`` is the frozen case that says so.
    Deactivating an account therefore stops its routers, which is the
    behaviour an operator expects from "deactivate".

    Re-hashing on a successful bcrypt verify is applied to the
    in-session row; the caller's commit persists it. A rollback loses
    the upgrade and nothing else — the next call redoes it.
    """
    if credentials is None or not credentials.username or not credentials.password:
        # No hash operation at all: there is nothing to compare. The
        # timing tell this leaves is "an empty password", which is not
        # a fact about the account.
        log.info("ddns.auth.absent_or_empty_credentials")
        return FailedAuth(reason=AUTH_FAILED_ABSENT)

    row = (
        await session.execute(
            sa.select(Device, User)
            .join(User, User.id == Device.user_id)
            .where(Device.username == credentials.username)
        )
    ).first()

    if row is None:
        await _burn_dummy_verify(credentials.password)
        log.info("ddns.auth.unknown_device", username=credentials.username)
        # Nobody to attribute this to. Not "attributed to nobody as a
        # fallback" — there is genuinely no owner, and the log row's
        # NULL user_id is that fact rather than a missing value.
        return FailedAuth(reason=AUTH_FAILED_UNKNOWN_USERNAME)

    device, user = row

    verified, upgraded = await verify_password(
        device.password_hash, credentials.password
    )
    if not verified:
        log.info(
            "ddns.auth.bad_password",
            username=credentials.username,
            device_id=device.id,
        )
        return FailedAuth(
            reason=AUTH_FAILED_BAD_PASSWORD,
            device=device,
            user_id=user.id,
            user_email=user.email,
        )

    if upgraded is not None:
        # The fleet migrates itself as routers check in — plan §3.2.
        device.password_hash = upgraded
        log.info(
            "ddns.auth.password_rehashed",
            device_id=device.id,
            to="argon2id",
        )

    if not user.is_active:
        log.info(
            "ddns.auth.inactive_owner", device_id=device.id, user_id=user.id
        )
        return FailedAuth(
            reason=AUTH_FAILED_INACTIVE_OWNER,
            device=device,
            user_id=user.id,
            user_email=user.email,
        )

    return DeviceAuth(device=device, user_id=user.id, user_email=user.email)


# --------------------------------------------------------------------- #
# The client address
# --------------------------------------------------------------------- #


def client_address(request: Request) -> str | None:
    """The address the service must consider the client's, normalised.

    ``None`` means *no parseable address*, which is a real state the
    frozen table asserts twice: ``checkip`` renders it as an empty
    string and ``update`` answers ``911`` for it. It is **not** the
    same as ``0.0.0.0`` and must never be rendered as one.

    ``X-Forwarded-For``, rightmost element, then the socket peer. The
    rightmost is what ``werkzeug.middleware.proxy_fix.ProxyFix`` with
    ``x_for=1`` takes (``values[-trusted]``), and the legacy service
    runs behind exactly that; taking the leftmost would let a client
    prepend any address it liked. An unparseable value is *not* skipped
    over in favour of the socket address — ``ProxyFix`` does not do
    that either, and ``client_ip: null`` in the table is arranged by
    sending an unparseable header precisely because omitting it leaves
    a perfectly parseable socket address behind.
    """
    forwarded = request.headers.get(FORWARDED_FOR)
    if forwarded is not None and forwarded.strip():
        candidate = forwarded.split(",")[-1].strip()
    else:
        candidate = request.client.host if request.client else ""
    return normalise_address(candidate)


def normalise_address(value: Any) -> str | None:
    """``str(ipaddress.ip_address(value))``, or ``None``.

    The normalisation is the observable half: the frozen table asserts
    ``2001:0DB8:0000::1`` comes back ``2001:db8::1``, that
    ``64:ff9b::192.0.2.33`` comes back ``64:ff9b::c000:221``, that a
    scope id survives (``fe80::1%eth0``), and that an IPv4-mapped
    address keeps its dotted tail (``::ffff:192.0.2.1``) rather than
    being unwrapped.
    """
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if not text:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


# --------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------- #

#: The window. One minute, matching the legacy
#: ``RateLimitConfig(requests_per_minute=30)`` the namespace default is
#: taken from.
RATE_LIMIT_WINDOW = timedelta(minutes=1)


def effective_rate_limit(device: Device, default_per_minute: int) -> int:
    """The device's limit, resolving ``NULL`` to the namespace default.

    ``NULL`` means *inherit* and ``0`` means *may never call*. They are
    two states and collapsing them is how a per-device override
    silently stops overriding: ``device.rate_limit_per_minute or
    default`` reads ``0`` as absent and hands an explicitly muted
    device the installation default.
    """
    override = device.rate_limit_per_minute
    return default_per_minute if override is None else override


async def check_rate_limit(
    session: AsyncSession,
    auth: DeviceAuth,
    *,
    default_per_minute: int,
    now: datetime | None = None,
) -> bool:
    """``True`` when the request may proceed; records it when it may.

    A table rather than a process-local counter, for the reason
    :class:`~atrium_ddns.models.RateLimitEvent` gives: api and worker
    are separate containers and api may be several processes, so an
    in-memory window is per-process, resets on every deploy, and lets a
    client multiply its allowance by the number of workers.

    **A refused request is not recorded.** The rows *are* the window —
    the index on ``(device_id, created_at)`` exists to answer "how many
    in the last minute for this device" — so recording refusals would
    make the window self-extending: a client hammering at ten times the
    limit would stay refused indefinitely rather than for one minute.
    The refusal is recorded in ``ddns_event`` instead, where it is
    searchable and does not feed back into the limiter.

    Reads through the scope even though ``device_id`` alone would be
    exact. Two reasons: the scope is the control this codebase applies
    to every host query without exception, and a hand-written
    ``device_id ==`` filter here is the shape that gets copied into the
    next query, where it is not exact. Note that #17 measured the
    limits of the audit signal — ``sa.true()`` is elided once ANDed, so
    a query log cannot be used to check the scope was applied on a
    composed statement; ``tests/test_auth_device.py`` asserts the
    behaviour instead.
    """
    limit = effective_rate_limit(auth.device, default_per_minute)
    if limit <= 0:
        log.info(
            "ddns.rate_limit.muted", device_id=auth.device.id, limit=limit
        )
        return False

    moment = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = moment - RATE_LIMIT_WINDOW

    scope = auth.scope
    used = (
        await session.execute(
            scope.select(RateLimitEvent, sa.func.count(RateLimitEvent.id)).where(
                RateLimitEvent.device_id == auth.device.id,
                RateLimitEvent.created_at >= cutoff,
            )
        )
    ).scalar_one()

    if used >= limit:
        log.info(
            "ddns.rate_limit.refused",
            device_id=auth.device.id,
            used=used,
            limit=limit,
        )
        return False

    session.add(RateLimitEvent(device_id=auth.device.id, created_at=moment))
    return True


__all__ = [
    "ATTRIBUTABLE_AUTH_FAILURES",
    "AUTH_FAILED_ABSENT",
    "AUTH_FAILED_BAD_PASSWORD",
    "AUTH_FAILED_INACTIVE_OWNER",
    "AUTH_FAILED_UNKNOWN_USERNAME",
    "FORWARDED_FOR",
    "RATE_LIMIT_WINDOW",
    "BasicCredentials",
    "DeviceAuth",
    "FailedAuth",
    "authenticate_device",
    "check_rate_limit",
    "client_address",
    "effective_rate_limit",
    "hash_password",
    "normalise_address",
    "parse_basic_auth",
    "verify_password",
]
