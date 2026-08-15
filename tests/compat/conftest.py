"""Dual-target runner for the DynDNS v2 compatibility case table.

`protocol_cases.yaml` is data. This file is the only thing that turns it into
requests, and it must do so **identically for both targets** — `--target`
selects which cases run, never how a request is built. Anything that has to
branch on the target to get a green run is a compatibility break wearing a
runner's clothes; surface it as a failure instead of branching around it.

Two invariants worth stating before the code:

* **`--target` is required and has no default.** A runner that picks one will
  eventually be pointed at the wrong service and report green about it. The
  requirement is enforced where the wire cases are *collected* — not at
  `pytest_configure`, which fires whenever this file is loaded and therefore
  took every sibling suite under `tests/compat/` down with it. See
  `pytest_configure` and `pytest_generate_tests` below; `test_runner_contract.py`
  is the proof that narrowing the blast radius did not relax the rule.
* **`--base-url` is used verbatim.** Nothing here reads the hostname to decide
  anything. `scripts/smoke.sh` inferred "this is the local stack" from the
  shape of a URL and read the wrong stack through an ssh tunnel while
  reporting green. This file contains no loopback literal at all —
  `test_no_url_shape_inference` in `test_protocol.py` fails if one appears,
  which is what keeps the rule true rather than merely stated.

The wire is spoken with `http.client` rather than requests/httpx because this
suite asserts exact bytes and exact headers. `http.client` adds only `Host` and
`Accept-Encoding: identity`; every other header on the wire is one this file
put there, so `omit_headers: [user-agent]` genuinely omits it.
"""
from __future__ import annotations

import base64
import functools
import http.client
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlencode, urlsplit

import pytest
import yaml

CASE_TABLE = Path(__file__).parent / "protocol_cases.yaml"

TARGETS = ("legacy", "host")

#: Sent on every request unless the case overrides or omits it. The table's
#: divergence-2 cases (`omit_headers: [user-agent]`, and a `curl/8.4.0`
#: override) are only meaningful if the runner sends *something* by default.
RUNNER_USER_AGENT = "atrium-ddns-compat-runner/1"

#: How `client_ip` is arranged. The table states the address the server must
#: *see*; the legacy service sits behind `ProxyFix(x_for=1)` and takes it from
#: this header. Same header for both targets — a per-target spelling here would
#: be exactly the branch this runner refuses to have.
CLIENT_IP_HEADER = "X-Forwarded-For"

#: `client_ip: null` means "the server sees no parseable address at all", and
#: it cannot be arranged by omitting the header: `ProxyFix._get_real_value`
#: ignores an absent *or empty* value and leaves `REMOTE_ADDR` as the socket
#: address, which is perfectly parseable. So the null case is expressed as a
#: value no IP parser accepts.
UNPARSEABLE_CLIENT_IP = "not-an-ip"

#: Preconditions the wire cannot establish. A case carrying one is skipped with
#: the precondition named — never silently passed, never counted as executed.
#: `rate_limited` needs the fixture's rate-limit state primed out of band; the
#: limit is not on the wire and hammering the endpoint to find it would poison
#: every case that follows.
_PRECONDITION_REASONS = {
    "rate_limited": (
        "requires the user to be over its rate limit before the request; "
        "that state is not arrangeable over the wire"
    ),
}


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("compat", "DynDNS v2 compatibility runner")
    group.addoption(
        "--target",
        action="store",
        default=None,
        choices=list(TARGETS),
        help=(
            "which implementation is behind --base-url. REQUIRED, no default: "
            "it decides which cases run, and guessing it means running the "
            "wrong table against the wrong service."
        ),
    )
    group.addoption(
        "--base-url",
        action="store",
        default=None,
        help=(
            "full base URL of the service under test, scheme and port "
            "included. REQUIRED, used verbatim. Nothing infers anything from "
            "its shape. See tests/compat/README.md for worked invocations."
        ),
    )
    group.addoption(
        "--compat-timeout",
        action="store",
        type=float,
        default=10.0,
        help="per-request timeout in seconds (default: 10)",
    )
    group.addoption(
        "--compat-insecure",
        action="store_true",
        default=False,
        help="do not verify TLS certificates (https base URLs only)",
    )


#: Printed instead of running a wire case when no `--target` was given. It says
#: *not run*, never *0 failures*: a session that executed no case at all and one
#: that executed 101 clean ones are different results and must not render the
#: same.
NO_TARGET_SKIP_REASON = (
    "--target was not given, so NO wire case ran. The wire table needs an "
    "explicit target and base URL, neither of which has a default: "
    "`make test-compat TARGET=legacy|host BASE_URL=<url>`, or "
    "`pytest tests/compat --target legacy|host --base-url <url>`. A runner "
    "that picks a target for you will eventually be pointed at the wrong "
    "service and report green about it."
)


