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
2. **the refusals** — every non-zero header rcode, plus a dropped
   connection, in the well-formed signed responses a nameserver actually
   sends. This is where #142 was found and where its fix is evidenced:
   see :class:`TestTheRcodeIsReadNow`, formerly ``TestTheRcodeIsNotRead``;
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
import structlog
import structlog.testing

from atrium_ddns.providers import (
    STATUS_DNSERR,
    STATUS_GOOD,
    STATUS_NOCHG,
    ProviderAccount,
    get_provider,
)
from atrium_ddns.providers import nsupdate as nsupdate_mod

from tsig_receiver import (
    HANG_SECONDS,
    RCODE_BEHAVIOURS,
    REFUSAL_BEHAVIOURS,
    TsigReceiver,
    can_bind,
)

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
        back as an ordinary message with no exception raised. Before
        #142 that was compounded — the adapter did not read the rcode
        either — and a rejected update reported ``good``. The rcode is
        read now (:class:`TestTheRcodeIsReadNow`), but this half still
        has to hold on its own: a refusal the client accepts silently is
        a refusal whose rcode never reaches the code that reads it.

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


class TestTheRcodeIsReadNow:
    """**A refused UPDATE reaches the tenant as ``dnserr``.** #142.

    Inverted, not deleted. This class was ``TestTheRcodeIsNotRead`` and
    it pinned the opposite answer: #131 found, on this socket, that
    ``dns.query.tcp`` returns the response and neither ``createrecords``
    nor ``deleterecords`` looked at ``response.rcode()``, so every
    non-zero rcode reached the tenant as ``good``. The client is then
    told the update succeeded, does not retry, and the zone stays stale
    for as long as it keeps sending the same address —
    ``base.py``'s own comment on ``check_hostnameon_server`` names that
    direction as the unsafe one.

    #131 pinned rather than fixed it because it changes what a live
    tenant sees on ``/nic/update``, which is the milestone owner's call.
    That call was taken on 2026-09-01: **fix it.** The assertions below
    therefore flipped from ``good`` to ``dnserr`` — a visible edit to an
    assertion that says what it is, which is exactly what the pin was
    for. The old name is kept in this paragraph so ``git log -S`` and a
    grep both still land here.

    **This is a deliberate divergence from ``dyndns-route53``, and it
    must not be "fixed" back.** Legacy's ``lib/account/nsupdate.py:61``
    binds ``response``, debug-logs it on line 67, and never reads it. It
    is not, however, a new idiom: legacy's own ``lib/accounts.py:229``
    reads ``response.rcode()``, compares it against ``dns.rcode.NOERROR``
    and renders it with ``dns.rcode.to_text`` — so this change applies
    legacy's pattern to the one call site legacy forgot. Recorded in
    ``docs/ops/refactor-plan.md`` § 5c beside the other accepted
    behaviour changes, and in ``providers/nsupdate.py``'s own docstring.

    The frozen wire table has no opinion to overrule; see
    :func:`test_the_wire_table_cannot_reach_this_adapter`, which asserts
    that from the table's own data rather than taking it on trust.
    """

    @pytest.mark.parametrize("behaviour", REFUSAL_BEHAVIOURS)
    def test_every_non_zero_rcode_is_dnserr_on_create(
        self, receiver: TsigReceiver, behaviour: str
    ) -> None:
        """Every one, not just ``REFUSED``.

        A fix keyed on one rcode is the same defect with a smaller blast
        radius, so the parametrisation is *derived* from ``dns.rcode``
        (see ``tsig_receiver.REFUSAL_BEHAVIOURS``) rather than typed out:
        a hand-written list is the same defect one release later.
        """
        receiver.behaviour = behaviour
        results = get_provider(account()).createrecords(V4, ZONES)

        assert results == {HOSTNAME: STATUS_DNSERR}, (
            f"a {behaviour.upper()} answer to the UPDATE was reported as "
            f"{results[HOSTNAME]!r}. If this reads 'good', the rcode has "
            "stopped being read and #142 is back."
        )
        assert len(receiver.updates) == 1
        assert receiver.rejections == [], (
            "the receiver refused the *signature*, so this says nothing "
            "about the rcode path"
        )

    @pytest.mark.parametrize("behaviour", REFUSAL_BEHAVIOURS)
    def test_every_non_zero_rcode_is_dnserr_on_delete(
        self, receiver: TsigReceiver, behaviour: str
    ) -> None:
        """The second ``dns.query.tcp`` call site, over the same socket.

        Both sites are covered because a fix applied to one is the more
        likely half of this defect's return: ``deleterecords`` sends no
        resolver query, so nothing else in this file would notice.
        """
        receiver.behaviour = behaviour
        results = get_provider(account()).deleterecords(ZONES)

        assert results == {HOSTNAME: STATUS_DNSERR}, (
            f"a {behaviour.upper()} answer to the delete was reported as "
            f"{results[HOSTNAME]!r}"
        )
        assert len(receiver.updates) == 1
        assert receiver.rejections == []

    def test_noerror_is_still_good_so_the_check_is_not_a_blanket_refusal(
        self, receiver: TsigReceiver
    ) -> None:
        """The other side of the branch, or the pair above is vacuous.

        An adapter that answered ``dnserr`` unconditionally would pass
        every parametrised case above and break every real tenant. This
        is the reading that separates *the rcode is read* from *the
        write path is broken*.
        """
        receiver.behaviour = "noerror"
        results = get_provider(account()).createrecords(V4, ZONES)

        assert results == {HOSTNAME: STATUS_GOOD}
        assert len(receiver.updates) == 1

    @pytest.mark.parametrize("behaviour", REFUSAL_BEHAVIOURS)
    def test_the_receiver_really_did_refuse(
        self, receiver: TsigReceiver, behaviour: str
    ) -> None:
        """Vacuity for the parametrised cases, from a second reading.

        #131 wrote this guard because the adapter discarded the
        response, so nothing else could tell "the adapter ignored a
        REFUSED" from "the receiver never sent one". **It is still
        needed, for the mirror reason**: the adapter now converts the
        rcode into a status and still does not report which rcode it
        saw, so a receiver that answered ``SERVFAIL`` to every
        ``behaviour`` would satisfy every assertion above. This sends the
        same message by hand and reads the rcode back off the wire.

        Extended to every behaviour rather than left on ``refused``,
        because a one-rcode vacuity guard under an eleven-rcode
        parametrisation is a guard covering one twelfth of what it
        appears to.
        """
        receiver.behaviour = behaviour
        update = dns.update.Update(
            f"{ZONE}.", keyring=receiver.keyring, keyalgorithm="hmac-sha256"
        )
        update.replace(f"{HOSTNAME}.", 300, "A", V4)

        response = dns.query.tcp(update, LOOPBACK, timeout=5.0)

        assert response.rcode() == RCODE_BEHAVIOURS[behaviour], (
            f"asked the receiver for {behaviour!r} and it answered "
            f"{dns.rcode.to_text(response.rcode())} — the cases above are "
            "not measuring the rcode they name"
        )
        assert response.rcode() != dns.rcode.NOERROR
        assert receiver.rejections == []

    def test_the_log_line_names_the_rcode(
        self, receiver: TsigReceiver
    ) -> None:
        """A refusal must be one grep, not three deploys.

        #142's whole cost was a diagnosis nobody could make from the
        outside, and ``dnserr`` alone does not distinguish a REFUSED
        from a timeout from a dropped connection. The rcode is carried
        as its own structlog key *and* in the message, and the name is
        asserted rather than the integer: ``rcode=5`` is a lookup, and a
        diagnosis that costs a lookup does not get made.

        Asserted on the captured event dict rather than on rendered
        text, because a formatter change must not silently delete the
        field.
        """
        receiver.behaviour = "notzone"
        with structlog.testing.capture_logs() as captured:
            results = get_provider(account()).createrecords(V4, ZONES)

        assert results == {HOSTNAME: STATUS_DNSERR}
        failures = [e for e in captured if e.get("event") == "provider.create_failed"]
        assert len(failures) == 1, (
            f"expected one provider.create_failed, got "
            f"{[e.get('event') for e in captured]}"
        )
        assert failures[0]["rcode"] == "NOTZONE", failures[0]
        assert "NOTZONE" in str(failures[0]["error"]), failures[0]

    def test_a_transport_fault_carries_no_rcode(
        self, receiver: TsigReceiver
    ) -> None:
        """``n/a`` is never a value — the other half of the field above.

        A dropped connection has no rcode at all. Rendering that as
        ``NOERROR``, or as ``0``, would fold *not measured* into
        *measured as zero* and send the reader to the wrong file. The
        field is ``None``.
        """
        receiver.behaviour = "drop"
        with structlog.testing.capture_logs() as captured:
            results = get_provider(account()).createrecords(V4, ZONES)

        assert results == {HOSTNAME: STATUS_DNSERR}
        failures = [e for e in captured if e.get("event") == "provider.create_failed"]
        assert len(failures) == 1
        assert failures[0]["rcode"] is None, failures[0]

    def test_the_refusal_set_is_derived_and_covers_rfc_2136(self) -> None:
        """The parametrisation is not a hand-kept list — asserted.

        Two instruments on the same question. The first is that
        ``REFUSAL_BEHAVIOURS`` is computed from ``dns.rcode.Rcode``, so a
        rcode dnspython adds is covered with no edit here. The second is
        this: the six rcodes RFC 2136 §2.2 actually names for an UPDATE
        are checked to be *in* it, by name, so a derivation that quietly
        started returning an empty tuple — which would make every
        parametrised case above disappear rather than fail — is caught.
        A vanished parametrisation is a green suite.
        """
        assert set(REFUSAL_BEHAVIOURS) >= {
            "formerr",
            "servfail",
            "refused",
            "yxrrset",
            "nxrrset",
            "notauth",
            "notzone",
        }, sorted(REFUSAL_BEHAVIOURS)
        assert "noerror" not in REFUSAL_BEHAVIOURS
        assert all(
            RCODE_BEHAVIOURS[name] != dns.rcode.NOERROR for name in REFUSAL_BEHAVIOURS
        )


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
