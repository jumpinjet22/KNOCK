"""Audit log and session browser routes.

Both read already-persisted local files -- the audit trail (which can
contain raw visitor speech, per `JSONLAuditLog`'s own docstring) and
per-session conversation state -- so both sit behind `require_auth` even
though they're read-only.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from knock.api.auth_routes import CurrentUserDep
from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.session_store import JSONFileSessionStore
from knock.core.state import SessionState

router = APIRouter(prefix="/api", tags=["history"])


def get_audit_log() -> JSONLAuditLog:
    return JSONLAuditLog()


def get_session_store() -> JSONFileSessionStore:
    return JSONFileSessionStore()


AuditLogDep = Annotated[JSONLAuditLog, Depends(get_audit_log)]
SessionStoreDep = Annotated[JSONFileSessionStore, Depends(get_session_store)]


@router.get("/audit", response_model=list[AuditEntry])
def get_audit_entries(
    current_user: CurrentUserDep,
    *,
    audit_log: AuditLogDep,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[AuditEntry]:
    return audit_log.recent(limit)


@router.get("/sessions", response_model=list[SessionState])
def list_sessions(
    current_user: CurrentUserDep,
    *,
    store: SessionStoreDep,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[SessionState]:
    sessions = []
    for session_id in store.list_ids()[:limit]:
        state = store.load(session_id)
        if state is not None:
            sessions.append(state)
    return sessions
