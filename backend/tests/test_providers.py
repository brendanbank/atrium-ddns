"""The three DNS provider adapters, and the helpers the wire depends on.

**This file is the only place record-type correctness is provable.**
``/nic/update`` echoes the normalised *address* and ``/nic/delete``
echoes neither address nor type, so ``rtype`` reaches
``createrecords()`` / ``deleterecords()`` and stops there. #29 proved it
by mutation: making ``getiptype`` answer ``A`` for every address — v4
and v6 alike — ran the whole frozen 131-case wire table at **121
executed, 0 failed**. An ``A``/``AAAA`` mix-up ships silently past every
other suite in this repository. So the assertions below are made on
**what the provider actually sends** — the boto3 ``ChangeBatch``, the
Hetzner request URL and body, the dnspython UPDATE message — and not on
the adapter's return value, which cannot tell the two apart either.

Two instruments, where a number matters:

- The static helpers are asserted directly *and* replayed against
  ``tests/compat/protocol_cases.yaml`` — the table frozen at v3 by #29,
  which this file did not author. ``TestAgainstTheFrozenWireTable``
  extracts every case whose expected body implies a verdict on
  ``isvalidhostname`` and checks the port agrees, with a vacuity guard
  on the number extracted.
- Absence of environment-variable credential fallbacks is asserted
  behaviourally (with the legacy variables set) *and* structurally, by
  walking every module in the package with ``ast`` — so a *new*
  provider file is covered by the second instrument without anyone
  remembering to add a test.

**No network.** ``boto3``, the Hetzner HTTP client and dnspython are
replaced at their single boundary, and an autouse fixture makes any
socket that escapes raise :class:`NetworkAttempt`.
``test_the_network_guard_bites`` proves that guard is live rather than
decorative.
"""
from __future__ import annotations

import ast
import ipaddress
import os
import pathlib
import socket
from typing import Any

import dns.exception
import dns.query
import dns.rdatatype
import dns.resolver
import pytest

from atrium_ddns import providers
from atrium_ddns.providers import (
    ADDRESS_FAMILIES,
    PROVIDER_STATUSES,
    STATUS_DNSERR,
    STATUS_GOOD,
    STATUS_NOCHG,
    STATUS_UNKNOWN_SERVICE,
    BaseProvider,
    HetznerProvider,
    NsUpdateProvider,
    ProviderAccount,
    Route53Provider,
    get_provider,
    known_services,
    provider_class,
    register,
    resolvable_services,
    unregister,
)
from atrium_ddns.providers import hetzner as hetzner_mod
from atrium_ddns.providers import nsupdate as nsupdate_mod
from atrium_ddns.providers import route53 as route53_mod

# Everything here is synchronous by design — see providers/base.py on
# why the adapters are not async — so opt this module out of the
# session-wide asyncio auto mode rather than letting it wrap sync tests.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# --------------------------------------------------------------------------
# Fixtures and doubles
# --------------------------------------------------------------------------

R53_CREDS = {
    "aws_access_key_id": "AKIAEXAMPLEEXAMPLE",
    "aws_secret_access_key": "wJalrXUtnFEMI-EXAMPLE-KEY",
}
# Deliberately shares no prefix with its own key name: the disclosure
# test below walks every prefix of the value looking for it in a repr,
# and a token spelled "hetzner-…" would collide with the literal
# "hetzner_api_token" that repr is *supposed* to print.
HETZNER_CREDS = {"hetzner_api_token": "hcloud-JQ3xR7-not-a-real-token"}
# 32 bytes of base64 — dns.tsigkeyring.from_text really parses this.
NSUPDATE_CREDS = {
    "nsupdate_secret": "c2VjcmV0LXRzaWcta2V5LW1hdGVyaWFsLWhlcmUh",
}
NSUPDATE_CONFIG = {
    "nsupdate_key": "ddns-key",
    "nsupdate_algo": "hmac-sha256",
    "nsupdate_nameserver": "192.0.2.53",
}
# The full legacy shape: all four in the credential blob.
NSUPDATE_LEGACY_CREDS = {**NSUPDATE_CREDS, **NSUPDATE_CONFIG}

V4 = "203.0.113.10"
V6 = "2001:db8::1"


