"""``atrium_ddns.scripts.seed_brand`` — the merge policy.

These tests exercise :func:`merge_brand` and never touch the database.
That is a constraint, not a shortcut: the backend suite runs ``-n auto``
over one MySQL and ``app_settings['brand']`` is a **singleton row**, so
unlike ``test_user_scope_secrets.py`` there is no ``PYTEST_XDIST_WORKER``
suffix to derive. A test that seeded the real row would collide with
every other worker and read as flakiness.

What is deliberately *not* covered here, so nobody reads a green run as
covering it: that the row is written, that ``--show`` prints it, and that
the value survives a restart. Those need a live stack and were taken
against one — the transcript is in the issue #47 PR body.
"""
from __future__ import annotations

import pytest

from atrium_ddns.scripts.seed_brand import (
    DEFAULT_NAME,
    DEFAULT_PRIMARY_COLOR,
    merge_brand,
)


def test_sets_the_name_when_no_row_exists() -> None:
    assert merge_brand({}, "Casa DDNS", None) == {"name": "Casa DDNS"}


def test_replaces_an_existing_name() -> None:
    before = {"name": "Atrium", "preset": "default"}
    assert merge_brand(before, "Casa DDNS", None)["name"] == "Casa DDNS"


def test_preserves_fields_it_was_not_asked_to_write() -> None:
    """The property that distinguishes this from atrium's own PUT.

    ``PUT /api/admin/app-config/brand`` validates through ``BrandConfig``
    and fills unset fields from model defaults, so a partial payload
    silently resets ``logo_url`` to ``/logo.svg``. This command must not.
    """
    before = {
        "name": "Atrium Ddns",
        "preset": "dark-glass",
        "logo_url": "/brand/casa.svg",
        "support_email": "help@example.test",
    }
    after = merge_brand(before, "Casa DDNS", None)
    assert after["preset"] == "dark-glass"
    assert after["logo_url"] == "/brand/casa.svg"
    assert after["support_email"] == "help@example.test"
    assert after["name"] == "Casa DDNS"


def test_primary_colour_does_not_flatten_sibling_overrides() -> None:
    """`overrides` is nested; a top-level merge would replace the dict.

    This is the mutation the naive implementation makes — ``merged =
    {**before, "name": n, "overrides": {"primaryColor": c}}`` passes every
    other test in this file and loses ``defaultRadius`` here.
    """
    before = {
        "name": "Atrium Ddns",
        "overrides": {"primaryColor": "blue", "defaultRadius": "md"},
    }
    after = merge_brand(before, "Casa DDNS", "grape")
    assert after["overrides"] == {"primaryColor": "grape", "defaultRadius": "md"}


def test_none_colour_leaves_stored_overrides_untouched() -> None:
    """``None`` means *leave it*, which is not the same as writing "".

    Rendering "not asked for" and "asked for nothing" in one type is how
    a command ends up clearing a field it was never given.
    """
    before = {"name": "x", "overrides": {"primaryColor": "grape"}}
    after = merge_brand(before, "y", None)
    assert after["overrides"] == {"primaryColor": "grape"}


def test_does_not_mutate_the_row_it_was_given() -> None:
    before = {"name": "x", "overrides": {"primaryColor": "blue"}}
    merge_brand(before, "y", "grape")
    assert before == {"name": "x", "overrides": {"primaryColor": "blue"}}


def test_repeating_the_same_write_is_a_fixed_point() -> None:
    """What makes ``make seed-brand`` safe to run on every deploy.

    The command compares ``merged == before`` and skips the write; if
    merging were not idempotent that comparison would never settle and
    every deploy would churn the row and its audit trail.
    """
    once = merge_brand({"preset": "default"}, "Casa DDNS", "grape")
    twice = merge_brand(once, "Casa DDNS", "grape")
    assert once == twice


@pytest.mark.parametrize("stored_name", ["Atrium", "Atrium Ddns", ""])
def test_the_defaults_are_the_migration_s(stored_name: str) -> None:
    """0001_init seeds these exact values.

    If the two disagree, a fresh deploy and a re-seeded one end up with
    different names and only one of them matches the documentation. This
    asserts the constants rather than re-reading the migration, so a
    change to either side has to be made deliberately in both.
    """
    assert DEFAULT_NAME == "Atrium Ddns"
    assert DEFAULT_PRIMARY_COLOR == "blue"
    after = merge_brand({"name": stored_name}, DEFAULT_NAME, DEFAULT_PRIMARY_COLOR)
    assert after["name"] == "Atrium Ddns"
    assert after["overrides"] == {"primaryColor": "blue"}
