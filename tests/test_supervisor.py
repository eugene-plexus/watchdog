"""Tests for `Supervisor` and `SupervisedProcess`.

We monkeypatch `asyncio.create_subprocess_exec` to return a controllable
fake process, so the supervision loop runs without actually forking
anything. Verifies the contract pieces that matter:

  - Right command (sys.executable -m <module-for-kind>)
  - Right env vars (config_file, bind_port, safe_mode), correctly
    prefixed per kind
  - Restart-on-exit: the loop respawns after the fake process "exits"
  - Stop: terminates and cleans up the supervision task
"""

from __future__ import annotations

import asyncio
import base64
import logging
import sys
from typing import Any

import pytest

from eugene_plexus_watchdog import security
from eugene_plexus_watchdog._generated.models import (
    ComponentEntry,
    ComponentKind,
    ComponentStatus,
    SpawnConfig,
)
from eugene_plexus_watchdog.auth_state import AuthState
from eugene_plexus_watchdog.supervisor import (
    SupervisedProcess,
    Supervisor,
    _colorize_alerts,
    _HEALTHZ_2XX_LINE,
)


class _FakeProcess:
    """Stand-in for `asyncio.subprocess.Process`. Tests drive
    `_finish(returncode)` to simulate the child exiting."""

    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._exit_event = asyncio.Event()
        # Supervisor pipes the child's stdout (with stderr merged in) and
        # spawns a reader task. None here makes the reader a no-op —
        # tests get the supervision-loop behavior without exercising the
        # output prefixing path.
        self.stdout: asyncio.StreamReader | None = None

    async def wait(self) -> int:
        await self._exit_event.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self._finish(0)

    def kill(self) -> None:
        self.killed = True
        self._finish(-9)

    def _finish(self, returncode: int) -> None:
        self.returncode = returncode
        self._exit_event.set()


@pytest.fixture
def driver_entry() -> ComponentEntry:
    return ComponentEntry(
        name="left",
        kind=ComponentKind.hemisphere_driver,
        url="http://127.0.0.1:8081",  # type: ignore[arg-type]
        spawn=SpawnConfig(configFile="/tmp/left/config.yaml"),
        safeMode=False,
    )


