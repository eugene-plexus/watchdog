"""Pytest fixtures shared across the watchdog test suite.

Tests run with a no-op stub supervisor injected on `app.state.supervisor`
so the routes layer's spawn / restart / stop calls become observable
no-ops instead of trying to fork real Python processes for the body
components. End-to-end smoke tests against real children belong in a
separate harness, not the unit suite.

v0.2 adds auth-protected routes. Tests get an `authed_client` fixture
that initializes a passphrase, captures the resulting session token,
and attaches it to every request. Tests that exercise the auth surface
itself (login flow, rate limiting, token validation) use the bare
`client` fixture so they control auth themselves.
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

TEST_PASSPHRASE = "correct horse battery staple"


class StubSupervisor:
    """No-op stand-in for `Supervisor` used in unit tests.

    Records every call so tests can assert which lifecycle methods the
    routes layer invoked, without ever actually spawning a child."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.restart_all_returns: list[str] = []

    def add_and_start(self, entry: ComponentEntry) -> None:
        self.calls.append(("add_and_start", entry.name))

    async def remove_and_stop(self, name: str) -> None:
        self.calls.append(("remove_and_stop", name))

    async def restart(self, name: str) -> bool:
        self.calls.append(("restart", name))
        return True

    async def restart_all(self) -> list[str]:
        """Records the call so the restart-on-login tests can assert
        the auth route triggered it. Returns an empty list by default
        — tests that need a non-empty result set `restart_all_returns`."""
        self.calls.append(("restart_all", ""))
        return list(self.restart_all_returns)

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
    """Bare TestClient — no auth headers attached. Use for testing
    the auth surface itself (login, rate limiting, etc.)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def authed_client(app: FastAPI) -> Iterator[TestClient]:
    """TestClient with a pre-initialized passphrase and the resulting
    session token attached as the default Authorization header.
    Use this for testing any v0.2-protected route."""
    with TestClient(app) as c:
        resp = c.post("/v1/auth/initialize", json={"passphrase": TEST_PASSPHRASE})
        assert resp.status_code == 200, f"initialize failed: {resp.status_code} {resp.text}"
        token = resp.json()["sessionToken"]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
