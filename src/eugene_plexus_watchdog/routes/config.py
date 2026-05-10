"""Standard config trio for the watchdog: UI prefs + firstRunComplete only.

Topology lives under /v1/components, deliberately NOT here — editing UI
prefs in the generic config editor must never accidentally restructure
the install. See `state.py` for the rationale.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from .._generated.common_models import (
    ConfigDocument,
    ConfigSchema,
    ConfigUpdateRequest,
    ConfigUpdateResult,
)
from ..state import WatchdogState

router = APIRouter(tags=["config"])


@router.get("/v1/config", response_model=ConfigDocument)
async def get_config(request: Request) -> ConfigDocument:
    state: WatchdogState = request.app.state.watchdog_state
    return state.as_config_document()


@router.get("/v1/config/schema", response_model=ConfigSchema)
async def get_config_schema(request: Request) -> ConfigSchema:
    state: WatchdogState = request.app.state.watchdog_state
    return state.as_config_schema()


@router.patch("/v1/config", response_model=ConfigUpdateResult)
async def patch_config(request: Request, body: ConfigUpdateRequest) -> ConfigUpdateResult:
    state: WatchdogState = request.app.state.watchdog_state
    return state.apply_config_patch(body)
