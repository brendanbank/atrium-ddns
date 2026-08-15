"""Row-level tenancy for the host's tables — the query-side control.

Atrium ships a no-op :class:`~app.auth.scope.AdminScope` and expects a
host that needs row-level filtering to define its own scope class plus
a ``get_scope`` dependency. This host needs it: domains, devices,
hostnames, provider bindings and events are all per-tenant.

**Every host query goes through this module. None of them write a
``user_id`` filter by hand** — the one that forgets is the leak, and it
will be the one written against a table added after this file was
reviewed. Plan §3.1.

Why this exists *even though* provider credentials are encrypted
per-user
------------------------------------------------------------------
The two controls cover different failures and it is tempting to think
the crypto subsumes this. It does not.

``UserSecret`` binds a ciphertext to its owner, so a blob cannot be
*decrypted* under the wrong owner. It has nothing to say about a query
that returns the wrong tenant's **row** in the first place — and every
non-secret column on that row (zone name, hostname, last IP, user
agent, the whole event history) is plaintext. A cross-tenant read that
returns someone else's zones and addresses and merely fails to reveal
their Route53 key is still a cross-tenant read. Plan §3.1.1, last
paragraph.

The shape of the API
--------------------
Four entry points, and every one of them refuses a model it has never
been told about::

    stmt = scope.select(Domain).order_by(Domain.name)     # a scoped SELECT
    stmt = scope.apply(select(...).join(...), Hostname)   # scope an existing one
    row  = await scope.get(session, Device, device_id)    # scoped primary-key get
    pred = scope.predicate(DnsEvent)                      # the clause itself

``session.get(Model, pk)`` is the one to watch: it goes through the
identity map, takes no ``WHERE`` clause, and therefore cannot be
scoped. :meth:`DdnsScope.get` is the replacement, and it is why the
method exists at all rather than leaving callers to write
``select(...).where(Model.id == pk)`` themselves.

Fail-closed, three ways
-----------------------
1. **A model that is not in the registry raises.** Not "returns
   everything" — a new table added by #15/#16/#17 that nobody
   classified is a hard error at the first query, and
   ``tests/test_tenant_isolation.py`` turns it into a hard error at
   test time instead.
2. **A scope with no tenant and no cross-tenant grant matches
   nothing.** :func:`sqlalchemy.false`, not an absent ``WHERE``. A bug
   that loses the user id returns zero rows rather than every row.
3. **"Unrestricted" is spelled in the SQL**, as a literal ``true``,
   rather than being the absence of a clause. An unscoped query and a
   deliberately cross-tenant one are otherwise indistinguishable to
   anyone reading a slow-query log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

import sqlalchemy as sa
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement, Delete, Select, Update

from app.auth.principal import Principal
from app.auth.rbac import current_principal
from app.models.auth import User

from .models import (
    AtriumDdnsState,
    Device,
    DnsEvent,
    Domain,
    DomainBackend,
    Hostname,
    HostBase,
    RateLimitEvent,
)

#: Holder of this sees every tenant's rows, on every model.
#: Seeded to ``admin`` (and auto-granted to ``super_admin``) by the
#: ``0002`` migration; ``user`` does not hold it.
CROSS_TENANT_PERMISSION = "atrium_ddns.admin"

#: Narrower: the audit log only. A support role that may read anyone's
#: update history without being able to touch anyone's zones.
EVENTS_CROSS_TENANT_PERMISSION = "atrium_ddns.events.read.all"


class ScopeError(Exception):
    """Base for the two refusals this module makes."""


class UnregisteredModel(ScopeError):
    """A model reached the scope without a declared tenancy path.

    Deliberately not "return it unfiltered". A table added by a later
    issue is exactly the one whose tenancy nobody thought about, and
    the failure mode of guessing is a silent cross-tenant read.
    """


class NotTenantScoped(ScopeError):
    """The model is registered as installation-wide, on purpose.

    Querying it through the scope is a category error rather than a
    security bug, so the message carries the recorded reason and the
    caller is expected to query it directly.
    """


# --------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class TenantPath:
    """How one model's owner is reached, declared once.

    ``clause`` is the thing the scope actually applies. ``owner_table``
    and ``through`` exist so the tests can *derive* what the emitted
    SQL should contain instead of restating it as a literal — a
    hardcoded expected string is the same defect one refactor later,
    and it would keep passing against a predicate naming the wrong
    table.
    """

    #: Table carrying the ``user_id`` this model's ownership resolves to.
    owner_table: str
    #: ``None`` when the model carries ``user_id`` itself; otherwise the
    #: name of the column on this model that reaches the owning row.
    through: str | None
    #: ``uid -> ColumnElement``. Built lazily so importing this module
    #: does not depend on mapper configuration order.
    clause: Callable[[int], ColumnElement[bool]]
    #: Permissions that lift the restriction for this model.
    cross_tenant_permissions: frozenset[str] = field(
        default_factory=lambda: frozenset({CROSS_TENANT_PERMISSION})
    )


def _owns_via_exists(
    owner: Any, owner_fk: Any, child_fk: Any, child: Any
) -> Callable[[int], ColumnElement[bool]]:
    """``EXISTS (SELECT 1 FROM owner WHERE owner.pk = child.fk AND owner.user_id = :uid)``.

    A correlated ``EXISTS`` rather than ``IN (SELECT …)``: MySQL 8
    optimises both to a semi-join, but ``IN`` with a NULL-able left
    side has three-valued-logic corners that ``EXISTS`` does not, and
    ``Hostname.device_id`` is nullable by design.

    ``.correlate()`` is explicit. Autocorrelation would do the right
    thing here, but it stops doing the right thing the moment the outer
    statement joins the owner table for its own reasons — and the
    symptom is a predicate that quietly compares the owner row to
    itself and matches everything.
    """

    def clause(uid: int) -> ColumnElement[bool]:
        return (
            # `literal_column("1")`, not `literal(1)`: the latter is a
            # bound parameter, so the tenant id stops being the only
            # value bound into the predicate and anything reading the
            # driver's parameter list has to know to skip one.
            sa.select(sa.literal_column("1"))
            .where(owner_fk == child_fk, owner.user_id == uid)
            .correlate(child)
            .exists()
        )

    return clause


#: Model -> how its owner is reached. **Adding a host model means
#: adding it here or to** :data:`UNSCOPED`; ``tests/test_tenant_isolation.py``
#: fails until one of the two happens.
TENANT_PATHS: dict[type[Any], TenantPath] = {
    Domain: TenantPath(
        owner_table=Domain.__tablename__,
        through=None,
        clause=lambda uid: Domain.user_id == uid,
    ),
    Device: TenantPath(
        owner_table=Device.__tablename__,
        through=None,
        clause=lambda uid: Device.user_id == uid,
    ),
    # Carries `user_id` itself only because `UserSecret` reads the owner
    # off the row holding the ciphertext (models.py). The scope filters
    # on that column rather than on `domain.user_id` so the two cannot
    # disagree about which rows are visible while the crypto is using
    # one of them to choose a key.
    DomainBackend: TenantPath(
        owner_table=DomainBackend.__tablename__,
        through=None,
        clause=lambda uid: DomainBackend.user_id == uid,
    ),
    # No `user_id` column, on purpose — the owner is `domain.user_id`
    # and a second copy would be a second source of truth for tenancy.
    # That decision is what makes this path an EXISTS.
    Hostname: TenantPath(
        owner_table=Domain.__tablename__,
        through="domain_id",
        clause=_owns_via_exists(Domain, Domain.id, Hostname.domain_id, Hostname),
    ),
    # `user_id` is nullable and `ON DELETE SET NULL`, so a deleted
    # tenant leaves events owned by nobody. `== uid` is NULL-safe in the
    # direction that matters: NULL never equals anything, so an orphan
    # is invisible to every tenant. Widening this to
    # `or_(user_id == uid, user_id.is_(None))` — which looks like a
    # kindness — hands every tenant the deleted tenant's history.
    DnsEvent: TenantPath(
        owner_table=DnsEvent.__tablename__,
        through=None,
        clause=lambda uid: DnsEvent.user_id == uid,
        cross_tenant_permissions=frozenset(
            {CROSS_TENANT_PERMISSION, EVENTS_CROSS_TENANT_PERMISSION}
        ),
    ),
    # Keyed to the device (plan §2), so its owner is the device's owner.
    # No writer until #16 — registered now because the table exists now,
    # and an unregistered model is a hard error at first use.
    RateLimitEvent: TenantPath(
        owner_table=Device.__tablename__,
        through="device_id",
        clause=_owns_via_exists(
            Device, Device.id, RateLimitEvent.device_id, RateLimitEvent
        ),
    ),
}

#: Model -> why it has no tenant. Every entry is a claim someone has to
#: make in writing; the empty string is not accepted by the tests.
UNSCOPED: dict[type[Any], str] = {
    AtriumDdnsState: (
        "Installation-wide singleton (id=1) behind the scaffold's demo "
        "widget. It has no owner column and no per-tenant meaning — one "
        "row, one message, one counter, the same for everybody. It goes "
        "when the real HTTP surface lands and this entry goes with it."
    ),
}


def host_models() -> set[type[Any]]:
    """Every class mapped on :class:`~atrium_ddns.models.HostBase`.

    Read off the registry rather than listed, so a model added in a
    later issue appears here the moment it is declared — which is the
    whole mechanism by which the classification check can fail for code
    that has not been written yet.
    """
    return {mapper.class_ for mapper in HostBase.registry.mappers}


def classify(model: type[Any]) -> TenantPath:
    """The tenancy path for ``model``, or a refusal naming the reason."""
    path = TENANT_PATHS.get(model)
    if path is not None:
        return path
    reason = UNSCOPED.get(model)
    if reason is not None:
        raise NotTenantScoped(
            f"{model.__name__} is registered as installation-wide and is not "
            f"queried through the scope: {reason}"
        )
    raise UnregisteredModel(
        f"{model.__name__} has no entry in atrium_ddns.scope.TENANT_PATHS and "
        f"none in UNSCOPED. Add one: a TenantPath if the rows belong to a "
        f"tenant, an UNSCOPED entry with a written reason if they do not. "
        f"Refusing to guess — guessing here is a cross-tenant read."
    )


# --------------------------------------------------------------------- #
# The scope
# --------------------------------------------------------------------- #

_Stmt = TypeVar("_Stmt", Select, Update, Delete)


@dataclass(frozen=True)
class DdnsScope:
    """One tenant's view of the host's tables.

    Frozen, and constructed through the classmethods rather than by
    hand at call sites, so the "which tenant" question is answered once
    per request (or once per ``/nic/update``) instead of once per query.

    ``user_id=None`` with no cross-tenant grant is a real and useful
    state: it matches nothing. That is the fail-closed direction, and
    it is what an anonymous or half-resolved caller gets.
    """

    user_id: int | None = None
    permissions: frozenset[str] = frozenset()
    #: Set only by :meth:`cross_tenant`. Non-``None`` lifts the
    #: restriction on every model without any permission being held —
    #: which is why it is a required argument there and absent here.
    cross_tenant_reason: str | None = None
    #: The atrium user, when there is one. ``/nic/update`` and the
    #: worker jobs have none, which is exactly why the tenant is an id
    #: and not a ``User``.
    user: User | None = None

    # --- construction ------------------------------------------------ #

    @classmethod
    def for_principal(cls, principal: Principal) -> DdnsScope:
        """From an authenticated atrium caller.

        ``principal.permissions`` is already *effective* — for a PAT it
        is the intersection of the token's scopes with the user's
        current permissions. So a token that does not carry
        ``atrium_ddns.admin`` cannot reach across tenants even when its
        owner could, and that falls out of using the principal rather
        than re-querying the user's roles here.
        """
        return cls(
            user_id=principal.user.id,
            permissions=frozenset(principal.permissions),
            user=principal.user,
        )

    @classmethod
    def for_user_id(cls, user_id: int) -> DdnsScope:
        """From a tenant id alone — the ``/nic/update`` shape.

        A router on HTTP Basic authenticates as a *device*; there is no
        atrium session anywhere in the request. Once the device row is
        found, ``device.user_id`` is the tenant, and every query the
        handler then makes goes through this. Carries no permissions,
        so it can never reach across tenants.
        """
        return cls(user_id=user_id, permissions=frozenset())

    @classmethod
    def cross_tenant(cls, *, reason: str) -> DdnsScope:
        """Every tenant's rows, for a job that legitimately sweeps them.

        The retention prune and the health-check job have no principal
        and are supposed to see everything. ``reason`` is required and
        must be non-empty: an unrestricted scope is worth one sentence
        of justification at the site that builds it, and the tests read
        it back.
        """
        if not reason or not reason.strip():
            raise ValueError(
                "cross_tenant(reason=…) needs a reason. An unrestricted "
                "scope with no stated purpose is the thing this module exists "
                "to make impossible to write by accident."
            )
        return cls(user_id=None, permissions=frozenset(), cross_tenant_reason=reason)

    # --- the predicate ------------------------------------------------ #

    def reaches_all_tenants(self, model: type[Any]) -> bool:
        """Whether this scope is unrestricted *for this model*.

        Per-model rather than global because ``atrium_ddns.events.read.all``
        opens the audit log and nothing else.
        """
        if self.cross_tenant_reason is not None:
            return True
        path = classify(model)
        return bool(self.permissions & path.cross_tenant_permissions)

    def predicate(self, model: type[Any]) -> ColumnElement[bool]:
        """The clause restricting ``model`` to this scope.

        Always a clause, never ``None``: the three outcomes are the
        tenant predicate, a literal ``true`` (cross-tenant, and visible
        as such in the SQL), and a literal ``false`` (no tenant
        identity — matches nothing).
        """
        path = classify(model)
        if self.reaches_all_tenants(model):
            return sa.true()
        if self.user_id is None:
            return sa.false()
        return path.clause(self.user_id)

    # --- the four entry points ---------------------------------------- #

    def apply(self, stmt: _Stmt, model: type[Any]) -> _Stmt:
        """Add the scope's predicate to an existing statement.

        Works for ``SELECT``, ``UPDATE`` and ``DELETE`` — a scoped read
        with an unscoped bulk update behind it is not a boundary.
        """
        return stmt.where(self.predicate(model))

    def select(self, model: type[Any], *entities: Any) -> Select:
        """A scoped ``SELECT``. ``entities`` defaults to the model itself."""
        return sa.select(*(entities or (model,))).where(self.predicate(model))

    async def get(
        self, session: AsyncSession, model: type[Any], pk: Any, *, pk_attr: str = "id"
    ) -> Any | None:
        """Scoped primary-key fetch — the replacement for ``session.get``.

        ``session.get`` consults the identity map and issues a lookup
        that takes no ``WHERE`` clause, so it cannot be scoped and will
        happily hand back another tenant's row (or, worse, one already
        loaded into the session by an earlier query). This is the same
        operation with the tenancy predicate attached.

        Returns ``None`` for both "no such row" and "not yours", on
        purpose: distinguishing them tells an attacker which ids exist.
        """
        stmt = self.select(model).where(getattr(model, pk_attr) == pk)
        return (await session.execute(stmt)).scalars().first()


#: What ``AdminScope`` is to atrium, ``DdnsScope`` is to this host. The
#: alias exists so host code can spell the dependency's return type the
#: way the atrium docs do.
Scope = DdnsScope


async def get_scope(
    principal: Principal = Depends(current_principal),
) -> DdnsScope:
    """FastAPI dependency — the host's replacement for ``app.auth.scope.get_scope``.

    Depends on ``current_principal`` rather than ``current_user`` so
    PAT-authenticated requests carry their *intersected* permission set
    (see :meth:`DdnsScope.for_principal`), and so the cookie path keeps
    atrium's TOTP gate — ``current_principal`` resolves through
    ``current_user``, which enforces it.
    """
    return DdnsScope.for_principal(principal)


__all__ = [
    "CROSS_TENANT_PERMISSION",
    "EVENTS_CROSS_TENANT_PERMISSION",
    "TENANT_PATHS",
    "UNSCOPED",
    "DdnsScope",
    "NotTenantScoped",
    "Scope",
    "ScopeError",
    "TenantPath",
    "UnregisteredModel",
    "classify",
    "get_scope",
    "host_models",
]
