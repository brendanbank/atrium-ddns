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

The ``atrium_ddns`` config namespace, and where it is registered
----------------------------------------------------------------
Not inside ``init_app``. This module is imported by **both** atrium
processes (``app/main.py`` for the api, ``app/worker.py`` for the
worker) but only the api calls ``init_app``, and
``app.services.app_config.NAMESPACES`` is a process-global dict — so a
namespace registered in ``init_app`` is present in the api and
**absent in the worker**, and every ``get_namespace(session,
"atrium_ddns")`` from a worker job raises ``KeyError`` on a scheduler
tick in the process nobody is tailing.

The ``register_namespace`` call therefore runs at import time. It
lives in :mod:`atrium_ddns.worker_jobs` rather than here, one step
further than the issue asked for and for the same reason: registering
here works only for a process that imported *this* module, and nothing
about importing ``worker_jobs`` requires that. Putting the call in the
module that reads the key makes the failure structurally impossible.
The ``from .worker_jobs import …`` below is what keeps atrium's own
import path — this module — registering it in both processes.
"""
from __future__ import annotations

from app.host_sdk.worker import HostWorkerCtx
from app.logging import log
from fastapi import FastAPI

# Top-level, and load-bearing: importing this module must register the
# host's config namespace, because ``ATRIUM_HOST_MODULE`` names this
# module and nothing else. Not to be moved inside a function.
from .worker_jobs import register_jobs


def init_app(app: FastAPI) -> None:
    from .router import router

    app.include_router(router)


def init_worker(host: HostWorkerCtx) -> None:
    """Register the host's scheduled jobs. Does no IO.

    ``app/worker.py`` calls this outside any ``try``/``except`` and
    *before* ``scheduler.start()``, so anything that raises here takes
    the whole worker process down at startup with nothing to retry it.
    :func:`~atrium_ddns.worker_jobs.register_jobs` therefore only adds
    jobs; every configuration read happens inside a job body, on a tick,
    behind :func:`~atrium_ddns.worker_jobs.guarded`.
    """
    registered = register_jobs(host.scheduler)
    log.info("atrium_ddns.init_worker.jobs_registered", jobs=registered)
