"""FastAPI app factory.

Skeleton scope: implements every spec endpoint with a working in-memory
+ on-disk state, but does NOT yet spawn subprocesses. The supervisor
loop is the obvious next commit; the wire shape is here so the UI's
first-run wizard and the ConfigEditor's Components tab can be built
against this in parallel.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .routes import components as components_routes
from .routes import config as config_routes
from .routes import health as health_routes
from .settings import Settings, load_settings
from .state import WatchdogState

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    state = WatchdogState(settings.config_file)
    if settings.safe_mode:
        log.warning(
            "starting in SAFE MODE (EUGENE_PLEXUS_WATCHDOG_SAFE_MODE=1); ignoring "
            "%s and running on defaults. Fix watchdog state via /v1/config or "
            "/v1/components, then restart without the env var.",
            settings.config_file,
        )
    else:
        state.load()
    app.state.watchdog_state = state
    app.state.safe_mode = settings.safe_mode
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI app with all routers mounted."""
    settings = settings or load_settings()

    app = FastAPI(
        title="Eugene Plexus — watchdog",
        description="Process supervisor and UI host for an Eugene Plexus install.",
        version=__version__,
        lifespan=_lifespan,
    )
    app.state.settings = settings

    app.include_router(health_routes.router)
    app.include_router(config_routes.router)
    app.include_router(components_routes.router)

    return app
