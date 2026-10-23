"""The visit runner with a fake host -- no UniFi anywhere. The same visit
behavior is pinned through the real bridge by
test_routing_characterization.py and test_doors_routing.py.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from knock.config import DoorsConfig, HomeAssistantConfig, UnifiConfig
from knock.conversation.prompts import GREETING
from knock.core.audit import AuditLog, NullAuditLog
from knock.core.doors import DoorConfig, resolve_doors
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.session_store import JSONFileSessionStore, SessionStore
from knock.core.state import SessionState
from knock.core.visit import VisitRunner
from knock.providers.vision.base import VisionProvider


class _Host:
    scene_wait_seconds = 1.0

    def __init__(self, tmp_path, *doors: DoorConfig, transcripts=("",)) -> None:
        self.config = SimpleNamespace(ring_chime_delay_seconds=0.0, ring_cooldown_seconds=30.0)
        self.doors = resolve_doors(
            UnifiConfig(), HomeAssistantConfig(), DoorsConfig(doors=list(doors))
        )
        self.orchestrator = Orchestrator()
        self.session_store: SessionStore = JSONFileSessionStore(tmp_path)
        self.audit_log: AuditLog = NullAuditLog()
        self.vision_provider: VisionProvider | None = None
        self.stt_provider: Any = object()
        self.tts_provider: Any = object()
        self.ha_notifier: Any = None
        self.calls: list[tuple[str, str]] = []
        self.spoken: list[str] = []
        self._transcripts = iter(transcripts)

    def build_event(self, event: Any) -> VisitorEvent:
        return VisitorEvent(source="fake", text="someone is here", timestamp=datetime.now(UTC))

    async def listen_to_visitor(self, device_id: str) -> str:
        self.calls.append(("listen", device_id))
        return next(self._transcripts, "")

    async def speak_to_visitor(self, device_id: str, text: str) -> None:
        self.calls.append(("speak", device_id))
        self.spoken.append(text)

    async def _play_thinking_tone(self, device_id: str) -> None:
        self.calls.append(("thinking", device_id))

    async def snapshot(self, device_id: str, *, package: bool = False) -> bytes | None:
        self.calls.append(("snapshot", device_id))
        return None

    async def _on_escalate_notify(self, *args: Any) -> None:
        pass

    async def _handle_turn_notifications(self, *args: Any) -> None:
        pass

    def devices(self, role: str) -> set[str]:
        return {device for r, device in self.calls if r == role}


def _event(device_id: str = "cam1", type_: str = "ring") -> Any:
    return SimpleNamespace(id="e1", type=type_, device_id=device_id, smart_detect_types=())


def _door(**kw) -> DoorConfig:
    fields: dict[str, Any] = {
        "key": "front",
        "triggers": {"protect_cameras": ["cam1"]},
        "listen_device": "mic",
        "speak_device": "speaker",
        "look_devices": ["eyes"],
    }
    return DoorConfig(**{**fields, **kw})


def test_a_visit_runs_on_a_host_that_is_not_unifi(tmp_path) -> None:
    host = _Host(tmp_path, transcripts=["I have a package for you", ""])

    decision = asyncio.run(VisitRunner(host).run(_event()))

    assert decision is not None
    assert host.spoken[0] == GREETING
    assert host.devices("listen") == host.devices("speak") == {"cam1"}
    assert host.session_store.load("unifi-cam1") is not None


def test_an_explicit_door_splits_the_visit_across_its_devices(tmp_path) -> None:
    host = _Host(tmp_path, _door(), transcripts=["hello", ""])

    asyncio.run(VisitRunner(host).run(_event()))

    assert host.devices("listen") == {"mic"}
    assert host.devices("speak") == {"speaker"}
    assert host.session_store.load("door-front") is not None


def test_a_ring_inside_the_cooldown_resumes_the_visit_instead_of_restarting(tmp_path) -> None:
    host = _Host(tmp_path)
    runner = VisitRunner(host)
    now = datetime.now(UTC)
    existing = SessionState(session_id="unifi-cam1", updated_at=now - timedelta(seconds=5))

    host.session_store.save(existing)
    resumed = runner._start_or_resume_session("unifi-cam1", _event(), now)
    assert resumed.updated_at == existing.updated_at

    stale = SessionState(session_id="unifi-cam1", updated_at=now - timedelta(minutes=10))
    host.session_store.save(stale)
    fresh = runner._start_or_resume_session("unifi-cam1", _event(), now)
    assert fresh.updated_at == now


def test_a_systems_check_phrase_is_answered_without_a_normal_reply(tmp_path) -> None:
    host = _Host(tmp_path, transcripts=["systems check", ""])
    host.orchestrator = Orchestrator()

    asyncio.run(VisitRunner(host).run(_event()))

    assert "No systems configured to check." in host.spoken
