"""``NsUpdateProvider`` against a real socket that verifies its signature.

Everything here runs over loopback TCP and UDP against
:class:`tsig_receiver.TsigReceiver`. Nothing is monkeypatched at the
dnspython boundary — the adapter opens the same sockets it opens in
production, and the bytes it puts on them are parsed and authenticated
by something that did not author them.

What this file is **not** for
-----------------------------
Message contents. ``test_providers.py`` owns those, deliberately: #29
showed that asserting an adapter's *return value* cannot tell an ``A``
from an ``AAAA`` — making ``getiptype`` answer ``A`` for every address
ran the whole frozen wire table at 121 executed, 0 failed — so record
type is proved by reading the ``dns.update.UpdateMessage`` the adapter
builds. Re-proving that here would add a second author to the same
assertion and no second instrument.

The three things below the mock boundary are what is left, and they are
what this file is:

1. **the signature** — a keyring built from the wrong secret, or naming
   a key the server does not hold, is indistinguishable from a correct
   one to a double that records whatever it is handed;
2. **the refusals** — ``REFUSED``, ``SERVFAIL``, a dropped connection,
   in the well-formed signed responses a nameserver actually sends;
3. **the timeout** — ``QUERY_TIMEOUT`` is passed to ``dns.query.tcp``
   and nothing has ever held a socket open long enough to reach it.

The wire table is not an instrument here, and that is a correction
-------------------------------------------------------------------
#131's acceptance criterion said *"the nsupdate cases of the wire table
run against it"*. There are none. ``tests/compat/protocol_cases.yaml``
declares seven fixture backends and every one is ``service: stub`` or
``service: no-such-service``; the only occurrence of the string
``nsupdate`` in the whole table is the ``updatetype=nsupdate`` **query
parameter** in ``update-updatetype-is-accepted-and-ignored``, which the
service ignores. The table's ``effects.dns`` entries are scripted
results on ``atrium_ddns.compat_stub``, which opens no socket at all.

So a receiver pointed at a wire-table run is a receiver **nothing
connects to** — a service that cannot go red, which is precisely the
probe-that-cannot-fail this issue was most exposed to.
:func:`test_the_wire_table_cannot_reach_this_adapter` asserts that
emptiness from the table's own data rather than leaving it as prose, so
if an nsupdate case is ever added the claim fails instead of quietly
becoming false.

Why every test here fails rather than skips
--------------------------------------------
The receiver binds port 53, because ``nsupdate.py`` sends to 53 and has
no port argument (see ``tsig_receiver``'s module docstring for why that
is not being changed). If the bind stops working — a base image that
raises ``net.ipv4.ip_unprivileged_port_start``, a daemon default that
moves — the honest outcome is a red suite naming the reason. A skip
would leave the socket, the signature and the refusals silently
uncovered while the suite went on reporting green, which is the same
shape as a stale image reporting the baseline for a file you just
edited.
"""
from __future__ import annotations

import ast
import pathlib
import time
from typing import Iterator

import dns.flags
import dns.message
import dns.opcode
import dns.query
import dns.rcode
import dns.rdatatype
import dns.tsig
import dns.update
import pytest

from atrium_ddns.providers import (
    STATUS_DNSERR,
    STATUS_GOOD,
    STATUS_NOCHG,
    ProviderAccount,
    get_provider,
)
from atrium_ddns.providers import nsupdate as nsupdate_mod

from tsig_receiver import HANG_SECONDS, TsigReceiver, can_bind

#: The key the receiver holds. 32 bytes of base64 —
#: ``dns.tsigkeyring.from_text`` really parses it and
#: ``dns.tsig.validate`` really computes an HMAC over it.
KEY_NAME = "ddns-key"
KEY_SECRET = "c2VjcmV0LXRzaWcta2V5LW1hdGVyaWFsLWhlcmUh"
#: Same length, same alphabet, different bytes. The whole point is that
#: nothing short of computing the MAC can tell the two apart.
WRONG_SECRET = "b3RoZXItdHNpZy1rZXktbWF0ZXJpYWwtaGVyZS4h"

