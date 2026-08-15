"""DNS provider adapters, and the registry that resolves one.

Public surface, and all ``/nic/*`` should need:

    from atrium_ddns.providers import ProviderAccount, get_provider

    provider = get_provider(ProviderAccount(
        service=row.backend_type,
        domains=(domain.name,),
        credentials=row.credentials.reveal(),
        config=row.config,
        id=str(row.id),
    ))
    if provider is None:
        status = STATUS_UNKNOWN_SERVICE          # "911"
    else:
        status = provider.createrecords(ip, zones, rtype=rtype, ttl=ttl)

Every adapter call is **blocking**; see :mod:`.base`. Run them in a
thread.

Auto-discovery was replaced with explicit registration — deliberately
--------------------------------------------------------------------

The legacy ``AccountFactory`` globbed ``lib/account/*.py`` off the
filesystem, imported every match, then walked ``dir()`` over the package
module looking for ``BaseAccount`` subclasses, sorted them by a
``_priority`` class attribute and asked each one's ``match()`` in turn.
This package uses a dict keyed on the service name, populated by
:data:`_PROVIDERS` below. Four reasons, in the order they matter:

1. **``glob`` over ``__file__``'s directory does not survive
   packaging.** This backend is installed as a wheel into the atrium
   image (``pip install /opt/host_app``); a zipimport or any
   non-filesystem loader gives a ``__file__`` that ``glob`` returns
   nothing for, and the failure mode is not an ``ImportError`` — it is
   an empty registry, every backend resolving to ``None``, and every
   hostname answering a perfectly well-formed ``911``. That is the
   "artefact with no writer" shape: a confident wrong answer with no
   traceback anywhere.
2. **The question is a dict lookup wearing a linear scan's clothes.**
   ``backend_type`` is one ``String(64)`` column with
   ``UNIQUE(domain_id, backend_type)`` over it. ``match()`` and
   ``_priority`` exist to arbitrate between predicates that could both
   claim an account; there are no such predicates, and ``_priority`` was
   ``255`` for all three adapters, so the sort never ordered anything.
3. **The set of providers becomes readable.** ``known_services()`` is
   derived from the same tuple the registry is built from, so a provider
   deleted from :data:`_PROVIDERS` takes its advertised name with it and
   cannot linger in a hand-kept list.
4. **A test can add one.** ``protocol_cases.yaml``'s fixture needs a
   ``stub`` service "the account factory knows and whose result is
   scripted", and #16 has to supply it. :func:`register` is that seam,
   with :func:`unregister` to undo it, rather than a file dropped into a
   directory the importer happens to scan.

What is *kept* from the legacy factory: resolution by the stored service
name, a ``None`` answer for a name nobody claims, and ``known_services``.
``model_cases.yaml::provider-is-selected-by-the-stored-service-name``
asserts exactly those and is satisfied by either implementation.
"""
from __future__ import annotations

from typing import Any, Mapping

import structlog

from .base import (
    ADDRESS_FAMILIES,
    DEFAULT_TTL,
    PROVIDER_STATUSES,
    RTYPE_A,
    RTYPE_AAAA,
    STATUS_DNSERR,
    STATUS_GOOD,
    STATUS_NOCHG,
    STATUS_UNKNOWN_SERVICE,
    BaseProvider,
    ProviderAccount,
)
from .hetzner import HetznerProvider
from .nsupdate import NsUpdateProvider
from .route53 import Route53Provider

log = structlog.get_logger(__name__)

#: The providers this build ships. The registry and ``known_services()``
#: are both derived from it; nothing else enumerates providers.
_PROVIDERS: tuple[type[BaseProvider], ...] = (
    Route53Provider,
    HetznerProvider,
    NsUpdateProvider,
)

_REGISTRY: dict[str, type[BaseProvider]] = {}


