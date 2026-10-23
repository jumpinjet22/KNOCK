"""Read and save the explicit doors (see `knock.core.doors`).

Doors are a list of objects, not flat fields, so they don't go through the
generic `/api/settings/{section}` routes (which are reflection over flat
`*Config` fields). Saved doors land in the config store's `doors` section --
the same place `DoorsConfig.from_sources()` reads -- and reach the UniFi
bridge process through `KNOCK_DOORS` (see `knock.core.supervisor`). The
bridge reads them at startup, so a saved change takes effect on its next
restart.

`KNOCK_DOORS` set in the environment always wins over anything saved (the
same rule every setting follows), so while it's set this refuses to save
rather than pretend a change took effect.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from knock.api.auth_routes import CurrentUserDep
from knock.api.settings_routes import ConfigStoreDep
from knock.config import DoorsConfig
from knock.core.doors import DoorConfig

router = APIRouter(prefix="/api/doors", tags=["doors"])


class DoorsResponse(BaseModel):
    doors: list[DoorConfig]
    # KNOCK_DOORS is set in the environment: it overrides whatever is saved,
    # so the list shown is the environment's and can't be edited here.
    shadowed_by_env: bool = False
    # The saved (or environment) doors couldn't be read -- shown instead of
    # failing the page, so a bad value can be seen and replaced.
    error: str | None = None


class UpdateDoorsRequest(BaseModel):
    doors: list[DoorConfig]


def _env_overrides() -> bool:
    return os.environ.get("KNOCK_DOORS") is not None


def _readable(exc: ValidationError) -> str:
    return "; ".join(str(error["msg"]).removeprefix("Value error, ") for error in exc.errors())


@router.get("", response_model=DoorsResponse)
def get_doors(current_user: CurrentUserDep, *, store: ConfigStoreDep) -> DoorsResponse:
    shadowed = _env_overrides()
    try:
        config = DoorsConfig.from_sources(store)
    except ValueError as exc:
        return DoorsResponse(doors=[], shadowed_by_env=shadowed, error=str(exc))
    return DoorsResponse(doors=config.doors, shadowed_by_env=shadowed)


@router.put("", response_model=DoorsResponse)
def update_doors(
    body: UpdateDoorsRequest, current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> DoorsResponse:
    if _env_overrides():
        raise HTTPException(
            status_code=409,
            detail="KNOCK_DOORS is set in the environment and overrides saved doors. "
            "Unset it to edit doors here.",
        )
    try:
        # Per-door checks already ran on the request; this adds the
        # cross-door ones (duplicate keys, one trigger on two doors).
        validated = DoorsConfig(doors=body.doors)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_readable(exc)) from exc
    store.set_section(
        "doors", {"doors": [door.model_dump(mode="json") for door in validated.doors]}
    )
    return DoorsResponse(doors=validated.doors)
