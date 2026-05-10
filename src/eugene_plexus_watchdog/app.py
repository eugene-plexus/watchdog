"""FastAPI app factory.

The supervisor is wired into the lifespan: at startup the watchdog reads
its topology config and asks the supervisor to spawn every spawned-mode
child; on shutdown it stops them in turn (SIGTERM with timeout, then
SIGKILL). The /v1/components routes layer delegates real-time status
queries to the supervisor so `Component.status` reflects live process
state instead of the skeleton's hard-coded `unreachable`.
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
from .supervisor import Supervisor

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

    # Supervisor injection: tests can pre-populate `app.state.supervisor`
    # with a stub before the lifespan runs (mirroring the orchestrator's
    # pattern with hemisphere clients and memory). Production builds the
    # real one here and tears it down on shutdown.
    if not hasattr(app.state, "supervisor"):
        supervisor = Supervisor(log=log)
        owns_supervisor = True
    else:
        supervisor = app.state.supervisor
        owns_supervisor = False
    app.state.supervisor = supervisor

    if not settings.safe_mode and owns_supervisor:
        for entry in state.list_topology_entries():
            supervisor.add_and_start(entry)
        await supervisor.start_health_loop(state.list_topology_entries)

    try:
        yield
    finally:
        if owns_supervisor:
            await supervisor.stop_all()


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
