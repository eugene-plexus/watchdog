"""File-backed state for the watchdog: UI prefs + topology, one YAML file.

Owns the persistent contents of `watchdog.yaml`. Two facades read through
this — `ConfigStore` (the standard config trio for UI prefs +
firstRunComplete) and `TopologyStore` (the components list under
`/v1/components`). Both share one lock and one on-disk file so concurrent
PATCH operations from different routes can't corrupt each other.

The on-disk shape:

    firstRunComplete: false
    uiTheme: auto
    uiFontSize: medium
    components:
      - name: orchestrator
        kind: orchestrator
        url: http://127.0.0.1:8080
        spawn:
          configFile: ~/.eugene-plexus/orchestrator/config.yaml
        safeMode: false
      - ...

Flat config-field naming (uiTheme, uiFontSize) rather than a nested `ui:`
section because the existing ConfigField machinery is flat — `category:
"ui"` provides the UI's grouping hint without forcing the YAML into a
shape the generic config editor doesn't natively understand.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from ._generated.common_models import (
    ConfigDocument,
    ConfigField,
    ConfigFieldError,
    ConfigSchema,
    ConfigUpdateRequest,
    ConfigUpdateResult,
    ConfigValueType,
)
from ._generated.models import Component, ComponentEntry, ComponentKind, ComponentStatus

CONFIG_FIELDS: list[ConfigField] = [
    ConfigField(
        key="firstRunComplete",
        label="First-run setup complete",
        description=(
            "Set to true by the first-run wizard's final Start step. "
            "While false the UI routes operators to /setup; flipping it "
            "back to false re-enters the wizard on the next reload. "
            "Safe to leave alone unless you want to re-do setup."
        ),
        category="setup",
        valueType=ConfigValueType.boolean,
        default=False,
    ),
    ConfigField(
        key="uiTheme",
        label="Theme",
        description="Color theme for the Eugene Plexus UI.",
        category="ui",
        valueType=ConfigValueType.enum,
        default="auto",
        enumValues=["light", "dark", "auto"],
        enumLabels=["Light", "Dark", "Auto (follow system)"],
    ),
    ConfigField(
        key="uiFontSize",
        label="Font size",
        description="Base font size for the Eugene Plexus UI.",
        category="ui",
        valueType=ConfigValueType.enum,
        default="medium",
        enumValues=["small", "medium", "large"],
        enumLabels=["Small", "Medium", "Large"],
    ),
]
CATEGORY_LABELS: dict[str, str] = {
    "setup": "Setup",
    "ui": "Appearance",
}

_CONFIG_FIELDS_BY_KEY: dict[str, ConfigField] = {f.key: f for f in CONFIG_FIELDS}


def _config_defaults() -> dict[str, Any]:
    return {f.key: f.default for f in CONFIG_FIELDS if f.default is not None}


class WatchdogState:
    """Threadsafe owner of `watchdog.yaml`. Single lock, single file write."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._config: dict[str, Any] = _config_defaults()
        self._components: dict[str, ComponentEntry] = {}

    # ----- lifecycle --------------------------------------------------

    def load(self) -> None:
        with self._lock:
            if self._path.exists():
                raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
                if not isinstance(raw, dict):
                    raise ValueError(f"{self._path} must be a YAML mapping at the root")
                merged = _config_defaults()
                for k, v in raw.items():
                    if k in _CONFIG_FIELDS_BY_KEY:
                        merged[k] = v
                self._config = merged
                comps_raw = raw.get("components") or []
                self._components = {}
                for entry in comps_raw:
                    parsed = ComponentEntry.model_validate(entry)
                    self._components[parsed.name] = parsed
            else:
                self._config = _config_defaults()
                self._components = {}
                self._write_locked()

    # ----- config trio ------------------------------------------------

    def as_config_document(self) -> ConfigDocument:
        with self._lock:
            return ConfigDocument.model_validate(dict(self._config))

    def apply_config_patch(self, request: ConfigUpdateRequest) -> ConfigUpdateResult:
        applied: list[str] = []
        rejected: list[ConfigFieldError] = []

        patch: dict[str, Any] = request.model_dump()

        with self._lock:
            for key, new_value in patch.items():
                field = _CONFIG_FIELDS_BY_KEY.get(key)
                if field is None:
                    rejected.append(ConfigFieldError(key=key, message="unknown field"))
                    continue
                err = _validate(field, new_value)
                if err is not None:
                    rejected.append(ConfigFieldError(key=key, message=err))
                    continue
                if new_value is None and field.default is not None:
                    self._config[key] = field.default
                else:
                    self._config[key] = new_value
                applied.append(key)

            if applied:
                self._write_locked()

            return ConfigUpdateResult(
                applied=applied,
                rejected=rejected,
                requiresRestart=False,
                pendingRestart=[],
            )

    def as_config_schema(self) -> ConfigSchema:
        return ConfigSchema(
            component="watchdog",
            fields=list(CONFIG_FIELDS),
            categories=CATEGORY_LABELS,
        )

    def get_config(self, key: str) -> Any:
        with self._lock:
            return self._config.get(key)

    # ----- topology ---------------------------------------------------
    #
    # `list_topology_entries` returns the declarative half — what the
    # operator wrote in `watchdog.yaml`. The routes layer combines this
    # with live state from the Supervisor (status, pid, lastRestart,
    # lastError) to produce the full `Component` view.

    def list_topology_entries(self) -> list[ComponentEntry]:
        with self._lock:
            return list(self._components.values())

    def get_topology_entry(self, name: str) -> ComponentEntry | None:
        with self._lock:
            return self._components.get(name)

    def add_topology_entry(self, entry: ComponentEntry) -> ComponentEntry:
        with self._lock:
            if entry.name in self._components:
                raise KeyError(f"component {entry.name!r} already exists")
            self._components[entry.name] = entry
            self._write_locked()
            return entry

    def update_topology_entry(self, name: str, entry: ComponentEntry) -> ComponentEntry | None:
        with self._lock:
            if name not in self._components:
                return None
            # Allow rename: replace under the new key, drop the old.
            if entry.name != name:
                if entry.name in self._components:
                    raise KeyError(f"component {entry.name!r} already exists")
                del self._components[name]
            self._components[entry.name] = entry
            self._write_locked()
            return entry

    def remove_topology_entry(self, name: str) -> bool:
        with self._lock:
            if name not in self._components:
                return False
            del self._components[name]
            self._write_locked()
            return True

    # ----- backwards-compatible Component composition -----------------
    #
    # Tests built against the skeleton's `list_components` etc. continue
    # to work — they just see `unreachable` status when no supervisor is
    # injected. Production code (routes layer) goes through the
    # Supervisor for live status.

    def list_components(self) -> list[Component]:
        return [_to_component(e) for e in self.list_topology_entries()]

    def get_component(self, name: str) -> Component | None:
        entry = self.get_topology_entry(name)
        return _to_component(entry) if entry is not None else None

    def add_component(self, entry: ComponentEntry) -> Component:
        return _to_component(self.add_topology_entry(entry))

    def update_component(self, name: str, entry: ComponentEntry) -> Component | None:
        updated = self.update_topology_entry(name, entry)
        return _to_component(updated) if updated is not None else None

    def remove_component(self, name: str) -> bool:
        return self.remove_topology_entry(name)

    # ----- internals --------------------------------------------------

    def _write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        out: dict[str, Any] = dict(self._config)
        out["components"] = [
            entry.model_dump(exclude_none=True, mode="json") for entry in self._components.values()
        ]
        with self._path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(out, f, sort_keys=True, default_flow_style=False)


def _to_component(entry: ComponentEntry) -> Component:
    """Compose a Component from a topology entry, with placeholder status.

    Used by the legacy `WatchdogState.list_components` etc. methods —
    when no Supervisor is available the status is hard-coded to
    `unreachable`. The routes layer overrides this with live state from
    the Supervisor when one is wired up."""
    return Component(
        name=entry.name,
        kind=ComponentKind(entry.kind.value),
        url=entry.url,
        spawn=entry.spawn,
        safeMode=entry.safeMode,
        status=ComponentStatus.unreachable,
    )


def _validate(field: ConfigField, value: Any) -> str | None:
    if value is None:
        return None
    vt = field.valueType
    if vt == ConfigValueType.boolean:
        if not isinstance(value, bool):
            return f"expected boolean, got {type(value).__name__}"
        return None
    if vt == ConfigValueType.enum:
        if not isinstance(value, str):
            return f"expected string, got {type(value).__name__}"
        allowed = field.enumValues or []
        if value not in allowed:
            return f"must be one of {allowed}"
        return None
    return f"unsupported valueType for watchdog config: {vt}"
