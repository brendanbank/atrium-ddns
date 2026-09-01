"""One-shot CLI: write ``system.host_bundle_url`` so atrium picks up
the host frontend bundle on the next page load.

Run inside the api container after migrations + admin seeding:

    python -m atrium_ddns.scripts.seed_host_bundle /host/main.js
    python -m atrium_ddns.scripts.seed_host_bundle --show

Idempotent. Preserves other ``system`` fields by JSON-merging onto
whatever's already there.
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from app.db import get_engine, get_session_factory
from app.models.ops import AppSetting

NAMESPACE = "system"
KEY = "host_bundle_url"

USAGE = "usage: seed_host_bundle <url> | seed_host_bundle --show"


async def _dispose() -> None:
    """Close the pool before the loop does.

    ``app.db`` caches a process-global engine, and its pool keeps the
    aiomysql connection alive past the end of the coroutine. Without
    this, ``asyncio.run`` closes the loop first and aiomysql's
    ``Connection.__del__`` then tries to close a transport on it during
    garbage collection, printing

        Exception ignored in: <function Connection.__del__ ...>
        ...
        RuntimeError: Event loop is closed

    *after* the write has succeeded. Because it is raised in a
    finaliser Python swallows it, so the exit code stays 0 and nothing
    downstream notices — a happy path that ends in a traceback, in the
    one step whose failure would look exactly the same (#129).

    Every sibling one-shot under this package already did this; this
    module was the last that did not.
    """
    await get_engine().dispose()


async def _seed(url: str) -> None:
    try:
        await _seed_inner(url)
    finally:
        await _dispose()


async def _seed_inner(url: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        existing = (
            await session.execute(
                select(AppSetting).where(AppSetting.key == NAMESPACE)
            )
        ).scalar_one_or_none()
        merged = {**(existing.value if existing else {}), KEY: url}

        stmt = insert(AppSetting).values(key=NAMESPACE, value=merged)
        stmt = stmt.on_duplicate_key_update(value=merged)
        await session.execute(stmt)
        await session.commit()
        print(f"{NAMESPACE}.{KEY} set to {url!r}")


async def _show() -> None:
    """Print the stored value and write nothing.

    A read-only mode is what lets the shutdown path be asserted by a
    test: ``app_settings`` is a singleton row per compose project and
    ``conftest`` owns every write to it (``test_harness_guards.py``'s
    #117 census), so a guard that ran the seeding half would be the
    second writer of a row with exactly one owner.
    """
    try:
        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(
                    select(AppSetting).where(AppSetting.key == NAMESPACE)
                )
            ).scalar_one_or_none()
            value = (row.value or {}).get(KEY) if row is not None else None
            if value is None:
                print(
                    f"app_settings[{NAMESPACE!r}].{KEY}: unset — atrium serves "
                    f"its own bundle and the host UI never mounts"
                )
            else:
                print(f"app_settings[{NAMESPACE!r}].{KEY} = {value!r}")
    finally:
        await _dispose()


def main() -> None:
    argv = sys.argv[1:]
    if argv == ["--show"]:
        asyncio.run(_show())
        return
    if len(argv) != 1 or argv[0].startswith("-"):
        print(USAGE, file=sys.stderr)
        sys.exit(2)
    asyncio.run(_seed(argv[0]))


if __name__ == "__main__":
    main()
