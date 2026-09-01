"""A DNS UPDATE receiver on loopback that really validates TSIG.

Why this exists at all
----------------------
``test_providers.py`` replaces ``dns.query.tcp`` with
:class:`~test_providers.FakeNsUpdate` and asserts the
:class:`dns.update.UpdateMessage` the adapter builds. That is the right
instrument for record-type correctness — #29 proved a return-value
assertion cannot tell an ``A`` from an ``AAAA`` — and it is deliberately
*not* what this module is for. Three things live below the mock
boundary and no mock can reach them:

* **the signature.** ``FakeNsUpdate.tcp`` accepts every message it is
  handed. Nothing in this repository has ever checked that what
  ``NsUpdateProvider`` signs is acceptable to something that verifies
  signatures — a keyring built from the wrong secret, or named for a
  key the server does not hold, produces a message the double swallows
  and a real nameserver refuses.
* **the refusals.** A double raises what a test tells it to. A
  nameserver answers ``REFUSED``, ``SERVFAIL``, ``NOTZONE`` — any of
  eleven non-zero header rcodes — *in a well-formed, correctly signed
  response*, which is a different thing entirely. Nothing raises and
  nothing is malformed; only reading ``response.rcode()`` tells the two
  apart. That is #142, and ``test_nsupdate_receiver.py``'s
  ``TestTheRcodeIsReadNow`` drives every one of the eleven through here.
* **the timeout.** ``QUERY_TIMEOUT`` is passed to ``dns.query.tcp`` and
  nothing has ever made a socket sit still long enough to reach it.

Port 53, and why there is no port setting
-----------------------------------------
``nsupdate.py`` calls ``dns.query.tcp(update, nameserver, timeout=…)``
with no ``port``, so every update goes to 53. Rather than add a port to
the ``nsupdate_nameserver`` setting — a schema change that would be
landing as a test hook rather than as a feature anyone asked for — the
receiver binds **53 inside the api container**, where the tests already
run. Production behaviour is unchanged, and the socket the adapter
opens in a test is the socket it opens in production.

**That works because of a sysctl, not because of root**, and the
distinction is worth writing down because the obvious reading is wrong:
the image's ``dev`` stage ends on ``USER app`` (uid 1000), so nothing
here is privileged. It binds because the container's
``net.ipv4.ip_unprivileged_port_start`` is ``0``.
``test_the_container_can_bind_the_port_the_adapter_talks_to`` asserts
the bind succeeds and **fails** — it does not skip — when it cannot, so
a base-image or daemon change that closes the low ports arrives as a
red test naming the reason rather than as a suite that quietly stops
covering the socket.

What it answers
---------------
Both transports, because the adapter uses both. ``createrecords()``
calls ``check_hostnameon_server`` first, which is a *resolver* query
over UDP to the same nameserver; only then does it send the UPDATE over
TCP. A receiver that served TCP alone would still work — the resolver
would get a connection refusal and return ``False``, which makes the
write proceed — but it would do so by accident, and the ``nochg`` path
would be unreachable. So UDP is served too, and
:attr:`TsigReceiver.query_answer` decides what it says.
"""
from __future__ import annotations

import socket
import struct
import threading
from typing import Any

import dns.flags
import dns.message
import dns.name
import dns.opcode
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rrset
import dns.tsig
import dns.tsigkeyring
import dns.update

#: Every rcode a nameserver can put in a response *header*, keyed by its
#: lowercase name, **derived from ``dns.rcode`` rather than typed out**.
#:
#: Typed out, this was ``noerror``/``refused``/``servfail``, and the
#: adapter check it now drives would have been evidenced against three
#: of twelve — which is #142's own warning (*a fix keyed on one rcode is
#: the same defect with a smaller blast radius*) reappearing in the
#: instrument instead of in the code. Derived, an rcode dnspython learns
#: about is covered here with no edit, and one it drops takes its own
#: coverage with it.
#:
#: ``value < 16`` is the whole filter and it is not arbitrary: the DNS
#: header carries four bits, so 0–15 is exactly what a response can say
#: without an OPT or TSIG record. ``BADVERS``/``BADSIG`` (16) and up are
#: extended rcodes belonging to EDNS0 and TSIG rather than answers to an
#: UPDATE, and ``dns.message.Message.set_rcode`` would have to invent an
#: OPT record to express them.
RCODE_BEHAVIOURS: dict[str, int] = {
    name.lower(): member.value
    for name, member in dns.rcode.Rcode.__members__.items()
    if member.value < 16
}

#: What the receiver may be told to do with a *well-signed* UPDATE:
#: every header rcode above, plus the two faults that are not an rcode
#: at all. ``hang`` holds the connection open past the client's timeout
#: without answering; ``drop`` closes it without a response. They are
#: different faults and the adapter is entitled to treat them
#: differently, so they are separate rather than one "failure" knob.
BEHAVIOURS: tuple[str, ...] = tuple(RCODE_BEHAVIOURS) + ("hang", "drop")

