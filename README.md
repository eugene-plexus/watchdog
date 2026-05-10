# eugene-plexus-watchdog

Process supervisor and UI host for [Eugene Plexus](https://github.com/eugene-plexus).

## What this is

The watchdog is the outermost process of an Eugene Plexus install — the "medulla" in the consciousness analogy. It does three things and nothing else:

1. **Supervises body components.** Reads its topology config (`watchdog.yaml`) and spawns the orchestrator, hemisphere drivers, and memory as subprocess children. When a child exits, the watchdog respawns it. Children flagged for safe-mode boot are launched with `EUGENE_PLEXUS_<KIND>_SAFE_MODE=1` so a broken on-disk config can't lock the operator out.
2. **Hosts the UI.** Serves the UI's pre-built static assets at `/` so the operator's browser has one stable address (default `http://localhost:8079`). The UI proxies API calls through the watchdog to the orchestrator and other components.
3. **Exposes its own configuration over HTTP** — UI preferences (theme, font size) on the standard config trio (`/v1/config{,/schema}` + `PATCH`), and the topology declaratively under `/v1/components`.

What the watchdog deliberately does NOT do:

- Think. It does not participate in the bicameral loop, has no NT state, consumes no LLM tokens.
- Authenticate. v0.1 ships with no application-level auth; deployment assumes a Tailscale tailnet or equivalent.
- Decide what to restart based on consciousness state — that's the orchestrator's job in v0.2+ when the interoceptive event stream lands. v0.1's supervisor is reactive: a child exits, the watchdog respawns it.

## Endpoints

```
GET    /v1/components                       list supervised components + status
POST   /v1/components                       add a component
GET    /v1/components/{name}                read one
PATCH  /v1/components/{name}                modify
DELETE /v1/components/{name}                remove
POST   /v1/components/{name}/restart        restart one (spawn lifecycle)

GET    /v1/config                           read UI prefs + firstRunComplete
GET    /v1/config/schema                    schema for the same
PATCH  /v1/config                           partial update

GET    /healthz                             liveness + degraded-mode signal
GET    /                                    UI assets (index.html, JS, etc.)
```

The full contract lives in [`eugene-plexus/specs/openapi/watchdog.yaml`](https://github.com/eugene-plexus/specs/blob/main/openapi/watchdog.yaml).

## Quick start

```bash
pip install -e ".[dev]"
python -m eugene_plexus_watchdog
# default port 8079; override via PATCH /v1/config or the config file
```

The first run creates a `watchdog.yaml` in the working directory with sensible defaults — `firstRunComplete: false`, an empty topology, and UI prefs. The watchdog then opens a browser at its own address and the UI walks the operator through configuration via the first-run wizard.

## Why a watchdog at all?

Per the project's [`project_supervisor_as_interoception`](https://github.com/eugene-plexus/specs/tree/main/.claude/projects) memory: process health is interoceptive sensory data, and the natural place to react to it is the orchestrator's NT system. The watchdog's existence in v0.1 is a transitional concession — the orchestrator can't yet supervise itself, and someone has to keep it running. v0.2+ moves the richer supervision logic (restart decisions modulated by NT state, "pain" signals on repeated component failure) into the orchestrator and shrinks the watchdog's role to "keep the orchestrator running, serve UI assets."

## Codegen

Pydantic models for the watchdog and shared schemas are generated from the pinned `eugene-plexus/specs` commit:

```bash
python scripts/codegen.py
```

`SPECS_REF` records the commit SHA. Bump it to track a newer specs release; CI re-runs codegen and fails if the working tree drifts.

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`CONTRIBUTING.md`](CONTRIBUTING.md) (DCO sign-off required).
