"""Atrium host bootstrap entry points.

The atrium image imports this module on startup when the operator sets
``ATRIUM_HOST_MODULE=atrium_ddns.bootstrap``:

- ``init_app(app)`` runs once during ``create_app()`` — after every
  atrium router is included and before the ASGI app starts serving.
  We use it to mount our router; permissions are seeded by the alembic
  migration (``alembic/versions/0001_init.py``).
- ``init_worker(host)`` runs on worker startup, after atrium's
  built-in handlers register and before APScheduler starts. ``host``
  is a :class:`~app.host_sdk.worker.HostWorkerCtx` that exposes the
  APScheduler instance plus a typed ``register_job_handler`` for
  ``scheduled_jobs`` dispatch.

Both functions are optional — a module that defines neither is
allowed (atrium logs ``host.init_app.absent`` and continues). Delete
either when you don't need it.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.host_sdk.worker import HostWorkerCtx


def init_app(app: FastAPI) -> None:
    from .router import router

    app.include_router(router)


def init_worker(host: HostWorkerCtx) -> None:
    # Recurring APScheduler tick — fires inside the worker process,
    # NOT the api process. Use this for stateless idempotent work.
    #
    # from .schedule import tick
    # host.scheduler.add_job(
    #     tick, "interval", seconds=30,
    #     id="atrium_ddns-tick", coalesce=True, max_instances=1,
    # )

    # Durable scheduled_jobs handler — each row is claimed FOR UPDATE
    # SKIP LOCKED by exactly one worker, retried on failure.
    #
    # from .handlers import handle_thing
    # host.register_job_handler(
    #     kind="atrium_ddns.thing",
    #     handler=handle_thing,
    #     description="Drain atrium_ddns.thing scheduled_jobs rows",
    # )
    _ = host  # marker so type-checkers don't warn about the unused arg