ZONE = "example.com"
HOSTNAME = "test.example.com"
ZONES = {ZONE: [HOSTNAME]}
V4 = "203.0.113.10"
V6 = "2001:db8::1"

#: ``127.0.0.1``, not ``localhost``: the adapter hands the string
#: straight to dnspython as a nameserver address, and a name would put a
#: resolver lookup in front of the thing being measured.
LOOPBACK = "127.0.0.1"

NSUPDATE_SOURCE = pathlib.Path(nsupdate_mod.__file__)


def _find_protocol_cases() -> pathlib.Path | None:
    """The frozen table, wherever this suite is being run from.

    Same two candidates as ``test_providers._find_protocol_cases``, and
    duplicated rather than imported: importing it would drag that
    module's autouse no-network fixture into this session, and this file
    is the one place in the suite that is *supposed* to open a socket.
    """
    for candidate in (
        # Inside the api container, where `make test-backend` runs.
        pathlib.Path("/opt/compat_tests/compat/protocol_cases.yaml"),
        # A plain checkout: backend/tests/ -> repo root -> tests/compat.
        pathlib.Path(__file__).resolve().parents[2]
        / "tests"
        / "compat"
        / "protocol_cases.yaml",
    ):
        if candidate.is_file():
            return candidate
    return None


def account(
    secret: str = KEY_SECRET,
    keyname: str = KEY_NAME,
    nameserver: str = LOOPBACK,
) -> ProviderAccount:
    """A backend row in the legacy shape — all four in `credentials`."""
    return ProviderAccount(
        service="nsupdate",
        domains=(ZONE,),
        credentials={
            "nsupdate_secret": secret,
            "nsupdate_key": keyname,
            "nsupdate_algo": "hmac-sha256",
            "nsupdate_nameserver": nameserver,
        },
    )


@pytest.fixture
def receiver() -> Iterator[TsigReceiver]:
    """A validating receiver on 53, torn down whatever the test did.

    Function-scoped rather than module-scoped so a test that leaves the
    receiver mid-``hang`` cannot bleed into the next one. Port 53 is
    reclaimed each time; ``SO_REUSEADDR`` covers the ``TIME_WAIT`` that
    would otherwise make the second test in the file flaky.
    """
    ok, why = can_bind(LOOPBACK, 53)
    assert ok, (
        f"cannot bind {LOOPBACK}:53 in this container ({why}). The whole "
        "point of binding 53 is that nsupdate.py sends there and takes no "
        "port argument; if the low ports have closed, this suite has "
        "stopped exercising the socket and must say so rather than skip."
    )
    with TsigReceiver(KEY_NAME, KEY_SECRET, host=LOOPBACK, port=53) as rx:
        yield rx


# --------------------------------------------------------------------------
# The premises this file rests on, asserted rather than assumed
# --------------------------------------------------------------------------


@pytest.mark.harness_guard
def test_the_container_can_bind_the_port_the_adapter_talks_to() -> None:
    """Binding 53 works because of a sysctl, not because of root.

    The image's ``dev`` stage ends on ``USER app``; ``id`` inside the
    api container reads ``uid=1000(app)``. Nothing here is privileged.
    The bind succeeds because the container's
    ``net.ipv4.ip_unprivileged_port_start`` is ``0``, which is the
    docker default and not something this repository pins.

    So it is asserted, with the reason named, and it fails rather than
    skips. Both readings are taken — the sysctl *and* an actual bind —
    because the sysctl is what explains it and the bind is what the
    tests need.
    """
    start = pathlib.Path("/proc/sys/net/ipv4/ip_unprivileged_port_start")
    ok, why = can_bind(LOOPBACK, 53)
    assert ok, (
        f"cannot bind {LOOPBACK}:53 ({why}); "
        f"ip_unprivileged_port_start reads "
        f"{start.read_text().strip() if start.exists() else 'unreadable'}"
    )
    assert start.exists(), "not a Linux container — this suite runs in the api image"
    assert int(start.read_text().strip()) <= 53, (
        "ip_unprivileged_port_start has moved above 53. The bind above "
        "still worked, so one of the two readings is wrong — investigate "
        "before trusting either."
    )