def pytest_configure(config: pytest.Config) -> None:
    target = config.getoption("--target")
    base_url = config.getoption("--base-url")

    if target is None:
        # No target: the wire table is not run, and that is reported as *not
        # run* rather than enforced here.
        #
        # This hook fires whenever this conftest is loaded, which is any
        # session rooted at or under `tests/compat/` — including
        # `pytest tests/`, `pytest tests/compat/legacy_behaviour` and the
        # guards in `test_protocol.py`, none of which open a socket. Raising
        # here refused all of them before collection had started, which is why
        # #10 had to reach for `--confcutdir`. The requirement itself is
        # unchanged and enforced one level down, at collection of the wire
        # cases (`pytest_generate_tests`): with no target, `test_case` is
        # collected as a single skip carrying NO_TARGET_SKIP_REASON, so no case
        # can execute against a service nobody named.
        #
        # One thing does still refuse outright: a `--base-url` with no
        # `--target`. That is not a sibling suite being swept up, it is someone
        # pointing the runner at a service without saying which service it is —
        # exactly the mistake `--target` exists to prevent.
        if base_url is not None:
            raise pytest.UsageError(
                f"--base-url {base_url!r} was given without --target. --target "
                "is required and has no default: it names which implementation "
                "is behind that URL, and it decides which cases run. Pass "
                "--target legacy or --target host, and make sure it names the "
                "service that is actually behind --base-url."
            )
        return

    if base_url is None:
        # Collection does not need a service; execution does. The only
        # conditional in this file that reads pytest's mode, and it reads
        # nothing about the target or the shape of the URL.
        if config.getoption("collectonly"):
            return
        raise pytest.UsageError(
            "--base-url is required and has no default. Pass the full base "
            "URL of the service under test, scheme and port included; see "
            "tests/compat/README.md for worked invocations."
        )
    _split_base_url(base_url)  # validate now, not on the first request


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def load_table() -> dict[str, Any]:
    with CASE_TABLE.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def resolve_case(case: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    """Apply `defaults` to one raw case.

    Top-level keys are defaulted whole; `expect` is merged key-by-key so a case
    that overrides only `body` still inherits `status` and `content_type`.
    """
    resolved = {k: v for k, v in defaults.items() if k != "expect"}
    resolved.update({k: v for k, v in case.items() if k != "expect"})
    expect = dict(defaults.get("expect", {}))
    expect.update(case.get("expect", {}))
    resolved["expect"] = expect
    return resolved


@functools.lru_cache(maxsize=1)
def all_cases() -> tuple[dict[str, Any], ...]:
    table = load_table()
    defaults = table["defaults"]
    return tuple(resolve_case(case, defaults) for case in table["cases"])


def deleted_case_ids() -> tuple[str, ...]:
    return tuple(entry["id"] for entry in load_table()["deleted_cases"])


def select_for_target(target: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition the table into (runs on this target, does not).

    Returned as a partition rather than a filter on purpose: the two lists are
    asserted to add up to the whole table, so a typo in a `targets:` list shows
    up as an arithmetic failure instead of quietly shrinking the run.
    """
    selected, excluded = [], []
    for case in all_cases():
        (selected if target in case["targets"] else excluded).append(case)
    return selected, excluded


def unmet_preconditions(case: Mapping[str, Any]) -> list[str]:
    return [
        f"{key}: {reason}"
        for key, reason in _PRECONDITION_REASONS.items()
        if case.get(key)
    ]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Where `--target` is actually enforced.

    With a target, one test per selected case. Without one, a single skip
    naming the reason — *not* an empty parametrisation, which pytest renders as
    a bare "got empty parameter set" and which reads like a filter rather than
    like a refusal.

    Either way the wire cases cannot execute without a target, because this is
    the only thing that supplies `case`.
    """
    if "case" not in metafunc.fixturenames:
        return
    target = metafunc.config.getoption("--target")
    if target is None:
        metafunc.parametrize(
            "case",
            [pytest.param(None, marks=pytest.mark.skip(reason=NO_TARGET_SKIP_REASON))],
            ids=["NO-TARGET-GIVEN-WIRE-TABLE-NOT-RUN"],
        )
        return
    selected, _ = select_for_target(target)
    metafunc.parametrize("case", selected, ids=[c["id"] for c in selected])


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompatRequest:
    method: str
    target_path: str  # path + query string, as it goes on the wire
    headers: dict[str, str]
    auth_label: str

    def describe(self, base_url: str) -> str:
        lines = [f"{self.method} {base_url}{self.target_path}", f"auth: {self.auth_label}"]
        for name, value in self.headers.items():
            if name.lower() == "authorization":
                # base64 is not encryption. The credentials are synthetic and
                # committed in the table, but a failure report is the last
                # place to build the habit of echoing one back.
                value = f"{value.split(' ', 1)[0]} <withheld; see the fixture block>"
            lines.append(f"header: {name}: {value}")
        return "\n".join(lines)


@dataclass
class CompatResponse:
    status: int
    content_type: str | None
    body: bytes
    headers: list[tuple[str, str]] = field(default_factory=list)


def _encode_query(query: Mapping[str, Any]) -> str:
    pairs = []
    for name, value in query.items():
        if value is None or isinstance(value, bool):
            raise ValueError(
                f"query parameter {name!r} is {value!r}; the table's values are "
                "literal strings, and 'absent' is spelled by leaving the key out"
            )
        pairs.append((name, str(value)))
    # `,` and `:` left literal because that is what a router in the field puts
    # on the wire (comma-separated hostname lists, IPv6 myip). Everything else
    # is percent-encoded, including the newline in the divergence-6 case.
    return urlencode(pairs, quote_via=quote, safe=",:")


def build_request(case: Mapping[str, Any]) -> CompatRequest:
    """Turn one resolved case into a wire request.

    Takes no target. That is the point: `--target` decides *which* cases run,
    never *how* they are sent.
    """
    query = _encode_query(case.get("query", {}))
    target_path = case["path"] + (f"?{query}" if query else "")

    headers: dict[str, str] = {"User-Agent": RUNNER_USER_AGENT}

    client_ip = case.get("client_ip")
    headers[CLIENT_IP_HEADER] = UNPARSEABLE_CLIENT_IP if client_ip is None else str(client_ip)

    auth = case.get("auth", {"type": "none"})
    auth_type = auth.get("type", "none")
    if auth_type == "basic":
        username, password = auth["username"], auth["password"]
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
        auth_label = f"basic, username={username!r} (password withheld)"
    elif auth_type == "none":
        auth_label = "none (no Authorization header)"
    else:
        raise ValueError(f"unknown auth type {auth_type!r} in case {case['id']!r}")

    for name, value in case.get("headers", {}).items():
        _set_header(headers, name, str(value))
    for name in case.get("omit_headers", []):
        _del_header(headers, name)

    return CompatRequest(
        method=case.get("method", "GET"),
        target_path=target_path,
        headers=headers,
        auth_label=auth_label,
    )


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    _del_header(headers, name)
    headers[name] = value


def _del_header(headers: dict[str, str], name: str) -> None:
    for existing in [k for k in headers if k.lower() == name.lower()]:
        del headers[existing]


def _split_base_url(base_url: str) -> tuple[str, str, int | None, str]:
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https"):
        raise pytest.UsageError(
            f"--base-url {base_url!r}: scheme must be http or https, got {parts.scheme!r}"
        )
    if not parts.hostname:
        raise pytest.UsageError(f"--base-url {base_url!r}: no host")
    if parts.query or parts.fragment:
        raise pytest.UsageError(
            f"--base-url {base_url!r}: must not carry a query string or fragment"
        )
    return parts.scheme, parts.hostname, parts.port, parts.path.rstrip("/")


class CompatClient:
    """One request per connection. No keep-alive, no redirect following.

    A redirect is a compatibility finding, not something to chase: DynDNS
    clients do not follow them.
    """

    def __init__(self, base_url: str, timeout: float, insecure: bool) -> None:
        self.base_url = base_url.rstrip("/")
        self._scheme, self._host, self._port, self._prefix = _split_base_url(base_url)
        self._timeout = timeout
        self._insecure = insecure

    def send(self, request: CompatRequest) -> CompatResponse:
        if self._scheme == "https":
            context = ssl._create_unverified_context() if self._insecure else ssl.create_default_context()
            conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=self._timeout, context=context
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            conn.request(
                request.method, self._prefix + request.target_path, headers=request.headers
            )
            raw = conn.getresponse()
            body = raw.read()
            return CompatResponse(
                status=raw.status,
                content_type=raw.getheader("Content-Type"),
                body=body,
                headers=list(raw.getheaders()),
            )
        finally:
            conn.close()


@pytest.fixture(scope="session")
def compat_client(pytestconfig: pytest.Config) -> CompatClient:
    base_url = pytestconfig.getoption("--base-url")
    client = CompatClient(
        base_url,
        timeout=pytestconfig.getoption("--compat-timeout"),
        insecure=pytestconfig.getoption("--compat-insecure"),
    )
    # One reachability probe, so an unreachable service is one clear message
    # rather than ninety-eight identical stack traces. Reachability only —
    # the status is reported, never required. A 404 here means "reachable, not
    # implemented", which is a finding for the run to make, not a transport
    # error for the runner to hide behind.
    probe = CompatRequest(
        method="GET",
        target_path="/nic/checkip",
        headers={"User-Agent": RUNNER_USER_AGENT},
        auth_label="none (no Authorization header)",
    )
    try:
        response = client.send(probe)
    except (OSError, socket.timeout, http.client.HTTPException) as exc:
        pytest.exit(
            f"compat: cannot reach --base-url {base_url} "
            f"(target={pytestconfig.getoption('--target')}): "
            f"{type(exc).__name__}: {exc}",
            returncode=4,
        )
    pytestconfig.stash[_PROBE_KEY] = f"GET {base_url}/nic/checkip -> HTTP {response.status}"
    return client


_PROBE_KEY = pytest.StashKey[str]()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _no_target_lines() -> list[str]:
    """What a session with no `--target` prints — in both hooks, one wording.

    Two numbers, never one: the table's size and the nought that ran. "0 cases
    failed" and "the table was not run" are different facts and this is the
    place they get told apart.
    """
    return [
        f"compat: NO --target GIVEN — the wire table was NOT RUN "
        f"(0 of {len(all_cases())} cases executed; service-free guards only).",
        "compat: run it with `make test-compat TARGET=legacy|host BASE_URL=<url>`. "
        "Neither option has a default; see tests/compat/README.md.",
    ]


def pytest_report_header(config: pytest.Config) -> list[str]:
    target = config.getoption("--target")
    base_url = config.getoption("--base-url")
    if target is None:
        return _no_target_lines()
    selected, excluded = select_for_target(target)
    skipped = [c for c in selected if unmet_preconditions(c)]
    return [
        f"compat: target={target} base-url={base_url or '(none — collect-only)'}",
        f"compat: table={CASE_TABLE} ({len(all_cases())} cases, "
        f"{len(deleted_case_ids())} deleted_cases never executed)",
        f"compat: {len(selected)} selected for target={target}, "
        f"{len(excluded)} excluded by `targets:`, "
        f"{len(skipped)} of the selected skipped for unmet preconditions, "
        f"{len(selected) - len(skipped)} executable",
    ]


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:
    target = config.getoption("--target")
    if target is None:
        # Not the accounting block with zeroes in it. A table of zeroes next to
        # a green summary line is the shape this whole file exists to refuse:
        # it reads as "101 cases, none of them a problem" when the fact is
        # "nothing was asked of the service at all".
        terminalreporter.write_line("")
        for line in _no_target_lines():
            terminalreporter.write_line(line)
        return
    selected, excluded = select_for_target(target)
    skipped = [c for c in selected if unmet_preconditions(c)]
    write = terminalreporter.write_line

    write("")
    write(f"compat accounting (target={target}):")
    write(f"  cases in table            {len(all_cases()):>4}")
    write(f"  excluded by `targets:`    {len(excluded):>4}  {[c['id'] for c in excluded]}")
    write(f"  selected for this target  {len(selected):>4}")
    write(f"  unmet preconditions       {len(skipped):>4}  {[c['id'] for c in skipped]}")
    write(f"  executable                {len(selected) - len(skipped):>4}")
    effects = [c["id"] for c in selected if "effects" in c]
    write(
        f"  wire-only                 {len(effects):>4}  cases carrying `effects:` "
        "(DNS ops / persisted columns) that this runner does NOT assert"
    )

    # What the table *offers* and what this session *ran* are two numbers, and
    # printing only the first is how a `-k` filter or a collection error turns
    # into a run that reports 98 cases and executed twelve.
    ran = _case_outcomes(terminalreporter)
    total_ran = sum(ran.values())
    write(
        f"  ran this session          {total_ran:>4}  "
        + (", ".join(f"{n} {outcome}" for outcome, n in sorted(ran.items())) or "no cases ran")
    )
    shortfall = len(selected) - total_ran
    if shortfall:
        write(
            f"  NOT RUN                   {shortfall:>4}  selected cases were neither "
            "passed, failed nor skipped this session (deselected by -k/-m, or "
            "collection stopped early)"
        )
    probe = config.stash.get(_PROBE_KEY, None)
    if probe is not None:
        write(f"  reachability probe        {probe}")


def _case_outcomes(terminalreporter) -> dict[str, int]:
    """Count `test_case[...]` results only, ignoring the runner's own guards."""
    counts: dict[str, int] = {}
    for outcome in ("passed", "failed", "error", "skipped", "xfailed", "xpassed"):
        reports = terminalreporter.stats.get(outcome, [])
        n = sum(
            1
            for r in reports
            if getattr(r, "nodeid", "").rsplit("::", 1)[-1].startswith("test_case[")
            and getattr(r, "when", "call") in ("call", "setup")
        )
        if n:
            counts[outcome] = n
    return counts
