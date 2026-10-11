"""The recursive resolvers this service asks, instead of the host's.

Both DNS paths used to start from ``/etc/resolv.conf``: the health check's
delegation walk (:func:`atrium_ddns.worker_jobs.authoritative_nameserver`)
and ``/nic/update``'s ``nochg`` check
(:meth:`atrium_ddns.providers.base.BaseProvider.get_authoritative_nameserver`).
Inside a container that file names Docker's embedded DNS, which forwards to
whatever the host is configured with — on the deploy host, the cloud
provider's recursive resolvers.

On 2026-10-08 those resolvers stopped answering for about six hours. Every
lookup the service started timed out or came back ``SERVFAIL``, the board
flipped to ``error`` row by row, and none of it was this service's fault or
fixable from inside it. The resolvers are now a setting
(``DdnsConfig.dns_resolvers``) with public defaults from two unrelated
operators, so one operator's outage is a failover and not an outage.

What still uses the host's resolver: the provider HTTP APIs (boto3 for
Route 53, httpx for Hetzner) resolve their endpoint names through the C
library, which this module does not reach.
"""
from __future__ import annotations

import ipaddress
from typing import Iterable

#: Google Public DNS and Cloudflare, IPv4 before IPv6 within each operator.
#: Order is the order they are tried. In a container with no IPv6 route an
#: IPv6 entry fails at ``sendto`` with no wait, so listing them costs nothing
#: where they cannot be used.
DEFAULT_DNS_RESOLVERS: tuple[str, ...] = (
    "8.8.8.8",
    "8.8.4.4",
    "2001:4860:4860::8888",
    "2001:4860:4860::8844",
    "1.1.1.1",
    "1.0.0.1",
    "2606:4700:4700::1111",
    "2606:4700:4700::1001",
)

#: Bounds on the list. One because an empty list has nowhere to start a
#: walk; sixteen because each entry is tried in turn on a failure, and a
#: long list is a long wait when the network itself is down.
MIN_DNS_RESOLVERS = 1
MAX_DNS_RESOLVERS = 16

#: Upper bound, in seconds, on one try against one recursive resolver.
#: Below the health check's own timeout on purpose: a resolver that has not
#: answered in two seconds is not going to, and the next one on the list
#: usually answers in milliseconds.
PER_RESOLVER_TIMEOUT = 2.0


def normalise_resolvers(values: Iterable[str]) -> list[str]:
    """Canonical addresses, in the order given, without duplicates.

    Refuses anything that is not an IP address. A hostname here would need a
    resolver to find the resolver, and the only one available is the one
    this setting exists to avoid.
    """
    out: list[str] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        try:
            address = str(ipaddress.ip_address(text))
        except ValueError:
            raise ValueError(
                f"{text!r} is not an IP address. Resolvers are given by "
                f"address, not by name."
            ) from None
        if address not in out:
            out.append(address)
    return out


__all__ = [
    "DEFAULT_DNS_RESOLVERS",
    "MAX_DNS_RESOLVERS",
    "MIN_DNS_RESOLVERS",
    "PER_RESOLVER_TIMEOUT",
    "normalise_resolvers",
]
