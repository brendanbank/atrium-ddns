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
    """Mount the host's routes. Called once, before the SPA catch-all.

    Order matters and atrium's ``create_app`` already gets it right:
    ``init_app`` runs before ``app.mount("/", SPAStaticFiles(...))``,
    and Starlette matches routes in registration order — so ``/nic/*``
    is reached rather than being swallowed by a catch-all that matches
    every path *and every method*. That last part is why
    :mod:`atrium_ddns.router_nic` declares its ``HEAD`` refusals by
    hand: a ``Mount`` full-matches a ``HEAD`` the ``GET`` route only
    partially matched, so the 405 the framework would have produced
    never reaches the wire (measured on ``atrium:0.28``: 405 bare,
    404 behind the mount — see
    ``tests/test_router_nic.py::test_the_head_refusal_does_not_fall_out_of_the_framework``).

    ``/nic/*`` is deliberately **not** under ``/api``. Atrium's rule is
    that host JSON routes live there so the SPA owns the un-prefixed
    space (atrium #89); these three are not JSON routes and not ours to
    move — the path is the compatibility contract, configured into
    every router in the field.
    """
    from .router import router
    from .router_nic import router as nic_router

    app.include_router(router)
    app.include_router(nic_router)

    # Fixture-only, refused in production, and gated twice. Imported
    # here rather than at module top level so the api process pays for
    # it and the worker does not; registering a provider is a
    # process-global dict write and the worker resolves providers too
    # (#17's health check), so this is the narrower of the two.
    from .compat_stub import register_stub_providers

    register_stub_providers()


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
