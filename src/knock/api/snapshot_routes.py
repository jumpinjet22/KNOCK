"""Unauthenticated, token-addressed doorbell snapshot delivery for a
household notification's image attachment.

Deliberately NOT behind `CurrentUserDep`/`require_auth` -- Home Assistant's
mobile app fetches a notification's `image` URL directly from the
household's phone, outside KNOCK's own session-cookie system, so it cannot
present a session cookie here. Safety instead comes entirely from
`SnapshotStore`'s token design (256-bit CSPRNG token, short TTL, single-use)
-- see core/snapshot_store.py for the full threat model. This is a
deliberate exception to "every API route requires auth"; do not add another
unauthenticated route without the same scrutiny.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response

from knock.core.snapshot_store import SnapshotStore

router = APIRouter(prefix="/api/snapshot", tags=["snapshot"])


def get_snapshot_store() -> SnapshotStore:
    return SnapshotStore()


SnapshotStoreDep = Annotated[SnapshotStore, Depends(get_snapshot_store)]


@router.get("/{token}")
async def get_snapshot(token: str, *, store: SnapshotStoreDep) -> Response:
    data = store.take(token)
    if data is None:
        raise HTTPException(status_code=404, detail="snapshot not found or expired")
    return Response(content=data, media_type="image/jpeg")