class NetworkAttempt(RuntimeError):
    """Raised when a unit test tries to open a socket."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket that escapes a double fails the test that opened it.

    Not decoration: the adapters' whole job is to talk to the network,
    so a boundary double that stops being reached is invisible — the
    test still passes, it just quietly starts asserting against a live
    provider. ``test_the_network_guard_bites`` keeps this honest.
    """

    def deny(*args: Any, **kwargs: Any) -> Any:
        raise NetworkAttempt(
            "a unit test tried to open a socket; the provider boundary "
            "double was not installed or was bypassed"
        )

    monkeypatch.setattr(socket.socket, "connect", deny, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", deny, raising=False)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


class FakeRdata:
    def __init__(self, address: str, rdtype: Any) -> None:
        self.address = address
        self.rdtype = rdtype


class FakeDns:
    """Stands in for every dnspython entry point the base class uses.

    Defaults are the *unresolved* case — NXDOMAIN from the resolver and
    a timeout from the delegation walk — because that is what makes a
    write proceed, and a test that wants ``nochg`` should have to say
    so.
    """

    def __init__(self) -> None:
        self.address: str | None = None
        self.rdtype: Any = dns.rdatatype.A
        self.error: Exception | None = dns.resolver.NXDOMAIN("no such name")
        self.queries: list[tuple[str, Any, tuple[str, ...]]] = []
        self.udp_queries: list[Any] = []
        self.system_nameservers: list[str] = ["192.0.2.1"]

    # -- what base.py reaches for -------------------------------------- #

    def resolver_factory(self, *args: Any, **kwargs: Any) -> Any:
        fake = self

        class _Resolver:
            def __init__(self) -> None:
                self.lifetime: float | None = None
                self.nameservers: list[str] = []

            def resolve(self, name: str, rdatatype: Any) -> Any:
                fake.queries.append((name, rdatatype, tuple(self.nameservers)))
                if fake.error is not None:
                    raise fake.error
                return [FakeRdata(fake.address or "", fake.rdtype)]

        return _Resolver()

    def default_resolver(self) -> Any:
        fake = self

        class _Default:
            nameservers = fake.system_nameservers

            def resolve(self, name: Any) -> Any:  # pragma: no cover - guarded
                raise NetworkAttempt("delegation walk not faked in this test")

        return _Default()

    def udp(self, query: Any, where: Any, timeout: Any = None) -> Any:
        self.udp_queries.append((query, where))
        raise dns.exception.Timeout("no udp in unit tests")

    @property
    def last_nameservers(self) -> tuple[str, ...]:
        assert self.queries, "no resolver query was made"
        return self.queries[-1][2]

    # -- knobs ---------------------------------------------------------- #

    def answers(self, address: str, rdtype: Any = None) -> None:
        self.address = address
        self.error = None
        if rdtype is not None:
            self.rdtype = rdtype
        elif ipaddress.ip_address(address).version == 6:
            self.rdtype = dns.rdatatype.AAAA
        else:
            self.rdtype = dns.rdatatype.A

    def fails(self, error: Exception) -> None:
        self.error = error
        self.address = None


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> FakeDns:
    fake = FakeDns()
    monkeypatch.setattr(dns.resolver, "Resolver", fake.resolver_factory)
    monkeypatch.setattr(dns.resolver, "get_default_resolver", fake.default_resolver)
    monkeypatch.setattr(dns.query, "udp", fake.udp)
    return fake


# -- Route 53 double ---------------------------------------------------- #


class FakeRoute53Client:
    """Records every call; scripts every answer. No socket anywhere."""

    def __init__(
        self,
        zone_pages: list[dict[str, Any]] | None = None,
        rrsets: list[dict[str, Any]] | None = None,
        raise_on: frozenset[str] = frozenset(),
    ) -> None:
        self.zone_pages = zone_pages if zone_pages is not None else [
            {"HostedZones": [{"Name": "example.com.", "Id": "/hostedzone/Z-EXAMPLE"}]}
        ]
        self.rrsets = rrsets or []
        self.raise_on = raise_on
        self.list_zone_calls: list[dict[str, Any]] = []
        self.list_rrset_calls: list[dict[str, Any]] = []
        self.change_calls: list[dict[str, Any]] = []

    def _maybe_raise(self, name: str) -> None:
        if name in self.raise_on:
            raise RuntimeError(f"route53 {name} exploded")

    def list_hosted_zones(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_raise("list_hosted_zones")
        self.list_zone_calls.append(kwargs)
        index = 0
        if kwargs.get("Marker"):
            index = int(kwargs["Marker"])
        return self.zone_pages[index]

    def list_resource_record_sets(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_raise("list_resource_record_sets")
        self.list_rrset_calls.append(kwargs)
        return {"ResourceRecordSets": list(self.rrsets)}

    def change_resource_record_sets(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_raise("change_resource_record_sets")
        self.change_calls.append(kwargs)
        return {"ChangeInfo": {"Id": "/change/C1", "Status": "PENDING"}}

    # -- readers used by the assertions ------------------------------- #

    @property
    def changes(self) -> list[dict[str, Any]]:
        return [
            change
            for call in self.change_calls
            for change in call["ChangeBatch"]["Changes"]
        ]

    @property
    def record_types(self) -> list[str]:
        return [c["ResourceRecordSet"]["Type"] for c in self.changes]


@pytest.fixture
def r53(monkeypatch: pytest.MonkeyPatch) -> FakeRoute53Client:
    client = FakeRoute53Client()
    monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
    return client


# -- Hetzner double ----------------------------------------------------- #


class FakeHTTPStatusError(Exception):
    pass


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise FakeHTTPStatusError(f"HTTP {self.status_code}")


class FakeHetznerClient:
    """Records (method, url, json); answers from a handler."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.zones: list[dict[str, Any]] = [{"id": 840, "name": "example.com"}]
        #: rrset paths (``/zones/840/rrsets/name/TYPE``) that already exist.
        self.existing: set[str] = set()
        self.raise_on: set[str] = set()
        self.entered = 0
        self.exited = 0

    def __enter__(self) -> FakeHetznerClient:
        self.entered += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        self.exited += 1

    def _record(self, method: str, url: str, json: Any = None) -> None:
        self.calls.append((method, url, json))

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self._record("GET", url)
        if "GET" in self.raise_on:
            raise FakeHTTPStatusError("hetzner GET exploded")
        if url.endswith("/zones"):
            return FakeResponse(200, {"zones": self.zones})
        return FakeResponse(200 if self._path(url) in self.existing else 404)

    def post(self, url: str, json: Any = None, **kwargs: Any) -> FakeResponse:
        self._record("POST", url, json)
        if "POST" in self.raise_on:
            raise FakeHTTPStatusError("hetzner POST exploded")
        return FakeResponse(200)

    def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        self._record("DELETE", url)
        if "DELETE" in self.raise_on:
            raise FakeHTTPStatusError("hetzner DELETE exploded")
        return FakeResponse(200 if self._path(url) in self.existing else 404)

    @staticmethod
    def _path(url: str) -> str:
        return url[len(hetzner_mod.API_BASE) :]

    # -- readers -------------------------------------------------------- #

    def paths(self, method: str) -> list[str]:
        return [self._path(url) for verb, url, _ in self.calls if verb == method]

    def bodies(self, method: str) -> list[Any]:
        return [body for verb, _, body in self.calls if verb == method]


@pytest.fixture
def hetzner(monkeypatch: pytest.MonkeyPatch) -> FakeHetznerClient:
    client = FakeHetznerClient()
    monkeypatch.setattr(hetzner_mod, "make_client", lambda credentials: client)
    return client


# -- nsupdate double ---------------------------------------------------- #


class FakeNsUpdate:
    def __init__(self) -> None:
        self.messages: list[Any] = []
        self.nameservers: list[Any] = []
        self.error: Exception | None = None

    def tcp(self, message: Any, where: Any, timeout: Any = None) -> Any:
        self.messages.append(message)
        self.nameservers.append(where)
        if self.error is not None:
            raise self.error
        return message

    @property
    def rdtypes(self) -> list[Any]:
        """Every rdatatype in every UPDATE section this adapter sent."""
        return [
            rrset.rdtype
            for message in self.messages
            for rrset in message.update
        ]

    @property
    def rdtype_texts(self) -> list[str]:
        return [dns.rdatatype.to_text(rdtype) for rdtype in self.rdtypes]


@pytest.fixture
def nsupdate(monkeypatch: pytest.MonkeyPatch) -> FakeNsUpdate:
    fake = FakeNsUpdate()
    monkeypatch.setattr(dns.query, "tcp", fake.tcp)
    return fake


# -- account builders --------------------------------------------------- #


def r53_account(**kwargs: Any) -> ProviderAccount:
    return ProviderAccount(
        service=kwargs.pop("service", "route53"),
        domains=kwargs.pop("domains", ("example.com",)),
        credentials=kwargs.pop("credentials", R53_CREDS),
        **kwargs,
    )


def hetzner_account(**kwargs: Any) -> ProviderAccount:
    return ProviderAccount(
        service=kwargs.pop("service", "hetzner"),
        domains=kwargs.pop("domains", ("example.com",)),
        credentials=kwargs.pop("credentials", HETZNER_CREDS),
        **kwargs,
    )


def nsupdate_account(**kwargs: Any) -> ProviderAccount:
    return ProviderAccount(
        service=kwargs.pop("service", "nsupdate"),
        domains=kwargs.pop("domains", ("example.com",)),
        credentials=kwargs.pop("credentials", NSUPDATE_LEGACY_CREDS),
        **kwargs,
    )


ZONES = {"example.com": ["test.example.com"]}


def _contacts(double: Any) -> list[Any]:
    """Everything a boundary double recorded, whatever kind it is."""
    if isinstance(double, FakeRoute53Client):
        return double.list_zone_calls + double.list_rrset_calls + double.change_calls
    if isinstance(double, FakeHetznerClient):
        return list(double.calls)
    if isinstance(double, FakeNsUpdate):
        return list(double.messages)
    raise AssertionError(f"no contact reader for {type(double).__name__}")


# --------------------------------------------------------------------------
# The guard on the guards
# --------------------------------------------------------------------------


def test_the_network_guard_bites() -> None:
    """The autouse no-network fixture is live, not decorative."""
    with pytest.raises(NetworkAttempt):
        socket.create_connection(("192.0.2.1", 53), timeout=0.001)
    with pytest.raises(NetworkAttempt):
        socket.getaddrinfo("example.invalid", 53)


# --------------------------------------------------------------------------
# The frozen static helpers
# --------------------------------------------------------------------------


class TestGetIp:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("203.0.113.10", "203.0.113.10"),
            ("0.0.0.0", "0.0.0.0"),
            ("255.255.255.255", "255.255.255.255"),
            ("2001:db8::1", "2001:db8::1"),
            # Normalisation is the whole reason the wire echoes this
            # value rather than the client's spelling.
            ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
            ("::1", "::1"),
        ],
    )
    def test_valid_addresses_normalise(self, raw: str, expected: str) -> None:
        assert str(BaseProvider.getip(raw)) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", "not-an-ip", "203.0.113.999", "2001:db8::g", None, [], "203.0.113.10/32"],
    )
    def test_invalid_is_false_not_an_exception(self, raw: Any) -> None:
        assert BaseProvider.getip(raw) is False

    def test_the_all_zeroes_address_is_truthy(self) -> None:
        """``False`` is a usable sentinel only because no address is falsy.

        ``0.0.0.0`` is the address that would break a ``None``-vs-falsy
        confusion, and ``if not getip(...)`` is how every caller reads
        the result.
        """
        assert bool(BaseProvider.getip("0.0.0.0")) is True
        assert bool(BaseProvider.getip("::")) is True


class TestGetIpType:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("203.0.113.10", "A"),
            ("0.0.0.0", "A"),
            ("255.255.255.255", "A"),
            ("192.0.2.1", "A"),
            ("2001:db8::1", "AAAA"),
            ("2001:0db8:0000:0000:0000:0000:0000:0001", "AAAA"),
            ("::1", "AAAA"),
            ("::", "AAAA"),
            # An IPv4-mapped IPv6 literal is an IPv6Address, so AAAA.
            # The address family of the *literal* decides, not the
            # address it embeds.
            ("::ffff:192.0.2.1", "AAAA"),
            # Scoped v6 — legal since Python 3.9 and the legacy service
            # runs 3.12, so `str()` keeps the zone.
            ("fe80::1%eth0", "AAAA"),
        ],
    )
    def test_family(self, raw: str, expected: str) -> None:
        assert BaseProvider.getiptype(raw) == expected

    @pytest.mark.parametrize("raw", ["", "nope", None, "203.0.113.256"])
    def test_invalid_is_false(self, raw: Any) -> None:
        assert BaseProvider.getiptype(raw) is False

    def test_accepts_an_already_parsed_address(self) -> None:
        """The router validates first and passes the object, not the string."""
        assert BaseProvider.getiptype(ipaddress.ip_address("203.0.113.10")) == "A"
        assert BaseProvider.getiptype(ipaddress.ip_address("2001:db8::1")) == "AAAA"


class TestIsValidHostname:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("ok.example.com", ["ok.example.com"]),
            (
                "ok.example.com,zz-ok.example.com",
                ["ok.example.com", "zz-ok.example.com"],
            ),
            ("UPPER.Example.COM", ["UPPER.Example.COM"]),
            ("a-b.example.com", ["a-b.example.com"]),
            ("1.2.example.com", ["1.2.example.com"]),
            # One trailing dot is stripped *for the check* and kept in
            # the returned element, so the lookup then misses.
            ("ok.example.com.", ["ok.example.com."]),
        ],
    )
    def test_accepted(self, raw: str, expected: list[str]) -> None:
        assert BaseProvider.isvalidhostname(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "_dmarc.example.com",
            "-bad.example.com",
            "bad-.example.com",
            "a..example.com",
            "a" * 64 + ".example.com",
            "a" * 256,
            "ok.example.com,,zz-ok.example.com",
            "ok.example.com,",
            ",ok.example.com",
            "ok.example.com,_dmarc.example.com",
            "ok example.com",
            "ok.example.com..",
        ],
    )
    def test_rejected(self, raw: str) -> None:
        assert BaseProvider.isvalidhostname(raw) is False

    def test_divergence_5_a_dotless_label_validates(self) -> None:
        """Protocol doc says notfqdn; the implementation says nohost.

        Preserved. Both spellings already end at ``nohost``, and a "fix"
        would promote a per-hostname ``nohost`` line to a whole-request
        ``notfqdn`` — a different answer for every *other* hostname in
        the same request.
        """
        assert BaseProvider.isvalidhostname("foo") == ["foo"]

    def test_divergence_6_a_trailing_newline_validates(self) -> None:
        r"""``$`` matches before a final newline, so ``foo\n`` passes.

        And the element comes back **unstripped**, which is what makes
        the subsequent lookup miss and the answer ``nohost``. Both
        halves matter: validating but stripping would turn this into a
        successful update of ``foo``.
        """
        assert BaseProvider.isvalidhostname("foo\n") == ["foo\n"]
        assert BaseProvider.isvalidhostname("ok.example.com\n") == [
            "ok.example.com\n"
        ]

    def test_divergence_6_is_per_label_not_per_hostname(self) -> None:
        r"""Measured, and it is wider than the frozen case shows.

        The table's case is ``foo\n`` — a newline at the end of the
        whole hostname. The regex runs per *label*, so any label ending
        in a newline passes: ``ok\n.example.com`` validates too, and the
        unstripped element then misses the lookup exactly as ``foo\n``
        does. Recorded here because the wire answer is the same
        (``nohost``) and the table therefore cannot distinguish the two
        readings, so an implementation that anchored with ``\Z`` would
        pass the frozen table and change this.
        """
        assert BaseProvider.isvalidhostname("ok\n.example.com") == [
            "ok\n.example.com"
        ]

    def test_a_newline_inside_a_label_is_still_rejected(self) -> None:
        """The divergence is ``$``-specific, not "newlines are fine".

        ``$`` matches at the end or immediately before a *final*
        newline, so a newline anywhere else fails.
        """
        assert BaseProvider.isvalidhostname("o\nk.example.com") is False
        assert BaseProvider.isvalidhostname("ok.exa\nmple.com") is False

    def test_the_empty_element_check_precedes_the_regex(self) -> None:
        """Which is why ``hostname=`` is ``911`` and a trailing comma is
        ``notfqdn`` — the ordering, not the regex, decides."""
        assert BaseProvider.isvalidhostname("") is False
        assert BaseProvider.isvalidhostname("ok.example.com,") is False

    def test_the_length_limit_is_measured_on_the_comma_element(self) -> None:
        """255 is the ceiling, and it is measured on the whole element.

        The two names below differ by exactly one character in one
        label, so nothing but the length rule can separate them — a
        version that only enforced the 63-octet label limit would accept
        both.
        """
        at_255 = ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 61, "b"])
        at_256 = ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 62, "b"])
        assert len(at_255) == 255 and len(at_256) == 256
        assert BaseProvider.isvalidhostname(at_255) == [at_255]
        assert BaseProvider.isvalidhostname(at_256) is False

    def test_a_single_label_longer_than_63_is_rejected_by_the_regex(self) -> None:
        """A different rule from the 255 ceiling, and worth separating:
        ``"a" * 255`` is under the length limit and still refused,
        because ``{1,63}`` followed by ``$`` cannot span it."""
        assert BaseProvider.isvalidhostname("a" * 255) is False


class TestDomainInHostname:
    def _provider(self, domains: tuple[str, ...]) -> BaseProvider:
        return BaseProvider(ProviderAccount(service="x", domains=domains))

    def test_exact_and_subdomain(self) -> None:
        provider = self._provider(("example.com",))
        assert provider.domaininhostname("example.com") == "example.com"
        assert provider.domaininhostname("a.example.com") == "example.com"
        assert provider.domaininhostname("a.b.example.com") == "example.com"

    def test_case_insensitive(self) -> None:
        provider = self._provider(("Example.COM",))
        assert provider.domaininhostname("A.example.com") == "Example.COM"

    def test_outside_every_domain_is_false(self) -> None:
        provider = self._provider(("example.com",))
        assert provider.domaininhostname("example.org") is False
        assert provider.domaininhostname("example.com.evil.test") is False

    def test_preserved_quirk_no_label_boundary(self) -> None:
        """``notexample.com`` matches ``example.com``. Recorded, not fixed.

        The legacy test is ``rfind`` plus an end-offset check with no
        dot boundary. It is not a tenancy hole under the rewrite — the
        domain set comes from the hostname's own owning row rather than
        from the request — but it is wrong, and pinning it here means a
        later correction reads as a deliberate edit to this assertion
        rather than as an unexplained behaviour change.
        """
        provider = self._provider(("example.com",))
        assert provider.domaininhostname("notexample.com") == "example.com"

    def test_first_configured_domain_wins_not_the_longest(self) -> None:
        provider = self._provider(("example.com", "dyn.example.com"))
        assert provider.domaininhostname("a.dyn.example.com") == "example.com"


class TestHostnamePerZone:
    def _provider(self, domains: tuple[str, ...]) -> BaseProvider:
        return BaseProvider(ProviderAccount(service="x", domains=domains))

    def test_groups_by_domain(self) -> None:
        provider = self._provider(("example.com", "example.org"))
        assert provider.hostnameperzone(
            ["a.example.com", "b.example.org", "c.example.com"]
        ) == {
            "example.com": ["a.example.com", "c.example.com"],
            "example.org": ["b.example.org"],
        }

    def test_one_outsider_refuses_the_whole_group(self) -> None:
        """``False``, not a partial map. The router turns it into
        ``nohost`` — ``protocol_cases.yaml::update-nohost-hostname-
        outside-backend-zone``."""
        provider = self._provider(("example.com",))
        assert provider.hostnameperzone(["a.example.com", "b.example.org"]) is False

    def test_empty_input_is_an_empty_map_not_false(self) -> None:
        provider = self._provider(("example.com",))
        assert provider.hostnameperzone([]) == {}

    def test_performs_no_io(self, fake_dns: FakeDns) -> None:
        """The legacy version called ``gethostedzones()`` for its side
        effect, which on the AWS adapter was a Route 53 API call per
        request whose result was discarded."""
        provider = self._provider(("example.com",))
        provider.hostnameperzone(["a.example.com"])
        assert fake_dns.queries == []


# --------------------------------------------------------------------------
# Two instruments: the port, replayed against the frozen wire table
# --------------------------------------------------------------------------


def _find_protocol_cases() -> pathlib.Path | None:
    candidates = [
        # Inside the api container, where `make test-backend` runs:
        # the Dockerfile's dev stage COPYs `tests/` to /opt/compat_tests.
        pathlib.Path("/opt/compat_tests/compat/protocol_cases.yaml"),
        # A plain checkout: backend/tests/ -> repo root -> tests/compat.
        pathlib.Path(__file__).resolve().parents[2]
        / "tests"
        / "compat"
        / "protocol_cases.yaml",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


#: Bodies whose first token implies the hostname *passed* validation.
#: `911` and `abuse` and `badauth` are decided before or instead of the
#: hostname check, so they imply nothing and are skipped rather than
#: guessed at.
_VALID_IMPLYING = ("good", "nochg", "nohost", "dnserr")


def _hostname_verdicts() -> list[tuple[str, str, bool]]:
    """(case id, hostname, expected truthiness of isvalidhostname)."""
    path = _find_protocol_cases()
    if path is None:
        return []
    yaml = pytest.importorskip("yaml")
    table = yaml.safe_load(path.read_text())

    verdicts: list[tuple[str, str, bool]] = []
    for case in table.get("cases", []):
        query = case.get("query") or {}
        hostname = query.get("hostname")
        if not isinstance(hostname, str) or not hostname:
            continue
        body = (case.get("expect") or {}).get("body")
        if not isinstance(body, str):
            continue
        covers = case.get("covers") or []
        if any(tag.endswith("/notfqdn") for tag in covers):
            verdicts.append((case["id"], hostname, False))
        elif body.split(" ")[0] in _VALID_IMPLYING:
            verdicts.append((case["id"], hostname, True))
    return verdicts


_VERDICTS = _hostname_verdicts()


class TestAgainstTheFrozenWireTable:
    """A second instrument on ``isvalidhostname``, authored by #11/#29.

    The assertions above and the implementation share an author. The
    frozen table does not, and it is the artefact V1M2 is measured
    against, so agreeing with it is the reading that matters.
    """

    def test_the_table_was_found_and_is_not_empty(self) -> None:
        """Vacuity guard. Without it a missing file renders as a pass.

        30 is well under the count this extracts today and well over
        zero, so it fails on "the file moved" without failing on "#16
        added a case".
        """
        if _find_protocol_cases() is None:
            pytest.skip(
                "protocol_cases.yaml not reachable from this checkout — the "
                "cross-check did NOT run (this is not a pass)"
            )
        assert len(_VERDICTS) >= 30, (
            f"only {len(_VERDICTS)} cases yielded a verdict on isvalidhostname; "
            "the extractor is probably reading the wrong shape"
        )

    @pytest.mark.parametrize(
        "case_id,hostname,expected_valid",
        _VERDICTS,
        ids=[case_id for case_id, _, _ in _VERDICTS] or None,
    )
    def test_agrees_with_the_frozen_expectation(
        self, case_id: str, hostname: str, expected_valid: bool
    ) -> None:
        actual = bool(BaseProvider.isvalidhostname(hostname))
        assert actual is expected_valid, (
            f"{case_id}: the frozen table expects isvalidhostname("
            f"{hostname!r}) to be {expected_valid}, the port answers {actual}"
        )


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


class TestRegistry:
    @pytest.mark.parametrize(
        "service,expected",
        [
            ("route53", Route53Provider),
            # The legacy literal. Every production row carries it, so the
            # V1M4 importer needs no data rewrite.
            ("aws", Route53Provider),
            ("hetzner", HetznerProvider),
            ("nsupdate", NsUpdateProvider),
        ],
    )
    def test_known_names_resolve(self, service: str, expected: type) -> None:
        assert provider_class(service) is expected

    @pytest.mark.parametrize(
        "service",
        [
            "no-such-service",
            "",
            None,
            5,
            b"hetzner",
            "ROUTE53",
            " hetzner",
            "hetzner ",
            {"service": "hetzner"},
        ],
    )
    def test_unknown_names_resolve_to_nothing_and_never_raise(
        self, service: Any
    ) -> None:
        assert provider_class(service) is None
        assert get_provider(ProviderAccount(service=service)) is None

    def test_the_status_for_an_unresolvable_backend_is_911(self) -> None:
        """And it is not in the adapter vocabulary, because by then there
        is no adapter."""
        assert STATUS_UNKNOWN_SERVICE == "911"
        assert STATUS_UNKNOWN_SERVICE not in PROVIDER_STATUSES

    def test_the_911_constant_agrees_with_the_frozen_table(self) -> None:
        """Second instrument on the constant: the table, not this file."""
        path = _find_protocol_cases()
        if path is None:
            pytest.skip("protocol_cases.yaml not reachable — cross-check NOT run")
        yaml = pytest.importorskip("yaml")
        table = yaml.safe_load(path.read_text())
        cases = {case["id"]: case for case in table.get("cases", [])}
        wanted = "update-911-backend-service-unknown-to-factory"
        assert wanted in cases, "the case this constant answers has been renamed"
        body = cases[wanted]["expect"]["body"]
        assert body.split(" ")[0] == STATUS_UNKNOWN_SERVICE

    def test_known_services_offers_canonical_names_only(self) -> None:
        assert known_services() == ("hetzner", "nsupdate", "route53")
        assert "aws" not in known_services()

    def test_every_legacy_service_name_still_resolves(self) -> None:
        """The union of the three legacy adapters' ``_services``.

        A migrated row whose ``backend_type`` is any of these must find
        an adapter, or V1M4 silently converts working backends into
        ``911``.
        """
        legacy = {"aws", "hetzner", "nsupdate"}
        assert legacy <= set(resolvable_services())

    def test_every_offered_name_resolves(self) -> None:
        for service in known_services():
            assert provider_class(service) is not None

    def test_registration_is_derived_from_the_provider_tuple(self) -> None:
        """A provider deleted from ``_PROVIDERS`` takes its advertised
        name with it — the list cannot drift from the registry because
        there is no second list."""
        from atrium_ddns.providers import _PROVIDERS, _REGISTRY

        derived = {
            name for cls in _PROVIDERS for name in cls.service_names()
        }
        assert derived <= set(_REGISTRY)

    def test_register_and_unregister_round_trip(self) -> None:
        """The seam ``protocol_cases.yaml``'s ``stub`` service needs."""

        class StubProvider(BaseProvider):
            SERVICE = "stub-for-tests"

        register(StubProvider)
        try:
            assert provider_class("stub-for-tests") is StubProvider
            resolved = get_provider(ProviderAccount(service="stub-for-tests"))
            assert isinstance(resolved, StubProvider)
        finally:
            unregister(StubProvider)
        assert provider_class("stub-for-tests") is None

    def test_register_refuses_a_collision(self) -> None:
        class Impostor(BaseProvider):
            SERVICE = "hetzner"

        with pytest.raises(ValueError, match="already registered"):
            register(Impostor)
        # …and the real one is untouched.
        assert provider_class("hetzner") is HetznerProvider

    def test_register_refuses_a_provider_with_no_service_name(self) -> None:
        class Nameless(BaseProvider):
            pass

        with pytest.raises(ValueError, match="no SERVICE"):
            register(Nameless)

    def test_construction_failure_answers_none_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A backstop, not a path — but a 500 here breaks every client
        that parses the body, so it must be ``911`` instead."""

        class Exploding(BaseProvider):
            SERVICE = "exploding-for-tests"

            def __init__(self, account: ProviderAccount) -> None:
                raise RuntimeError("boom")

        register(Exploding)
        try:
            assert get_provider(ProviderAccount(service="exploding-for-tests")) is None
        finally:
            unregister(Exploding)

    def test_account_from_mapping_reads_the_legacy_dict(self) -> None:
        account = providers.account_from_mapping(
            {
                "service": "hetzner",
                "domains": ["example.com"],
                "credentials": HETZNER_CREDS,
            }
        )
        assert account.service == "hetzner"
        assert account.domains == ("example.com",)
        assert account.credentials == HETZNER_CREDS
        assert account.config == {}


# --------------------------------------------------------------------------
# Credentials: from the row, and from nowhere else
# --------------------------------------------------------------------------


#: The environment variables the legacy adapters fell back to.
LEGACY_ENV = {
    "AWS_ACCESS_KEY_ID": "env-access-key",
    "AWS_SECRET_ACCESS_KEY": "env-secret-key",
    "HETZNER_API_TOKEN": "env-hetzner-token",
    "NSUPDATE_KEY": "env-key-name",
    "NSUPDATE_ALGO": "hmac-sha256",
    "NSUPDATE_SECRET": "ZW52LXNlY3JldC1tYXRlcmlhbC1oZXJlLW9rYXkh",
    "NSUPDATE_NAMESERVER": "192.0.2.99",
}


@pytest.fixture
def legacy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in LEGACY_ENV.items():
        monkeypatch.setenv(name, value)


class TestNoEnvironmentCredentialFallback:
    """``model_cases.yaml::provider-must-not-fall-back-to-environment-
    credentials``, disposition ``fix``.

    The legacy test asserted the fallback *worked*. Ported inverted: the
    surviving assertion is that it does not happen. Under multi-tenancy
    one operator-set variable would serve every tenant whose row is
    blank, which is a cross-tenant credential leak wearing a
    convenience's clothes.
    """

    @pytest.mark.parametrize(
        "account_factory,fake_name",
        [
            (lambda: r53_account(credentials={}), "r53"),
            (lambda: hetzner_account(credentials={}), "hetzner"),
            (lambda: nsupdate_account(credentials={}, config={}), "nsupdate"),
        ],
    )
    def test_an_empty_row_is_uncredentialled_even_with_the_env_set(
        self,
        account_factory: Any,
        fake_name: str,
        legacy_env: None,
        request: pytest.FixtureRequest,
        fake_dns: FakeDns,
    ) -> None:
        double = request.getfixturevalue(fake_name)
        provider = get_provider(account_factory())
        assert provider is not None
        assert provider.has_credentials() is False

        create = provider.createrecords(V4, ZONES, rtype="A")
        delete = provider.deleterecords(ZONES)
        assert create == {"test.example.com": STATUS_DNSERR}
        assert delete == {"test.example.com": STATUS_DNSERR}

        # And nothing was even attempted against the provider — the
        # status alone would be satisfied by an adapter that tried the
        # environment credential, failed, and reported the failure.
        assert not _contacts(double), (
            f"{fake_name} was contacted with no stored credentials"
        )

    def test_no_module_in_the_package_reads_the_environment(self) -> None:
        """Structural instrument, so a *new* provider is covered too.

        ``ast`` rather than ``grep``: this package's docstrings name
        ``AWS_ACCESS_KEY_ID`` and ``HETZNER_API_TOKEN`` on purpose, and a
        text search would either flag them or be weakened until it did
        not flag anything.
        """
        package_dir = pathlib.Path(providers.__file__).parent
        modules = sorted(package_dir.glob("*.py"))
        assert len(modules) >= 5, (
            f"only found {len(modules)} modules under {package_dir}; the sweep "
            "is looking in the wrong place"
        )

        offenders: list[str] = []
        for module in modules:
            tree = ast.parse(module.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "os" or alias.name.startswith("os."):
                            offenders.append(f"{module.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "os":
                        names = ", ".join(a.name for a in node.names)
                        offenders.append(f"{module.name}: from os import {names}")
                elif isinstance(node, ast.Attribute):
                    if node.attr in {"environ", "getenv"}:
                        offenders.append(f"{module.name}: .{node.attr}")
                elif isinstance(node, ast.Name):
                    if node.id in {"environ", "getenv"}:
                        offenders.append(f"{module.name}: {node.id}")
        assert not offenders, (
            "provider modules must read credentials from the row only; found: "
            + "; ".join(offenders)
        )


class TestCredentialDisclosure:
    def test_the_account_repr_masks_the_credentials(self) -> None:
        """This object reaches log formatters and assertion rewriting.

        The log store in this estate has deletion disabled, so one
        unlucky ``log.debug(account)`` is permanent.
        """
        account = r53_account()
        rendered = repr(account)
        assert R53_CREDS["aws_secret_access_key"] not in rendered
        assert R53_CREDS["aws_access_key_id"] not in rendered
        # Key names are diagnostics, not secrets, and the whole point of
        # the message is to say what was looked for.
        assert "aws_secret_access_key" in rendered
        assert "<redacted>" in rendered

    def test_not_even_a_prefix_of_the_secret_survives(self) -> None:
        account = hetzner_account()
        token = HETZNER_CREDS["hetzner_api_token"]
        rendered = repr(account)
        for length in range(4, len(token) + 1):
            assert token[:length] not in rendered

    def test_the_provider_repr_does_not_carry_the_account(
        self, hetzner: FakeHetznerClient
    ) -> None:
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert HETZNER_CREDS["hetzner_api_token"] not in repr(provider)


# --------------------------------------------------------------------------
# Record-type dispatch — the point of this issue
# --------------------------------------------------------------------------


#: One address per interesting spelling, with the family it must produce.
#: This is the set #29's mutation (``getiptype`` answering ``A`` for
#: everything) has to fail against.
FAMILY_CASES = [
    ("203.0.113.10", "A"),
    ("192.0.2.1", "A"),
    ("0.0.0.0", "A"),
    ("255.255.255.255", "A"),
    ("2001:db8::1", "AAAA"),
    ("2001:0db8:0000:0000:0000:0000:0000:0001", "AAAA"),
    ("::1", "AAAA"),
    ("::ffff:192.0.2.1", "AAAA"),
]


class TestRoute53RecordType:
    @pytest.mark.parametrize("address,family", FAMILY_CASES)
    def test_create_sends_the_family_the_address_belongs_to(
        self,
        address: str,
        family: str,
        r53: FakeRoute53Client,
        fake_dns: FakeDns,
    ) -> None:
        """End to end through ``getiptype``, asserted on the ChangeBatch.

        The router derives ``rtype`` exactly this way, so a mutation in
        ``getiptype`` shows up here — and nowhere on the wire.
        """
        provider = get_provider(r53_account())
        assert provider is not None
        rtype = BaseProvider.getiptype(address)
        result = provider.createrecords(address, ZONES, rtype=rtype, ttl=90)

        assert result == {"test.example.com": STATUS_GOOD}
        assert r53.record_types == [family]
        rrset = r53.changes[0]["ResourceRecordSet"]
        assert rrset["Type"] == family
        assert rrset["Name"] == "test.example.com"
        assert rrset["ResourceRecords"] == [{"Value": address}]
        assert rrset["TTL"] == 90
        assert r53.changes[0]["Action"] == "UPSERT"
        assert r53.change_calls[0]["HostedZoneId"] == "/hostedzone/Z-EXAMPLE"

    def test_route53_names_are_fully_qualified_not_relative(
        self, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        """Route 53 takes the FQDN. Sending a Hetzner-style relative
        name would create ``test.example.com.example.com``."""
        provider = get_provider(r53_account())
        assert provider is not None
        provider.createrecords(V4, ZONES, rtype="A")
        assert r53.changes[0]["ResourceRecordSet"]["Name"] == "test.example.com"

    @pytest.mark.parametrize("rtype,expected", [("A", ["A"]), ("AAAA", ["AAAA"])])
    def test_delete_removes_only_the_named_family(
        self, rtype: str, expected: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeRoute53Client(
            rrsets=[
                {"Name": "test.example.com.", "Type": "A", "TTL": 60},
                {"Name": "test.example.com.", "Type": "AAAA", "TTL": 60},
                {"Name": "test.example.com.", "Type": "TXT", "TTL": 60},
                {"Name": "other.example.com.", "Type": "A", "TTL": 60},
            ]
        )
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None

        result = provider.deleterecords(ZONES, rtype=rtype)
        assert result == {"test.example.com": STATUS_GOOD}
        assert client.record_types == expected
        assert all(
            change["Action"] == "DELETE" for change in client.changes
        )
        # The other name in the same zone is untouched.
        assert all(
            change["ResourceRecordSet"]["Name"] == "test.example.com."
            for change in client.changes
        )

    def test_delete_without_a_type_covers_both_families(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeRoute53Client(
            rrsets=[
                {"Name": "test.example.com.", "Type": "A", "TTL": 60},
                {"Name": "test.example.com.", "Type": "AAAA", "TTL": 60},
            ]
        )
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_GOOD}
        assert sorted(client.record_types) == ["A", "AAAA"]


class TestHetznerRecordType:
    @pytest.mark.parametrize("address,family", FAMILY_CASES)
    def test_create_sends_the_family_in_the_url_and_the_body(
        self,
        address: str,
        family: str,
        hetzner: FakeHetznerClient,
        fake_dns: FakeDns,
    ) -> None:
        provider = get_provider(hetzner_account())
        assert provider is not None
        rtype = BaseProvider.getiptype(address)
        result = provider.createrecords(address, ZONES, rtype=rtype, ttl=90)

        assert result == {"test.example.com": STATUS_GOOD}
        # The existence probe carries the type…
        assert f"/zones/840/rrsets/test/{family}" in hetzner.paths("GET")
        # …and so does the create body.
        body = hetzner.bodies("POST")[-1]
        assert body == {
            "name": "test",
            "type": family,
            "ttl": 90,
            "records": [{"value": address}],
        }

    def test_an_existing_rrset_is_replaced_through_set_records(
        self, hetzner: FakeHetznerClient, fake_dns: FakeDns
    ) -> None:
        hetzner.existing.add("/zones/840/rrsets/test/AAAA")
        provider = get_provider(hetzner_account())
        assert provider is not None
        result = provider.createrecords(V6, ZONES, rtype="AAAA", ttl=60)

        assert result == {"test.example.com": STATUS_GOOD}
        assert hetzner.paths("POST") == [
            "/zones/840/rrsets/test/AAAA/actions/set_records"
        ]
        assert hetzner.bodies("POST")[-1] == {
            "ttl": 60,
            "records": [{"value": V6}],
        }

    @pytest.mark.parametrize("rtype", ["A", "AAAA"])
    def test_delete_targets_only_the_named_family(
        self, rtype: str, hetzner: FakeHetznerClient
    ) -> None:
        hetzner.existing.update(
            {
                "/zones/840/rrsets/test/A",
                "/zones/840/rrsets/test/AAAA",
            }
        )
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert provider.deleterecords(ZONES, rtype=rtype) == {
            "test.example.com": STATUS_GOOD
        }
        assert hetzner.paths("DELETE") == [f"/zones/840/rrsets/test/{rtype}"]

    def test_delete_without_a_type_makes_two_calls_one_per_family(
        self, hetzner: FakeHetznerClient
    ) -> None:
        hetzner.existing.update(
            {
                "/zones/840/rrsets/test/A",
                "/zones/840/rrsets/test/AAAA",
            }
        )
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_GOOD}
        assert hetzner.paths("DELETE") == [
            "/zones/840/rrsets/test/A",
            "/zones/840/rrsets/test/AAAA",
        ]


class TestNsUpdateRecordType:
    @pytest.mark.parametrize("address,family", FAMILY_CASES)
    def test_create_sets_the_rdatatype_on_the_update_message(
        self,
        address: str,
        family: str,
        nsupdate: FakeNsUpdate,
        fake_dns: FakeDns,
    ) -> None:
        """Asserted by reading the DNS UPDATE message itself.

        Not by reading a keyword argument back out of a mock — the
        message is what leaves the process, and dnspython builds it from
        ``rtype`` in a way that a wrong string would not survive.
        """
        provider = get_provider(nsupdate_account())
        assert provider is not None
        rtype = BaseProvider.getiptype(address)
        result = provider.createrecords(address, ZONES, rtype=rtype, ttl=90)

        assert result == {"test.example.com": STATUS_GOOD}
        # `replace()` emits two RRsets: an empty delete-the-RRset entry
        # and the addition. Both carry the record type, and *both* have
        # to be right — a mismatched pair would delete one family and
        # add the other.
        assert nsupdate.rdtype_texts == [family, family]

        message = nsupdate.messages[0]
        assert message.zone[0].name.to_text() == "example.com."
        deletion, addition = message.update
        assert len(deletion) == 0
        assert deletion.name.to_text() == "test.example.com."
        assert addition.name.to_text() == "test.example.com."
        assert addition.ttl == 90
        assert [rdata.address for rdata in addition] == [
            str(ipaddress.ip_address(address))
        ]
        assert nsupdate.nameservers == [NSUPDATE_CONFIG["nsupdate_nameserver"]]

    @pytest.mark.parametrize("rtype", ["A", "AAAA"])
    def test_delete_names_only_the_requested_family(
        self, rtype: str, nsupdate: FakeNsUpdate
    ) -> None:
        provider = get_provider(nsupdate_account())
        assert provider is not None
        assert provider.deleterecords(ZONES, rtype=rtype) == {
            "test.example.com": STATUS_GOOD
        }
        assert nsupdate.rdtype_texts == [rtype]

    def test_delete_without_a_type_carries_both_families_in_one_message(
        self, nsupdate: FakeNsUpdate
    ) -> None:
        """One atomic UPDATE, two deletions — not two messages."""
        provider = get_provider(nsupdate_account())
        assert provider is not None
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_GOOD}
        assert len(nsupdate.messages) == 1
        assert nsupdate.rdtype_texts == list(ADDRESS_FAMILIES)


# --------------------------------------------------------------------------
# nochg — the skip-if-unchanged behaviour
# --------------------------------------------------------------------------


class TestSkipIfUnchanged:
    def test_route53_matching_dns_is_nochg_and_writes_nothing(
        self, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        fake_dns.answers(V4)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_NOCHG
        }
        # "nothing is written" is the assertion, not the status string.
        assert r53.change_calls == []

    def test_hetzner_matching_dns_is_nochg_and_writes_nothing(
        self, hetzner: FakeHetznerClient, fake_dns: FakeDns
    ) -> None:
        fake_dns.answers(V4)
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_NOCHG
        }
        assert hetzner.paths("POST") == []

    def test_nsupdate_matching_dns_is_nochg_and_writes_nothing(
        self, nsupdate: FakeNsUpdate, fake_dns: FakeDns
    ) -> None:
        fake_dns.answers(V4)
        provider = get_provider(nsupdate_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_NOCHG
        }
        assert nsupdate.messages == []

    def test_a_different_address_is_good_and_does_write(
        self, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        fake_dns.answers("203.0.113.99")
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_GOOD
        }
        assert len(r53.change_calls) == 1

    def test_the_comparison_is_on_addresses_not_strings(
        self, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        """DNS answers a different spelling of the same v6 address.

        A string comparison here would write on every single update for
        every hostname whose provider normalises differently from the
        client — and the write would succeed, so nothing would ever
        report it.
        """
        fake_dns.answers("2001:0db8:0000:0000:0000:0000:0000:0001")
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.createrecords("2001:db8::1", ZONES, rtype="AAAA") == {
            "test.example.com": STATUS_NOCHG
        }
        assert r53.change_calls == []

    @pytest.mark.parametrize(
        "error",
        [
            dns.resolver.NXDOMAIN("no such name"),
            dns.resolver.NoAnswer(),
            dns.exception.Timeout("timed out"),
            dns.resolver.NoNameservers(),
            RuntimeError("something else entirely"),
        ],
    )
    def test_an_unresolvable_name_writes_rather_than_skipping(
        self, error: Exception, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        """The failure direction is deliberate and it is the safe one.

        A spurious write is idempotent; a spurious ``nochg`` leaves DNS
        stale for as long as the client keeps sending the same address,
        which is forever.
        """
        fake_dns.fails(error)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_GOOD
        }

    def test_a_non_address_answer_is_not_a_match(
        self, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        fake_dns.answers(V4, rdtype=dns.rdatatype.CNAME)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_GOOD
        }

    def test_an_unparseable_dns_answer_is_not_a_match(
        self, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        fake_dns.answers(V4)
        fake_dns.address = "not-an-address"
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_GOOD
        }

    def test_the_query_carries_the_requested_record_type(
        self, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        provider = get_provider(r53_account())
        assert provider is not None
        provider.createrecords(V6, ZONES, rtype="AAAA")
        assert fake_dns.queries[0][0] == "test.example.com"
        assert fake_dns.queries[0][1] == dns.rdatatype.AAAA


class TestNameserverSelection:
    def test_nsupdate_asks_the_configured_nameserver(
        self, nsupdate: FakeNsUpdate, fake_dns: FakeDns
    ) -> None:
        """A hidden primary is normally minutes ahead of the public
        authoritative servers; asking the wrong one turns a real change
        into a ``nochg``."""
        provider = get_provider(nsupdate_account())
        assert provider is not None
        provider.createrecords(V4, ZONES, rtype="A")
        assert fake_dns.last_nameservers == ("192.0.2.53",)

    def test_others_fall_back_to_a_public_resolver(
        self, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        """``get_authoritative_nameserver`` returning ``None`` must not
        stop the check — it falls back, which is conservative."""
        provider = get_provider(r53_account())
        assert provider is not None
        provider.createrecords(V4, ZONES, rtype="A")
        assert fake_dns.last_nameservers == (providers.base.FALLBACK_NAMESERVER,)

    def test_the_authoritative_walk_answers_none_on_a_timeout(
        self, fake_dns: FakeDns
    ) -> None:
        provider = BaseProvider(ProviderAccount(service="x", domains=("example.com",)))
        assert provider.get_authoritative_nameserver("a.example.com") is None
        assert fake_dns.udp_queries, "the delegation walk was never attempted"

    def test_the_authoritative_walk_answers_none_with_no_system_resolver(
        self, fake_dns: FakeDns
    ) -> None:
        fake_dns.system_nameservers = []
        provider = BaseProvider(ProviderAccount(service="x", domains=("example.com",)))
        assert provider.get_authoritative_nameserver("a.example.com") is None


# --------------------------------------------------------------------------
# Failures are dnserr, never an exception
# --------------------------------------------------------------------------


class TestFailuresAreDnserr:
    def test_route53_create_api_failure(
        self, monkeypatch: pytest.MonkeyPatch, fake_dns: FakeDns
    ) -> None:
        client = FakeRoute53Client(raise_on=frozenset({"change_resource_record_sets"}))
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_DNSERR
        }

    def test_route53_delete_listing_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeRoute53Client(raise_on=frozenset({"list_resource_record_sets"}))
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_DNSERR}

    def test_route53_delete_change_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeRoute53Client(
            rrsets=[{"Name": "test.example.com.", "Type": "A"}],
            raise_on=frozenset({"change_resource_record_sets"}),
        )
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.deleterecords(ZONES, rtype="A") == {
            "test.example.com": STATUS_DNSERR
        }

    def test_route53_client_construction_failure(
        self, monkeypatch: pytest.MonkeyPatch, fake_dns: FakeDns
    ) -> None:
        def explode(credentials: Any) -> Any:
            raise RuntimeError("no credentials chain")

        monkeypatch.setattr(route53_mod, "make_client", explode)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_DNSERR
        }
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_DNSERR}

    def test_hetzner_create_failure(
        self, hetzner: FakeHetznerClient, fake_dns: FakeDns
    ) -> None:
        hetzner.raise_on.add("POST")
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_DNSERR
        }

    def test_hetzner_delete_failure(self, hetzner: FakeHetznerClient) -> None:
        hetzner.raise_on.add("DELETE")
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_DNSERR}

    def test_hetzner_non_2xx_is_dnserr(
        self, monkeypatch: pytest.MonkeyPatch, hetzner: FakeHetznerClient, fake_dns: FakeDns
    ) -> None:
        def failing_post(url: str, json: Any = None, **kwargs: Any) -> FakeResponse:
            hetzner.calls.append(("POST", url, json))
            return FakeResponse(500)

        monkeypatch.setattr(hetzner, "post", failing_post)
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_DNSERR
        }

    def test_nsupdate_query_failure(
        self, nsupdate: FakeNsUpdate, fake_dns: FakeDns
    ) -> None:
        nsupdate.error = dns.exception.Timeout("no answer")
        provider = get_provider(nsupdate_account())
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_DNSERR
        }
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_DNSERR}

    def test_nsupdate_bad_tsig_material_is_dnserr(
        self, nsupdate: FakeNsUpdate, fake_dns: FakeDns
    ) -> None:
        """A stored secret that is not base64 must not raise out of the
        adapter — it is a misconfiguration, and misconfiguration is
        ``dnserr``."""
        account = nsupdate_account(
            credentials={**NSUPDATE_LEGACY_CREDS, "nsupdate_secret": "not base64!!"}
        )
        provider = get_provider(account)
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_DNSERR
        }

    #: nsupdate is absent on purpose: it holds no zone *handles* — it
    #: builds the zone name straight from the map key and lets the
    #: server refuse it — so there is no lookup that could KeyError.
    @pytest.mark.parametrize("fake_name", ["r53", "hetzner"])
    def test_a_zone_the_adapter_does_not_hold_is_dnserr_not_a_keyerror(
        self, fake_name: str, request: pytest.FixtureRequest, fake_dns: FakeDns
    ) -> None:
        """``hostnameperzone`` only produces keys the adapter holds, so
        this is defensive — but a ``KeyError`` escaping into the router
        is a 500, and a 500 breaks every client that parses the body."""
        request.getfixturevalue(fake_name)
        factories = {"r53": r53_account, "hetzner": hetzner_account}
        provider = get_provider(factories[fake_name]())
        assert provider is not None
        rogue = {"never.configured.test": ["a.never.configured.test"]}
        assert provider.createrecords(V4, rogue, rtype="A") == {
            "a.never.configured.test": STATUS_DNSERR
        }
        assert provider.deleterecords(rogue) == {
            "a.never.configured.test": STATUS_DNSERR
        }


class TestStatusVocabulary:
    @pytest.mark.parametrize("fake_name", ["r53", "hetzner", "nsupdate"])
    @pytest.mark.parametrize("rtype", ["A", "AAAA", None])
    def test_every_status_is_in_the_closed_vocabulary(
        self,
        fake_name: str,
        rtype: str | None,
        request: pytest.FixtureRequest,
        fake_dns: FakeDns,
    ) -> None:
        """``good`` | ``nochg`` | ``dnserr`` and nothing else.

        The frozen table's ``stub`` provider is written against this
        vocabulary and cannot verify it — a real adapter answering
        ``ok`` would pass every case in that table and fail in
        production.
        """
        request.getfixturevalue(fake_name)
        factories = {
            "r53": r53_account,
            "hetzner": hetzner_account,
            "nsupdate": nsupdate_account,
        }
        provider = get_provider(factories[fake_name]())
        assert provider is not None

        results: list[dict[str, str]] = [provider.deleterecords(ZONES, rtype=rtype)]
        if rtype is not None:
            results.append(provider.createrecords(V4, ZONES, rtype=rtype))

        for result in results:
            assert set(result) == {"test.example.com"}
            assert set(result.values()) <= PROVIDER_STATUSES


class TestDeleteIdempotence:
    def test_hetzner_absent_records_are_nochg_not_an_error(
        self, hetzner: FakeHetznerClient
    ) -> None:
        """Deleting twice has to be safe or a retrying client cannot
        recover from a dropped response."""
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_NOCHG}

    def test_hetzner_good_if_either_family_existed(
        self, hetzner: FakeHetznerClient
    ) -> None:
        """The mixed case is the *common* one in production — most
        hostnames track one family — so an implementation returning
        ``nochg`` unless both existed would report most successful
        deletes as no-ops."""
        hetzner.existing.add("/zones/840/rrsets/test/A")
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_GOOD}
        assert hetzner.paths("DELETE") == [
            "/zones/840/rrsets/test/A",
            "/zones/840/rrsets/test/AAAA",
        ]

    def test_route53_absent_records_are_nochg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeRoute53Client(rrsets=[])
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_NOCHG}
        assert client.change_calls == []

    def test_route53_good_if_either_family_existed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeRoute53Client(
            rrsets=[{"Name": "test.example.com.", "Type": "AAAA"}]
        )
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_GOOD}
        assert client.record_types == ["AAAA"]

    def test_nsupdate_cannot_answer_nochg_and_that_is_recorded(
        self, nsupdate: FakeNsUpdate
    ) -> None:
        """A preserved divergence, pinned so it stays a decision.

        RFC 2136 answers NOERROR for the deletion of an RRset that was
        not there, so telling absent from removed would need a prequery
        per family per hostname on the request path. The legacy adapter
        answers ``good``; so does this one. No case in the frozen table
        exercises the difference.
        """
        provider = get_provider(nsupdate_account())
        assert provider is not None
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_GOOD}


# --------------------------------------------------------------------------
# Zones: discovery, containment, and relative names
# --------------------------------------------------------------------------


class TestHetznerZones:
    def test_maps_configured_domains_to_zone_ids(
        self, hetzner: FakeHetznerClient
    ) -> None:
        hetzner.zones = [
            {"id": 840, "name": "example.com"},
            {"id": 999, "name": "other.com"},
        ]
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert provider._zones == {"example.com": 840}

    def test_zones_the_row_does_not_name_are_not_candidates(
        self, hetzner: FakeHetznerClient
    ) -> None:
        """Containment, not optimisation: it is what stops one tenant's
        token reaching another tenant's zone on the same account."""
        hetzner.zones = [
            {"id": 840, "name": "example.com"},
            {"id": 999, "name": "someone-elses.com"},
        ]
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert "someone-elses.com" not in provider._zones
        assert set(provider.gethostedzones()) == {"example.com"}

    def test_reverse_zones_are_never_candidates(
        self, hetzner: FakeHetznerClient
    ) -> None:
        hetzner.zones = [
            {"id": 840, "name": "example.com"},
            {"id": 1, "name": "168.192.in-addr.arpa"},
            {"id": 2, "name": "8.b.d.0.1.0.0.2.ip6.arpa"},
        ]
        provider = get_provider(
            hetzner_account(
                domains=("example.com", "168.192.in-addr.arpa", "8.b.d.0.1.0.0.2.ip6.arpa")
            )
        )
        assert provider is not None
        # Configured but not hosted -> keeps its identity mapping and is
        # never bound to a provider zone id.
        assert provider._zones["168.192.in-addr.arpa"] == "168.192.in-addr.arpa"
        assert provider._zones["example.com"] == 840

    def test_a_domain_inside_a_wider_hosted_zone_maps_to_that_zone(
        self, hetzner: FakeHetznerClient
    ) -> None:
        hetzner.zones = [{"id": 840, "name": "example.com"}]
        provider = get_provider(hetzner_account(domains=("dyn.example.com",)))
        assert provider is not None
        assert provider._zones["dyn.example.com"] == 840
        assert provider._api_zone_names["dyn.example.com"] == "example.com"

    def test_the_closest_enclosing_zone_wins_not_the_first(
        self, hetzner: FakeHetznerClient
    ) -> None:
        """A fix, stated as one.

        Both legacy adapters took the first parent they iterated over,
        so an account hosting ``example.com`` *and* ``dyn.example.com``
        bound ``a.dyn.example.com`` to whichever the API listed first —
        a silently wrong zone rather than an error. The frozen case
        (``provider-zone-is-the-closest-enclosing-hosted-zone``) says
        *closest*, and calibrated ``agree`` against a fixture with one
        candidate, where first and closest cannot differ.
        """
        hetzner.zones = [
            {"id": 111, "name": "example.com"},
            {"id": 222, "name": "dyn.example.com"},
        ]
        provider = get_provider(hetzner_account(domains=("a.dyn.example.com",)))
        assert provider is not None
        assert provider._zones["a.dyn.example.com"] == 222
        assert provider._api_zone_names["a.dyn.example.com"] == "dyn.example.com"

    def test_discovery_failure_is_not_fatal(
        self, hetzner: FakeHetznerClient
    ) -> None:
        """A provider outage degrades to ``dnserr`` per hostname, not to
        a construction failure that would take the whole request to
        ``911`` — which would lose the distinction between "your
        provider is down" and "your backend is misconfigured"."""
        hetzner.raise_on.add("GET")
        provider = get_provider(hetzner_account())
        assert provider is not None
        assert set(provider.gethostedzones()) == {"example.com"}

    def test_no_zone_call_without_credentials(
        self, hetzner: FakeHetznerClient
    ) -> None:
        provider = get_provider(hetzner_account(credentials={}))
        assert provider is not None
        assert hetzner.calls == []
        assert set(provider.gethostedzones()) == {"example.com"}

    @pytest.mark.parametrize(
        "hostname,zone,expected",
        [
            ("test.example.com", "example.com", "test"),
            ("a.b.example.com", "example.com", "a.b"),
            ("example.com", "example.com", "@"),
            ("elsewhere.org", "example.com", "elsewhere.org"),
        ],
    )
    def test_relative_names(
        self,
        hostname: str,
        zone: str,
        expected: str,
        hetzner: FakeHetznerClient,
    ) -> None:
        provider = get_provider(hetzner_account(domains=(zone,)))
        assert provider is not None
        assert provider.relative_name(hostname, zone) == expected

    def test_the_relative_name_is_computed_against_the_hosted_zone(
        self, hetzner: FakeHetznerClient, fake_dns: FakeDns
    ) -> None:
        """``windtre.dyn.example.com`` in a row configured for
        ``dyn.example.com`` but hosted at ``example.com`` is
        ``windtre.dyn``, not ``windtre``. Computing against the
        configured domain writes the record one level too shallow — a
        wrong record, not an error."""
        hetzner.zones = [{"id": 840, "name": "example.com"}]
        provider = get_provider(hetzner_account(domains=("dyn.example.com",)))
        assert provider is not None
        result = provider.createrecords(
            V4, {"dyn.example.com": ["windtre.dyn.example.com"]}, rtype="A"
        )
        assert result == {"windtre.dyn.example.com": STATUS_GOOD}
        assert hetzner.bodies("POST")[-1]["name"] == "windtre.dyn"
        assert "/zones/840/rrsets" in hetzner.paths("POST")[-1]


class TestRoute53Zones:
    def test_maps_configured_domains_to_hosted_zone_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeRoute53Client(
            zone_pages=[
                {
                    "HostedZones": [
                        {"Name": "example.com.", "Id": "/hostedzone/Z1"},
                        {"Name": "someone-elses.com.", "Id": "/hostedzone/Z2"},
                        {"Name": "168.192.in-addr.arpa.", "Id": "/hostedzone/Z3"},
                    ]
                }
            ]
        )
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider._zones == {"example.com": "/hostedzone/Z1"}

    def test_zones_the_row_does_not_name_are_not_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fix, stated as one.

        The legacy AWS adapter wrote *every* forward zone in the account
        into ``_zones``, so a Route 53 credential with account-wide
        access made every zone in it a write target. The Hetzner adapter
        had the containment property and this one did not; the frozen
        case's note ("what stops one tenant's provider credential
        reaching another tenant's zone hosted on the same provider
        account") is exactly the hole. Closed.
        """
        client = FakeRoute53Client(
            zone_pages=[
                {
                    "HostedZones": [
                        {"Name": "example.com.", "Id": "/hostedzone/Z1"},
                        {"Name": "someone-elses.com.", "Id": "/hostedzone/Z2"},
                    ]
                }
            ]
        )
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None
        assert "someone-elses.com" not in provider._zones
        assert provider.hostnameperzone(["a.someone-elses.com"]) is False

    def test_the_zone_listing_is_paginated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fix, stated as one.

        ``list_hosted_zones`` returns at most 100 zones and sets
        ``IsTruncated``; the legacy adapter made one call and ignored the
        flag. On a large account a tenant's own zone could simply be
        absent, and the symptom is ``dnserr`` from a perfectly healthy
        credential.
        """
        client = FakeRoute53Client(
            zone_pages=[
                {
                    "HostedZones": [{"Name": "filler.test.", "Id": "/hostedzone/Z0"}],
                    "IsTruncated": True,
                    "NextMarker": "1",
                },
                {"HostedZones": [{"Name": "example.com.", "Id": "/hostedzone/Z1"}]},
            ]
        )
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None
        assert provider._zones == {"example.com": "/hostedzone/Z1"}
        assert len(client.list_zone_calls) == 2

    def test_a_truncated_page_with_no_marker_stops_rather_than_looping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeRoute53Client(
            zone_pages=[
                {
                    "HostedZones": [{"Name": "example.com.", "Id": "/hostedzone/Z1"}],
                    "IsTruncated": True,
                }
            ]
        )
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None
        assert len(client.list_zone_calls) == 1

    def test_the_closest_enclosing_zone_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeRoute53Client(
            zone_pages=[
                {
                    "HostedZones": [
                        {"Name": "example.com.", "Id": "/hostedzone/Z-WIDE"},
                        {"Name": "dyn.example.com.", "Id": "/hostedzone/Z-NARROW"},
                    ]
                }
            ]
        )
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account(domains=("a.dyn.example.com",)))
        assert provider is not None
        assert provider._zones["a.dyn.example.com"] == "/hostedzone/Z-NARROW"

    def test_discovery_failure_is_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeRoute53Client(raise_on=frozenset({"list_hosted_zones"}))
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)
        provider = get_provider(r53_account())
        assert provider is not None
        assert set(provider.gethostedzones()) == {"example.com"}

    def test_no_zone_call_without_credentials(
        self, r53: FakeRoute53Client
    ) -> None:
        provider = get_provider(r53_account(credentials={}))
        assert provider is not None
        assert r53.list_zone_calls == []


class TestNsUpdateZones:
    def test_zones_are_exactly_the_configured_domains(self) -> None:
        provider = get_provider(
            nsupdate_account(domains=("example.com", "example.org"))
        )
        assert provider is not None
        assert set(provider.gethostedzones()) == {"example.com", "example.org"}

    def test_the_zone_name_reaches_the_update_message(
        self, nsupdate: FakeNsUpdate, fake_dns: FakeDns
    ) -> None:
        provider = get_provider(nsupdate_account(domains=("example.org",)))
        assert provider is not None
        provider.createrecords(V4, {"example.org": ["a.example.org"]}, rtype="A")
        assert nsupdate.messages[0].zone[0].name.to_text() == "example.org."


class TestNsUpdateSettings:
    def test_the_legacy_shape_puts_everything_in_the_credential_blob(
        self, nsupdate: FakeNsUpdate, fake_dns: FakeDns
    ) -> None:
        provider = get_provider(nsupdate_account(credentials=NSUPDATE_LEGACY_CREDS))
        assert provider is not None
        assert provider.has_credentials() is True

    def test_the_new_shape_puts_the_non_secrets_in_config(
        self, nsupdate: FakeNsUpdate, fake_dns: FakeDns
    ) -> None:
        provider = get_provider(
            nsupdate_account(credentials=NSUPDATE_CREDS, config=NSUPDATE_CONFIG)
        )
        assert provider is not None
        assert provider.has_credentials() is True
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_GOOD
        }

    def test_a_secret_in_the_plaintext_config_column_is_refused(
        self, nsupdate: FakeNsUpdate
    ) -> None:
        """``config`` is a plain JSON column. Reading a TSIG secret out
        of it would quietly undo the encryption the credential column
        exists for."""
        provider = get_provider(
            nsupdate_account(
                credentials={},
                config={**NSUPDATE_CONFIG, "nsupdate_secret": "would-be-plaintext"},
            )
        )
        assert provider is not None
        assert provider.has_credentials() is False
        assert provider.settings()["nsupdate_secret"] is None

    @pytest.mark.parametrize("missing", list(nsupdate_mod.REQUIRED_SETTINGS))
    def test_any_missing_setting_makes_it_uncredentialled(
        self, missing: str, nsupdate: FakeNsUpdate
    ) -> None:
        credentials = {k: v for k, v in NSUPDATE_LEGACY_CREDS.items() if k != missing}
        provider = get_provider(nsupdate_account(credentials=credentials))
        assert provider is not None
        assert provider.has_credentials() is False
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_DNSERR
        }
        assert nsupdate.messages == []


