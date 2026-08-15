"""Demo HTTP surface.

- ``GET /api/atrium_ddns/state`` — read-only, auth required.
- ``POST /api/atrium_ddns/bump`` — gated by ``atrium_ddns.write``,
  increments the demo counter and writes an audit row.

Replace these with your real routes. Atrium mounts every JSON route
under ``/api/...`` so the SPA owns un-prefixed URL space (atrium
issue #89); host routes follow the same contract. The auth
dependencies and the audit/notify helpers (imported from ``app.*``)
are the surface a host calls atrium through.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import require_perm
from app.auth.users import current_user
from app.db import get_session
from app.models.auth import User
from app.services.audit import record as record_audit

from .models import AtriumDdnsState

router = APIRouter(prefix="/api/atrium_ddns", tags=["atrium_ddns"])


class StateOut(BaseModel):
    message: str
    counter: int


async def _load_state(session: AsyncSession) -> AtriumDdnsState:
    state = (
        await session.execute(
            select(AtriumDdnsState).where(AtriumDdnsState.id == 1)
        )
    ).scalar_one_or_none()
    if state is None:
        raise RuntimeError(
            "atrium_ddns_state row id=1 missing — run the host alembic upgrade",
        )
    return state


@router.get("/state", response_model=StateOut)
async def get_state(
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StateOut:
    state = await _load_state(session)
    return StateOut(message=state.message, counter=state.counter)


@router.post("/bump", response_model=StateOut)
async def bump(
    user: User = Depends(require_perm("atrium_ddns.write")),
    session: AsyncSession = Depends(get_session),
) -> StateOut:
    state = await _load_state(session)
    before = state.counter
    state.counter += 1
    await record_audit(
        session,
        actor_user_id=user.id,
        entity="atrium_ddns_state",
        entity_id=state.id,
        action="bump",
        diff={"counter": {"before": before, "after": state.counter}},
    )
    await session.commit()
    return StateOut(message=state.message, counter=state.counter)
