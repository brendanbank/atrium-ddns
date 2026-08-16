"""Declarative branding: write the ``brand`` app-config namespace from
configuration, so the SPA's name is reproducible rather than typed into a
deployed database by hand.

    python -m atrium_ddns.scripts.seed_brand
    python -m atrium_ddns.scripts.seed_brand --name "Atrium Ddns" --primary-color blue

Run it from ``make seed-brand``, beside ``make seed-bundle``. Both are
promotion steps: a merged file that nothing points at does not run, and a
brand name nothing writes does not show.

Why this exists when ``0001_init`` already seeds a brand row
-----------------------------------------------------------

``0001_init`` upserts ``app_settings['brand']`` and is the reason a *fresh*
deploy already comes up named "Atrium Ddns" rather than "Atrium". It is not
enough on its own, for one reason that only shows up the second time:

**An alembic revision runs once per database.** Correcting the name after
``0001_init`` has been stamped means either editing an applied revision --
which never re-runs, so the correction reaches new databases and no existing
one -- or an ``UPDATE`` typed at the deployed database, which is exactly what
the issue asked not to do. This module is re-runnable, so the declared value
and the stored value can be brought back together at any point in a
deployment's life, by the same command that established them.

Merge, not replace
------------------

The row is JSON-merged, mirroring ``seed_host_bundle``. Two consequences,
both deliberate:

* Fields this command does not name -- ``logo_url``, ``support_email``, and
  any ``overrides`` key other than the primary colour -- survive. An
  operator's edits in atrium's Branding admin tab are not collateral.
* Atrium's own ``PUT /api/admin/app-config/brand`` is a **full replace**:
  it validates the payload through ``BrandConfig`` and unset fields fall
  back to model defaults. Sending ``{"name": ...}`` there silently resets
  ``logo_url`` to ``/logo.svg``. That is the API's contract, not a bug, and
  it is why this command writes the row rather than calling that endpoint.

What it does not reach
----------------------

``frontend/index.html`` in the atrium image carries a literal
``<title>Atrium</title>``. Atrium's ``ThemedApp`` overwrites
``document.title`` from ``brand.name`` once ``/api/app-config`` resolves, so
a browser settles on the configured name -- but the bytes the server sends
say "Atrium", and no host-side configuration surface can change them. That
is atrium's static asset, not a KV value. Named here so the next reader does
not go looking for the setting that would fix it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from app.db import get_engine, get_session_factory
from app.models.ops import AppSetting

#: The namespace atrium reads branding from. Not configurable -- it is
#: atrium's, declared by ``register_namespace("brand", BrandConfig)``.
NAMESPACE = "brand"

#: Environment fallbacks, so a deployment declares its branding in the same
#: ``.env`` that carries its ports and secrets rather than in an argument
#: somebody has to remember to pass. ``--name`` still wins, so a one-off
#: correction does not need an edit-and-restart cycle.
NAME_ENV = "BRAND_NAME"
PRIMARY_COLOR_ENV = "BRAND_PRIMARY_COLOR"

#: Matches ``0001_init``'s seed. Kept identical on purpose: the migration and
#: this command must not disagree about what an unconfigured deployment is
#: called, or a fresh deploy and a re-seeded one end up with different names.
DEFAULT_NAME = "Atrium Ddns"
DEFAULT_PRIMARY_COLOR = "blue"


def merge_brand(
    before: dict[str, object],
    name: str,
    primary_color: str | None,
) -> dict[str, object]:
    """The whole policy of this command, as a pure function.

    Kept separate from the write so it can be tested. The alternative —
    a test that seeds ``app_settings['brand']`` and reads it back — cannot
    be made parallel-safe: the backend suite runs ``-n auto`` over one
    MySQL and ``brand`` is a **singleton row**, so there is no per-worker
    name to derive. A test that wrote it would fail as flakiness in
    whichever worker lost the race.

    ``before`` is the stored row (``{}`` when absent). ``primary_color``
    of ``None`` means *leave whatever is stored*, which is not the same
    as writing an empty colour.
    """
    merged: dict[str, object] = {**before, "name": name}
    if primary_color is not None:
        # `overrides` is a nested dict. A top-level merge would replace it
        # wholesale and drop every other Mantine token the operator set.
        overrides = dict(before.get("overrides") or {})  # type: ignore[arg-type]
        overrides["primaryColor"] = primary_color
        merged["overrides"] = overrides
    return merged


async def _dispose() -> None:
    """Close the pool before the loop does.

    Without this, aiomysql's ``Connection.__del__`` runs after
    ``asyncio.run`` has closed the loop and prints a multi-line
    ``RuntimeError: Event loop is closed`` traceback *after* a successful
    write. A command whose happy path ends in a traceback is a command
    whose next real failure gets ignored. (``seed_host_bundle`` has the
    same noise; it is that module's to fix.)
    """
    await get_engine().dispose()


async def _seed(name: str, primary_color: str | None) -> int:
    try:
        return await _seed_inner(name, primary_color)
    finally:
        await _dispose()


async def _seed_inner(name: str, primary_color: str | None) -> int:
    factory = get_session_factory()
    async with factory() as session:
        existing = (
            await session.execute(
                select(AppSetting).where(AppSetting.key == NAMESPACE)
            )
        ).scalar_one_or_none()

        before = dict(existing.value) if existing else {}
        merged = merge_brand(before, name, primary_color)

        if merged == before:
            print(
                f"brand.name already {name!r} — nothing to write "
                f"(this command is idempotent)"
            )
            return 0

        stmt = insert(AppSetting).values(key=NAMESPACE, value=merged)
        stmt = stmt.on_duplicate_key_update(value=merged)
        await session.execute(stmt)
        await session.commit()

        was = before.get("name")
        print(
            f"brand.name {was!r} -> {name!r}"
            if was is not None
            else f"brand.name set to {name!r} (no row existed)"
        )
        if primary_color is not None:
            print(f"brand.overrides.primaryColor set to {primary_color!r}")
        print(f"preserved alongside: {sorted(set(before) - {'name', 'overrides'})}")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="seed_brand",
        description="Write app_settings['brand'] from configuration.",
    )
    parser.add_argument(
        "--name",
        default=os.environ.get(NAME_ENV) or DEFAULT_NAME,
        help=f"SPA name; defaults to ${NAME_ENV}, then {DEFAULT_NAME!r}",
    )
    parser.add_argument(
        "--primary-color",
        default=os.environ.get(PRIMARY_COLOR_ENV) or DEFAULT_PRIMARY_COLOR,
        help=(
            f"Mantine primary colour; defaults to ${PRIMARY_COLOR_ENV}, "
            f"then {DEFAULT_PRIMARY_COLOR!r}. Pass an empty string to leave "
            f"whatever is stored untouched."
        ),
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the stored row and exit without writing",
    )
    args = parser.parse_args()

    if args.show:
        asyncio.run(_show())
        return

    name = args.name.strip()
    if not name:
        # An empty name is not a rename to nothing: atrium's ThemedApp skips
        # the assignment on a blank value, so the tab would silently keep the
        # image's literal and this command would report success.
        print(
            f"refusing to write an empty brand.name — atrium ignores a blank "
            f"value, so this would report success and change nothing. Set "
            f"${NAME_ENV} or pass --name.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    color = args.primary_color.strip() or None
    raise SystemExit(asyncio.run(_seed(name, color)))


async def _show() -> None:
    try:
        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(
                    select(AppSetting).where(AppSetting.key == NAMESPACE)
                )
            ).scalar_one_or_none()
            if row is None:
                print(
                    f"app_settings[{NAMESPACE!r}]: no row — "
                    f"atrium serves BrandConfig() defaults"
                )
            else:
                print(
                    f"app_settings[{NAMESPACE!r}] = "
                    f"{json.dumps(row.value, sort_keys=True)}"
                )
    finally:
        await _dispose()


if __name__ == "__main__":
    main()