def register(provider: type[BaseProvider]) -> type[BaseProvider]:
    """Bind every name a provider answers to. Refuses a collision.

    Refusing rather than overwriting: two providers claiming one
    ``backend_type`` is a packaging mistake, and silently letting the
    last import win would make which adapter runs depend on import
    order.
    """
    names = provider.service_names()
    if not provider.SERVICE:
        raise ValueError(f"{provider.__name__} declares no SERVICE")
    for name in names:
        existing = _REGISTRY.get(name)
        if existing is not None and existing is not provider:
            raise ValueError(
                f"service {name!r} is already registered to "
                f"{existing.__name__}; {provider.__name__} cannot claim it"
            )
    for name in names:
        _REGISTRY[name] = provider
    return provider


def unregister(provider: type[BaseProvider]) -> None:
    """Undo :func:`register`. For tests; nothing in the app calls it."""
    for name in provider.service_names():
        if _REGISTRY.get(name) is provider:
            del _REGISTRY[name]


for _provider in _PROVIDERS:
    register(_provider)


def provider_class(service: Any) -> type[BaseProvider] | None:
    """The class registered for ``service``, or ``None``.

    Case-sensitive and whitespace-sensitive, matching the stored column
    exactly. Normalising here would make ``ROUTE53`` resolve while the
    ``UNIQUE(domain_id, backend_type)`` constraint still treats it as a
    distinct row, so two rows would collapse onto one adapter and the
    UI would show a duplicate that the API insists is not one.
    """
    if not isinstance(service, str):
        return None
    return _REGISTRY.get(service)


def get_provider(account: ProviderAccount) -> BaseProvider | None:
    """Resolve and construct the adapter for ``account``, or ``None``.

    ``None`` — never an exception — for a service nobody claims, and
    also for a provider whose construction failed. The caller answers
    :data:`~atrium_ddns.providers.base.STATUS_UNKNOWN_SERVICE` (``911``)
    to both, which is what
    ``protocol_cases.yaml::update-911-backend-service-unknown-to-factory``
    expects on the wire.

    Construction is not supposed to be able to fail — zone discovery is
    already wrapped — so the second branch is a backstop rather than a
    path. It logs at ``critical`` with the exception text intact,
    because the alternative is a tenant seeing ``911`` forever with
    nothing in the log to say why.
    """
    cls = provider_class(account.service)
    if cls is None:
        log.warning(
            "provider.unknown_service",
            service=account.service,
            account_id=account.id,
            known=known_services(),
        )
        return None

    try:
        return cls(account)
    except Exception as exc:  # noqa: BLE001
        log.critical(
            "provider.construction_failed",
            service=account.service,
            account_id=account.id,
            error=str(exc),
        )
        return None


def known_services() -> tuple[str, ...]:
    """Canonical service names, sorted. Aliases are resolvable, not offered."""
    return tuple(
        sorted({name for cls in _PROVIDERS for name in cls.known_services()})
    )


def resolvable_services() -> tuple[str, ...]:
    """Every name :func:`get_provider` resolves, aliases included."""
    return tuple(sorted(_REGISTRY))


def account_from_mapping(data: Mapping[str, Any]) -> ProviderAccount:
    """Build a :class:`ProviderAccount` from the legacy dict shape.

    Kept narrow on purpose: it exists so ported tests and the V1M4
    importer can read a legacy ``account`` dict without spreading the
    stringly-typed shape any further.
    """
    return ProviderAccount(
        service=data["service"],
        domains=tuple(data.get("domains") or ()),
        credentials=data.get("credentials") or {},
        config=data.get("config") or {},
        id=data.get("id"),
        description=data.get("description", ""),
    )


__all__ = [
    "ADDRESS_FAMILIES",
    "DEFAULT_TTL",
    "PROVIDER_STATUSES",
    "RTYPE_A",
    "RTYPE_AAAA",
    "STATUS_DNSERR",
    "STATUS_GOOD",
    "STATUS_NOCHG",
    "STATUS_UNKNOWN_SERVICE",
    "BaseProvider",
    "HetznerProvider",
    "NsUpdateProvider",
    "ProviderAccount",
    "Route53Provider",
    "account_from_mapping",
    "get_provider",
    "known_services",
    "provider_class",
    "register",
    "resolvable_services",
    "unregister",
]
