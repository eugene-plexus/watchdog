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
import logging
import sys
from typing import Any

import pytest

from eugene_plexus_watchdog._generated.models import (
    ComponentEntry,
    ComponentKind,
    ComponentStatus,
    SpawnConfig,
)
from eugene_plexus_watchdog.supervisor import SupervisedProcess, Supervisor


class _FakeProcess:
    """Stand-in for `asyncio.subprocess.Process`. Tests drive
    `_finish(returncode)` to simulate the child exiting."""

    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._exit_event = asyncio.Event()

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
