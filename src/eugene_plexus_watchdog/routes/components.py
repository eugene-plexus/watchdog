"""Topology endpoints: /v1/components and /v1/components/{name}.

Skeleton implementation — every component reports `unreachable` because
the supervisor isn't wired yet. Once subprocess management lands the
status field will reflect actual process state, restart will SIGTERM +
respawn, and POST will scaffold per-component config files. Wire-shape
matches the spec so consumers (UI, tests) can be developed against this.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from .._generated.common_models import Problem, RestartResult
from .._generated.models import Component, ComponentEntry, ComponentList
from ..state import WatchdogState

router = APIRouter(tags=["components"])


def _not_found(name: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=Problem(
            type="https://github.com/eugene-plexus/watchdog#component-not-found",
            title="Component not found",
            status=404,
            detail=f"No component named {name!r} in the topology.",
            component="watchdog",
        ).model_dump(exclude_none=True),
    )


@router.get("/v1/components", response_model=ComponentList)
async def list_components(request: Request) -> ComponentList:
    state: WatchdogState = request.app.state.watchdog_state
    return ComponentList(components=state.list_components())


@router.post("/v1/components", response_model=Component, status_code=201)
async def create_component(request: Request, body: ComponentEntry) -> Component:
    state: WatchdogState = request.app.state.watchdog_state
    try:
        return state.add_component(body)
    except KeyError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=Problem(
                type="https://github.com/eugene-plexus/watchdog#component-name-conflict",
                title="Name already in use",
                status=409,
                detail=str(e),
                component="watchdog",
            ).model_dump(exclude_none=True),
        ) from e


@router.get("/v1/components/{name}", response_model=Component)
async def get_component(request: Request, name: str) -> Component:
    state: WatchdogState = request.app.state.watchdog_state
    found = state.get_component(name)
    if found is None:
        raise _not_found(name)
    return found


@router.patch("/v1/components/{name}", response_model=Component)
async def update_component(request: Request, name: str, body: ComponentEntry) -> Component:
    state: WatchdogState = request.app.state.watchdog_state
    try:
        updated = state.update_component(name, body)
    except KeyError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=Problem(
                type="https://github.com/eugene-plexus/watchdog#component-name-conflict",
                title="Name already in use",
                status=409,
                detail=str(e),
                component="watchdog",
            ).model_dump(exclude_none=True),
        ) from e
    if updated is None:
        raise _not_found(name)
    return updated


@router.delete("/v1/components/{name}", status_code=204)
async def delete_component(request: Request, name: str) -> Response:
    state: WatchdogState = request.app.state.watchdog_state
    if not state.remove_component(name):
        raise _not_found(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/v1/components/{name}/restart",
    response_model=RestartResult,
    status_code=202,
)
async def restart_component(request: Request, name: str) -> RestartResult:
    """Skeleton: returns 202 with a stub message. Once the supervisor is
    wired up this will SIGTERM + respawn the spawned process, or 409 for
    remote components per the spec."""
    state: WatchdogState = request.app.state.watchdog_state
    component = state.get_component(name)
    if component is None:
        raise _not_found(name)
    return RestartResult(
        scheduled=True,
        delayMs=0,
        message=(
            "Skeleton implementation — supervisor is stubbed. The "
            "spec-defined restart behavior (SIGTERM the spawned process "
            "and let the watchdog respawn) lands once subprocess "
            "management is wired up."
        ),
    )