def test_every_update_goes_to_port_53_with_no_port_argument() -> None:
    """Derived from the source, so a port setting arrives as a red test.

    Read with ``ast`` rather than by grep or from memory: this is the
    fact that makes binding 53 the right choice, and #131 turned on it.
    If ``nsupdate_nameserver`` ever grows a port — defensible on its own
    merits, hidden primaries do listen high — this fails and names the
    call site, so the receiver is fixed in the same change rather than
    silently testing a port nothing sends to.

    ``timeout=QUERY_TIMEOUT`` is asserted at the same time and for the
    same reason: ``test_a_hung_receiver_times_out_on_query_timeout``
    drives a *reduced* timeout, and that is only evidence about the
    shipped constant if the shipped constant is what the call sites
    pass.
    """
    tree = ast.parse(NSUPDATE_SOURCE.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "tcp"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "query"
    ]
    assert len(calls) == 2, (
        f"expected the two dns.query.tcp call sites this file is about, "
        f"found {len(calls)} in {NSUPDATE_SOURCE.name} — if a third was "
        "added, decide what port and timeout it uses before this passes"
    )
    for call in calls:
        keywords = {kw.arg for kw in call.keywords}
        assert "port" not in keywords, (
            f"{NSUPDATE_SOURCE.name}:{call.lineno} now passes a port. The "
            "receiver in tsig_receiver.py binds 53 because this call did "
            "not; give it the same port or the test is talking to nothing."
        )
        timeout = next(kw for kw in call.keywords if kw.arg == "timeout")
        assert isinstance(timeout.value, ast.Name), (
            f"{NSUPDATE_SOURCE.name}:{call.lineno} passes a literal timeout"
        )
        assert timeout.value.id == "QUERY_TIMEOUT"


@pytest.mark.harness_guard
def test_the_wire_table_cannot_reach_this_adapter() -> None:
    """#131's wire-table clause is unsatisfiable, from the table's data.

    The correction, asserted rather than written down: every backend the
    frozen table declares is a scripted stub, so no case in it opens a
    socket to anything, let alone a nameserver. Pointing a TSIG receiver
    at a wire-table run gives a receiver nothing connects to.

    A vacuity guard first — a YAML file that failed to load, or a
    ``fixture`` block that moved, would otherwise make this pass by
    finding nothing.
    """
    yaml = pytest.importorskip("yaml")
    path = _find_protocol_cases()
    assert path is not None, (
        "protocol_cases.yaml is not reachable from this run, so the "
        "correction this test records cannot be checked. It is asserted "
        "rather than skipped: an unreachable table is exactly the state "
        "in which a claim about the table goes unverified."
    )
    table = yaml.safe_load(path.read_text(encoding="utf-8"))
    backends = table["fixture"]["backends"]
    assert len(backends) >= 5, f"only {len(backends)} backends — the scan is vacuous"
    services = {backend["service"] for backend in backends}
    assert "nsupdate" not in services, (
        "the frozen table now declares an nsupdate backend, so #131's "
        f"wire-table clause is live after all: {sorted(services)}"
    )
    assert services <= {"stub", "no-such-service"}, sorted(services)


# --------------------------------------------------------------------------
# The signature
# --------------------------------------------------------------------------


