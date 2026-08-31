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
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import dns.query
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
                    dns.query.tcp(update, nameserver, timeout=QUERY_TIMEOUT)
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "provider.create_failed",
                        service=self.SERVICE,
                        hostname=hostname,
                        rtype=rtype,
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
                    dns.query.tcp(update, nameserver, timeout=QUERY_TIMEOUT)
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "provider.delete_failed",
                        service=self.SERVICE,
                        hostname=hostname,
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
]
