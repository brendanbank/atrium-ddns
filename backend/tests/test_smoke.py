"""Smoke tests that don't need a running database.

The atrium runtime image strips ``pytest`` to keep the prod image
small; install it on demand with the ``test-backend`` Make target.
Tests here verify the package imports cleanly and the bootstrap
entry points have the right signatures — enough for CI to catch a
broken extension before it hits the api container.
"""
from __future__ import annotations

import inspect


def test_package_imports() -> None:
    import atrium_ddns  # noqa: F401


def test_bootstrap_signatures() -> None:
    from atrium_ddns.bootstrap import init_app, init_worker

    # Shape match against what atrium calls. The signatures are part
    # of the contract; if they drift, fail loudly here rather than on
    # api boot.
    assert list(inspect.signature(init_app).parameters) == ["app"]
    assert list(inspect.signature(init_worker).parameters) == ["host"]


def test_router_mounted() -> None:
    from atrium_ddns.router import router

    paths = {route.path for route in router.routes}
    assert "/api/atrium_ddns/state" in paths
    assert "/api/atrium_ddns/bump" in paths


def test_models_isolated_from_atrium_base() -> None:
    """HostBase must not share metadata with atrium's app.db.Base.

    Sharing would mean autogenerate sees atrium tables and proposes
    drop_table ops on every revision — the most painful failure
    mode of the host-extension model. Catch it here.
    """
    from app.db import Base as AtriumBase

    from atrium_ddns.models import HostBase

    assert HostBase.metadata is not AtriumBase.metadata
