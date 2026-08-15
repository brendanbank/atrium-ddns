"""The provider contract, and the helpers whose exact semantics are frozen.

Ported from ``dyndns-route53``'s ``lib/accounts.py`` (``BaseAccount``),
stripped of Flask, of module globals, and of environment-variable
credential fallbacks.

Three things about this module are load-bearing and none of them are
obvious from the code:

**The static helpers are frozen behaviour, not utilities.**
``getip``, ``getiptype``, ``isvalidhostname``, ``hostnameperzone`` and
``domaininhostname`` decide what the wire answers, and
``tests/compat/protocol_cases.yaml`` asserts the results byte-for-byte.
Two documented divergences from the DynDNS protocol document live
*inside* ``isvalidhostname`` and are preserved deliberately — see the
docstring there. A third quirk lives in ``domaininhostname`` and is
recorded there for the same reason.

**Every call is synchronous, on purpose.** boto3 has no async API, and
an adapter set that is half-async is worse than one that is honestly
blocking. ``/nic/*`` (#16) must therefore call ``createrecords`` /
``deleterecords`` through ``anyio.to_thread.run_sync`` (or
``fastapi.concurrency.run_in_threadpool``) rather than awaiting them.
A blocking boto3 call on the event loop stalls every other request in
the process, and it will not show up in a unit test.

**Credentials come from the row and from nowhere else.** The legacy
adapters fell back to ``AWS_ACCESS_KEY_ID`` / ``HETZNER_API_TOKEN`` /
``NSUPDATE_SECRET`` when the stored credential was empty. Under
multi-tenancy that is a cross-tenant credential leak: one operator-set
environment variable would serve every tenant whose row happens to be
blank. ``tests/compat/legacy_behaviour/model_cases.yaml`` carries it as
``provider-must-not-fall-back-to-environment-credentials``, disposition
``fix``. ``test_providers.py`` asserts the absence two ways — behaviourally
with the variable set, and by sweeping this package's source for any
reference to the environment at all, so a *new* provider cannot
reintroduce it either.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterable, Mapping, Sequence

import dns.exception
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.rdatatype
import dns.resolver
import structlog

log = structlog.get_logger(__name__)

#: The three statuses an adapter may return, per hostname.
#: ``model_cases.yaml::provider-status-vocabulary-is-good-nochg-dnserr``
#: — and a hostname *absent* from the returned map is read as ``dnserr``
#: by the caller, which is why the vocabulary is closed rather than
#: merely conventional.
STATUS_GOOD = "good"
STATUS_NOCHG = "nochg"
STATUS_DNSERR = "dnserr"
PROVIDER_STATUSES: frozenset[str] = frozenset(
    {STATUS_GOOD, STATUS_NOCHG, STATUS_DNSERR}
)

#: What the router contributes for a backend row this package cannot
#: resolve — an unknown ``backend_type``, or a provider that failed to
#: construct. It is deliberately **not** in ``PROVIDER_STATUSES``: no
#: adapter ever returns it, because by then there is no adapter.
#: ``protocol_cases.yaml::update-911-backend-service-unknown-to-factory``
#: is the wire half.
STATUS_UNKNOWN_SERVICE = "911"

RTYPE_A = "A"
RTYPE_AAAA = "AAAA"
#: The order a type-less delete walks. Both families, A first — the
#: legacy order, and ``model_cases.yaml::provider-delete-without-a-record-
#: type-covers-both-families`` counts the calls.
ADDRESS_FAMILIES: tuple[str, str] = (RTYPE_A, RTYPE_AAAA)

#: ``model_cases.yaml::ttl-default-is-60``.
DEFAULT_TTL = 60

#: Where ``check_hostnameon_server`` asks when it cannot find an
#: authoritative server. Legacy behaviour, preserved: a public resolver
#: answering from cache makes the check *conservative* (a stale answer
#: says "changed", so we write) rather than wrong.
FALLBACK_NAMESERVER = "8.8.8.8"

#: Seconds. Legacy value, preserved — the resolver is on the request
#: path and ``/nic/update`` has a router waiting on it.
RESOLVER_LIFETIME = 10.0

#: The label pattern, character for character as the legacy service
#: spells it. Do not "tidy" it; see :meth:`BaseProvider.isvalidhostname`.
_LABEL = re.compile(r"(?!-)[A-Z\d-]{1,63}(?<!-)$", re.IGNORECASE)

#: Longest legal FQDN, in the legacy service's own (over-generous)
#: reading — it measures the whole comma element, not the wire form.
_MAX_HOSTNAME = 255

#: Suffixes that are never forward zones. A provider account that hosts
#: reverse zones must not offer them as write targets.
_REVERSE_ZONE_MARKERS = ("in-addr.arpa", "ip6.arpa")

#: What a masked credential renders as. Never the value, never its
#: length, never a prefix — a prefix of an API token is still a
#: disclosure and it is the shape people reach for first.
_MASK = "<redacted>"


@dataclass(frozen=True)
class ProviderAccount:
    """Everything an adapter is allowed to know about a backend row.

    Deliberately a dataclass and not the legacy ``dict``: the dict was
    addressed by string key in five modules, and a typo in one of them
    read as "no credentials configured" — which is a status the wire
    already has a legitimate reason to produce (``911``), so the defect
    would have been indistinguishable from the correct answer.

    ``credentials`` holds the **revealed** plaintext. The unwrapping
    (``await unlock_user_secrets(session, row.user_id)`` then
    ``row.credentials.reveal()``) belongs to the caller, because it is a
    database read and this package does no IO of that kind. Nothing here
    keeps a reference beyond the adapter's own lifetime.
    """

    #: The stored ``ddns_domain_backend.backend_type``. Matched against
    #: each provider's ``SERVICE`` and ``ALIASES``.
    service: str
    #: The zones this backend row is allowed to write to — one per
    #: ``ddns_domain`` under the row's owner. The *containment*
    #: property: a credential with account-wide access at the provider
    #: still only reaches these.
    domains: tuple[str, ...] = ()
    #: Provider secrets, plaintext, from the encrypted column.
    credentials: Mapping[str, Any] = field(default_factory=dict)
    #: Non-secret provider settings (``ddns_domain_backend.config``).
    config: Mapping[str, Any] = field(default_factory=dict)
    #: The row id, for log correlation. Never a tenancy check.
    id: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        # Normalise rather than validate. The router hands us whatever
        # the row held, including NULL config and NULL credentials —
        # `credentials: absent` is a real fixture state, not an error.
        object.__setattr__(self, "domains", tuple(self.domains or ()))
        object.__setattr__(self, "credentials", dict(self.credentials or {}))
        object.__setattr__(self, "config", dict(self.config or {}))

    def __repr__(self) -> str:
        """Mask the credentials, name everything else.

        The default dataclass ``__repr__`` renders every field, and this
        object reaches log formatters, exception messages and
        ``pytest``'s assertion rewriting. One unlucky ``log.debug(account)``
        puts a tenant's Route 53 secret key in the log store, which in
        this estate has deletion disabled.
        """
        creds = (
            {key: _MASK for key in sorted(self.credentials)}
            if self.credentials
            else {}
        )
        return (
            f"{type(self).__name__}(service={self.service!r}, "
            f"domains={self.domains!r}, credentials={creds!r}, "
            f"config={self.config!r}, id={self.id!r}, "
            f"description={self.description!r})"
        )


class BaseProvider:
    """One DNS backend, for one tenant, for one set of zones.

    Subclasses supply :attr:`SERVICE`, optionally :attr:`ALIASES` and
    :attr:`REQUIRED_CREDENTIALS`, and implement ``createrecords`` /
    ``deleterecords`` and (where the provider has a zone API)
    ``_discover_zones``.
    """

    #: Canonical ``backend_type``. What a new row should store and what
    #: :func:`~atrium_ddns.providers.known_services` advertises.
    SERVICE: ClassVar[str] = ""
    #: Other names that resolve to this class. Legacy rows carry these;
    #: see the ``aws`` note on :class:`~atrium_ddns.providers.route53.Route53Provider`.
    ALIASES: ClassVar[tuple[str, ...]] = ()
    #: Credential keys that must all be present and truthy before the
    #: adapter will talk to the provider at all.
    REQUIRED_CREDENTIALS: ClassVar[tuple[str, ...]] = ()

    def __init__(self, account: ProviderAccount) -> None:
        self._account = account
        # Seeded from the configured domains and from nothing else.
        # Zone discovery may *refine* an entry (a domain hosted inside a
        # wider zone maps to that zone's provider-side handle) but it
        # may never add one, which is the containment property in
        # ``model_cases.yaml::provider-zone-candidates-are-limited-to-
        # the-configured-domains``.
        self._zones: dict[str, Any] = {domain: domain for domain in account.domains}
        # Configured domain -> the name of the provider-side zone that
        # actually holds it. Equal to the domain in the common case;
        # different when the domain is a strict subdomain of the hosted
        # zone, which is the case a relative name has to be computed
        # against.
        self._api_zone_names: dict[str, str] = {
            domain: domain for domain in account.domains
        }
        self._default_resolver: dns.resolver.Resolver | None = None

        # Discovery runs at construction, and never raises.
        # ``model_cases.yaml::provider-zone-discovery-failure-is-not-fatal``
        # — a provider outage must degrade to `dnserr` per hostname, not
        # to a construction failure that would take the whole request
        # to `911` and lose the distinction between "your provider is
        # down" and "your backend is misconfigured".
        try:
            self._discover_zones()
        except Exception as exc:  # noqa: BLE001 — the whole point
            log.error(
                "provider.zone_discovery_failed",
                service=self.SERVICE,
                account_id=account.id,
                error=str(exc),
            )

    # -- identity -------------------------------------------------- #

    @classmethod
    def service_names(cls) -> tuple[str, ...]:
        """Every name that resolves to this class, canonical first."""
        return (cls.SERVICE, *cls.ALIASES)

    @classmethod
    def known_services(cls) -> tuple[str, ...]:
        """The name a *new* row should store. Aliases are not offered."""
        return (cls.SERVICE,)

    @property
    def account(self) -> ProviderAccount:
        return self._account

    @property
    def id(self) -> str | None:
        return self._account.id

    @property
    def description(self) -> str:
        return f"{self._account.id} [{self._account.service} - {self._account.description}]"

    @property
    def credentials(self) -> Mapping[str, Any]:
        """The stored credentials. **No environment fallback** — see the
        module docstring."""
        return self._account.credentials

    def has_credentials(self) -> bool:
        """True when every :attr:`REQUIRED_CREDENTIALS` key is present
        and truthy.

        Reads :attr:`credentials`, the property, and not
        ``self._account.credentials`` directly. The difference is not
        cosmetic: with the direct read, a subclass that widened
        ``credentials`` — say by reintroducing an environment fallback —
        would answer "no credentials" here and then happily use the
        environment value at the call site, so the guard and the thing
        it guards would disagree. Found by mutation: re-adding the
        environment fallback as a ``credentials`` override was caught by
        **one** test with the direct read (the structural ``ast`` sweep,
        which sees ``import os`` and nothing else) and by **two** once
        this reads the property — the second being the behavioural one,
        which is the only one a fallback spelled without importing
        ``os`` would trip.
        """
        creds = self.credentials
        return all(creds.get(key) for key in self.REQUIRED_CREDENTIALS)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(service={self.SERVICE!r}, zones={sorted(self._zones)!r})"

    # -- zones ----------------------------------------------------- #

    def _discover_zones(self) -> None:
        """Refine :attr:`_zones` from the provider's zone API.

        No-op by default: ``nsupdate`` has no zone API and its zones are
        exactly the configured domains.
        """

    def gethostedzones(self) -> Iterable[str]:
        """The configured domains, as refined by discovery.

        Unlike the legacy method of this name it performs **no IO**.
        The legacy ``AWS.gethostedzones()`` called ``list_hosted_zones``
        on every invocation, and ``hostnameperzone`` called it once per
        request for its side effect only — so an ordinary
        ``/nic/update`` made a Route 53 API call it did not use the
        result of.
        """
        return self._zones.keys()

    @staticmethod
    def _is_reverse_zone(name: str) -> bool:
        return any(marker in name for marker in _REVERSE_ZONE_MARKERS)

    def _map_domains_to_zones(self, api_zones: Mapping[str, Any]) -> None:
        """Bind each configured domain to the closest enclosing hosted zone.

        ``api_zones`` maps a provider-side zone *name* (no trailing dot,
        reverse zones already filtered) to whatever handle that provider
        needs later — a Route 53 hosted-zone id, a Hetzner zone id.

        **Closest enclosing, not first enclosing.** Both legacy adapters
        took the first parent they happened to iterate over, so an
        account hosting both ``example.com`` and ``dyn.example.com``
        bound ``a.dyn.example.com`` to whichever the API listed first —
        a silently wrong zone rather than an error. The frozen case is
        ``model_cases.yaml::provider-zone-is-the-closest-enclosing-
        hosted-zone`` and it says *closest*; it calibrated ``agree``
        against a fixture holding one candidate, where first and closest
        cannot differ. Longest suffix wins here.

        Domains with no enclosing hosted zone keep their seeded
        identity mapping, so they still resolve as a zone and the write
        fails at the provider rather than vanishing.
        """
        for domain in self._account.domains:
            best_name: str | None = None
            for api_name in api_zones:
                if domain == api_name or domain.endswith("." + api_name):
                    if best_name is None or len(api_name) > len(best_name):
                        best_name = api_name
            if best_name is None:
                log.warning(
                    "provider.zone_not_hosted",
                    service=self.SERVICE,
                    domain=domain,
                )
                continue
            self._zones[domain] = api_zones[best_name]
            self._api_zone_names[domain] = best_name

    def _zone_handle(self, zonename: str) -> Any:
        """The provider-side handle for a configured domain, or ``None``.

        ``hostnameperzone`` only ever produces keys drawn from
        :attr:`_zones`, so ``None`` here means the caller passed a map it
        did not build — which must be a ``dnserr``, never a ``KeyError``
        escaping into the router as a 500.
        """
        return self._zones.get(zonename)

    def relative_name(self, hostname: str, zonename: str) -> str:
        """``hostname`` as a label path inside its hosted zone.

        Computed against the **hosted zone**, not against the configured
        domain: ``a.b.example.com`` in a row configured for
        ``b.example.com`` but hosted at ``example.com`` is ``a.b``, not
        ``a``. Getting this wrong writes the record one level too
        shallow — a wrong record, not an error.
        ``model_cases.yaml::provider-relative-name-is-computed-against-
        the-hosted-zone``.

        The zone apex is ``@`` and not the empty string
        (``provider-zone-apex-relative-name-is-at``).
        """
        api_zone = self._api_zone_names.get(zonename, zonename)
        if hostname == api_zone:
            return "@"
        suffix = "." + api_zone
        if hostname.endswith(suffix):
            return hostname[: -len(suffix)]
        return hostname

    # -- the frozen static helpers --------------------------------- #

    @staticmethod
    def getip(ip: Any) -> Any:
        """``ipaddress.ip_address(ip)``, or ``False``.

        ``False`` rather than ``None`` because every caller tests it
        with ``if not ...``, and because ``0`` is a legal address
        component: an ``IPv4Address`` is always truthy (it defines
        neither ``__bool__`` nor ``__len__``), so ``0.0.0.0`` does not
        collide with the sentinel.
        """
        try:
            return ipaddress.ip_address(ip)
        except Exception as exc:  # noqa: BLE001 — ValueError today, sentinel either way
            log.debug("provider.invalid_ip", ip=str(ip), error=str(exc))
            return False

    @staticmethod
    def getiptype(ip: Any) -> Any:
        """``"A"`` for IPv4, ``"AAAA"`` for IPv6, ``False`` for neither.

        **This is the only thing that decides which record type gets
        written, and the wire cannot see it.** ``/nic/update`` echoes
        the normalised address and ``/nic/delete`` echoes nothing;
        ``rtype`` reaches ``createrecords`` / ``deleterecords`` and
        stops. #29 proved it by mutation — answering ``A`` for every
        address ran the whole 131-case table at 121 executed, 0 failed.
        So the assertions that this function and its three consumers
        dispatch correctly live in ``backend/tests/test_providers.py``
        and nowhere else in this repository.
        """
        ip_object = BaseProvider.getip(ip)
        if ip_object is False:
            return False
        if isinstance(ip_object, ipaddress.IPv4Address):
            return RTYPE_A
        if isinstance(ip_object, ipaddress.IPv6Address):
            return RTYPE_AAAA
        return False

    @staticmethod
    def isvalidhostname(hostnames: str) -> Any:
        """Split a comma list and validate every label, or return ``False``.

        Returns the split list **unstripped**, exactly as the legacy
        service does, so a caller that looks an element up gets the same
        miss the legacy service got.

        Two divergences from ``docs/DYNDNS-PROTOCOL.md`` live in here and
        are **preserved deliberately** (refactor plan §1, divergences 5
        and 6):

        - A single label with no dot (``foo``) validates, and falls
          through to ``nohost``.
        - A label with a **trailing newline** (``foo\\n``) validates,
          because the pattern ends in ``$`` and Python's ``$`` matches
          before a final newline. The unstripped element is then
          returned, the lookup misses, and the answer is ``nohost``.

        Both spellings already end at ``nohost``, so preserving costs
        nothing; "fixing" either would promote a per-hostname ``nohost``
        line to a whole-request ``notfqdn``, which is a different answer
        for every other hostname in the same request.

        The regex is the legacy one character for character. In
        particular it is applied with ``.match()`` (anchored at the
        start) and carries its own ``$``; ``\\A``/``\\Z`` would change
        the newline behaviour above.
        """
        parts = hostnames.split(",")
        # Fires *before* the regex, which is why `hostname=` empty is
        # `911` and a trailing comma is `notfqdn` — the empty element is
        # inside the list in the second case and is the whole list in
        # the first.
        if "" in parts:
            return False

        for hostname in parts:
            if len(hostname) > _MAX_HOSTNAME:
                return False
            if hostname[-1] == ".":
                hostname = hostname[:-1]  # exactly one trailing dot
            if not all(_LABEL.match(label) for label in hostname.split(".")):
                return False

        return parts

    def domaininhostname(self, hostname: str) -> Any:
        """The configured domain ``hostname`` sits under, or ``False``.

        **Preserved quirk, recorded so a later "fix" is a decision.**
        The test is ``rfind`` plus an end-offset check, with no label
        boundary: with ``example.com`` configured, ``notexample.com``
        matches. It is not a tenancy hole under the rewrite — the domain
        set comes from the hostname's own owning row, not from the
        request — but it is wrong, and ``test_providers.py`` pins it so
        that changing it shows up as a deliberate edit rather than as a
        behaviour change nobody proposed.

        Iteration order is insertion order, i.e. the order the row's
        domains were listed; the first match wins, not the longest.
        """
        for domain in self._zones:
            position = hostname.lower().rfind(domain.lower())
            if position >= 0 and position + len(domain) == len(hostname):
                return domain
        return False

    def hostnameperzone(self, hostnames: Sequence[str]) -> Any:
        """Group hostnames by configured domain, or ``False``.

        ``False`` — not a partial map — as soon as one hostname is
        outside every configured domain. The router turns that into
        ``nohost`` for the whole hostname
        (``protocol_cases.yaml::update-nohost-hostname-outside-backend-zone``).
        """
        grouped: dict[str, list[str]] = {}
        for hostname in hostnames:
            domain = self.domaininhostname(hostname)
            if not domain:
                log.warning(
                    "provider.hostname_outside_configured_domains",
                    service=self.SERVICE,
                    hostname=hostname,
                )
                return False
            grouped.setdefault(domain, []).append(hostname)
        return grouped

    # -- the skip-if-unchanged check ------------------------------- #

    def check_hostnameon_server(
        self,
        hostname: str,
        ip: str,
        rrtype: str = RTYPE_A,
        nameserver: str | None = None,
    ) -> bool:
        """True when DNS already answers ``ip`` for ``hostname``.

        **This is what produces ``nochg``**, and ``nochg`` is in the
        frozen table. Production's hostnames mostly answer ``nochg``, so
        this is also what makes a five-minute client cheap: a ``True``
        here means no write reaches the provider at all.

        Every failure path answers ``False``, which makes the write
        proceed. That direction is deliberate and it is the safe one: a
        spurious write is idempotent, a spurious ``nochg`` leaves DNS
        stale for as long as the client keeps sending the same address.
        """
        log.debug(
            "provider.check_hostname",
            hostname=hostname,
            ip=ip,
            rrtype=rrtype,
            nameserver=nameserver,
        )

        resolver = dns.resolver.Resolver(configure=False)
        resolver.lifetime = RESOLVER_LIFETIME

        if nameserver is None:
            nameserver = self.get_authoritative_nameserver(hostname)
            if nameserver is None:
                nameserver = FALLBACK_NAMESERVER

        resolver.nameservers = [nameserver]

        try:
            rdatatype = dns.rdatatype.from_text(rrtype)
            answers = resolver.resolve(hostname, rdatatype)
        except Exception as exc:  # noqa: BLE001 — NXDOMAIN, timeout, servfail…
            log.info(
                "provider.check_hostname_unresolved",
                service=self.SERVICE,
                hostname=hostname,
                rrtype=rrtype,
                error=str(exc),
            )
            return False

        if not (
            answers
            and answers[0]
            and answers[0].rdtype in (dns.rdatatype.A, dns.rdatatype.AAAA)
        ):
            log.error(
                "provider.check_hostname_no_address_record",
                service=self.SERVICE,
                hostname=hostname,
                rrtype=rrtype,
            )
            return False

        record = answers[0]
        ip_request = self.getip(ip)
        ip_dns = self.getip(record.address)

        if ip_request is False or ip_dns is False:
            log.error(
                "provider.check_hostname_invalid_address",
                service=self.SERVICE,
                hostname=hostname,
            )
            return False

        if ip_request == ip_dns:
            log.info(
                "provider.unchanged",
                service=self.SERVICE,
                hostname=hostname,
                ip=str(ip_request),
            )
            return True

        log.info(
            "provider.changed",
            service=self.SERVICE,
            hostname=hostname,
            resolved=str(ip_dns),
            requested=str(ip_request),
        )
        return False

    def get_authoritative_nameserver(self, domain: str) -> str | None:
        """Walk the delegation chain to the server that owns ``domain``.

        Returns ``None`` rather than a guess when the walk cannot
        finish; :meth:`check_hostnameon_server` then falls back to a
        public resolver, which is conservative in the right direction.
        """
        name = dns.name.from_text(domain)

        default = self._resolver()
        if not default.nameservers:
            log.error("provider.no_system_resolver", domain=domain)
            return None
        nameserver = default.nameservers[0]

        depth = 2
        last = False
        while not last:
            split = name.split(depth)
            last = split[0].to_unicode() == "@"
            sub = split[1]

            query = dns.message.make_query(sub, dns.rdatatype.NS)
            try:
                response = dns.query.udp(query, nameserver, timeout=5)
            except (dns.exception.Timeout, OSError) as exc:
                log.error(
                    "provider.ns_query_failed",
                    zone=str(sub),
                    nameserver=str(nameserver),
                    error=str(exc),
                )
                return None

            rcode = response.rcode()
            if rcode != dns.rcode.NOERROR:
                if rcode == dns.rcode.NXDOMAIN and depth > 2:
                    # No delegation at this level, so the server found
                    # at the parent is authoritative for the whole
                    # remaining name.
                    return nameserver
                log.error(
                    "provider.ns_query_rcode",
                    zone=str(sub),
                    rcode=dns.rcode.to_text(rcode),
                )
                return None

            rrset = (
                response.authority[0] if response.authority else response.answer[0]
            )
            rr = rrset[0]
            if rr.rdtype != dns.rdatatype.SOA:
                authority = rr.target
                try:
                    nameserver = default.resolve(authority).rrset[0].to_text()
                except (
                    dns.exception.Timeout,
                    dns.resolver.NoNameservers,
                    OSError,
                ) as exc:
                    log.error(
                        "provider.authority_unresolvable",
                        authority=str(authority),
                        error=str(exc),
                    )
                    return None

            depth += 1

        return nameserver

    def _resolver(self) -> dns.resolver.Resolver:
        """The system resolver, built once, lazily.

        Lazily because ``get_default_resolver()`` reads
        ``/etc/resolv.conf``: doing it in ``__init__`` makes constructing
        an adapter — which happens once per backend row per request — a
        filesystem read, and makes this class untestable without one.
        """
        if self._default_resolver is None:
            self._default_resolver = dns.resolver.get_default_resolver()
        return self._default_resolver

    # -- the operations -------------------------------------------- #

    def createrecords(
        self,
        ip: str,
        hostname_zones: Mapping[str, Sequence[str]],
        rtype: str = RTYPE_A,
        ttl: int = DEFAULT_TTL,
    ) -> dict[str, str]:
        """Upsert one address record per hostname. Subclasses override."""
        return self._all_dnserr(hostname_zones)

    def deleterecords(
        self,
        hostname_zones: Mapping[str, Sequence[str]],
        rtype: str | None = None,
    ) -> dict[str, str]:
        """Remove records. ``rtype=None`` means both families. Subclasses override."""
        return self._all_dnserr(hostname_zones)

    # -- helpers for subclasses ------------------------------------ #

    @staticmethod
    def _all_dnserr(hostname_zones: Mapping[str, Sequence[str]]) -> dict[str, str]:
        return {
            hostname: STATUS_DNSERR
            for hostnames in hostname_zones.values()
            for hostname in hostnames
        }

    @staticmethod
    def rtypes_for(rtype: str | None) -> tuple[str, ...]:
        """``(rtype,)``, or both families when ``rtype`` is ``None``.

        A type-less delete makes **two** provider calls and returns
        **one** status, and the aggregate is ``good`` if *either* family
        existed. Production's shape makes the mixed case the common one
        — most hostnames track v6 and only some track v4 — so an
        implementation that reported ``nochg`` unless both existed would
        report most successful deletes as no-ops.
        """
        return (rtype,) if rtype else ADDRESS_FAMILIES

    def _missing_credentials_result(
        self, hostname_zones: Mapping[str, Sequence[str]], operation: str
    ) -> dict[str, str]:
        log.error(
            "provider.credentials_missing",
            service=self.SERVICE,
            account_id=self._account.id,
            operation=operation,
            # Names only. Which keys are required is not a secret; which
            # are populated would be a hint about a stored value.
            required=list(self.REQUIRED_CREDENTIALS),
        )
        return self._all_dnserr(hostname_zones)


__all__ = [
    "ADDRESS_FAMILIES",
    "DEFAULT_TTL",
    "FALLBACK_NAMESERVER",
    "PROVIDER_STATUSES",
    "RESOLVER_LIFETIME",
    "RTYPE_A",
    "RTYPE_AAAA",
    "STATUS_DNSERR",
    "STATUS_GOOD",
    "STATUS_NOCHG",
    "STATUS_UNKNOWN_SERVICE",
    "BaseProvider",
    "ProviderAccount",
]