async def test_spawn_invokes_correct_command_and_env(
    driver_entry: ComponentEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    sp = SupervisedProcess(driver_entry, logging.getLogger("test"))
    sp.start()

    # Give the supervision loop one tick to spawn.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if "args" in captured:
            break

    await sp.stop()

    assert "args" in captured, "subprocess was never invoked"
    assert captured["args"][0] == sys.executable
    assert captured["args"][1] == "-m"
    assert captured["args"][2] == "eugene_plexus_hemisphere_driver"

    env = captured["env"]
    assert env["EUGENE_PLEXUS_HD_CONFIG_FILE"] == "/tmp/left/config.yaml"
    assert env["EUGENE_PLEXUS_HD_BIND_PORT"] == "8081"
    assert env["EUGENE_PLEXUS_HD_SAFE_MODE"] == "0"


async def test_safe_mode_threads_env_var(
    driver_entry: ComponentEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> _FakeProcess:
        captured["env"] = kwargs.get("env")
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    safe_entry = driver_entry.model_copy(update={"safeMode": True})
    sp = SupervisedProcess(safe_entry, logging.getLogger("test"))
    sp.start()

    for _ in range(50):
        await asyncio.sleep(0.01)
        if "env" in captured:
            break
    await sp.stop()

    assert captured["env"]["EUGENE_PLEXUS_HD_SAFE_MODE"] == "1"


async def test_clean_exit_triggers_respawn(
    driver_entry: ComponentEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn_count = 0
    processes: list[_FakeProcess] = []

    async def fake_create(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        nonlocal spawn_count
        spawn_count += 1
        proc = _FakeProcess()
        processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    sp = SupervisedProcess(driver_entry, logging.getLogger("test"))
    sp.start()

    # Wait for first spawn, then simulate clean exit.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if spawn_count >= 1:
            break
    assert spawn_count == 1
    processes[0]._finish(0)

    # Wait for respawn (the loop sleeps ~0s for clean exits since
    # consecutive_crashes is 0).
    for _ in range(100):
        await asyncio.sleep(0.01)
        if spawn_count >= 2:
            break
    assert spawn_count >= 2, "supervisor did not respawn after clean exit"

    await sp.stop()


async def test_supervisor_stop_all_terminates_each_process(
    driver_entry: ComponentEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[_FakeProcess] = []

    async def fake_create(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        proc = _FakeProcess()
        processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    sup = Supervisor(log=logging.getLogger("test"))
    sup.add_and_start(driver_entry)

    for _ in range(50):
        await asyncio.sleep(0.01)
        if processes:
            break

    await sup.stop_all()

    assert processes[0].terminated, "stop_all should terminate every process"


async def test_remote_entry_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned = False

    async def fake_create(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        nonlocal spawned
        spawned = True
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    remote_entry = ComponentEntry(
        name="memory",
        kind=ComponentKind.memory,
        url="http://memory.lan:8083",  # type: ignore[arg-type]
        # No spawn block => remote, watchdog must not try to launch it.
        safeMode=False,
    )
    sup = Supervisor(log=logging.getLogger("test"))
    sup.add_and_start(remote_entry)

    await asyncio.sleep(0.05)
    await sup.stop_all()

    assert spawned is False
    # Status is reported as `unreachable` because no health probe ran.
    status, _, _, pid = sup.status_for("memory", has_spawn=False)
    assert status == ComponentStatus.unreachable
    assert pid is None


# --------------------------------------------------------------------------- #
# v0.2 auth env-var threading
# --------------------------------------------------------------------------- #


async def test_auth_state_threads_signing_key_and_service_token(
    driver_entry: ComponentEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When an AuthState is wired in, every spawned child receives the
    base64'd JWT signing key plus a freshly-issued service token bound
    to the component's kind."""
    captured: dict[str, Any] = {}

    async def fake_create(*_args: Any, **kwargs: Any) -> _FakeProcess:
        captured["env"] = kwargs.get("env")
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    auth = AuthState(signing_key=security.generate_signing_key())
    sp = SupervisedProcess(driver_entry, logging.getLogger("test"), auth_state=auth)
    sp.start()

    for _ in range(50):
        await asyncio.sleep(0.01)
        if "env" in captured:
            break
    await sp.stop()

    env = captured["env"]
    # Signing key is the shared base64-encoded HMAC key.
    assert (
        base64.b64decode(env["EUGENE_PLEXUS_HD_AUTH_SIGNING_KEY"]) == auth.signing_key
    )
    # Service token must validate against the same signing key with the
    # correct service audience.
    payload = security.decode_token(
        token=env["EUGENE_PLEXUS_HD_SERVICE_TOKEN"],
        signing_key=auth.signing_key,
        expected_audience="service:hemisphere-driver",
    )
    assert payload.sub == "hemisphere-driver"
    # Master key absent because the operator hasn't logged in yet.
    assert "EUGENE_PLEXUS_HD_MASTER_KEY" not in env


async def test_master_key_threaded_after_login(
    driver_entry: ComponentEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once `AuthState.master_key` is populated (i.e. operator has
    logged in), subsequent spawns carry the base64'd master key so
    children can decrypt at-rest secrets."""
    captured: dict[str, Any] = {}

    async def fake_create(*_args: Any, **kwargs: Any) -> _FakeProcess:
        captured["env"] = kwargs.get("env")
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    auth = AuthState(signing_key=security.generate_signing_key())
    auth.set_master_key(b"\x55" * 32)

    sp = SupervisedProcess(driver_entry, logging.getLogger("test"), auth_state=auth)
    sp.start()

    for _ in range(50):
        await asyncio.sleep(0.01)
        if "env" in captured:
            break
    await sp.stop()

    env = captured["env"]
    assert (
        base64.b64decode(env["EUGENE_PLEXUS_HD_MASTER_KEY"]) == auth.master_key
    )


async def test_orchestrator_and_memory_kinds_get_correct_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service-token audience + env-var prefix must follow the kind.

    Exercises every spawnable kind so a future enum addition is caught
    by an obvious mismatch in this test rather than by a silent
    unsupported-kind KeyError at spawn time.
    """
    captured: dict[str, dict[str, str]] = {}

    async def fake_create(*_args: Any, **kwargs: Any) -> _FakeProcess:
        # Tag captures by the kind we expect (read off CONFIG_FILE).
        env = kwargs.get("env") or {}
        for kind_prefix in ("ORCH", "HD", "MEM", "IDENTITY", "CONNECTOR"):
            if f"EUGENE_PLEXUS_{kind_prefix}_CONFIG_FILE" in env:
                captured[kind_prefix] = env
                break
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    auth = AuthState(signing_key=security.generate_signing_key())
    procs: list[SupervisedProcess] = []
    for kind, prefix, port in [
        (ComponentKind.orchestrator, "ORCH", 8080),
        (ComponentKind.memory, "MEM", 8083),
        (ComponentKind.identity, "IDENTITY", 8084),
        (ComponentKind.connector, "CONNECTOR", 8085),
    ]:
        entry = ComponentEntry(
            name=prefix.lower(),
            kind=kind,
            url=f"http://127.0.0.1:{port}",  # type: ignore[arg-type]
            spawn=SpawnConfig(configFile=f"/tmp/{prefix}/config.yaml"),
            safeMode=False,
        )
        sp = SupervisedProcess(entry, logging.getLogger("test"), auth_state=auth)
        sp.start()
        procs.append(sp)

    for _ in range(100):
        await asyncio.sleep(0.01)
        if {"ORCH", "MEM", "IDENTITY", "CONNECTOR"}.issubset(captured.keys()):
            break

    for sp in procs:
        await sp.stop()

    payload_orch = security.decode_token(
        token=captured["ORCH"]["EUGENE_PLEXUS_ORCH_SERVICE_TOKEN"],
        signing_key=auth.signing_key,
        expected_audience="service:orchestrator",
    )
    assert payload_orch.sub == "orchestrator"
    payload_mem = security.decode_token(
        token=captured["MEM"]["EUGENE_PLEXUS_MEM_SERVICE_TOKEN"],
        signing_key=auth.signing_key,
        expected_audience="service:memory",
    )
    assert payload_mem.sub == "memory"
    payload_identity = security.decode_token(
        token=captured["IDENTITY"]["EUGENE_PLEXUS_IDENTITY_SERVICE_TOKEN"],
        signing_key=auth.signing_key,
        expected_audience="service:identity",
    )
    assert payload_identity.sub == "identity"
    payload_connector = security.decode_token(
        token=captured["CONNECTOR"]["EUGENE_PLEXUS_CONNECTOR_SERVICE_TOKEN"],
        signing_key=auth.signing_key,
        expected_audience="service:connector",
    )
    assert payload_connector.sub == "connector"


# --------------------------------------------------------------------------- #
# Child-output filtering / coloring (signal-noise reduction)
# --------------------------------------------------------------------------- #


def test_healthz_filter_matches_2xx_only() -> None:
    """Successful /healthz access logs are the dominant noise source —
    we suppress them. Non-2xx must pass through so a newly-unhealthy
    component is still visible."""
    ok = 'INFO:     127.0.0.1:59471 - "GET /healthz HTTP/1.1" 200 OK\n'
    accepted = 'INFO:     127.0.0.1:59471 - "GET /healthz HTTP/1.1" 202 Accepted\n'
    sad_503 = 'INFO:     127.0.0.1:59471 - "GET /healthz HTTP/1.1" 503 Service Unavailable\n'
    sad_404 = 'INFO:     127.0.0.1:59471 - "GET /healthz HTTP/1.1" 404 Not Found\n'
    unrelated = 'INFO:     127.0.0.1:59471 - "POST /v1/chat HTTP/1.1" 200 OK\n'

    assert _HEALTHZ_2XX_LINE.search(ok)
    assert _HEALTHZ_2XX_LINE.search(accepted)
    assert not _HEALTHZ_2XX_LINE.search(sad_503)
    assert not _HEALTHZ_2XX_LINE.search(sad_404)
    assert not _HEALTHZ_2XX_LINE.search(unrelated)


def test_colorize_alerts_wraps_just_the_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the alert WORD gets wrapped — coloring the whole line makes
    red-on-dark unreadable. Case must be preserved."""
    # Force-enable color regardless of NO_COLOR in the test env.
    monkeypatch.setattr("eugene_plexus_watchdog.supervisor._USE_COLOR", True)

    err = _colorize_alerts("ERROR: something broke\n")
    assert err.startswith("\x1b[31mERROR\x1b[0m: something broke")

    warn = _colorize_alerts("Warning: dropping cache\n")
    assert warn.startswith("\x1b[33mWarning\x1b[0m: dropping cache")

    # Mid-line, mixed case, both severities in one line.
    multi = _colorize_alerts("warn x; ERROR y\n")
    assert "\x1b[33mwarn\x1b[0m" in multi
    assert "\x1b[31mERROR\x1b[0m" in multi

    # Substring matches don't fire (no \berror\b inside "errored-out").
    no_match = _colorize_alerts("The action errored-out cleanly\n")
    assert "\x1b[" not in no_match


def test_colorize_alerts_respects_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """NO_COLOR is a documented opt-out (https://no-color.org). When set,
    the helper returns the original text unchanged."""
    monkeypatch.setattr("eugene_plexus_watchdog.supervisor._USE_COLOR", False)
    assert _colorize_alerts("ERROR boom\n") == "ERROR boom\n"
