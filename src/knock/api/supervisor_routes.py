"""Process supervision routes: start/stop/restart the four bridge
subprocesses and tail their logs. Gated behind `require_auth` -- this
controls real child processes that talk to real cameras/brokers/hubs.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from knock.api.auth_routes import CurrentUserDep
from knock.core.supervisor import BRIDGE_COMMANDS, BridgeName, BridgeSupervisor

router = APIRouter(prefix="/api/supervisor", tags=["supervisor"])

_supervisor = BridgeSupervisor()


def get_bridge_supervisor() -> BridgeSupervisor:
    return _supervisor


BridgeSupervisorDep = Annotated[BridgeSupervisor, Depends(get_bridge_supervisor)]


def _require_bridge(name: str) -> BridgeName:
    if name not in BRIDGE_COMMANDS:
        raise HTTPException(status_code=404, detail=f"unknown bridge: {name!r}")
    return name


class BridgeState(BaseModel):
    name: str
    status: str
    restart_count: int
    pid: int | None = None
    autostart: bool = False


class BridgeLogsResponse(BaseModel):
    lines: list[str]
    next_after: int


class AutostartRequest(BaseModel):
    enabled: bool


def _state(supervisor: BridgeSupervisor, name: BridgeName) -> BridgeState:
    info = supervisor.describe(name)
    return BridgeState(
        name=info.name,
        status=info.status,
        restart_count=info.restart_count,
        pid=info.pid,
        autostart=info.autostart,
    )


@router.get("", response_model=list[BridgeState])
def list_bridges(
    current_user: CurrentUserDep, *, supervisor: BridgeSupervisorDep
) -> list[BridgeState]:
    return [_state(supervisor, name) for name in BRIDGE_COMMANDS]


@router.get("/{name}", response_model=BridgeState)
def get_bridge(
    name: str, current_user: CurrentUserDep, *, supervisor: BridgeSupervisorDep
) -> BridgeState:
    bridge_name = _require_bridge(name)
    return _state(supervisor, bridge_name)


@router.post("/{name}/start", response_model=BridgeState)
def start_bridge(
    name: str, current_user: CurrentUserDep, *, supervisor: BridgeSupervisorDep
) -> BridgeState:
    bridge_name = _require_bridge(name)
    supervisor.start(bridge_name)
    return _state(supervisor, bridge_name)


@router.post("/{name}/stop", response_model=BridgeState)
def stop_bridge(
    name: str, current_user: CurrentUserDep, *, supervisor: BridgeSupervisorDep
) -> BridgeState:
    bridge_name = _require_bridge(name)
    supervisor.stop(bridge_name)
    return _state(supervisor, bridge_name)


@router.post("/{name}/restart", response_model=BridgeState)
def restart_bridge(
    name: str, current_user: CurrentUserDep, *, supervisor: BridgeSupervisorDep
) -> BridgeState:
    bridge_name = _require_bridge(name)
    supervisor.restart(bridge_name)
    return _state(supervisor, bridge_name)


@router.put("/{name}/autostart", response_model=BridgeState)
def set_bridge_autostart(
    name: str,
    body: AutostartRequest,
    current_user: CurrentUserDep,
    *,
    supervisor: BridgeSupervisorDep,
) -> BridgeState:
    bridge_name = _require_bridge(name)
    supervisor.set_autostart(bridge_name, body.enabled)
    return _state(supervisor, bridge_name)


@router.get("/{name}/logs", response_model=BridgeLogsResponse)
def get_bridge_logs(
    name: str,
    current_user: CurrentUserDep,
    *,
    supervisor: BridgeSupervisorDep,
    after: int = Query(default=0, ge=0),
) -> BridgeLogsResponse:
    bridge_name = _require_bridge(name)
    lines, next_after = supervisor.tail(bridge_name, after)
    return BridgeLogsResponse(lines=lines, next_after=next_after)