class TestMissingCredentials:
    @pytest.mark.parametrize(
        "factory,fake_name",
        [
            (r53_account, "r53"),
            (hetzner_account, "hetzner"),
            (nsupdate_account, "nsupdate"),
        ],
    )
    def test_create_and_delete_are_dnserr_rather_than_raising(
        self,
        factory: Any,
        fake_name: str,
        request: pytest.FixtureRequest,
        fake_dns: FakeDns,
    ) -> None:
        request.getfixturevalue(fake_name)
        provider = get_provider(factory(credentials={}, config={}))
        assert provider is not None
        assert provider.createrecords(V4, ZONES, rtype="A") == {
            "test.example.com": STATUS_DNSERR
        }
        assert provider.deleterecords(ZONES) == {
            "test.example.com": STATUS_DNSERR
        }

    @pytest.mark.parametrize(
        "missing", ["aws_access_key_id", "aws_secret_access_key"]
    )
    def test_a_partial_route53_credential_is_still_missing(
        self, missing: str, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        credentials = {k: v for k, v in R53_CREDS.items() if k != missing}
        provider = get_provider(r53_account(credentials=credentials))
        assert provider is not None
        assert provider.has_credentials() is False

    @pytest.mark.parametrize("blank", ["", None])
    def test_a_blank_credential_is_missing_not_present(
        self, blank: Any, hetzner: FakeHetznerClient, fake_dns: FakeDns
    ) -> None:
        provider = get_provider(
            hetzner_account(credentials={"hetzner_api_token": blank})
        )
        assert provider is not None
        assert provider.has_credentials() is False

    def test_a_null_credential_column_is_a_state_not_an_error(self) -> None:
        """``credentials: absent`` is a real fixture state — the frozen
        table's ``nocreds.example.com`` contributes ``911`` — so ``None``
        must construct rather than raise."""
        account = ProviderAccount(service="hetzner", credentials=None, config=None)
        assert account.credentials == {}
        assert account.config == {}


# --------------------------------------------------------------------------
# Multi-hostname and multi-zone shapes
# --------------------------------------------------------------------------


class TestBatching:
    def test_route53_batches_one_change_per_hostname_per_zone(
        self, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        provider = get_provider(r53_account())
        assert provider is not None
        result = provider.createrecords(
            V4,
            {"example.com": ["a.example.com", "b.example.com"]},
            rtype="A",
        )
        assert result == {
            "a.example.com": STATUS_GOOD,
            "b.example.com": STATUS_GOOD,
        }
        assert len(r53.change_calls) == 1
        assert r53.record_types == ["A", "A"]

    def test_route53_skips_the_batch_entirely_when_everything_is_nochg(
        self, r53: FakeRoute53Client, fake_dns: FakeDns
    ) -> None:
        fake_dns.answers(V4)
        provider = get_provider(r53_account())
        assert provider is not None
        result = provider.createrecords(
            V4,
            {"example.com": ["a.example.com", "b.example.com"]},
            rtype="A",
        )
        assert result == {
            "a.example.com": STATUS_NOCHG,
            "b.example.com": STATUS_NOCHG,
        }
        assert r53.change_calls == []

    def test_route53_a_failed_batch_marks_only_the_changed_hostnames(
        self, monkeypatch: pytest.MonkeyPatch, fake_dns: FakeDns
    ) -> None:
        """A hostname that answered ``nochg`` never entered the batch,
        so a batch failure must not retroactively turn it into an
        error."""
        client = FakeRoute53Client(raise_on=frozenset({"change_resource_record_sets"}))
        monkeypatch.setattr(route53_mod, "make_client", lambda credentials: client)

        calls: list[str] = []

        def selective(self: Any, hostname: str, ip: str, rrtype: str = "A", nameserver: Any = None) -> bool:
            calls.append(hostname)
            return hostname == "a.example.com"

        monkeypatch.setattr(Route53Provider, "check_hostnameon_server", selective)
        provider = get_provider(r53_account())
        assert provider is not None
        result = provider.createrecords(
            V4,
            {"example.com": ["a.example.com", "b.example.com"]},
            rtype="A",
        )
        assert result == {
            "a.example.com": STATUS_NOCHG,
            "b.example.com": STATUS_DNSERR,
        }

    def test_hetzner_writes_one_request_per_hostname(
        self, hetzner: FakeHetznerClient, fake_dns: FakeDns
    ) -> None:
        provider = get_provider(hetzner_account())
        assert provider is not None
        result = provider.createrecords(
            V4,
            {"example.com": ["a.example.com", "b.example.com"]},
            rtype="A",
        )
        assert result == {
            "a.example.com": STATUS_GOOD,
            "b.example.com": STATUS_GOOD,
        }
        assert [body["name"] for body in hetzner.bodies("POST")] == ["a", "b"]


# --------------------------------------------------------------------------
# The boundary functions themselves
# --------------------------------------------------------------------------


class TestBoundaryFunctions:
    def test_the_route53_client_is_built_from_the_stored_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``make_client`` is the one place this package touches boto3.

        Asserted by intercepting ``boto3.client`` rather than by calling
        it, because calling it would resolve credentials off the
        machine — which is the exact class of accident this issue
        removed.
        """
        captured: dict[str, Any] = {}

        def fake_boto_client(service: str, **kwargs: Any) -> str:
            captured["service"] = service
            captured.update(kwargs)
            return "client"

        monkeypatch.setattr(route53_mod.boto3, "client", fake_boto_client)
        assert route53_mod.make_client(R53_CREDS) == "client"
        assert captured["service"] == "route53"
        assert captured["aws_access_key_id"] == R53_CREDS["aws_access_key_id"]
        assert captured["aws_secret_access_key"] == R53_CREDS["aws_secret_access_key"]
        assert captured["config"] is route53_mod.BOTO_CONFIG

    def test_the_route53_config_bounds_the_request_path(self) -> None:
        """botocore's defaults are 60s connect and 5 attempts, which is
        minutes of a held ``/nic/update``."""
        config = route53_mod.BOTO_CONFIG
        assert config.connect_timeout == 5
        assert config.read_timeout == 10
        assert config.retries == {"max_attempts": 1}

    def test_the_hetzner_client_carries_the_bearer_token_and_a_timeout(
        self,
    ) -> None:
        client = hetzner_mod.make_client(HETZNER_CREDS)
        try:
            assert (
                client.headers["Authorization"]
                == f"Bearer {HETZNER_CREDS['hetzner_api_token']}"
            )
            assert client.timeout.connect == hetzner_mod.HTTP_TIMEOUT
        finally:
            client.close()

    def test_the_hetzner_api_base_is_the_cloud_api(self) -> None:
        assert hetzner_mod.API_BASE == "https://api.hetzner.cloud/v1"

    def test_boto3_is_importable_here(self) -> None:
        """The atrium base image ships neither boto3 nor requests.

        This asserts the dependency declared in ``backend/pyproject.toml``
        actually reached the environment the app runs in — the "local
        green, dead on the deployed host" shape, caught at the one place
        that can see it.
        """
        assert route53_mod.boto3.__name__ == "boto3"

    def test_the_hetzner_adapter_uses_httpx_not_requests(self) -> None:
        """Stated as an assertion because the choice was a measurement.

        ``ghcr.io/brendanbank/atrium:0.28`` ships httpx and does not ship
        ``requests``; porting the import was cheaper and truer than
        adding a second HTTP client to the image.
        """
        assert hetzner_mod.httpx.__name__ == "httpx"
        assert not hasattr(hetzner_mod, "requests")


# --------------------------------------------------------------------------
# Shape of the base class itself
# --------------------------------------------------------------------------


class TestBaseProviderContract:
    def test_the_default_operations_are_dnserr_not_silence(self) -> None:
        """A provider that forgets to implement one must be visibly
        broken, not quietly successful."""
        provider = BaseProvider(ProviderAccount(service="x", domains=("example.com",)))
        assert provider.createrecords(V4, ZONES) == {
            "test.example.com": STATUS_DNSERR
        }
        assert provider.deleterecords(ZONES) == {"test.example.com": STATUS_DNSERR}

    @pytest.mark.parametrize(
        "rtype,expected",
        [("A", ("A",)), ("AAAA", ("AAAA",)), (None, ("A", "AAAA"))],
    )
    def test_rtypes_for(self, rtype: str | None, expected: tuple[str, ...]) -> None:
        assert BaseProvider.rtypes_for(rtype) == expected

    def test_service_names_puts_the_canonical_name_first(self) -> None:
        assert Route53Provider.service_names() == ("route53", "aws")
        assert Route53Provider.known_services() == ("route53",)

    def test_constructing_an_adapter_opens_no_socket(
        self, r53: FakeRoute53Client, hetzner: FakeHetznerClient, nsupdate: FakeNsUpdate
    ) -> None:
        """The autouse guard would catch it, but say so explicitly: the
        legacy base class built a system resolver in ``__init__``, which
        is a filesystem read per backend row per request."""
        for factory in (r53_account, hetzner_account, nsupdate_account):
            assert get_provider(factory()) is not None

    def test_the_resolver_is_built_lazily_and_once(
        self, fake_dns: FakeDns
    ) -> None:
        provider = BaseProvider(ProviderAccount(service="x"))
        assert provider._default_resolver is None
        first = provider._resolver()
        assert provider._resolver() is first
