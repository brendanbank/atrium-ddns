"""RFC 2136 dynamic update, TSIG-signed, through dnspython.

Ported from ``dyndns-route53``'s ``lib/account/nsupdate.py``.

No zone API, so no discovery: the zones are exactly the domains the row
names, and :meth:`BaseProvider._discover_zones` stays a no-op.

**Only ``nsupdate_secret`` is read exclusively from the encrypted
column.** The other three settings — the TSIG key *name*, the algorithm
and the nameserver address — are not secrets, and ``models.py`` says so
in as many words ("Non-secret provider settings: hosted-zone id, TTL,
nameserver address, TSIG algorithm"). They are accepted from
``ddns_domain_backend.config`` as well as from the credential blob, the
latter winning, so a row imported wholesale from the legacy service
keeps working while a row created through the new UI can put three of
the four where they belong. A ``nsupdate_secret`` found in ``config`` is
**refused and logged**, not used: ``config`` is a plain JSON column and
treating it as a place a TSIG secret may live would quietly undo the
encryption.

**The response rcode IS read, and that is a deliberate divergence from
the legacy adapter. Do not "fix" this back to match it.** #142, settled
by the milestone owner on 2026-09-01.

``dns.query.tcp`` returns the nameserver's answer, and a non-zero rcode
in it means the zone was not written. Nothing raises: a ``REFUSED`` from
BIND — the ordinary shape of an ``update-policy`` denying a key, a zone
that is not a primary here, or an ACL the operator tightened — is a
well-formed, correctly TSIG-signed message, and dnspython hands it back
exactly as it hands back a ``NOERROR``. So the check has to be written
or it does not happen.

Legacy does not write it. ``dyndns-route53``'s
``lib/account/nsupdate.py:61`` binds ``response``, logs it at debug on
line 67, and never reads it — so every non-zero rcode reaches the tenant
as ``good``, the client does not retry, and the zone stays stale for as
long as it keeps sending the same address. ``base.py``'s own comment on
``check_hostnameon_server`` names that direction as the unsafe one.

Two things make this the right divergence rather than a rewrite
regression pointing the other way:

* **Legacy already reads rcodes — in the other adapter half.**
  ``lib/accounts.py:229`` reads ``response.rcode()``, compares it to
  ``dns.rcode.NOERROR`` and renders it with ``dns.rcode.to_text`` in the
  log, in ``get_authoritative_nameserver``. So this is not a new idiom
  imported into a codebase that had none; it is legacy's *own* idiom
  applied to the one call site legacy forgot.
* **The frozen wire table has no opinion to overrule.** Every backend
  ``tests/compat/protocol_cases.yaml`` declares is ``service: stub`` or
  ``service: no-such-service``, so no case in it reaches this module —
  asserted, not assumed, by
  ``test_nsupdate_receiver.py::test_the_wire_table_cannot_reach_this_adapter``.

Every non-zero rcode is covered, not just ``REFUSED``: a fix keyed on
one rcode is the same defect with a smaller blast radius. The evidence
is ``backend/tests/test_nsupdate_receiver.py::TestTheRcodeIsReadNow``
— formerly ``TestTheRcodeIsNotRead``, which pinned the old answer —
driving each header rcode through a receiver that really signs and
really refuses. ``test_providers.py``'s ``FakeNsUpdate`` returns the
*request* from ``tcp()`` and a ``dns.update.Update`` reads as
``NOERROR``, so the mocked suite passes either way and is not evidence
about this at all.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import dns.message
import dns.query
import dns.rcode
import dns.tsig
import dns.tsigkeyring
import dns.update
import structlog

from .base import (
    SettingField,
    DEFAULT_TTL,
    RTYPE_A,
    STATUS_DNSERR,
    STATUS_GOOD,
    STATUS_NOCHG,
    BaseProvider,
)

log = structlog.get_logger(__name__)

#: Seconds, as the legacy adapter spells it.
QUERY_TIMEOUT = 10.0

#: Settings that must all be resolved before an update is attempted.
REQUIRED_SETTINGS: tuple[str, ...] = (
    "nsupdate_key",
    "nsupdate_algo",
    "nsupdate_secret",
    "nsupdate_nameserver",
)

#: The one setting that may only ever come from the encrypted column.
SECRET_SETTINGS: frozenset[str] = frozenset({"nsupdate_secret"})


class UpdateRefused(Exception):
    """The nameserver answered a well-formed refusal.

    Not an error dnspython raises — it is a message that parsed, that
    authenticated, and that says the zone was not written. Raised inside
    the same ``try`` as ``dns.query.tcp`` so a refusal and a transport
    fault take one path to ``dnserr``, and carrying the rcode by name so
    the diagnosis is one grep rather than three deploys.
    """

    def __init__(self, rcode: int) -> None:
        self.rcode = rcode
        #: ``REFUSED``, ``SERVFAIL``, ``NOTZONE`` … The text, not the
        #: integer: a log line reading ``rcode=5`` is a lookup, and #142
        #: exists because a diagnosis that costs a lookup does not get
        #: made.
        self.rcode_text = dns.rcode.to_text(rcode)
        super().__init__(f"nameserver answered {self.rcode_text}")


def _raise_for_rcode(response: dns.message.Message) -> None:
    """Refuse anything that is not ``NOERROR``.

    Deliberately not a list of the rcodes RFC 2136 §2.2 names.
    ``NOTZONE``, ``NXRRSET``, ``YXRRSET``, ``NOTAUTH``, ``SERVFAIL`` and
    ``REFUSED`` are the ones seen in practice, but a fix keyed on an
    enumeration is the same defect with a smaller blast radius: a rcode
    left off the list is reported to the tenant as ``good``. The only
    rcode that means the zone was written is ``NOERROR``, so that is the
    one this tests for.
    """
    rcode = response.rcode()
    if rcode != dns.rcode.NOERROR:
        raise UpdateRefused(rcode)


#: The TSIG algorithms dnspython will actually accept, read off
#: ``dns.tsig`` rather than typed from memory — a name this list gets
#: wrong is a publish-time failure from a value the form offered.
#: ``HMAC-MD5`` is deliberately last: BIND still accepts it and legacy
#: keys exist, but it should not be the one anybody reaches for first.
TSIG_ALGORITHMS: tuple[str, ...] = (
    str(dns.tsig.HMAC_SHA256),
    str(dns.tsig.HMAC_SHA512),
    str(dns.tsig.HMAC_SHA384),
    str(dns.tsig.HMAC_SHA224),
    str(dns.tsig.HMAC_SHA1),
    str(dns.tsig.HMAC_MD5),
)


class NsUpdateProvider(BaseProvider):
    SERVICE = "nsupdate"
    REQUIRED_CREDENTIALS = ("nsupdate_secret",)
    CREDENTIAL_LABELS = {
        "nsupdate_secret": (
            "TSIG secret",
            "The base64 secret from the same `key` block as the name and "
            "algorithm above. Stored encrypted and never shown again.",
        ),
    }
    #: The three non-secret halves of a TSIG configuration. All required:
    #: ``settings()`` already treats every one of ``REQUIRED_SETTINGS`` as
    #: mandatory and ``has_credentials`` returns False without them, so a
    #: binding missing any of these contributes ``911`` on the wire. The
    #: form refusing it is the same rule stated where it can be acted on.
    SETTING_FIELDS = (
        SettingField(
            key="nsupdate_nameserver",
            label="Nameserver",
            help=(
                "Address of the server that accepts the dynamic update — "
                "the machine running BIND or Knot, not the zone name."
            ),
            required=True,
        ),
        SettingField(
            key="nsupdate_key",
            label="TSIG key name",
            help=(
                "The key's name as the nameserver knows it, without the "
                "trailing dot. From the `key \"...\" { }` block in named.conf."
            ),
            required=True,
        ),
        SettingField(
            key="nsupdate_algo",
            label="TSIG algorithm",
            help="Must match the algorithm in the nameserver's key block.",
            choices=TSIG_ALGORITHMS,
            required=True,
            default=str(dns.tsig.HMAC_SHA256),
        ),
    )

    def settings(self) -> dict[str, Any]:
        """The four values an update needs, resolved from row + config."""
        resolved: dict[str, Any] = {}
        config = self._account.config
        # The property, not ``self._account.credentials`` — see
        # ``BaseProvider.has_credentials`` on why the two must not
        # diverge.
        credentials = self.credentials

        for key in REQUIRED_SETTINGS:
            if key in SECRET_SETTINGS:
                if key in config:
                    log.error(
                        "provider.secret_in_plaintext_config",
                        service=self.SERVICE,
                        account_id=self._account.id,
                        key=key,
                    )
                resolved[key] = credentials.get(key)
            else:
                resolved[key] = credentials.get(key) or config.get(key)

        return resolved

    def has_credentials(self) -> bool:
        settings = self.settings()
        return all(settings.get(key) for key in REQUIRED_SETTINGS)

    def _message(self, zonename: str, settings: Mapping[str, Any]) -> dns.update.Update:
        keyring = dns.tsigkeyring.from_text(
            {settings["nsupdate_key"] + ".": settings["nsupdate_secret"]}
        )
        return dns.update.Update(
            zonename + ".",
            keyring=keyring,
            keyalgorithm=settings["nsupdate_algo"],
        )

    # -- operations ------------------------------------------------ #

    def createrecords(
        self,
        ip: str,
        hostname_zones: Mapping[str, Sequence[str]],
        rtype: str = RTYPE_A,
        ttl: int = DEFAULT_TTL,
    ) -> dict[str, str]:
        if not self.has_credentials():
            return self._missing_credentials_result(hostname_zones, "create")

        settings = self.settings()
        nameserver = settings["nsupdate_nameserver"]
        results: dict[str, str] = {}

        for zonename, hostnames in hostname_zones.items():
            for hostname in hostnames:
                # Asks the *configured* nameserver rather than walking
                # the delegation chain: an nsupdate backend is normally
                # a hidden primary, and the public authoritative servers
                # may be minutes behind it. Asking the wrong server here
                # turns a `nochg` into a redundant write, which is
                # harmless, or a real change into a `nochg`, which is
                # not.
                if self.check_hostnameon_server(
                    hostname, ip, rtype, nameserver=nameserver
                ):
                    results[hostname] = STATUS_NOCHG
                    continue

                try:
                    update = self._message(zonename, settings)
                    # `rtype` becomes the rdatatype of the RRset in the
                    # UPDATE section. It is the only place the record
                    # type appears, and it is asserted for both families
                    # in `test_providers.py` by reading the message this
                    # call produces.
                    update.replace(hostname + ".", ttl, rtype, ip)
                    response = dns.query.tcp(
                        update, nameserver, timeout=QUERY_TIMEOUT
                    )
                    _raise_for_rcode(response)
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "provider.create_failed",
                        service=self.SERVICE,
                        hostname=hostname,
                        rtype=rtype,
                        # ``None`` for a transport fault, the rcode name
                        # for a refusal. Two states, two renderings: a
                        # transport fault and a REFUSED are different
                        # things to go and look at.
                        rcode=getattr(exc, "rcode_text", None),
                        error=str(exc),
                    )
                    results[hostname] = STATUS_DNSERR
                    continue

                results[hostname] = STATUS_GOOD
                log.info(
                    "provider.created",
                    service=self.SERVICE,
                    hostname=hostname,
                    rtype=rtype,
                    ip=ip,
                )

        return results

    def deleterecords(
        self,
        hostname_zones: Mapping[str, Sequence[str]],
        rtype: str | None = None,
    ) -> dict[str, str]:
        """Remove records. ``rtype=None`` means both families.

        **This adapter cannot answer ``nochg``, and that is a preserved
        divergence.** ``model_cases.yaml::provider-delete-returns-nochg-
        when-the-record-was-absent`` is evidenced against the Hetzner
        adapter, which sees a 404 per family. RFC 2136 answers NOERROR
        for the deletion of an RRset that was not there, so telling the
        two apart would need a prequery per family per hostname on the
        request path. The legacy adapter answers ``good`` and so does
        this one; the wire consequence is that a repeated
        ``/nic/delete`` against an nsupdate backend says ``good`` where
        a Hetzner backend says ``nochg``, which no case in the frozen
        table exercises.
        """
        if not self.has_credentials():
            return self._missing_credentials_result(hostname_zones, "delete")

        settings = self.settings()
        nameserver = settings["nsupdate_nameserver"]
        rtypes = self.rtypes_for(rtype)
        results: dict[str, str] = {}

        for zonename, hostnames in hostname_zones.items():
            for hostname in hostnames:
                try:
                    update = self._message(zonename, settings)
                    # One message carrying one delete per family, not
                    # one message per family: RFC 2136 applies an update
                    # atomically, so a two-family delete either happens
                    # or does not.
                    for record_type in rtypes:
                        update.delete(hostname + ".", record_type)
                    response = dns.query.tcp(
                        update, nameserver, timeout=QUERY_TIMEOUT
                    )
                    _raise_for_rcode(response)
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "provider.delete_failed",
                        service=self.SERVICE,
                        hostname=hostname,
                        rcode=getattr(exc, "rcode_text", None),
                        error=str(exc),
                    )
                    results[hostname] = STATUS_DNSERR
                    continue

                results[hostname] = STATUS_GOOD
                log.info(
                    "provider.deleted",
                    service=self.SERVICE,
                    hostname=hostname,
                    rtypes=list(rtypes),
                )

        return results


__all__ = [
    "QUERY_TIMEOUT",
    "REQUIRED_SETTINGS",
    "SECRET_SETTINGS",
    "NsUpdateProvider",
    "UpdateRefused",
]
