"""The settings schema #73's form renders from.

The form is the first thing in this repository that can change
`atrium_ddns` configuration from a browser, and the failure mode it
introduces is not "the page looks wrong". It is a **number box whose
bounds disagree with the model's**, in either direction:

* looser than the model — the operator types a legal-looking value, the
  PUT answers 400, and the message is pydantic's rather than the form's;
* tighter than the model — a legal value silently stops being offered,
  and nothing anywhere says so.

Both are the same defect: a second copy of the model. So the descriptors
are derived from ``DdnsConfig.model_json_schema()`` and this file checks
the derivation against a **different reading of the same model** —
pydantic's per-field ``metadata`` (``annotated_types.Ge`` / ``Le``),
which is where the constraint is recorded before any schema is
generated. Two instruments of different shape over one source; a
hardcoded expected table would share an author with the thing it checks.

The population guard is the other half. A field added to ``DdnsConfig``
and to no group is the "artefact with no writer" family aimed at a
settings form — it would validate, enforce, and be uneditable, which is
the exact state #73 was opened about. :func:`settings_schema` puts such
a field in the ``ungrouped`` bucket rather than dropping it, and the
test below asserts that bucket is *empty*, so the gate names the field
before an operator has to find it on a page titled "assigned to no page
yet".
"""
from __future__ import annotations

from typing import Any

import annotated_types
import httpx
import pytest
from app.auth.principal import Principal
from app.auth.rbac import current_principal
from app.models.auth import User
from app.services.app_config import NAMESPACES
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

from atrium_ddns.router import router
from atrium_ddns.settings_schema import (
    APP_CONFIG_MANAGE_PERMISSION,
    FIELD_GROUPS,
    UNGROUPED_KEY,
    settings_schema,
)
from atrium_ddns.worker_jobs import CONFIG_NAMESPACE, DdnsConfig

pytestmark = pytest.mark.asyncio

SCHEMA_PATH = "/api/atrium_ddns/config/schema"


def _client(permissions: set[str]) -> httpx.AsyncClient:
    """An app carrying the host router and a principal with ``permissions``.

    No database: the schema endpoint declares no session and touches no
    row, which is itself part of what this file asserts (it is a
    description of a global namespace, identical for every caller).
    """
    application = FastAPI()
    application.include_router(router)
    user = User(
        id=1,
        email="settings-probe@example.invalid",
        hashed_password="x",
        is_active=True,
        is_verified=True,
    )

    async def _principal() -> Principal:
        return Principal(
            user=user,
            permissions=frozenset(permissions),
            auth_method="password",
            token_id=None,
            auth_session_id=None,
        )

    application.dependency_overrides[current_principal] = _principal
    return httpx.AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://settings.test",
    )


def _all_descriptors() -> dict[str, Any]:
    schema = settings_schema()
    out: dict[str, Any] = {}
    for group in schema.groups:
        for field in group.fields:
            assert field.name not in out, (
                f"{field.name} appears in more than one group — a field "
                f"edited on two pages has two last-writers"
            )
            out[field.name] = field
    return out


# ===================================================================== #
# 1. The population: every field of the model, on exactly one page
# ===================================================================== #


async def test_every_field_of_the_model_is_on_exactly_one_page():
    """Swept from the model's own field list to a negative result.

    ``DdnsConfig.model_fields`` is the population, not a list in this
    file — so a twelfth setting added tomorrow fails here rather than
    shipping unreachable.
    """
    expected = set(DdnsConfig.model_fields)
    rendered = set(_all_descriptors())
    assert rendered == expected, (
        f"the form renders {sorted(rendered - expected)} that the model "
        f"does not have, and omits {sorted(expected - rendered)} that it "
        f"does"
    )
    # Vacuity: twelve fields today — the eleven #73 was opened about,
    # plus #75's `health_check_manual_cooldown_seconds`, which arrived at
    # the rebase and was caught by the `ungrouped` guard below rather
    # than by anybody reading a diff. A model that lost every field would
    # satisfy the equality above.
    assert len(expected) == 12, (
        f"DdnsConfig now has {len(expected)} fields, not 12 — update the "
        f"grouping in settings_schema.FIELD_GROUPS and this count together"
    )


async def test_no_field_lands_in_the_ungrouped_bucket():
    """The bucket exists so nothing is dropped; it should stay empty.

    The bucket is the *backstop*, rendered on the surface an operator is
    already looking at. This assertion is the thing that means they
    never see it.
    """
    schema = settings_schema()
    ungrouped = [g for g in schema.groups if g.key == UNGROUPED_KEY]
    assert ungrouped == [], (
        f"unassigned settings: "
        f"{[f.name for g in ungrouped for f in g.fields]}. "
        f"Add each to a group in settings_schema.FIELD_GROUPS."
    )


async def test_every_field_carries_a_sentence_an_operator_can_act_on():
    """An empty ``description`` is a box labelled only by its own name.

    Cheap to satisfy and cheap to forget, which is why it is a test
    rather than a convention.
    """
    missing = [
        name
        for name, field in _all_descriptors().items()
        if len(field.help.strip()) < 20
    ]
    assert missing == [], (
        f"{missing} have no usable description on DdnsConfig. The form "
        f"renders `description` as the help text under the input."
    )


# ===================================================================== #
# 2. The bounds, read a second way
# ===================================================================== #


