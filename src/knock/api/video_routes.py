"""Live video preview routes.

UniFi has no equivalent of Frigate's own live-view web UI, so its preview
is server-side snapshot polling: reuses the same public-API snapshot call
the UniFi bridge already makes on a real ring/detection event, just fired
on a timer instead. Frigate already runs its own web UI (which itself runs
go2rtc for real live streams) -- rather than guess at and re-implement its
internal stream URLs, this proxies Frigate's own `latest.jpg` snapshot
endpoint the same way, and the frontend separately links out to Frigate's
own UI for a true live view.

`UnifiConfig` carries only an `api_key` (no username/password), which
`uiprotect`'s `ProtectApiClient` treats as a "public-only" client -- the
private API (`get_cameras()`, `get_camera_snapshot()`) raises
`PublicOnlyModeError` on it. This uses the public API surface
(`get_cameras_public()`, `get_public_api_camera_snapshot()`) instead, which
is exactly what api_key-only auth is meant to grant.

Both snapshot endpoints sit behind a small per-camera cache with a minimum
refetch interval, so multiple open browser tabs (or an eager poll loop)
can't each hammer real camera hardware faster than that floor.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from uiprotect import ProtectApiClient

from knock.api.auth_routes import CurrentUserDep
from knock.api.settings_routes import ConfigStoreDep
from knock.config import FrigateConfig, UnifiConfig

router = APIRouter(prefix="/api/video", tags=["video"])

_SNAPSHOT_MIN_INTERVAL_SECONDS = 1.0


class SnapshotCache:
    def __init__(self, min_interval: float = _SNAPSHOT_MIN_INTERVAL_SECONDS) -> None:
        self._min_interval = min_interval
        self._entries: dict[str, tuple[float, bytes]] = {}

    async def get(self, key: str, fetch: Callable[[], Awaitable[bytes]]) -> bytes:
        cached = self._entries.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._min_interval:
            return cached[1]
        data = await fetch()
        self._entries[key] = (now, data)
        return data


_snapshot_cache = SnapshotCache()


def get_snapshot_cache() -> SnapshotCache:
    return _snapshot_cache


SnapshotCacheDep = Annotated[SnapshotCache, Depends(get_snapshot_cache)]


def _default_unifi_client_factory(config: UnifiConfig) -> ProtectApiClient:
    return ProtectApiClient(
        host=config.host,
        port=config.port,
        api_key=config.api_key.get_secret_value(),
        verify_ssl=config.verify_ssl,
    )


def get_unifi_client_factory() -> Callable[[UnifiConfig], ProtectApiClient]:
    return _default_unifi_client_factory


UnifiClientFactoryDep = Annotated[
    Callable[[UnifiConfig], ProtectApiClient], Depends(get_unifi_client_factory)
]


# -- unifi --------------------------------------------------------------------


class UnifiCameraInfo(BaseModel):
    device_id: str
    name: str
    is_connected: bool


@router.get("/unifi/cameras", response_model=list[UnifiCameraInfo])
async def list_unifi_cameras(
    current_user: CurrentUserDep,
    *,
    store: ConfigStoreDep,
    client_factory: UnifiClientFactoryDep,
) -> list[UnifiCameraInfo]:
    config = UnifiConfig.from_sources(store)
    client = client_factory(config)
    try:
        cameras = await client.get_cameras_public()
    except Exception as exc:  # noqa: BLE001 - surfaced as a 502, not a crash
        raise HTTPException(status_code=502, detail=f"failed to list UniFi cameras: {exc}") from exc
    finally:
        await client.close_session()

    return [
        UnifiCameraInfo(
            device_id=camera.id, name=camera.name or camera.id, is_connected=camera.is_reachable
        )
        for camera in cameras
    ]


@router.get("/unifi/{device_id}/snapshot")
async def get_unifi_snapshot(
    device_id: str,
    current_user: CurrentUserDep,
    *,
    store: ConfigStoreDep,
    client_factory: UnifiClientFactoryDep,
    cache: SnapshotCacheDep,
) -> Response:
    config = UnifiConfig.from_sources(store)

    async def fetch() -> bytes:
        client = client_factory(config)
        try:
            snapshot = await client.get_public_api_camera_snapshot(device_id)
        except Exception as exc:  # noqa: BLE001 - surfaced as a 502, not a crash
            raise HTTPException(
                status_code=502, detail=f"failed to fetch UniFi snapshot: {exc}"
            ) from exc
        finally:
            await client.close_session()
        if snapshot is None:
            raise HTTPException(status_code=502, detail="camera did not return a snapshot")
        return snapshot

    data = await cache.get(f"unifi:{device_id}", fetch)
    return Response(content=data, media_type="image/jpeg")


# -- frigate ------------------------------------------------------------------


class FrigateCameraInfo(BaseModel):
    name: str


@router.get("/frigate/cameras", response_model=list[FrigateCameraInfo])
def list_frigate_cameras(
    current_user: CurrentUserDep, *, store: ConfigStoreDep
) -> list[FrigateCameraInfo]:
    config = FrigateConfig.from_sources(store)
    try:
        response = httpx.get(
            f"http://{config.http_host}:{config.http_port}/api/config", timeout=10.0
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"failed to reach Frigate: {exc}") from exc

    cameras = response.json().get("cameras", {})
    return [FrigateCameraInfo(name=name) for name in cameras]


@router.get("/frigate/{camera_name}/snapshot")
async def get_frigate_snapshot(
    camera_name: str,
    current_user: CurrentUserDep,
    *,
    store: ConfigStoreDep,
    cache: SnapshotCacheDep,
) -> Response:
    config = FrigateConfig.from_sources(store)

    async def fetch() -> bytes:
        url = f"http://{config.http_host}:{config.http_port}/api/{camera_name}/latest.jpg"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502, detail=f"failed to fetch Frigate snapshot: {exc}"
                ) from exc
        return response.content

    data = await cache.get(f"frigate:{camera_name}", fetch)
    return Response(content=data, media_type="image/jpeg")
