"""Standard config trio for the watchdog: UI prefs + firstRunComplete only.

Topology lives under /v1/components, deliberately NOT here — editing UI
prefs in the generic config editor must never accidentally restructure
the install. See `state.py` for the rationale.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from .. import keyring_store
from .._generated.common_models import (
    ConfigDocument,
    ConfigSchema,
    ConfigUpdateRequest,
    ConfigUpdateResult,
)
from ..state import WatchdogState

log = logging.getLogger(__name__)

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

    # Snapshot the prior securityMode so we can react to a transition.
    # The keyring side-effects (write on flip to os_keyring, delete on
    # flip away) belong at this layer — `state` is a YAML serializer
    # and shouldn't know about OS secret stores.
    prior_mode = state.get_config("securityMode")
    result = state.apply_config_patch(body)
    new_mode = state.get_config("securityMode")

    if prior_mode == "os_keyring" and new_mode != "os_keyring":
        # Operator moved to the stronger boundary. The stored auto-
        # unlock secret must go — otherwise the install would still
        # auto-recover, contradicting the promise of the new mode.
        if keyring_store.delete_master_key():
            log.info(
                "securityMode changed from os_keyring to %s; deleted stored "
                "master key from OS keyring",
                new_mode,
            )
    elif prior_mode != "os_keyring" and new_mode == "os_keyring":
        # Operator opted into auto-unlock. If we already have the
        # master key in memory (logged in), persist it now so the
        # next restart actually auto-recovers. If we don't have it
        # in memory, the next /v1/auth/login will save it instead —
        # both paths converge to "next restart works".
        auth = request.app.state.auth_state
        if auth.has_master_key() and keyring_store.set_master_key(auth.master_key):
            log.info(
                "securityMode changed to os_keyring; persisted master key "
                "for auto-unlock"
            )

    return result
