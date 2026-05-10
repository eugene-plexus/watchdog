"""Entrypoint: `python -m eugene_plexus_watchdog`."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .settings import load_settings
from .state import WatchdogState


def main() -> None:
    settings = load_settings()

    bootstrap_state = WatchdogState(settings.config_file)
    if not settings.safe_mode:
        bootstrap_state.load()

    # Watchdog port is fixed at 8079 in v0.1 so the UI ships with a
    # known target. If a future release makes it configurable the
    # value will move into watchdog.yaml — the pattern would mirror
    # how the body components handle their own bind ports.
    port = 8079
    log_level = "info"

    app = create_app(settings)
    uvicorn.run(app, host=settings.bind_host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
