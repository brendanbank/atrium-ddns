"""Hetzner DNS, through the Cloud API's ``rrsets`` endpoints.

Ported from ``dyndns-route53``'s ``lib/account/hetzner.py``.

**On ``httpx`` rather than ``requests``.** The legacy adapter used
``requests``; the atrium base image ships ``httpx`` and does not ship
``requests``. Adding a second HTTP client to the image to preserve an
import line would be a dependency for its own sake — ``httpx`` covers
every call this module makes with the same spelling
(``.get``/``.post``/``.delete``, ``.status_code``, ``.json()``,
``.raise_for_status()``), and leaves the door open to an async client if
``/nic/*`` ever stops paying for a thread. Verified against the base
image rather than assumed: ``ghcr.io/brendanbank/atrium:0.28`` has
``httpx 0.28.1`` and no ``requests``.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import httpx
import structlog

from .base import (
    DEFAULT_TTL,
    RTYPE_A,
    STATUS_DNSERR,
    STATUS_GOOD,
    STATUS_NOCHG,
    BaseProvider,
)

log = structlog.get_logger(__name__)

API_BASE = "https://api.hetzner.cloud/v1"

#: Seconds. As with Route 53: this is on the ``/nic/update`` request
#: path, so it is a budget rather than a generosity.
HTTP_TIMEOUT = 10.0


def make_client(credentials: Mapping[str, Any]) -> httpx.Client:
    """An authenticated HTTP client, from **stored** credentials.

    The single boundary between this module and the network. Unit tests
    replace this function; nothing else in here opens a socket.
    """
    return httpx.Client(
        headers={"Authorization": f"Bearer {credentials['hetzner_api_token']}"},
        timeout=HTTP_TIMEOUT,
    )


class HetznerProvider(BaseProvider):
    SERVICE = "hetzner"
    REQUIRED_CREDENTIALS = ("hetzner_api_token",)

    # -- zones ----------------------------------------------------- #

    def _discover_zones(self) -> None:
        """Map each configured domain onto its Hetzner zone id.

        Only the configured domains are mapped, which is the containment
        property: a token with account-wide access still cannot reach a
        zone this row does not name.
        """
        if not self.has_credentials():
            log.error(
                "provider.credentials_missing",
                service=self.SERVICE,
                account_id=self._account.id,
                operation="zone_discovery",
                required=list(self.REQUIRED_CREDENTIALS),
            )
            return

        with make_client(self.credentials) as client:
            response = client.get(f"{API_BASE}/zones")
            response.raise_for_status()
            payload = response.json()

        api_zones: dict[str, Any] = {}
        for zone in payload.get("zones", []):
            name = zone["name"]
            if self._is_reverse_zone(name):
                continue
            api_zones[name] = zone["id"]

        self._map_domains_to_zones(api_zones)
        log.debug(
            "provider.zones_mapped", service=self.SERVICE, zones=sorted(self._zones)
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

        results: dict[str, str] = {}

        with make_client(self.credentials) as client:
            for zonename, hostnames in hostname_zones.items():
                zone_id = self._zone_handle(zonename)
                if zone_id is None:
                    log.error(
                        "provider.unknown_zone", service=self.SERVICE, zone=zonename
                    )
                    results.update(
                        {hostname: STATUS_DNSERR for hostname in hostnames}
                    )
                    continue

                for hostname in hostnames:
                    # Skip-if-unchanged, and the only producer of `nochg`.
                    if self.check_hostnameon_server(hostname, ip, rtype):
                        results[hostname] = STATUS_NOCHG
                        continue

                    name = self.relative_name(hostname, zonename)
                    # `rtype` is in the URL *and* in the create body.
                    # Nothing downstream can tell an A from an AAAA, so
                    # both spellings are asserted in `test_providers.py`
                    # for both families.
                    rrset_url = f"{API_BASE}/zones/{zone_id}/rrsets/{name}/{rtype}"

                    try:
                        existing = client.get(rrset_url)
                        if existing.status_code == 200:
                            response = client.post(
                                f"{rrset_url}/actions/set_records",
                                json={"ttl": ttl, "records": [{"value": ip}]},
                            )
                        else:
                            response = client.post(
                                f"{API_BASE}/zones/{zone_id}/rrsets",
                                json={
                                    "name": name,
                                    "type": rtype,
                                    "ttl": ttl,
                                    "records": [{"value": ip}],
                                },
                            )
                        response.raise_for_status()
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
        if not self.has_credentials():
            return self._missing_credentials_result(hostname_zones, "delete")

        results: dict[str, str] = {}
        rtypes = self.rtypes_for(rtype)

        with make_client(self.credentials) as client:
            for zonename, hostnames in hostname_zones.items():
                zone_id = self._zone_handle(zonename)
                if zone_id is None:
                    log.error(
                        "provider.unknown_zone", service=self.SERVICE, zone=zonename
                    )
                    results.update(
                        {hostname: STATUS_DNSERR for hostname in hostnames}
                    )
                    continue

                for hostname in hostnames:
                    name = self.relative_name(hostname, zonename)
                    deleted_any = False

                    try:
                        for record_type in rtypes:
                            response = client.delete(
                                f"{API_BASE}/zones/{zone_id}/rrsets/{name}/{record_type}"
                            )
                            if response.status_code == 404:
                                continue
                            response.raise_for_status()
                            deleted_any = True
                    except Exception as exc:  # noqa: BLE001
                        log.error(
                            "provider.delete_failed",
                            service=self.SERVICE,
                            hostname=hostname,
                            error=str(exc),
                        )
                        results[hostname] = STATUS_DNSERR
                        continue

                    if deleted_any:
                        # `good` if *either* family existed. Most
                        # hostnames in production track one family only,
                        # so requiring both would report the ordinary
                        # successful delete as a no-op.
                        results[hostname] = STATUS_GOOD
                        log.info(
                            "provider.deleted",
                            service=self.SERVICE,
                            hostname=hostname,
                            rtypes=list(rtypes),
                        )
                    else:
                        results[hostname] = STATUS_NOCHG
                        log.info(
                            "provider.delete_nothing_to_do",
                            service=self.SERVICE,
                            hostname=hostname,
                            rtypes=list(rtypes),
                        )

        return results


__all__ = ["API_BASE", "HTTP_TIMEOUT", "HetznerProvider", "make_client"]
