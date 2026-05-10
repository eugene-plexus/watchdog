"""Pytest fixtures shared across the watchdog test suite.

Tests run with a no-op stub supervisor injected on `app.state.supervisor`
so the routes layer's spawn / restart / stop calls become observable
no-ops instead of trying to fork real Python processes for the body
components. End-to-end smoke tests against real children belong in a
separate harness, not the unit suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eugene_plexus_watchdog._generated.models import ComponentEntry, ComponentStatus
from eugene_plexus_watchdog.app import create_app
from eugene_plexus_watchdog.settings import Settings


class StubSupervisor:
    """No-op stand-in for `Supervisor` used in unit tests.

    Records every call so tests can assert which lifecycle methods the
    routes layer invoked, without ever actually spawning a child."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def add_and_start(self, entry: ComponentEntry) -> None:
        self.calls.append(("add_and_start", entry.name))

    async def remove_and_stop(self, name: str) -> None:
        self.calls.append(("remove_and_stop", name))

    async def restart(self, name: str) -> bool:
        self.calls.append(("restart", name))
        return True

    async def start_health_loop(self, _get_components: Any) -> None:
        self.calls.append(("start_health_loop", ""))

    async def stop_all(self) -> None:
        self.calls.append(("stop_all", ""))

    def status_for(
        self, _name: str, *, has_spawn: bool
    ) -> tuple[ComponentStatus, str | None, datetime | None, int | None]:
        # Mirrors the real supervisor's "no info available" answer so
        # tests see deterministic placeholder status.
        return ComponentStatus.unreachable, None, None, None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(config_file=tmp_path / "watchdog.yaml")


@pytest.fixture
def stub_supervisor() -> StubSupervisor:
    return StubSupervisor()


@pytest.fixture
def app(settings: Settings, stub_supervisor: StubSupervisor) -> FastAPI:
    app = create_app(settings=settings)
    app.state.supervisor = stub_supervisor
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
