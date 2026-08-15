"""The compat fixture's scripted DNS provider. **Never enabled in production.**

``tests/compat/protocol_cases.yaml``'s ``fixture:`` block asks for
``service: stub`` — "a provider the account factory knows and whose
result is scripted by ``result``". Every real adapter reaches a real
nameserver, so without one of these the frozen table cannot be run
against anything at all. ``tests/compat/README.md`` §2 describes the
same module on the legacy side; this is its host counterpart.

**Why it lives in the installed package rather than under ``tests/``.**
The table is replayed over the wire against the running api process, so
the provider has to be resolvable *inside that process*. ``tests/`` is
copied to ``/opt/compat_tests`` in the Dockerfile's ``dev`` stage and is
not on the api process's ``sys.path``; putting the stub there and then
arranging for the app to import it would be a longer path to the same
place, with an extra way to go wrong.

**Two independent gates, and both must be open.** A provider that
answers ``good`` without writing DNS is the "confident wrong answer"
shape: a tenant would see a green update and an unchanged zone, with
nothing in any log to say why. So:

1. ``ATRIUM_DDNS_COMPAT_STUB=1`` must be set — a deliberate opt-in
   naming the thing it enables, absent from ``.env.example`` and from
   the deploy host;
2. ``ENVIRONMENT`` must not be ``prod`` — so the opt-in cannot be
   pasted onto a production stack and work.

Each is testable on its own and
``backend/tests/test_compat_stub.py`` tests both directions of both.
:func:`register_stub_providers` logs at ``warning`` when it opens,
because a stack with fake DNS backends registered should say so on
every boot rather than only in a file somebody has to think to read.

**Three slots, behaviour in ``config``.** ``ddns_domain_backend`` is
``UNIQUE(domain_id, backend_type)`` and the fixture needs up to three
differently-scripted backends on one domain
(``firsterr.example.com`` is ordered ``nochg, 911, dnserr``). So the
service names are *slots* — ``stub1``, ``stub2``, ``stub3`` — and the
scripted result is a column on the row (``config["result"]``), not part
of the name. Encoding the result in the name instead would mean a
name per behaviour per slot, and the name would then have to be
reverse-engineered to read the fixture.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Sequence

from app.logging import log
from app.settings import get_settings

from .providers import (
    PROVIDER_STATUSES,
    STATUS_DNSERR,
    BaseProvider,
    provider_class,
    register,
    unregister,
)

#: The opt-in. Deliberately not read anywhere inside
#: :mod:`atrium_ddns.providers` — ``test_providers.py`` sweeps that
#: package's source for any reference to the environment at all, and
#: that sweep is what stops a real adapter reintroducing a credential
#: fallback. This module is outside it.
ENABLE_VAR = "ATRIUM_DDNS_COMPAT_STUB"

#: Slot names. Three because ``firsterr.example.com`` needs three
#: backends on one domain and the unique constraint is on
#: ``(domain_id, backend_type)``.
SLOTS: tuple[str, ...] = ("stub1", "stub2", "stub3")

#: What ``config["result"]`` may say. ``911`` is deliberately absent:
#: the fixture produces it by storing no credentials or by naming a
#: service the factory does not know, which are the two conditions the
#: table's spec rows actually describe. A stub that could *return*
#: ``911`` would let a green run coexist with a router that never
#: checks credentials.
SCRIPTABLE: frozenset[str] = PROVIDER_STATUSES


@dataclass(frozen=True)
class StubCall:
    """One recorded provider call — the ``effects:`` instrument.

    The frozen table's 25 ``effects:`` blocks carry DNS operations that
    no wire runner asserts, and #29 proved the gap is total: mutating
    ``getiptype`` to answer ``A`` for every address ran the whole table
    at 121 executed, 0 failed. ``rtype`` below is the field that
    mutation would have changed, and reading it is how
    ``backend/tests/test_router_nic.py`` asserts the record type the
    wire cannot see.
    """

    service: str
    op: str
    hostname: str
    rtype: str | None
    ip: str | None
    ttl: int | None
    result: str
    #: The thread this call ran on. boto3, httpx and dnspython are all
    #: synchronous (:mod:`atrium_ddns.providers`), so a provider call
    #: awaited on the event loop stalls every other request in the
    #: worker — and it will not show up in a unit test that only reads
    #: the answer. Recorded so one can.
    thread_id: int = 0


#: Append-only within a process. Read (and cleared) by the host tests;
#: the wire runner is a different process and never sees it.
CALLS: list[StubCall] = []


def reset_calls() -> None:
    CALLS.clear()


class _StubProvider(BaseProvider):
    """Answers whatever the row says, and touches no network.

    Everything above ``createrecords`` / ``deleterecords`` is
    unmodified :class:`~atrium_ddns.providers.base.BaseProvider`: the
    zone check (``hostnameperzone`` / ``domaininhostname``), the
    credential check and the factory lookup all run for real. That is
    the same rule ``tests/compat/README.md`` states for the legacy
    stub — "keep the module strictly below ``hostnameperzone``, or the
    table is calibrating against you".
    """

    SERVICE: ClassVar[str] = ""
    REQUIRED_CREDENTIALS: ClassVar[tuple[str, ...]] = ()

    def _scripted(self) -> str:
        result = self._account.config.get("result")
        if result not in SCRIPTABLE:
            log.error(
                "compat_stub.unscriptable_result",
                service=self.SERVICE,
                account_id=self._account.id,
                result=repr(result),
                scriptable=sorted(SCRIPTABLE),
            )
            return STATUS_DNSERR
        return str(result)

    def createrecords(
        self,
        ip: str,
        hostname_zones: Mapping[str, Sequence[str]],
        rtype: str = "A",
        ttl: int = 60,
    ) -> dict[str, str]:
        status = self._scripted()
        out: dict[str, str] = {}
        for hostnames in hostname_zones.values():
            for hostname in hostnames:
                CALLS.append(
                    StubCall(
                        service=self.SERVICE,
                        op="create",
                        hostname=hostname,
                        rtype=rtype,
                        ip=ip,
                        ttl=ttl,
                        result=status,
                        thread_id=threading.get_ident(),
                    )
                )
                out[hostname] = status
        return out

    def deleterecords(
        self,
        hostname_zones: Mapping[str, Sequence[str]],
        rtype: str | None = None,
    ) -> dict[str, str]:
        status = self._scripted()
        out: dict[str, str] = {}
        for hostnames in hostname_zones.values():
            for hostname in hostnames:
                CALLS.append(
                    StubCall(
                        service=self.SERVICE,
                        op="delete",
                        hostname=hostname,
                        # `None` means "both families", and it is a
                        # third state beside "A" and "AAAA" rather than
                        # a missing one. The frozen
                        # `delete-myip-absent-deletes-both-families`
                        # carries `rtype: null` and this is where that
                        # is observable.
                        rtype=rtype,
                        ip=None,
                        ttl=None,
                        result=status,
                        thread_id=threading.get_ident(),
                    )
                )
                out[hostname] = status
        return out


def _slot_class(name: str) -> type[_StubProvider]:
    return type(
        f"StubProvider_{name}",
        (_StubProvider,),
        {"SERVICE": name, "__doc__": _StubProvider.__doc__},
    )


#: One class per slot, built rather than written out three times so a
#: slot added here cannot drift from the tuple above.
STUB_CLASSES: tuple[type[_StubProvider], ...] = tuple(
    _slot_class(name) for name in SLOTS
)


def stub_providers_allowed() -> tuple[bool, str]:
    """``(allowed, reason)`` — both gates, with the reason either way.

    Returns the reason rather than a bare boolean so the log line and
    the test assertion read the same sentence, and so a stack where the
    stub is *not* registered can say which gate was shut.
    """
    if os.environ.get(ENABLE_VAR) != "1":
        return False, f"{ENABLE_VAR} is not '1'"
    environment = get_settings().environment
    if environment == "prod":
        return (
            False,
            f"{ENABLE_VAR}=1 but ENVIRONMENT={environment!r}: the compat "
            "stub answers `good` without writing DNS and is refused in "
            "production regardless of the opt-in",
        )
    return True, f"{ENABLE_VAR}=1 and ENVIRONMENT={environment!r}"


def register_stub_providers(*, force: bool = False) -> tuple[str, ...]:
    """Register the slots when both gates are open. Returns the names.

    ``force`` is for the host tests, which import this module directly
    and need the classes registered without setting process-wide
    environment. It is not reachable from :mod:`atrium_ddns.bootstrap`.
    """
    if not force:
        allowed, reason = stub_providers_allowed()
        if not allowed:
            log.debug("compat_stub.not_registered", reason=reason)
            return ()
        log.warning(
            "compat_stub.registered",
            reason=reason,
            slots=list(SLOTS),
            detail=(
                "this stack resolves the compat fixture's scripted DNS "
                "backends. They answer good/nochg/dnserr without "
                "contacting any nameserver."
            ),
        )
    for cls in STUB_CLASSES:
        if provider_class(cls.SERVICE) is None:
            register(cls)
    return SLOTS


def unregister_stub_providers() -> None:
    """Undo :func:`register_stub_providers`. For tests."""
    for cls in STUB_CLASSES:
        unregister(cls)


def scripted_config(result: str, **extra: Any) -> dict[str, Any]:
    """The ``ddns_domain_backend.config`` a scripted backend needs."""
    if result not in SCRIPTABLE:
        raise ValueError(
            f"{result!r} is not scriptable; expected one of "
            f"{sorted(SCRIPTABLE)}. `911` is produced by absent "
            "credentials or an unknown service name, not by a stub."
        )
    return {"result": result, **extra}


__all__ = [
    "CALLS",
    "ENABLE_VAR",
    "SCRIPTABLE",
    "SLOTS",
    "STUB_CLASSES",
    "StubCall",
    "register_stub_providers",
    "reset_calls",
    "scripted_config",
    "stub_providers_allowed",
    "unregister_stub_providers",
]
