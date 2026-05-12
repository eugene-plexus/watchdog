"""Subprocess supervisor for Eugene Plexus body components.

Spawns child processes per the topology in `watchdog.yaml`, threads the
right env vars through (config-file path, bind port parsed from the
component's URL, safe-mode flag), and respawns any child that exits.
The "I'll restart Eugene's heart for you" piece — the watchdog's whole
reason to exist in v0.1.

Long-term (v0.2+) the orchestrator absorbs this responsibility because
process supervision IS interoception in the brain analogy. See
`project_supervisor_as_interoception` in the project memory directory.
For now, the watchdog is the medulla — autonomic reflex layer that
keeps things running without thinking.

## Cross-platform notes

`asyncio.subprocess.Process.terminate()` is the graceful-shutdown signal
we use on every supervised exit. On POSIX that maps to SIGTERM (children
get a chance to flush logs and answer their last in-flight request). On
Windows it's `TerminateProcess`, which is a hard kill — no graceful
window. Living with that for v0.1 personal-use; Windows is primarily a
dev surface, real installs are Linux/Mac/Docker.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from . import orphan_kill, security
from ._generated.models import ComponentEntry, ComponentKind, ComponentStatus
from .auth_state import AuthState

# How long an exiting child gets to finish flushing before we SIGKILL it
# during watchdog shutdown. Long enough for a /v1/admin/restart-style
# response body to flush over loopback, short enough that a hung child
# doesn't drag shutdown out for the operator.
_TERM_TIMEOUT_SECONDS = 5.0

# Polling interval for the health-watcher loop. Each spawned component's
# /healthz is hit on this cadence to refresh its status; pick a value
# that's responsive enough for the UI (sub-second after a restart) but
# light enough not to drown the loopback in HTTP traffic.
_HEALTH_POLL_SECONDS = 1.5

# After how many consecutive crashes (non-zero exits) does the watchdog
# stop respawning a component? Without this, a misconfigured driver that
# exits immediately would respawn-storm forever. Operator must POST to
# /v1/components/<name>/restart to clear the crashed state.
_CRASH_BACKOFF_THRESHOLD = 5

# Module name to spawn for each body-component kind. The watchdog uses
# `sys.executable -m <module>` so it spawns whichever Python interpreter
# the watchdog itself is running under — production installs that put
# all components in one venv work out of the box; dev setups with
# per-component venvs need every component installed into the watchdog's
# venv (or a shared one).
_KIND_TO_MODULE: dict[ComponentKind, str] = {
    ComponentKind.orchestrator: "eugene_plexus_orchestrator",
    ComponentKind.hemisphere_driver: "eugene_plexus_hemisphere_driver",
    ComponentKind.memory: "eugene_plexus_memory",
    ComponentKind.identity: "eugene_plexus_identity",
    ComponentKind.connector: "eugene_plexus_connector",
}

# Env-var prefix per component kind, matching what each component's
# pydantic-settings `env_prefix` already expects.
_KIND_TO_ENV_PREFIX: dict[ComponentKind, str] = {
    ComponentKind.orchestrator: "EUGENE_PLEXUS_ORCH",
    ComponentKind.hemisphere_driver: "EUGENE_PLEXUS_HD",
    ComponentKind.memory: "EUGENE_PLEXUS_MEM",
    ComponentKind.identity: "EUGENE_PLEXUS_IDENTITY",
    ComponentKind.connector: "EUGENE_PLEXUS_CONNECTOR",
}


class SupervisedProcess:
    """One supervised child: its declared topology entry plus live state.

    The supervision loop is a long-running task (`_run`) that spawns the
    child, awaits its exit, applies the back-off rules, and respawns —
    until `stop()` is called or the crash threshold trips.
    """

    def __init__(
        self,
        entry: ComponentEntry,
        log: logging.Logger,
        auth_state: AuthState | None = None,
    ) -> None:
        self.entry = entry
        self._log = log
        # v0.2: auth_state is the source of the per-restart JWT signing
        # key + (post-login) master key + service token issuance. Optional
        # for test ergonomics — supervisor tests that don't care about
        # auth can pass None and get the v0.1 env-var set only.
        self._auth_state = auth_state
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._consecutive_crashes = 0

        self.status: ComponentStatus = ComponentStatus.starting
        self.last_error: str | None = None
        self.last_restart: datetime | None = None

    # --- public lifecycle --------------------------------------------------

    def start(self) -> None:
        """Kick off the supervision loop. Returns immediately; the loop
        runs in the background until `stop()` is called."""
        self._stop_requested = False
        self._consecutive_crashes = 0
        self._task = asyncio.create_task(self._run(), name=f"supervise:{self.entry.name}")

    async def restart(self) -> None:
        """SIGTERM the child; the supervision loop respawns it. Clears
        the crash counter so a manual restart can recover from the
        backoff state."""
        self._consecutive_crashes = 0
        proc = self._proc
        if proc is not None and proc.returncode is None:
            self._log.info(
                "restart requested for %s; terminating pid %d",
                self.entry.name,
                proc.pid,
            )
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    async def stop(self) -> None:
        """Stop the supervision loop and ensure the child is dead.
        Terminates with a timeout, escalates to kill if the child hangs."""
        self._stop_requested = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERM_TIMEOUT_SECONDS)
            except TimeoutError:
                self._log.warning(
                    "%s did not exit within %.1fs of terminate; killing",
                    self.entry.name,
                    _TERM_TIMEOUT_SECONDS,
                )
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(BaseException):
                    await proc.wait()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await self._task

    # --- introspection (read-only properties for the routes layer) ---------

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None

    # --- internals ---------------------------------------------------------

    async def _run(self) -> None:
        """Spawn-watch-respawn loop. Exits cleanly on stop or after the
        crash threshold trips."""
        while not self._stop_requested:
            await self._spawn_once()
            if self._stop_requested:
                return
            if self._consecutive_crashes >= _CRASH_BACKOFF_THRESHOLD:
                self._log.error(
                    "%s crashed %d times in a row; giving up. POST "
                    "/v1/components/%s/restart to reset.",
                    self.entry.name,
                    self._consecutive_crashes,
                    self.entry.name,
                )
                self.status = ComponentStatus.crashed
                return
            await asyncio.sleep(min(2.0 * self._consecutive_crashes, 10.0))

    async def _spawn_once(self) -> None:
        """One spawn / wait / mark-status iteration."""
        spawn = self.entry.spawn
        if spawn is None:
            self._log.warning("%s has no spawn block; skipping", self.entry.name)
            self.status = ComponentStatus.unreachable
            return

        env = os.environ.copy()
        prefix = _KIND_TO_ENV_PREFIX[self.entry.kind]
        env[f"{prefix}_CONFIG_FILE"] = str(spawn.configFile)
        port = urlparse(str(self.entry.url)).port
        if port is not None:
            env[f"{prefix}_BIND_PORT"] = str(port)
        env[f"{prefix}_SAFE_MODE"] = "1" if self.entry.safeMode else "0"

        # v0.2 auth env vars. Children that have implemented the v0.2
        # auth surface (currently watchdog itself; orchestrator/drivers/
        # memory follow in subsequent commits) read these to (a) validate
        # inbound bearer tokens against the shared signing key, (b)
        # present a service token of their own on outbound calls, and
        # (c) decrypt at-rest secrets like apiKey. Children unaware of
        # these env vars simply ignore them — fully backward-compatible
        # rollout.
        if self._auth_state is not None:
            kind_value = self.entry.kind.value  # "orchestrator", "hemisphere-driver", "memory"
            env[f"{prefix}_AUTH_SIGNING_KEY"] = base64.b64encode(
                self._auth_state.signing_key
            ).decode("ascii")
            env[f"{prefix}_SERVICE_TOKEN"] = security.issue_service_token(
                signing_key=self._auth_state.signing_key,
                kind=kind_value,
            )
            if self._auth_state.master_key is not None:
                env[f"{prefix}_MASTER_KEY"] = base64.b64encode(
                    self._auth_state.master_key
                ).decode("ascii")
            else:
                # Be explicit about absence so a child running stale env
                # from a previous shell can't pick up an unrelated value.
                env.pop(f"{prefix}_MASTER_KEY", None)

        if spawn.env:
            env.update({k: str(v) for k, v in spawn.env.items()})

        module = _KIND_TO_MODULE[self.entry.kind]
        cmd = [sys.executable, "-m", module]
        self._log.info("spawning %s: %s", self.entry.name, " ".join(cmd))

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, env=env, **orphan_kill.kwargs_for_platform()
            )
        except OSError as e:
            self._log.error("failed to spawn %s: %s", self.entry.name, e)
            self.status = ComponentStatus.crashed
            self.last_error = f"spawn failed: {e}"
            self._consecutive_crashes += 1
            return

        # Windows: assign to the watchdog's Job Object so the OS reaps
        # the child if the watchdog dies hard. POSIX uses preexec_fn
        # (handled in kwargs above), no post-spawn step needed.
        win_job = orphan_kill.windows_job()
        if win_job is not None and self._proc.pid is not None:
            win_job.assign(self._proc.pid)

        self.status = ComponentStatus.safe_mode if self.entry.safeMode else ComponentStatus.starting
        self.last_restart = datetime.now(UTC)
        self.last_error = None

        return_code = await self._proc.wait()
        self._proc = None

        if self._stop_requested:
            self.status = ComponentStatus.exited
            return

        if return_code == 0:
            self._log.info("%s exited cleanly (rc=0); respawning", self.entry.name)
            self.status = ComponentStatus.exited
            self._consecutive_crashes = 0
        else:
            self._consecutive_crashes += 1
            self.last_error = f"exited with code {return_code}"
            self._log.warning(
                "%s exited rc=%d (consecutive crashes: %d)",
                self.entry.name,
                return_code,
                self._consecutive_crashes,
            )
            self.status = ComponentStatus.crashed


class Supervisor:
    """Owns every supervised child plus a background health-poll task.

    Two responsibilities:
      1. Lifecycle of the SupervisedProcess collection — add, remove,
         restart, stop_all.
      2. Periodic /healthz polling so the routes layer can report
         `running` vs `safe_mode` distinctly from the raw process state
         (a child can be alive but in safe mode, etc.).
    """

    def __init__(
        self,
        log: logging.Logger | None = None,
        auth_state: AuthState | None = None,
    ) -> None:
        self._log = log or logging.getLogger(__name__)
        # v0.2: shared with every SupervisedProcess so each spawn can
        # issue a fresh service token, base64-encode the signing key,
        # and forward the (possibly-still-None) master key. Optional —
        # absent for tests that don't care about auth.
        self._auth_state = auth_state
        self._processes: dict[str, SupervisedProcess] = {}
        self._health_task: asyncio.Task[None] | None = None
        self._health_client: httpx.AsyncClient | None = None
        # Maps component name -> True iff /healthz reported safeMode=true.
        # Drives the running-vs-safe_mode distinction on Component.status.
        self._safe_mode_observed: dict[str, bool] = {}
        # Maps component name -> True iff /healthz returned 2xx in the
        # last poll. Lets us promote `starting` -> `running` once the
        # child is actually serving requests.
        self._reachable: dict[str, bool] = {}

    # --- collection management --------------------------------------------

    def add_and_start(self, entry: ComponentEntry) -> None:
        """Begin supervising a topology entry. No-op for remote entries
        (`spawn is None`); they're tracked for health-polling only."""
        if entry.name in self._processes:
            return
        if entry.spawn is None:
            # Remote: register a placeholder so /healthz polling tracks
            # reachability, but no SupervisedProcess.
            self._reachable[entry.name] = False
            return
        sp = SupervisedProcess(entry, self._log, auth_state=self._auth_state)
        self._processes[entry.name] = sp
        sp.start()

    async def remove_and_stop(self, name: str) -> None:
        sp = self._processes.pop(name, None)
        self._safe_mode_observed.pop(name, None)
        self._reachable.pop(name, None)
        if sp is not None:
            await sp.stop()

    async def restart(self, name: str) -> bool:
        sp = self._processes.get(name)
        if sp is None:
            return False
        await sp.restart()
        return True

    async def restart_all(self) -> list[str]:
        """SIGTERM every supervised child so their supervision loops
        respawn them. Used after the operator logs in: children that
        were already spawned ran with no MASTER_KEY env var (the
        operator hadn't unlocked yet); the respawn picks up the now-
        populated key from the shared AuthState.

        Returns the list of component names that were signaled — empty
        if no children are running yet (e.g. login before topology has
        anything spawned). Best-effort: per-process restart failures
        are logged but never raised; the operator's login flow
        shouldn't error out because one supervised child wedged.
        """
        names = list(self._processes.keys())
        if not names:
            return []
        self._log.info(
            "restart_all: signaling %d supervised process(es) to respawn — "
            "typically because master key just became available",
            len(names),
        )
        results = await asyncio.gather(
            *(self._processes[n].restart() for n in names),
            return_exceptions=True,
        )
        for name, result in zip(names, results, strict=False):
            if isinstance(result, BaseException):
                self._log.warning("restart_all: %s failed to restart: %s", name, result)
        return names

    async def start_health_loop(self, get_components: Any) -> None:
        """Start the background /healthz polling task. `get_components`
        is a 0-arg callable returning the current ComponentEntry list,
        so the loop sees fresh entries when topology changes."""
        if self._health_task is not None:
            return
        # Short timeout so a hung child doesn't block the whole poll round.
        self._health_client = httpx.AsyncClient(timeout=2.0)
        self._health_task = asyncio.create_task(
            self._health_loop(get_components), name="supervisor-health"
        )

    async def stop_all(self) -> None:
        """Shut everything down. Cancels the health loop, terminates and
        waits for every supervised process. Best-effort — never raises."""
        if self._health_task is not None:
            self._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await self._health_task
            self._health_task = None
        if self._health_client is not None:
            with contextlib.suppress(BaseException):
                await self._health_client.aclose()
            self._health_client = None

        await asyncio.gather(
            *(sp.stop() for sp in self._processes.values()),
            return_exceptions=True,
        )
        self._processes.clear()
        self._safe_mode_observed.clear()
        self._reachable.clear()

    # --- introspection (read by the routes layer) -------------------------

    def status_for(
        self, name: str, *, has_spawn: bool
    ) -> tuple[ComponentStatus, str | None, datetime | None, int | None]:
        """Return (status, lastError, lastRestart, pid) for one component.

        For spawned children: derives status from process state +
        /healthz observations (running, starting, safe_mode, exited,
        crashed).

        For remote entries (`has_spawn=False`): derives from the last
        /healthz poll only — `running` if the URL is answering,
        `unreachable` otherwise.
        """
        sp = self._processes.get(name)
        if sp is not None:
            base = sp.status
            if base == ComponentStatus.starting and self._reachable.get(name, False):
                base = (
                    ComponentStatus.safe_mode
                    if self._safe_mode_observed.get(name)
                    else ComponentStatus.running
                )
            return base, sp.last_error, sp.last_restart, sp.pid
        if not has_spawn:
            reachable = self._reachable.get(name, False)
            status = ComponentStatus.running if reachable else ComponentStatus.unreachable
            return status, None, None, None
        return ComponentStatus.unreachable, None, None, None

    # --- internals --------------------------------------------------------

    async def _health_loop(self, get_components: Any) -> None:
        """Hit `/healthz` on every known component once per
        `_HEALTH_POLL_SECONDS`. Records reachability + observed safe-mode
        flag; never mutates `SupervisedProcess` state directly."""
        try:
            while True:
                entries: list[ComponentEntry] = get_components()
                await asyncio.gather(
                    *(self._poll_one(e) for e in entries),
                    return_exceptions=True,
                )
                await asyncio.sleep(_HEALTH_POLL_SECONDS)
        except asyncio.CancelledError:
            return

    async def _poll_one(self, entry: ComponentEntry) -> None:
        client = self._health_client
        if client is None:
            return
        url = str(entry.url).rstrip("/") + "/healthz"
        start = time.monotonic()
        try:
            response = await client.get(url)
        except httpx.HTTPError:
            self._reachable[entry.name] = False
            return
        finally:
            # Defensive: an unexpectedly slow probe shouldn't cascade
            # into a long poll round.
            elapsed = time.monotonic() - start
            if elapsed > _HEALTH_POLL_SECONDS:
                self._log.debug(
                    "healthz probe for %s took %.2fs (poll interval %.2fs)",
                    entry.name,
                    elapsed,
                    _HEALTH_POLL_SECONDS,
                )

        self._reachable[entry.name] = response.is_success
        if response.is_success:
            with contextlib.suppress(ValueError):
                body = response.json()
                self._safe_mode_observed[entry.name] = bool(body.get("safeMode"))