def _constraints_from_field_metadata(name: str) -> tuple[Any, Any]:
    """``(minimum, maximum)`` out of pydantic's per-field metadata.

    ``annotated_types.Ge`` / ``Le`` are what ``Field(ge=…, le=…)``
    records on the model *before* any JSON schema is generated, so this
    is a genuinely different reading from the one the descriptors are
    built out of — not the same query twice.
    """
    minimum = maximum = None
    for constraint in DdnsConfig.model_fields[name].metadata:
        if isinstance(constraint, annotated_types.Ge):
            minimum = constraint.ge
        if isinstance(constraint, annotated_types.Le):
            maximum = constraint.le
    return minimum, maximum


async def test_the_bounds_the_form_offers_are_the_bounds_the_model_holds():
    disagreements: list[str] = []
    for name, field in _all_descriptors().items():
        minimum, maximum = _constraints_from_field_metadata(name)
        if (field.minimum, field.maximum) != (minimum, maximum):
            disagreements.append(
                f"{name}: form offers [{field.minimum}, {field.maximum}], "
                f"model holds [{minimum}, {maximum}]"
            )
    assert disagreements == [], disagreements
    # Vacuity: at least one field has to actually carry a bound, or the
    # sweep above compares None to None for every field and passes.
    bounded = [f for f in _all_descriptors().values() if f.minimum is not None]
    assert len(bounded) >= 10, f"only {len(bounded)} fields carry a minimum"


async def test_the_model_refuses_one_step_below_every_minimum():
    """AC 2's example, generalised: *"a form that lets an operator write
    ``0`` into ``health_check_batch_size`` has replaced a bad UX with an
    outage"*.

    The form's ``min`` is only worth anything if the server refuses the
    value below it — otherwise the browser is the only control and a
    ``curl`` walks straight past it. Swept over every bounded field
    rather than asserted for the one the issue named.
    """
    refused: list[str] = []
    for name, field in _all_descriptors().items():
        if field.minimum is None or field.type == "boolean":
            continue
        below = field.minimum - (1 if field.type == "integer" else 0.05)
        try:
            DdnsConfig.model_validate({name: below})
        except ValidationError:
            refused.append(name)
    expected = [
        name
        for name, field in _all_descriptors().items()
        if field.minimum is not None and field.type != "boolean"
    ]
    assert sorted(refused) == sorted(expected), (
        f"these fields accept a value below the minimum the form offers: "
        f"{sorted(set(expected) - set(refused))}"
    )
    assert "health_check_batch_size" in refused, (
        "the field the issue named by hand is not covered by the sweep"
    )


async def test_the_float_field_is_not_rendered_as_an_integer():
    """``health_check_timeout_seconds`` is a float and an input that
    rounds it to 5 has silently changed the operator's setting."""
    fields = _all_descriptors()
    assert fields["health_check_timeout_seconds"].type == "number"
    assert fields["health_check_enabled"].type == "boolean"
    assert fields["health_check_batch_size"].type == "integer"


# ===================================================================== #
# 3. The write path, and the gate on all three of them
# ===================================================================== #


async def test_the_write_path_names_a_namespace_atrium_validates():
    """The form PUTs to ``/admin/app-config/atrium_ddns``. That endpoint
    404s for a namespace atrium has never heard of, and the registration
    happens at import time in ``worker_jobs`` — so a rename on either
    side is a form whose save button cannot work."""
    schema = settings_schema()
    assert schema.namespace == CONFIG_NAMESPACE
    assert schema.write_path == f"/admin/app-config/{CONFIG_NAMESPACE}"
    registered = NAMESPACES.get(CONFIG_NAMESPACE)
    assert registered is not None, (
        f"{CONFIG_NAMESPACE} is not in app_config.NAMESPACES; the PUT the "
        f"form makes would answer 404"
    )
    assert registered.model is DdnsConfig
    # The namespace is admin-only. A public one would be in the
    # unauthenticated boot bundle, and the retention/limit knobs are not
    # something to hand an anonymous caller.
    assert registered.public is False


async def test_the_schema_endpoint_is_gated_on_atriums_own_permission():
    """One gate for the read, the schema and the write.

    A host-specific permission here would open a form whose save button
    answers 403 — which reads as broken rather than as refused.
    """
    async with _client({APP_CONFIG_MANAGE_PERMISSION}) as allowed:
        response = await allowed.get(SCHEMA_PATH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["permission"] == APP_CONFIG_MANAGE_PERMISSION
    assert [group["key"] for group in body["groups"]] == list(FIELD_GROUPS)

    # The gate bites, and it bites on the *right* permission: a caller
    # holding every `atrium_ddns.*` permission there is still cannot
    # read this.
    async with _client(
        {
            "atrium_ddns.admin",
            "atrium_ddns.write",
            "atrium_ddns.device.manage",
            "atrium_ddns.domain.manage",
            "atrium_ddns.hostname.manage",
        }
    ) as refused:
        denied = await refused.get(SCHEMA_PATH)
    assert denied.status_code == 403, denied.text
    assert APP_CONFIG_MANAGE_PERMISSION in denied.text


async def test_the_served_document_carries_every_field_over_the_wire():
    """The endpoint, not the function — the JSON a browser receives.

    ``settings_schema()`` returning every field says nothing about
    what survives response-model serialisation.
    """
    async with _client({APP_CONFIG_MANAGE_PERMISSION}) as client:
        body = (await client.get(SCHEMA_PATH)).json()
    names = [
        field["name"] for group in body["groups"] for field in group["fields"]
    ]
    assert sorted(names) == sorted(DdnsConfig.model_fields)
    by_name = {
        field["name"]: field
        for group in body["groups"]
        for field in group["fields"]
    }
    # Defaults travel too — the form shows "inherit means 30", and a
    # null default would make that sentence unwritable.
    assert by_name["rate_limit_per_minute"]["default"] == 30
    assert by_name["health_check_enabled"]["default"] is True
    assert all(field["default"] is not None for field in by_name.values())
