"""The ``atrium_ddns`` config namespace, described well enough to render.

Issue #73. The namespace has been served by
``GET /api/admin/app-config`` since #17 and no screen in the SPA could
reach it: atrium's shell has eleven hand-built admin sections, four of
which edit a config namespace, and every one of those four passes a
**string literal** to the namespace-parameterised mutation hook.
Nothing derives a namespace from what the admin API returns, so a
namespace no screen names is unreachable however completely the API
serves it.

The host closes that with ``registerSettingsGroup`` and three child
pages. This module is what those pages read.

Why the field list is derived and not typed out
-----------------------------------------------
The form needs, per field: the input type, the bounds, the default and
a sentence saying what it does. All four already exist — in
:class:`~atrium_ddns.worker_jobs.DdnsConfig`, which is the model
atrium's ``PUT /admin/app-config/atrium_ddns`` validates against. A
second copy in TypeScript would agree on the day it was written and
diverge the first time either copy was edited, and the divergence is
silent in the direction that matters: a form offering ``min=0`` for
``health_check_batch_size`` against a model that requires ``ge=1``
produces a 400 the operator cannot act on, and a form offering
``min=1`` against a model that later allows ``0`` quietly stops
offering a legal value.

So the descriptors below come out of ``DdnsConfig.model_json_schema()``
— the same schema pydantic validates the PUT with. There is no field
list in this module and none in the frontend.

The one thing that *is* typed out is the grouping
-------------------------------------------------
Which page a field appears on is a presentation decision and there is
nothing in the model to derive it from. :data:`FIELD_GROUPS` is that
decision, and it is written here rather than in the browser so the
exhaustiveness check is in the same language as the model.

A field in the model and in no group is **not dropped**. It lands in
:data:`UNGROUPED_KEY`, which renders as "assigned to no page yet" —
loudly, on the surface an operator is already looking at.
``tests/test_settings_schema.py`` asserts that group is empty, so the
gate catches it first and the UI is the backstop rather than the
notification. A silent drop here is the "artefact with no writer"
family pointed at a settings form: the field would exist, validate,
enforce, and be uneditable, which is precisely the state #73 was opened
about.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from .worker_jobs import CONFIG_NAMESPACE, DdnsConfig

#: Atrium's own permission, deliberately not a new ``atrium_ddns.*``
#: one. The form writes through atrium's
#: ``PUT /api/admin/app-config/{namespace}``, which is gated on exactly
#: this — so a host-specific permission would let a holder open a form
#: whose save button answers 403. One gate for the read, the schema and
#: the write.
APP_CONFIG_MANAGE_PERMISSION = "app_setting.manage"

#: Where a field the grouping forgot ends up. Never silently dropped.
UNGROUPED_KEY = "ungrouped"

#: Group key -> the fields on that page, in the order they render.
#:
#: Three pages, one per legacy surface #73 lists: ``/admin/rate-limits``,
#: ``/admin/health-checks/config``, and the retention settings that
#: replaced ``/admin/events/clear`` (``ui-parity.md`` §3.4).
#:
#: ``device_idle_window_days`` is the one field whose home is arguable:
#: it is the board's denominator, not a retention window. It sits on the
#: retention page under its own heading rather than in a fourth group of
#: one, and the heading says what it is — a field filed somewhere
#: defensible with a label that lies would be worse than either.
FIELD_GROUPS: dict[str, tuple[str, ...]] = {
    "rate-limits": (
        "rate_limit_per_minute",
        "rate_limit_event_retention_hours",
    ),
    "health-checks": (
        "health_check_enabled",
        "health_check_interval_minutes",
        "health_check_batch_size",
        "health_check_timeout_seconds",
        "health_check_concurrency",
        # #75's manual-trigger debounce, added to `DdnsConfig` on a
        # branch cut from the same base as this file. It arrived at the
        # rebase and the guard below caught it: an unassigned field
        # lands in `ungrouped`, and `test_settings_schema` refuses that
        # bucket being non-empty. This is what the derivation is for —
        # nobody had to notice.
        "health_check_manual_cooldown_seconds",
    ),
    "retention": (
        "event_retention_days",
        "prune_batch_size",
        "prune_max_batches",
        "device_idle_window_days",
    ),
}

#: Group key -> (label, the sentence under the heading). The labels are
#: the operator's words for the legacy pages these replace, so someone
#: who ran the old service recognises the surface.
GROUP_LABELS: dict[str, tuple[str, str]] = {
    "rate-limits": (
        "Rate limits",
        "The abuse control on /nic/update, which is reachable by anyone "
        "who can reach the service and is authenticated only by HTTP "
        "Basic. A device's own limit overrides the default below.",
    ),
    "health-checks": (
        "Health checks",
        "The scheduled resolution that fills the board's 'answered' "
        "station. It reads the zone's authoritative nameserver; it "
        "publishes nothing.",
    ),
    "retention": (
        "Retention",
        "How long the log and the limiter's own rows are kept, and how "
        "hard the scheduled prune is allowed to work in one tick.",
    ),
    UNGROUPED_KEY: (
        "Assigned to no page yet",
        "These settings exist in the model and this build assigns them "
        "to no page. They are shown here rather than dropped — an "
        "unreachable setting is what this whole surface was built to "
        "stop.",
    ),
}


class SettingFieldOut(BaseModel):
    """One editable field, with everything a form needs to render it.

    Every value here is read from ``DdnsConfig``'s JSON schema. Nothing
    on this model is a second opinion about the model.
    """

    name: str
    #: JSON-schema type. ``integer`` and ``number`` are different on
    #: purpose: ``health_check_timeout_seconds`` is a float and an input
    #: that rounds it to 5 has silently changed the operator's setting.
    type: Literal["integer", "number", "boolean", "string"]
    label: str
    #: The model's ``description``. Empty is a test failure, not a
    #: rendering decision — see the module docstring.
    help: str
    #: The model's default, i.e. what the field reads as with nothing
    #: stored. Shown beside the input so "inherit" has a number.
    default: Any
    #: ``None`` for an unbounded field. Rendered as *no minimum*, never
    #: as ``0`` — the four states rule applied to a bound.
    minimum: float | None
    maximum: float | None


class SettingGroupOut(BaseModel):
    key: str
    label: str
    blurb: str
    fields: list[SettingFieldOut]


class SettingsSchemaOut(BaseModel):
    """The whole namespace, grouped.

    ``namespace`` and ``write_path`` are here so the browser does not
    build the write URL out of a literal — the exact defect #73
    measured in atrium's own shell, where four call sites each spell
    their namespace by hand.
    """

    namespace: str
    #: The atrium endpoint the form PUTs to. Whole-namespace: atrium
    #: validates the payload against the model, so a partial body resets
    #: every field it omits to the model default. The form sends
    #: everything it read.
    write_path: str
    permission: str
    groups: list[SettingGroupOut]


def _field_descriptor(name: str, schema: dict[str, Any]) -> SettingFieldOut:
    """One property of ``DdnsConfig``'s JSON schema, as a form field."""
    # `anyOf` would appear for an optional field; DdnsConfig has none
    # today, and a `type` that is missing rather than wrong is the
    # honest failure. Refuse instead of guessing "string".
    field_type = schema.get("type")
    if field_type not in ("integer", "number", "boolean", "string"):
        raise ValueError(
            f"{CONFIG_NAMESPACE}.{name}: JSON-schema type {field_type!r} "
            f"is not one this form can render. Add a renderer before "
            f"adding the field."
        )
    return SettingFieldOut(
        name=name,
        type=field_type,
        # pydantic derives `title` from the field name
        # ("event_retention_days" -> "Event Retention Days"). Derived
        # rather than a hand-kept label table, for the reason the module
        # docstring gives about second copies.
        label=str(schema.get("title") or name),
        help=str(schema.get("description") or ""),
        default=schema.get("default"),
        minimum=schema.get("minimum"),
        maximum=schema.get("maximum"),
    )


