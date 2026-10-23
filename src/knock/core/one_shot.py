"""Answering a trigger once: load (or start) its session, ask the
orchestrator a single time, save the session.

This is the whole flow for the Frigate and Home Assistant bridges. It is
deliberately not `VisitRunner`: there is no listening or speaking here, no
idle reset (a session resumes however long ago it was) and no conversation
loop, and the orchestrator's own greeting stays in the reply text.
"""

from __future__ import annotations

from typing import Any

from knock.core.audit import AuditLog
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.responses import ResponseDecision
from knock.core.session_store import SessionStore
from knock.core.state import SessionState


def answer_once(
    orchestrator: Orchestrator,
    session_store: SessionStore,
    audit_log: AuditLog,
    session_id: str,
    event: VisitorEvent,
    **respond_kwargs: Any,
) -> ResponseDecision:
    """`respond_kwargs` go straight to `Orchestrator.respond` (e.g. `image=`)."""
    state = session_store.load(session_id) or SessionState(
        session_id=session_id, updated_at=event.timestamp
    )
    decision = orchestrator.respond(event, state=state, audit_log=audit_log, **respond_kwargs)
    session_store.save(state)
    return decision