class TestTheSignatureIsVerified:
    def test_a_correctly_signed_update_is_accepted(
        self, receiver: TsigReceiver
    ) -> None:
        """The positive control, and it carries the adapter's own bytes.

        Without this the refusal tests below would all pass against a
        receiver that refused everything, which is the mirror image of
        the defect they exist to exclude.
        """
        provider = get_provider(account())
        results = provider.createrecords(V4, ZONES)

        assert results == {HOSTNAME: STATUS_GOOD}
        assert receiver.rejections == []
        assert len(receiver.updates) == 1

        # Read back what actually crossed the socket: the zone, the name
        # and the rdatatype. Not to re-prove record type — that is
        # test_providers.py's — but to prove these bytes came from the
        # adapter rather than from the receiver's imagination.
        update = receiver.updates[0]
        assert update.zone[0].name.to_text() == f"{ZONE}."
        rrset = update.update[0]
        assert rrset.name.to_text() == f"{HOSTNAME}."
        assert rrset.rdtype == dns.rdatatype.A

    def test_a_wrong_secret_is_refused_and_surfaces_as_dnserr(
        self, receiver: TsigReceiver
    ) -> None:
        """The deliberately wrong key, and the verdict it produces.

        The adapter signs with ``WRONG_SECRET``; the receiver holds
        ``KEY_SECRET``. The names and the algorithm match, so nothing
        short of computing the MAC can tell them apart — which is the
        whole reason this cannot be done at a mock boundary.

        The receiver answers ``NOTAUTH`` with a TSIG carrying
        ``BADSIG``; dnspython's client raises ``PeerBadSignature``; the
        adapter's ``except Exception`` turns that into ``dnserr``, which
        is the wire verdict for a provider that failed.
        """
        provider = get_provider(account(secret=WRONG_SECRET))
        results = provider.createrecords(V4, ZONES)

        assert results == {HOSTNAME: STATUS_DNSERR}
        assert [name for name, _ in receiver.rejections] == ["BadSignature"]
        assert receiver.updates == [], (
            "the receiver recorded the badly-signed message as accepted"
        )

    def test_an_unknown_key_name_is_refused_and_surfaces_as_dnserr(
        self, receiver: TsigReceiver
    ) -> None:
        """The other half of a wrong key: a name the server never heard of.

        Distinct from a bad MAC on the wire (``BADKEY`` rather than
        ``BADSIG``) and distinct in dnspython (``PeerBadKey`` rather
        than ``PeerBadSignature``), so it gets its own test rather than
        being folded into "the key is wrong".
        """
        provider = get_provider(account(keyname="not-the-servers-key"))
        results = provider.createrecords(V4, ZONES)

        assert results == {HOSTNAME: STATUS_DNSERR}
        assert [name for name, _ in receiver.rejections] == ["UnknownTSIGKey"]
        assert receiver.updates == []

    @pytest.mark.harness_guard
    def test_the_receiver_is_not_a_receiver_that_accepts_everything(
        self, receiver: TsigReceiver
    ) -> None:
        """Both directions, in one test, on one receiver.

        A receiver that answers ``NOERROR`` regardless makes every test
        above pass and proves nothing; a receiver that refuses
        regardless makes the refusal tests pass and proves nothing
        either. Neither is visible from a single-sided assertion, so
        both are driven here against the *same* instance and the
        accounting is asserted as a whole.

        **Both the receiver's own record and the verdict the client got**,
        because the first version of this test asserted only the record
        and a mutation walked straight through it. Made to validate,
        record the rejection, and then answer ``NOERROR`` anyway — which
        is how anyone writes a validator wrong the first time —
        ``rejections`` and ``updates`` both read exactly as they do now
        and this test passed while three others failed. That is an
        assertion about an instrument's report rather than about the
        thing being reported, written by someone who had just finished
        reading the section warning about it.
        """
        good = get_provider(account()).createrecords(V4, ZONES)
        accepted_after_good = len(receiver.updates)
        rejected_after_good = len(receiver.rejections)

        bad = get_provider(account(secret=WRONG_SECRET)).createrecords(V4, ZONES)

        assert (accepted_after_good, rejected_after_good) == (1, 0), (
            "a correctly signed update was not accepted — the receiver "
            "refuses everything and the refusal tests above are vacuous"
        )
        assert good == {HOSTNAME: STATUS_GOOD}, good
        assert (len(receiver.updates), len(receiver.rejections)) == (1, 1), (
            "a badly signed update was not refused — the receiver accepts "
            f"everything. updates={len(receiver.updates)} "
            f"rejections={receiver.rejections}"
        )
        assert bad == {HOSTNAME: STATUS_DNSERR}, (
            "the receiver recorded a rejection and then answered the client "
            f"as though nothing were wrong: {bad}. A refusal nobody is told "
            "about is not a refusal."
        )

    def test_an_unsigned_update_is_refused(self, receiver: TsigReceiver) -> None:
        """A missing signature, not a wrong one — a separate hole.

        Found by mutation, not by reading. ``dns.message.from_wire``
        only runs ``dns.tsig.validate`` when there *is* a TSIG record to
        validate, so a message carrying none parses cleanly however good
        the keyring is. With ``NsUpdateProvider._message`` made to build
        its ``dns.update.Update`` without a keyring — the shape a
        refactor that loses the signing takes — the receiver accepted
        every unsigned update and answered ``NOERROR``, and the only
        tests that went red were the ones expecting a *wrong* key to be
        refused. An adapter that had stopped signing altogether reported
        ``good``.

        Sent by hand rather than through the adapter, because the
        adapter cannot be asked to send an unsigned update without being
        broken first; that is what the mutation is for and this is what
        keeps it caught.
        """
        unsigned = dns.update.Update(f"{ZONE}.")
        unsigned.replace(f"{HOSTNAME}.", 300, "A", V4)

        # ``dns.message.UnknownTSIGKey`` rather than
        # ``dns.tsig.PeerBadKey``: an unsigned *request* carries no
        # keyring, so the client cannot even read the signed refusal it
        # gets back. The client-side wording differs from the wrong-key
        # case and the receiver's own record does not — which is why
        # both are asserted.
        with pytest.raises(dns.message.UnknownTSIGKey):
            dns.query.tcp(unsigned, LOOPBACK, timeout=5.0)

        assert [name for name, _ in receiver.rejections] == ["UnknownTSIGKey"]
        assert receiver.rejections[0][1] == "unsigned message: no TSIG record"
        assert receiver.updates == []

    @pytest.mark.harness_guard
    def test_an_unsigned_refusal_would_be_accepted_by_the_client(
        self, receiver: TsigReceiver
    ) -> None:
        """Why the receiver's refusal carries a TSIG error code.

        Measured rather than assumed, because the obvious
        implementation — answer a bare ``NOTAUTH`` and let the client
        notice — does not work: dnspython does not require a signed
        request to get a signed response, so an unsigned refusal comes
        back as an ordinary message with no exception raised. Combined
        with ``TestTheRcodeIsNotRead`` below, that would have made a
        rejected update report ``good``.

        This sends the raw message itself rather than going through the
        adapter: it is a statement about dnspython's client, not about
        ``nsupdate.py``.
        """
        signed = dns.update.Update(
            f"{ZONE}.",
            keyring=receiver.keyring,
            keyalgorithm="hmac-sha256",
        )
        signed.replace(f"{HOSTNAME}.", 300, "A", V4)
        signed.to_wire()  # signs it, and populates `mac`
        assert signed.mac, "the request was not signed, so this proves nothing"

        # An unsigned NOTAUTH — built by hand, because it is exactly
        # what `TsigReceiver._tsig_error_response` deliberately does not
        # send and there is no dnspython helper that produces one.
        bare = dns.message.Message(id=signed.id)
        bare.flags = dns.flags.QR
        bare.set_opcode(dns.opcode.UPDATE)
        bare.set_rcode(dns.rcode.NOTAUTH)
        wire = bare.to_wire()

        # Parsed the way `dns.query.tcp` parses a response to a signed
        # request: the client's keyring and the request's MAC. No
        # exception, which is the finding — an unsigned refusal is
        # accepted, so a receiver that answered one would be reporting
        # a rejected update through a path the adapter treats as normal.
        parsed = dns.message.from_wire(
            wire, keyring=receiver.keyring, request_mac=signed.mac
        )
        assert parsed.rcode() == dns.rcode.NOTAUTH
        assert not parsed.had_tsig, (
            "this test is about an UNsigned refusal and built a signed one"
        )


