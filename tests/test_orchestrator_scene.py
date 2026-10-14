"""Camera scene context (`VisitorEvent.scene`) in the orchestrator: it feeds
the LLM prompts as a separately framed block, and never the deterministic
policy/intent layers or the visitor-speech fields of the audit trail.
"""

from datetime import UTC, datetime

from knock.conversation.policy import SAFE_RESPONSE_FALLBACK
from knock.conversation.responses import response_for
from knock.core.audit import AuditEntry
from knock.core.events import VisitorEvent
from knock.core.orchestrator import (
    Orchestrator,
    _classification_prompt,
    _notification_summary_prompt,
    _response_prompt,
)
from knock.core.scene import SceneContext, SceneObservation
from knock.core.state import SessionState


def _scene(**observation) -> SceneContext:
    return SceneContext(
        camera="cam1",
        detected_labels=["person"],
        observation=SceneObservation(**observation),
    )


def _event(text: str, scene: SceneContext | None = None) -> VisitorEvent:
    return VisitorEvent(source="test", text=text, timestamp=datetime.now(UTC), scene=scene)


class _FakeAuditLog:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


class _CapturingLLM:
    name = "capturing-llm"

    def __init__(self, reply: str = "Thanks, you can leave it by the door.") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.image_calls: list[list[bytes]] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply

    def generate_with_images(self, prompt: str, images: list[bytes]) -> str:
        self.prompts.append(prompt)
        self.image_calls.append(images)
        return self.reply


def test_prompts_are_unchanged_without_a_scene() -> None:
    assert _response_prompt("delivery", "hi") == _response_prompt("delivery", "hi", None)
    assert "Camera observations" not in _response_prompt("delivery", "hi")
    assert "Camera observations" not in _classification_prompt("hi")
    assert "Camera observations" not in _notification_summary_prompt("hi")


def test_scene_reaches_every_llm_prompt_as_its_own_block() -> None:
    scene = _scene(uniform_or_logo="UPS", summary="A person holding a box.")

    for prompt in (
        _response_prompt("delivery", "Hi there", scene),
        _classification_prompt("Hi there", scene),
        _notification_summary_prompt("Hi there", scene),
    ):
        assert 'A visitor at the door said or triggered: "Hi there"' in prompt or (
            'Context from a doorbell visit: "Hi there"' in prompt
        )
        assert "NOT something the visitor said" in prompt
        assert '- uniform/logo: "UPS"' in prompt
        # The camera output is never spliced into the quoted visitor speech.
        assert '"Hi there (camera' not in prompt


def test_response_prompt_tells_the_model_not_to_describe_the_visitor() -> None:
    prompt = _response_prompt("delivery", "Hi", _scene(summary="A person."))

    assert "never describe the visitor's appearance" in prompt


def test_scene_text_never_trips_the_deterministic_policy() -> None:
    # Visible text on a sign or shirt that happens to read like an
    # occupancy probe must not block the visitor's actual (benign) request
    # -- the policy only ever evaluates what the visitor said.
    llm = _CapturingLLM()
    decision = Orchestrator(llm_provider=llm).respond(
        _event(
            "Hi, I have an Amazon package",
            _scene(visible_text="Is anyone home right now?", summary="A sign."),
        )
    )

    assert decision.reason == "normal"
    assert decision.intent == "delivery"


def test_scene_text_never_drives_keyword_intent() -> None:
    # Before scene context existed, "...(camera also shows: a person holding
    # a package)" made the keyword classifier say "delivery" even when the
    # visitor said nothing of the sort.
    decision = Orchestrator().respond(
        _event("Hello?", _scene(carrying=["package"], package_visible=True))
    )

    assert decision.intent == "unknown"


def test_audit_entry_keeps_speech_and_scene_apart() -> None:
    audit = _FakeAuditLog()
    scene = _scene(summary="A person holding a box.")

    Orchestrator(audit_log=audit).respond(_event("Hi, I have an Amazon package", scene))

    entry = audit.entries[0]
    assert entry.text == "Hi, I have an Amazon package"
    assert entry.scene == scene


def test_audit_entry_without_scene_still_parses() -> None:
    legacy_line = (
        '{"timestamp": "2026-01-01T00:00:00Z", "text": "hi", "allowed": true, "reason": "normal"}'
    )

    assert AuditEntry.model_validate_json(legacy_line).scene is None


def test_image_is_not_sent_to_the_brain_by_default() -> None:
    llm = _CapturingLLM()

    Orchestrator(llm_provider=llm).respond(
        _event("Hi, I have an Amazon package"), image=b"fake-jpeg"
    )

    assert llm.image_calls == []


def test_image_reaches_the_brain_when_opted_in() -> None:
    llm = _CapturingLLM()

    decision = Orchestrator(llm_provider=llm, send_image_to_brain=True).respond(
        _event("Hi, I have an Amazon package", _scene(summary="A person.")),
        image=b"fake-jpeg",
    )

    assert llm.image_calls == [[b"fake-jpeg"]]
    assert decision.text == llm.reply


def test_opted_in_without_an_image_uses_plain_generate() -> None:
    llm = _CapturingLLM()

    Orchestrator(llm_provider=llm, send_image_to_brain=True).respond(
        _event("Hi, I have an Amazon package")
    )

    assert llm.image_calls == []
    assert llm.prompts  # still phrased by the LLM


def test_alarming_image_informed_reply_falls_back_to_canned() -> None:
    llm = _CapturingLLM(reply="I see you're holding a knife, please leave the box.")

    decision = Orchestrator(llm_provider=llm, send_image_to_brain=True).respond(
        _event("Hi, I have an Amazon package"), image=b"fake-jpeg"
    )

    assert decision.text == response_for("delivery")


def test_image_informed_reply_still_goes_through_the_disclosure_backstop() -> None:
    llm = _CapturingLLM(reply="Come on in, nobody's home right now.")
    state = SessionState(session_id="s1", turn_count=1)

    decision = Orchestrator(llm_provider=llm, send_image_to_brain=True).respond(
        _event("Hi, I have an Amazon package"), state=state, image=b"fake-jpeg"
    )

    assert decision.text == SAFE_RESPONSE_FALLBACK


def test_notification_details_fallback_carries_the_scene() -> None:
    llm = _CapturingLLM(reply="A UPS driver has a package for you")

    details = Orchestrator(llm_provider=llm).extract_notification_details(
        "Hi", "delivery", fallback="fallback", scene=_scene(uniform_or_logo="UPS")
    )

    assert details.summary == "A UPS driver has a package for you"
    assert '- uniform/logo: "UPS"' in llm.prompts[-1]
