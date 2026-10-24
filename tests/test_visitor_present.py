"""Stay silent when nobody is at the door -- ring-and-ditch (issue #58)."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from knock.core.conversation_runner import ConversationHooks, run_conversation_loop
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.scene import SceneObservation
from knock.core.state import SessionState
from knock.core.visit import VisitRunner


class _Store:
    def save(self, state: Any) -> None: ...

    def load(self, session_id: str) -> None:
        return None


def _event() -> VisitorEvent:
    return VisitorEvent(source="x", text="Doorbell ring on cam", timestamp=datetime.now(UTC))


def test_loop_stays_silent_when_the_presence_hook_says_nobody() -> None:
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    async def nobody() -> bool:
        return False

    result = asyncio.run(
        run_conversation_loop(
            _event(),
            state=SessionState(session_id="s"),
            orchestrator=Orchestrator(),
            session_store=_Store(),  # type: ignore[arg-type]
            hooks=ConversationHooks(speak=speak, confirm_visitor_present=nobody),
            audit_log=None,
            max_turns=3,
            scene_wait_seconds=0.1,
        )
    )
    assert result is None
    assert spoken == []


class _Host:
    def __init__(self, counts: list[int | None], snapshot: bytes | None = b"img") -> None:
        self.config = SimpleNamespace(visitor_check_attempts=3, visitor_check_interval_seconds=0.0)
        self.vision_provider = object()
        self.snapshots = 0
        self.notified: list[str] = []
        self._counts = iter(counts)
        self._snapshot = snapshot

    async def snapshot(self, device_id: str, *, package: bool = False) -> bytes | None:
        self.snapshots += 1
        return self._snapshot

    async def _notify_nobody_visible(self, device_id: str) -> None:
        self.notified.append(device_id)


def _check(host: _Host, monkeypatch, event: Any = None) -> bool:
    import knock.core.visit as visit

    def fake_observe(provider: Any, image: bytes) -> SceneObservation:
        return SceneObservation(people_count=next(host._counts))

    monkeypatch.setattr(visit, "observe_scene", fake_observe)
    ev = event or SimpleNamespace(type="ring", smart_detect_types=())
    return asyncio.run(VisitRunner(host)._visitor_is_present(ev, "cam1"))  # type: ignore[arg-type]


def test_nobody_on_every_attempt_means_absent_and_notifies(monkeypatch) -> None:
    host = _Host([0, 0, 0])
    assert _check(host, monkeypatch) is False
    assert host.snapshots == 3
    assert host.notified == ["cam1"]


def test_a_person_on_a_later_attempt_counts_as_present(monkeypatch) -> None:
    host = _Host([0, 1])
    assert _check(host, monkeypatch) is True
    assert host.notified == []


def test_unknown_count_or_no_snapshot_assumes_present(monkeypatch) -> None:
    assert _check(_Host([None]), monkeypatch) is True
    assert _check(_Host([], snapshot=None), monkeypatch) is True


def test_a_person_detection_skips_the_look(monkeypatch) -> None:
    host = _Host([0, 0, 0])
    event = SimpleNamespace(type="smartDetectZone", smart_detect_types=["person"])
    assert _check(host, monkeypatch, event) is True
    assert host.snapshots == 0


def test_a_vision_failure_never_silences_a_visitor(monkeypatch) -> None:
    import knock.core.visit as visit

    def boom(provider: Any, image: bytes) -> SceneObservation:
        raise RuntimeError("vision down")

    monkeypatch.setattr(visit, "observe_scene", boom)
    host = _Host([])
    assert asyncio.run(
        VisitRunner(host)._visitor_is_present(SimpleNamespace(type="ring"), "cam1")  # type: ignore[arg-type]
    )