# --------------------------------------------------------------------------
# The refusals a mock cannot reach
# --------------------------------------------------------------------------


class TestTheRefusalPaths:
    def test_a_dropped_connection_surfaces_as_dnserr(
        self, receiver: TsigReceiver
    ) -> None:
        """The nameserver authenticates, then closes without answering.

        dnspython raises ``EOFError`` reading the length prefix. Not a
        timeout and not a refusal — a third shape, and one no double has
        ever produced here.
        """
        receiver.behaviour = "drop"
        results = get_provider(account()).createrecords(V4, ZONES)

        assert results == {HOSTNAME: STATUS_DNSERR}
        assert len(receiver.updates) == 1, (
            "the connection was dropped before the message was even read, "
            "so this proves nothing about the response path"
        )

    def test_a_hung_receiver_times_out_on_query_timeout(
        self, receiver: TsigReceiver, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``QUERY_TIMEOUT`` governs, measured against the clock.

        The shipped value is 10 seconds and this suite is not going to
        spend 10 seconds proving it, so the constant is reduced and the
        *elapsed time* is asserted against the reduced value. That is
        what makes this evidence about the timeout rather than about
        "an exception happened eventually": a call that ignored the
        timeout would sit here for ``HANG_SECONDS``, comfortably outside
        the band.

        The second instrument is
        :func:`test_every_update_goes_to_port_53_with_no_port_argument`,
        which reads the source and asserts both call sites pass this
        exact name — so the constant reduced here is the constant that
        ships.
        """
        reduced = 1.0
        assert reduced < HANG_SECONDS, "the receiver would answer first"
        monkeypatch.setattr(nsupdate_mod, "QUERY_TIMEOUT", reduced)
        receiver.behaviour = "hang"

        started = time.monotonic()
        results = get_provider(account()).createrecords(V4, ZONES)
        elapsed = time.monotonic() - started

        assert results == {HOSTNAME: STATUS_DNSERR}
        assert reduced * 0.8 <= elapsed < HANG_SECONDS, (
            f"took {elapsed:.2f}s against a {reduced}s timeout and a "
            f"{HANG_SECONDS}s hang — the timeout is not what ended the call"
        )
        assert nsupdate_mod.QUERY_TIMEOUT == reduced

    def test_the_shipped_timeout_is_ten_seconds(self) -> None:
        """The value the test above reduces, asserted where it is read.

        Legacy passes ``timeout=10`` literally
        (``lib/account/nsupdate.py``); this module names it. A change to
        either number is a change to how long ``/nic/update`` blocks a
        router thread, so it does not get to happen silently.
        """
        assert nsupdate_mod.QUERY_TIMEOUT == 10.0


class TestTheRcodeIsNotRead:
    """**A refused UPDATE is reported to the tenant as ``good``.**

    Found by this file, on a real socket, and it is a defect rather than
    a quirk: the client is told the update succeeded, so it does not
    retry, and the zone stays stale for as long as it keeps sending the
    same address. ``base.py``'s own comment on ``check_hostnameon_server``
    names that direction as the unsafe one.

    The mechanism is that ``dns.query.tcp`` returns the response and
    neither ``createrecords`` nor ``deleterecords`` looks at
    ``response.rcode()``. Nothing raises: a ``REFUSED`` from BIND is a
    well-formed, correctly TSIG-signed message, and dnspython hands it
    back exactly as it hands back a ``NOERROR``.

    **Not fixed here, deliberately.** The legacy adapter has the same
    behaviour — ``lib/account/nsupdate.py:61`` binds ``response`` and
    never reads it — so this is a faithfully ported divergence and not a
    rewrite regression, and correcting it changes what a live tenant
    sees on ``/nic/update``. The frozen table cannot adjudicate it (see
    :func:`test_the_wire_table_cannot_reach_this_adapter`), so there is
    no compat answer to appeal to. That makes it a product decision for
    the milestone owner, raised as #142.

    These tests therefore pin the behaviour that exists, named so nobody
    reads them as approval. When the fix lands they go red, which is the
    point: the change is then a visible edit to an assertion that says
    what it is, not a silent drift.
    """

    @pytest.mark.parametrize("behaviour", ["refused", "servfail"])
    def test_a_refused_update_is_reported_as_good_which_is_wrong(
        self, receiver: TsigReceiver, behaviour: str
    ) -> None:
        receiver.behaviour = behaviour
        results = get_provider(account()).createrecords(V4, ZONES)

        assert results == {HOSTNAME: STATUS_GOOD}, (
            "if this is now dnserr, the rcode is being read and this test "
            "should be deleted along with the issue it was pinned for"
        )
        assert len(receiver.updates) == 1

    def test_the_receiver_really_did_refuse(
        self, receiver: TsigReceiver
    ) -> None:
        """Vacuity for the pair above, from the client's own reading.

        The adapter discards the response, so the test above cannot show
        what the rcode was. This sends the same message by hand and
        reads it, which is the only way to tell "the adapter ignored a
        REFUSED" from "the receiver never sent one".
        """
        receiver.behaviour = "refused"
        update = dns.update.Update(
            f"{ZONE}.", keyring=receiver.keyring, keyalgorithm="hmac-sha256"
        )
        update.replace(f"{HOSTNAME}.", 300, "A", V4)

        response = dns.query.tcp(update, LOOPBACK, timeout=5.0)

        assert response.rcode() == dns.rcode.REFUSED
        assert receiver.rejections == []


# --------------------------------------------------------------------------
# The other two operations, over the same socket
# --------------------------------------------------------------------------


class TestTheOtherOperations:
    def test_a_matching_address_is_nochg_and_no_update_is_sent(
        self, receiver: TsigReceiver
    ) -> None:
        """``nochg`` over a real resolver query, not a faked one.

        ``createrecords`` asks the configured nameserver over **UDP**
        before it writes. Here the receiver answers with the address the
        client already claims, so the adapter must return ``nochg`` and
        open no TCP connection at all — the "no write reaches the
        provider" path that makes a five-minute client cheap.
        """
        receiver.query_answer = V4
        results = get_provider(account()).createrecords(V4, ZONES)

        assert results == {HOSTNAME: STATUS_NOCHG}
        assert receiver.updates == [], "a nochg still sent an UPDATE"

    def test_a_different_address_writes_after_the_resolver_disagrees(
        self, receiver: TsigReceiver
    ) -> None:
        """The other side of the same branch, so neither is vacuous."""
        receiver.query_answer = "203.0.113.99"
        results = get_provider(account()).createrecords(V4, ZONES)

        assert results == {HOSTNAME: STATUS_GOOD}
        assert len(receiver.updates) == 1

    def test_deleterecords_is_signed_verified_and_covers_both_families(
        self, receiver: TsigReceiver
    ) -> None:
        """The second ``dns.query.tcp`` call site, over the real socket.

        ``deleterecords`` sends no resolver query — it cannot answer
        ``nochg``, which is a recorded divergence — so this is a
        straight signed write, and both families ride in one message
        because RFC 2136 applies an update atomically.
        """
        results = get_provider(account()).deleterecords(ZONES)

        assert results == {HOSTNAME: STATUS_GOOD}
        assert receiver.rejections == []
        assert len(receiver.updates) == 1
        rdtypes = {rrset.rdtype for rrset in receiver.updates[0].update}
        assert rdtypes == {dns.rdatatype.A, dns.rdatatype.AAAA}

    def test_a_wrong_secret_on_delete_is_refused_and_surfaces_as_dnserr(
        self, receiver: TsigReceiver
    ) -> None:
        """Both call sites sign, so both are shown being refused."""
        results = get_provider(account(secret=WRONG_SECRET)).deleterecords(ZONES)

        assert results == {HOSTNAME: STATUS_DNSERR}
        assert [name for name, _ in receiver.rejections] == ["BadSignature"]
        assert receiver.updates == []

    def test_an_ipv6_update_is_signed_and_verified_too(
        self, receiver: TsigReceiver
    ) -> None:
        """AAAA over the socket — the rdatatype read off the wire.

        Record-type correctness is ``test_providers.py``'s subject and
        is not being re-proved here. What is new is that the AAAA
        message is one a validating server accepts: a v6 update takes
        the same signing path and is not, for instance, mis-encoded in a
        way only a real parser would notice.
        """
        results = get_provider(account()).createrecords(V6, ZONES, rtype="AAAA")

        assert results == {HOSTNAME: STATUS_GOOD}
        assert receiver.rejections == []
        assert receiver.updates[0].update[0].rdtype == dns.rdatatype.AAAA
