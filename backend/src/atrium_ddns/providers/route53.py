"""Amazon Route 53, through boto3.

Ported from ``dyndns-route53``'s ``lib/account/aws.py``.

**The stored ``backend_type`` is ``aws``, not ``route53``.** The legacy
service's ``AWS._services`` is ``['aws']`` and that is the literal in
every production row, so ``aws`` is kept as an alias and the V1M4
importer needs no data rewrite. ``route53`` is the canonical name —
it is what ``models.py`` documents, and "aws" names a company rather
than a DNS service. :func:`~atrium_ddns.providers.known_services` offers
only the canonical name, so new rows converge on it while old rows keep
resolving.

**boto3 is a declared dependency of this package** (``backend/pyproject.toml``)
because the atrium base image does not ship it. It is imported at module
scope on purpose: a lazy import would let the package import cleanly on a
host where Route 53 can never work, and the first tenant to press "update"
would find out. Failing at import is loud; failing at first write is a
support ticket.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import boto3
import structlog
from botocore.config import Config

from .base import (
    DEFAULT_TTL,
    RTYPE_A,
    STATUS_DNSERR,
    STATUS_GOOD,
    STATUS_NOCHG,
    BaseProvider,
)

log = structlog.get_logger(__name__)

#: Short timeouts and no retries. This runs on the ``/nic/update``
#: request path with a router waiting; botocore's default is 60s connect
#: and 5 attempts, which is several minutes of a held request.
BOTO_CONFIG = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})


def make_client(credentials: Mapping[str, Any]) -> Any:
    """Build a Route 53 client from **stored** credentials.

    The single boundary between this module and the network. Unit tests
    replace this function; nothing else in here opens a socket.

    No ``region_name``: Route 53's control plane is global and botocore
    signs it against ``us-east-1`` by itself. Passing a tenant's zone
    region here would be wrong rather than merely redundant.
    """
    return boto3.client(
        "route53",
        config=BOTO_CONFIG,
        aws_access_key_id=credentials["aws_access_key_id"],
        aws_secret_access_key=credentials["aws_secret_access_key"],
    )


class Route53Provider(BaseProvider):
    SERVICE = "route53"
    ALIASES = ("aws",)
    REQUIRED_CREDENTIALS = ("aws_access_key_id", "aws_secret_access_key")

    # -- zones ----------------------------------------------------- #

    def _discover_zones(self) -> None:
        """Map each configured domain onto its hosted zone id.

        Two changes from the legacy adapter, both deliberate:

        **Only the configured domains are mapped.** The legacy version
        wrote *every* forward zone in the AWS account into ``_zones``,
        so a tenant whose Route 53 credential had account-wide access
        could write to any zone in it that a hostname happened to match.
        ``model_cases.yaml::provider-zone-candidates-are-limited-to-the-
        configured-domains`` states the containment property against the
        Hetzner adapter, which had it; this one did not, and the note on
        that case ("what stops one tenant's provider credential reaching
        another tenant's zone hosted on the same provider account") is
        exactly the hole. Closed here.

        **The listing is paginated.** ``list_hosted_zones`` returns at
        most 100 zones and sets ``IsTruncated``. The legacy adapter made
        one call and ignored the flag, so on an account with more than
        100 zones a tenant's own zone could simply be absent — and the
        symptom is a ``dnserr`` with a perfectly healthy credential.
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

        client = make_client(self.credentials)

        api_zones: dict[str, str] = {}
        marker: str | None = None
        while True:
            kwargs = {"Marker": marker} if marker else {}
            response = client.list_hosted_zones(**kwargs)
            for zone in response.get("HostedZones", []):
                name = zone["Name"].rstrip(".")
                if self._is_reverse_zone(name):
                    continue
                api_zones[name] = zone["Id"]
            if not response.get("IsTruncated"):
                break
            marker = response.get("NextMarker")
            if not marker:
                # Truncated with no marker is a broken response; stop
                # rather than loop forever on the request path.
                log.error("provider.zone_listing_truncated_without_marker",
                          service=self.SERVICE)
                break

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

        try:
            client = make_client(self.credentials)
        except Exception as exc:  # noqa: BLE001
            log.error("provider.client_failed", service=self.SERVICE, error=str(exc))
            return self._all_dnserr(hostname_zones)

        results: dict[str, str] = {}

        for zonename, hostnames in hostname_zones.items():
            zone_id = self._zone_handle(zonename)
            if zone_id is None:
                log.error(
                    "provider.unknown_zone", service=self.SERVICE, zone=zonename
                )
                results.update({hostname: STATUS_DNSERR for hostname in hostnames})
                continue

            changes: list[dict[str, Any]] = []
            changed: list[str] = []

            for hostname in hostnames:
                # The skip-if-unchanged check, and the only producer of
                # `nochg`. It asks DNS, not Route 53, so it is also what
                # keeps the API call count down on the common path.
                if self.check_hostnameon_server(hostname, ip, rtype):
                    results[hostname] = STATUS_NOCHG
                    continue

                changes.append(
                    {
                        "Action": "UPSERT",
                        "ResourceRecordSet": {
                            "Name": hostname,
                            "ResourceRecords": [{"Value": ip}],
                            "TTL": ttl,
                            # The record type. Nothing downstream of
                            # here can tell an A from an AAAA, so this
                            # line is asserted directly in
                            # `test_providers.py` for both families.
                            "Type": rtype,
                        },
                    }
                )
                changed.append(hostname)

            if not changes:
                continue

            try:
                client.change_resource_record_sets(
                    ChangeBatch={"Changes": changes}, HostedZoneId=zone_id
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "provider.create_failed",
                    service=self.SERVICE,
                    zone=zonename,
                    rtype=rtype,
                    error=str(exc),
                )
                results.update({hostname: STATUS_DNSERR for hostname in changed})
                continue

            for hostname in changed:
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

        try:
            client = make_client(self.credentials)
        except Exception as exc:  # noqa: BLE001
            log.error("provider.client_failed", service=self.SERVICE, error=str(exc))
            return self._all_dnserr(hostname_zones)

        results: dict[str, str] = {}
        rtypes = self.rtypes_for(rtype)

        for zonename, hostnames in hostname_zones.items():
            zone_id = self._zone_handle(zonename)
            if zone_id is None:
                log.error(
                    "provider.unknown_zone", service=self.SERVICE, zone=zonename
                )
                results.update({hostname: STATUS_DNSERR for hostname in hostnames})
                continue

            try:
                rrsets = client.list_resource_record_sets(HostedZoneId=zone_id)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "provider.list_records_failed",
                    service=self.SERVICE,
                    zone=zonename,
                    error=str(exc),
                )
                results.update({hostname: STATUS_DNSERR for hostname in hostnames})
                continue

            existing = rrsets.get("ResourceRecordSets", [])

            for hostname in hostnames:
                changes = [
                    {"Action": "DELETE", "ResourceRecordSet": rrset}
                    for rrset in existing
                    if rrset["Name"].rstrip(".").lower() == hostname.lower()
                    and rrset["Type"] in rtypes
                ]

                if not changes:
                    # Absent is `nochg`, not an error: deleting twice
                    # has to be idempotent or a retrying client cannot
                    # recover from a dropped response.
                    log.info(
                        "provider.delete_nothing_to_do",
                        service=self.SERVICE,
                        hostname=hostname,
                        rtypes=list(rtypes),
                    )
                    results[hostname] = STATUS_NOCHG
                    continue

                try:
                    client.change_resource_record_sets(
                        ChangeBatch={"Changes": changes}, HostedZoneId=zone_id
                    )
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


__all__ = ["BOTO_CONFIG", "Route53Provider", "make_client"]
