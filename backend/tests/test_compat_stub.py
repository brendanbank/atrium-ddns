"""The compat fixture's stub provider — and both of its gates, both ways.

:mod:`atrium_ddns.compat_stub` ships a DNS provider that answers
``good`` without contacting a nameserver. On a real tenant that is a
green update over an unchanged zone with nothing in any log to say why
— the "confident wrong answer" shape — so it is gated twice, and each
gate is asserted here in **both** directions. A gate tested only in the
direction that opens it is a gate nobody has seen shut.

Needs no database: everything below is registry state, a settings read
and an environment variable.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from atrium_ddns import compat_stub
from atrium_ddns.compat_stub import ENABLE_VAR, SLOTS, STUB_CLASSES
from atrium_ddns.providers import (
    ProviderAccount,
    STATUS_DNSERR,
    get_provider,
    known_services,
    provider_class,
    resolvable_services,
)


@dataclass
class _FakeSettings:
    environment: str


@pytest.fixture(autouse=True)
def clean_registry():
    """Leave the registry exactly as found.

    The registry is a process-global dict and xdist gives each worker
    one process, so a test that registers and forgets changes the
    answer for every later test in the same worker — including
    ``test_the_slots_are_not_advertised_as_installable``.
    """
    was_registered = {
        cls.SERVICE: provider_class(cls.SERVICE) for cls in STUB_CLASSES
    }
    yield
    compat_stub.unregister_stub_providers()
    for name, previous in was_registered.items():
        if previous is not None:
            from atrium_ddns.providers import register

            register(previous)


# --------------------------------------------------------------------- #
# The gates
# --------------------------------------------------------------------- #


def test_the_opt_in_must_be_exactly_one(monkeypatch) -> None:
    monkeypatch.setattr(
        compat_stub, "get_settings", lambda: _FakeSettings(environment="dev")
    )
    for value in ("", "0", "true", "yes", "1 ", "TRUE"):
        monkeypatch.setenv(ENABLE_VAR, value)
        allowed, reason = compat_stub.stub_providers_allowed()
        assert allowed is False, f"{value!r} opened the gate"
        assert ENABLE_VAR in reason

    monkeypatch.delenv(ENABLE_VAR, raising=False)
    allowed, reason = compat_stub.stub_providers_allowed()
    assert allowed is False
    assert ENABLE_VAR in reason

    monkeypatch.setenv(ENABLE_VAR, "1")
    allowed, reason = compat_stub.stub_providers_allowed()
    assert allowed is True
    assert "dev" in reason


def test_prod_refuses_even_with_the_opt_in(monkeypatch) -> None:
    """The second gate, and the one that matters if the first is pasted.

    An environment variable travels: it gets copied into a ``.env``, a
    systemd unit, a compose override. This is what makes copying it
    onto a production stack inert rather than catastrophic.
    """
    monkeypatch.setenv(ENABLE_VAR, "1")
    monkeypatch.setattr(
        compat_stub, "get_settings", lambda: _FakeSettings(environment="prod")
    )
    allowed, reason = compat_stub.stub_providers_allowed()
    assert allowed is False
    assert "prod" in reason and "without writing DNS" in reason


@pytest.mark.parametrize("environment", ["dev", "test", "staging"])
def test_non_prod_environments_open_the_second_gate(
    monkeypatch, environment
) -> None:
    monkeypatch.setenv(ENABLE_VAR, "1")
    monkeypatch.setattr(
        compat_stub, "get_settings", lambda: _FakeSettings(environment=environment)
    )
    allowed, _ = compat_stub.stub_providers_allowed()
    assert allowed is True


def test_register_is_a_no_op_when_a_gate_is_shut(monkeypatch) -> None:
    """Not merely "returns nothing" — the registry must be untouched.

    A version that logged a refusal and registered anyway would satisfy
    a test that only read the return value.
    """
    compat_stub.unregister_stub_providers()
    monkeypatch.setenv(ENABLE_VAR, "1")
    monkeypatch.setattr(
        compat_stub, "get_settings", lambda: _FakeSettings(environment="prod")
    )

    assert compat_stub.register_stub_providers() == ()
    assert [provider_class(name) for name in SLOTS] == [None] * len(SLOTS)

    # …and the same call with both gates open does register them, so the
    # assertion above is about the gate and not about the function being
    # broken.
    monkeypatch.setattr(
        compat_stub, "get_settings", lambda: _FakeSettings(environment="dev")
    )
    assert compat_stub.register_stub_providers() == SLOTS
    assert [provider_class(name) for name in SLOTS] != [None] * len(SLOTS)


def test_the_slots_are_never_advertised_as_installable() -> None:
    """``known_services()`` is what a UI offers when creating a backend.

    A stub reachable from a dropdown is a stub somebody will select.
    Asserted against ``known_services`` even while the classes are
    registered, because ``resolvable_services`` and ``known_services``
    are deliberately different sets.
    """
    compat_stub.register_stub_providers(force=True)
    assert set(SLOTS).isdisjoint(known_services())
    assert set(SLOTS).issubset(resolvable_services())


# --------------------------------------------------------------------- #
# The behaviour
# --------------------------------------------------------------------- #


def _account(result: str | None, **config) -> ProviderAccount:
    merged = dict(config)
    if result is not None:
        merged["result"] = result
    return ProviderAccount(
        service=SLOTS[0],
        domains=("example.com",),
        credentials={"stub_token": "x"},
        config=merged,
        id="1",
    )


@pytest.mark.parametrize("result", ["good", "nochg", "dnserr"])
def test_the_scripted_result_comes_from_the_row(result) -> None:
    compat_stub.register_stub_providers(force=True)
    provider = get_provider(_account(result))
    assert provider is not None
    compat_stub.reset_calls()
    answered = provider.createrecords(
        "203.0.113.10", {"example.com": ["a.example.com"]}, rtype="A", ttl=60
    )
    assert answered == {"a.example.com": result}
    assert [call.result for call in compat_stub.CALLS] == [result]


@pytest.mark.parametrize("result", [None, "911", "badauth", "", "GOOD"])
def test_an_unscriptable_result_degrades_to_dnserr(result) -> None:
    """``911`` is deliberately not scriptable, and this is why.

    The fixture produces ``911`` by storing **no credentials** or by
    naming a service the factory does not know — the two conditions the
    table's spec rows actually describe. A stub that could *return*
    ``911`` would let ``update-911-backend-without-stored-credentials``
    go green against a router that never checks credentials at all.
    """
    compat_stub.register_stub_providers(force=True)
    provider = get_provider(_account(result))
    assert provider is not None
    assert provider.createrecords(
        "203.0.113.10", {"example.com": ["a.example.com"]}
    ) == {"a.example.com": STATUS_DNSERR}


def test_the_stub_does_not_shortcut_the_zone_check() -> None:
    """Everything above ``createrecords`` stays unmodified base behaviour.

    ``tests/compat/README.md`` states the rule for the legacy stub:
    "keep the module strictly below ``hostnameperzone`` … or the table
    is calibrating against you". Same rule here — this is what makes
    ``update-nohost-hostname-outside-backend-zone`` a real assertion.
    """
    compat_stub.register_stub_providers(force=True)
    provider = get_provider(_account("good"))
    assert provider is not None
    assert provider.hostnameperzone(["a.example.com"]) == {
        "example.com": ["a.example.com"]
    }
    assert provider.hostnameperzone(["a.elsewhere.example.org"]) is False


def test_the_stub_contacts_nothing() -> None:
    """A structural sweep, because "it did not resolve" is unobservable.

    The whole justification for shipping this module is that it does no
    IO. A behavioural check cannot prove a negative in one run; reading
    the source for an import of anything that speaks a network protocol
    can.
    """
    import ast
    from pathlib import Path

    source = Path(compat_stub.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    networked = {
        "boto3", "botocore", "httpx", "requests", "urllib", "urllib3",
        "socket", "http", "dns", "ftplib", "smtplib", "telnetlib", "asyncio",
    }
    assert imported & networked == set(), (
        f"{compat_stub.__file__} imports {sorted(imported & networked)}. "
        "The stub's entire purpose is answering without touching a "
        "nameserver; an import that can open a socket defeats it."
    )


def test_the_slot_count_matches_the_deepest_backend_stack() -> None:
    """Three slots because ``firsterr`` needs three on one domain.

    ``UNIQUE(domain_id, backend_type)`` is why they cannot share a
    name. Derived from the frozen table when it is present rather than
    asserted as the literal 3, and **skipped with a reason** when it is
    not — a hardcoded 3 would keep passing after the table grew a
    four-backend hostname, and the seeder would then fail at 03:00
    with a message about slots.
    """
    from collections import Counter
    from pathlib import Path

    table = Path("/opt/compat_tests/compat/protocol_cases.yaml")
    if not table.is_file():
        pytest.skip(
            f"{table} is absent (it reaches the image through the "
            "Dockerfile's `dev` stage), so the required slot count has "
            "NOT been re-derived here"
        )
    import yaml

    fixture = yaml.safe_load(table.read_text(encoding="utf-8"))["fixture"]
    specs = {b["name"]: b for b in fixture["backends"]}
    deepest = max(
        (
            Counter(
                specs[name]["service"] for name in (entry.get("backends") or [])
            )["stub"]
            for entry in fixture["hostnames"]
        ),
        default=0,
    )
    assert len(SLOTS) >= deepest, (
        f"the frozen fixture needs {deepest} scripted backends on one "
        f"domain and compat_stub.SLOTS has {len(SLOTS)}"
    )
