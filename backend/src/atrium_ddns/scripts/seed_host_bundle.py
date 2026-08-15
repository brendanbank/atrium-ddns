"""One-shot CLI: write ``system.host_bundle_url`` so atrium picks up
the host frontend bundle on the next page load.

Run inside the api container after migrations + admin seeding:

    python -m atrium_ddns.scripts.seed_host_bundle /host/main.js

Idempotent. Preserves other ``system`` fields by JSON-merging onto
whatever's already there.
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from app.db import get_session_factory
from app.models.ops import AppSetting


async def _seed(url: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        existing = (
            await session.execute(
                select(AppSetting).where(AppSetting.key == "system")
            )
        ).scalar_one_or_none()
        merged = {**(existing.value if existing else {}), "host_bundle_url": url}

        stmt = insert(AppSetting).values(key="system", value=merged)
        stmt = stmt.on_duplicate_key_update(value=merged)
        await session.execute(stmt)
        await session.commit()
        print(f"system.host_bundle_url set to {url!r}")


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: seed_host_bundle <url>", file=sys.stderr)
        sys.exit(2)
    asyncio.run(_seed(sys.argv[1]))


if __name__ == "__main__":
    main()