#: The subset that means *the zone was not written* — everything the
#: adapter must turn into ``dnserr``. The complement is exactly
#: ``noerror``, which is the point: the adapter tests for the one, not
#: for a list of the others.
REFUSAL_BEHAVIOURS: tuple[str, ...] = tuple(
    name for name in RCODE_BEHAVIOURS if name != "noerror"
)

#: Seconds the ``hang`` behaviour sits on a connection. Longer than the
#: timeouts the tests set, shorter than anyone's patience.
HANG_SECONDS = 5.0

#: How long the accept loops block before re-checking the stop flag.
#: Small enough that teardown is not perceptible, large enough not to
#: spin.
_POLL = 0.05


class TsigReceiver:
    """A loopback nameserver that verifies TSIG and records what it got.

    Not a nameserver in any general sense: it answers exactly the two
    things ``NsUpdateProvider`` asks for, and it refuses everything it
    does not understand rather than inventing an answer.
    """

    def __init__(
        self,
        keyname: str,
        secret: str,
        algorithm: str = "hmac-sha256",
        host: str = "127.0.0.1",
        port: int = 53,
    ) -> None:
        self.host = host
        self.port = port
        self.keyname = dns.name.from_text(keyname)
        self.algorithm = dns.name.from_text(algorithm)
        self.keyring = dns.tsigkeyring.from_text({keyname + ".": secret})

        #: Every UPDATE whose signature verified, in arrival order.
        self.updates: list[dns.message.Message] = []
        #: One entry per message this receiver refused to authenticate,
        #: as ``(exception class name, str(exception))``. A test asserts
        #: on this rather than on a log line, because a receiver that
        #: silently accepted a bad signature and a receiver that never
        #: saw the message look identical from the client side.
        self.rejections: list[tuple[str, str]] = []

        #: What a well-signed UPDATE is answered with. One of
        #: :data:`BEHAVIOURS`.
        self.behaviour = "noerror"

        #: What the UDP resolver query answers. ``None`` is NXDOMAIN,
        #: which is what makes ``check_hostnameon_server`` return False
        #: and the write proceed. An address here produces ``nochg``.
        self.query_answer: str | None = None

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._tcp: socket.socket | None = None
        self._udp: socket.socket | None = None

    # -- lifecycle ------------------------------------------------------ #

    def start(self) -> "TsigReceiver":
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind((self.host, self.port))
        self._tcp.listen(8)
        self._tcp.settimeout(_POLL)

        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind((self.host, self.port))
        self._udp.settimeout(_POLL)

        for target in (self._serve_tcp, self._serve_udp):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=HANG_SECONDS + 2.0)
        for sock in (self._tcp, self._udp):
            if sock is not None:
                sock.close()
        self._tcp = self._udp = None
        self._threads = []

    def __enter__(self) -> "TsigReceiver":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- TSIG ----------------------------------------------------------- #

    def _parse(self, wire: bytes) -> dns.message.Message:
        """Parse, verify, and **require** a signature.

        ``keyring=`` is what makes this a *validating* receiver: with a
        keyring, ``from_wire`` runs ``dns.tsig.validate`` over the wire
        bytes and raises ``UnknownTSIGKey`` for a name it does not hold
        and ``BadSignature`` for a MAC that does not check out.

        The explicit ``had_tsig`` check is the half that is easy to
        leave out, and leaving it out was caught by mutation rather than
        by reading: a message carrying **no TSIG record at all** never
        reaches ``dns.tsig.validate``, so ``from_wire`` returns it
        cleanly however good the keyring is. Made to drop its keyring
        entirely, ``NsUpdateProvider._message`` sent unsigned updates
        that this receiver accepted and answered ``NOERROR`` to — an
        adapter that had silently stopped signing, reported as ``good``.
        A nameserver with ``update-policy`` keyed on a TSIG key refuses
        that, and so does this.
        """
        message = dns.message.from_wire(wire, keyring=self.keyring)
        if not message.had_tsig:
            raise dns.message.UnknownTSIGKey("unsigned message: no TSIG record")
        return message

    def _tsig_error_response(self, wire: bytes, error: int) -> bytes:
        """The refusal a nameserver sends, in the shape RFC 8945 gives it.

        ``NOTAUTH``, plus a TSIG record carrying the error code — and
        the error code is the whole point. ``dns.tsig.validate`` on the
        client side checks ``rdata.error`` *before* it checks the MAC,
        so this reaches the client as ``PeerBadKey`` / ``PeerBadSignature``
        rather than as a response it accepts. A bare unsigned ``NOTAUTH``
        is accepted by dnspython's client without complaint — measured,
        not assumed; see ``test_an_unsigned_refusal_would_be_accepted``
        — which is why this is not one.
        """
        # The header is all we can trust: the rest failed to
        # authenticate. Opcode comes off the wire by hand rather than
        # from a parse we have just refused.
        (msg_id, flags) = struct.unpack("!HH", wire[0:4])
        opcode = dns.opcode.from_flags(flags)
        response = dns.message.Message(id=msg_id)
        response.flags = dns.flags.QR
        response.set_opcode(opcode)
        response.set_rcode(dns.rcode.NOTAUTH)
        # An empty secret: RFC 8945 §5.3 has the MAC empty when the
        # server cannot compute one, and the client never reaches the
        # MAC check because ``error`` is non-zero.
        keyring = {self.keyname: dns.tsig.Key(self.keyname, b"", self.algorithm)}
        response.use_tsig(
            keyring,
            keyname=self.keyname,
            algorithm=self.algorithm,
            tsig_error=error,
        )
        return response.to_wire()

    # -- transports ----------------------------------------------------- #

    def _serve_tcp(self) -> None:
        assert self._tcp is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._tcp.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            with conn:
                try:
                    self._handle_tcp(conn)
                except OSError:
                    continue

    def _handle_tcp(self, conn: socket.socket) -> None:
        conn.settimeout(HANG_SECONDS + 2.0)
        header = _recv_exactly(conn, 2)
        if header is None:
            return
        (length,) = struct.unpack("!H", header)
        wire = _recv_exactly(conn, length)
        if wire is None:
            return

        try:
            request = self._parse(wire)
        except Exception as exc:  # noqa: BLE001 — every TSIG failure
            self.rejections.append((type(exc).__name__, str(exc)))
            error = (
                dns.rcode.BADKEY
                if isinstance(exc, dns.message.UnknownTSIGKey)
                else dns.rcode.BADSIG
            )
            _send_tcp(conn, self._tsig_error_response(wire, error))
            return

        self.updates.append(request)

        if self.behaviour == "drop":
            return
        if self.behaviour == "hang":
            # Sit on the connection. ``_stop`` cuts it short at teardown
            # so a finished test does not hold the suite for HANG_SECONDS.
            self._stop.wait(HANG_SECONDS)
            return

        response = dns.message.make_response(request)
        # ``make_response`` carries the request's keyring and key name
        # over, so this answer is really TSIG-signed, with ``error=0``.
        # That matters: an *unsigned* refusal is a different fault, and
        # one dnspython's client accepts without complaint — measured by
        # ``test_an_unsigned_refusal_would_be_accepted_by_the_client``.
        # The refusal the adapter is shown here is the one a nameserver
        # actually sends.
        response.set_rcode(RCODE_BEHAVIOURS[self.behaviour])
        _send_tcp(conn, response.to_wire())

    def _serve_udp(self) -> None:
        assert self._udp is not None
        while not self._stop.is_set():
            try:
                wire, peer = self._udp.recvfrom(65535)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            try:
                self._udp.sendto(self._answer_query(wire), peer)
            except OSError:
                return

    def _answer_query(self, wire: bytes) -> bytes:
        """The resolver half: A/AAAA for the name, or NXDOMAIN.

        Unsigned, because ``check_hostnameon_server`` builds a plain
        ``dns.resolver.Resolver`` and sends no TSIG — asserting a
        signature on a query nobody signs would be this module lying
        about the code it exercises.
        """
        query = dns.message.from_wire(wire)
        response = dns.message.make_response(query)
        response.flags |= dns.flags.AA
        if self.query_answer is None:
            response.set_rcode(dns.rcode.NXDOMAIN)
            return response.to_wire()

        name = query.question[0].name
        rdtype = query.question[0].rdtype
        wanted = dns.rdatatype.AAAA if ":" in self.query_answer else dns.rdatatype.A
        if rdtype != wanted:
            return response.to_wire()  # NODATA, not NXDOMAIN
        response.answer.append(
            dns.rrset.from_text(
                name, 60, dns.rdataclass.IN, rdtype, self.query_answer
            )
        )
        return response.to_wire()


def _recv_exactly(conn: socket.socket, count: int) -> bytes | None:
    chunks: list[bytes] = []
    got = 0
    while got < count:
        try:
            chunk = conn.recv(count - got)
        except (socket.timeout, TimeoutError):
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _send_tcp(conn: socket.socket, wire: bytes) -> None:
    conn.sendall(struct.pack("!H", len(wire)) + wire)


def can_bind(host: str = "127.0.0.1", port: int = 53) -> tuple[bool, str]:
    """Whether this process can take the port, and why not when it cannot.

    Returned rather than raised so the caller decides between a skip and
    a failure. The tests here choose failure: a suite that silently
    stops exercising the socket is the thing the port choice was made to
    avoid.
    """
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        sock = socket.socket(socket.AF_INET, kind)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            transport = "tcp" if kind == socket.SOCK_STREAM else "udp"
            return False, f"{transport}: {exc}"
        finally:
            sock.close()
    return True, ""


__all__ = [
    "BEHAVIOURS",
    "HANG_SECONDS",
    "RCODE_BEHAVIOURS",
    "REFUSAL_BEHAVIOURS",
    "TsigReceiver",
    "can_bind",
]