def settings_schema() -> SettingsSchemaOut:
    """Describe every field of the namespace, grouped for the form.

    Reads ``DdnsConfig.model_json_schema()``, so the population is the
    model's own and cannot be a subset of it: anything the grouping does
    not claim is emitted under :data:`UNGROUPED_KEY` rather than left
    out.
    """
    schema = DdnsConfig.model_json_schema()
    properties: dict[str, Any] = schema.get("properties", {})

    claimed: set[str] = set()
    groups: list[SettingGroupOut] = []
    for key, names in FIELD_GROUPS.items():
        label, blurb = GROUP_LABELS[key]
        fields: list[SettingFieldOut] = []
        for name in names:
            if name not in properties:
                # A group naming a field the model dropped. Skipping it
                # silently would leave the page one input short with
                # nothing saying so.
                raise ValueError(
                    f"{CONFIG_NAMESPACE} settings group {key!r} names "
                    f"{name!r}, which is not a field of DdnsConfig"
                )
            claimed.add(name)
            fields.append(_field_descriptor(name, properties[name]))
        groups.append(
            SettingGroupOut(key=key, label=label, blurb=blurb, fields=fields)
        )

    leftover = [name for name in properties if name not in claimed]
    if leftover:
        label, blurb = GROUP_LABELS[UNGROUPED_KEY]
        groups.append(
            SettingGroupOut(
                key=UNGROUPED_KEY,
                label=label,
                blurb=blurb,
                fields=[
                    _field_descriptor(name, properties[name])
                    for name in leftover
                ],
            )
        )

    return SettingsSchemaOut(
        namespace=CONFIG_NAMESPACE,
        write_path=f"/admin/app-config/{CONFIG_NAMESPACE}",
        permission=APP_CONFIG_MANAGE_PERMISSION,
        groups=groups,
    )
