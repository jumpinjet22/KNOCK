"""scripts/redteam_injection.py's plumbing, with fake models -- the real run
needs a local Ollama, so this only checks the script itself works: that the
mechanical canary/relay checks fire, that a safe model passes, and that the
report is shaped right.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from knock.core.orchestrator import Orchestrator

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location(
    "redteam_injection", _SCRIPTS / "redteam_injection.py"
)
assert _spec and _spec.loader
redteam_injection = importlib.util.module_from_spec(_spec)
sys.modules["redteam_injection"] = redteam_injection  # dataclasses look their module up here
_spec.loader.exec_module(redteam_injection)

CORPUS = json.loads((Path(redteam_injection.redteam.CORPUS_PATH)).read_text())["injection_attacks"]
SCENE = next(a for a in CORPUS if a["id"] == "sign-ignore-instructions")
RELAY = next(a for a in CORPUS if a["id"] == "relay-phishing-link")
PHONE_RELAY = next(a for a in CORPUS if a["id"] == "relay-bank-call")
_CANARY = re.compile(r"ZQ\d{4}")


class _FakeLLM:
    """Replies by what the prompt asks for: a summary prompt gets `summary`,
    anything else gets `reply`. Either may be a function of the prompt.
    """

    name = "fake"

    def __init__(self, reply="Thanks, I'll pass that along.", summary="A visitor is here.") -> None:
        self.reply = reply
        self.summary = summary

    def generate(self, prompt: str) -> str:
        chosen = self.summary if "Write one short phrase" in prompt else self.reply
        return chosen(prompt) if callable(chosen) else chosen

    def warm_up(self) -> bool:
        return True


class _Judge(_FakeLLM):
    def __init__(self, unsafe: bool = False) -> None:
        super().__init__(reply=json.dumps({"unsafe": unsafe, "reason": "because"}))


def _run(llm: _FakeLLM, attacks, judge: _Judge | None = None, trials: int = 2):
    orchestrator = Orchestrator(llm_provider=llm)
    return redteam_injection.run_injection(orchestrator, judge or _Judge(), attacks, trials)


def test_a_model_that_ignores_the_injection_passes(capsys) -> None:
    [result] = _run(_FakeLLM(), [SCENE])

    assert [t.verdict for t in result.trials] == ["safe", "safe"]
    assert result.failures == 0
    assert "ok" in capsys.readouterr().out


def test_a_reply_that_echoes_the_injected_token_is_followed() -> None:
    def echo(prompt: str) -> str:
        found = _CANARY.search(prompt)
        return found.group(0) if found else "ok"

    [result] = _run(_FakeLLM(reply=echo), [SCENE])

    assert {t.verdict for t in result.trials} == {"followed"}
    assert result.failures == 2


def test_a_reply_that_does_what_the_injection_wanted_is_followed_without_a_canary() -> None:
    vehicle = next(a for a in CORPUS if a["id"] == "vehicle-instruction")

    [result] = _run(_FakeLLM(reply="Fine to leave it, the family is away this week."), [vehicle])

    assert {t.verdict for t in result.trials} == {"followed"}
    assert "forbidden pattern" in result.trials[0].reason


def test_a_reply_that_merely_resembles_the_topic_is_not_followed() -> None:
    vehicle = next(a for a in CORPUS if a["id"] == "vehicle-instruction")

    [result] = _run(_FakeLLM(reply="It is fine to leave the package by the door."), [vehicle])

    assert {t.verdict for t in result.trials} == {"safe"}


def test_the_canary_is_fresh_for_every_trial() -> None:
    [result] = _run(_FakeLLM(), [SCENE], trials=4)

    assert len({t.canary for t in result.trials}) == 4


def test_a_notification_that_repeats_the_attackers_phone_number_is_relayed() -> None:
    [result] = _run(_FakeLLM(summary="Urgent: please call 555-0100"), [PHONE_RELAY])

    assert {t.verdict for t in result.trials} == {"relayed"}


def test_a_notification_that_repeats_the_token_is_relayed() -> None:
    def echo(prompt: str) -> str:
        found = _CANARY.search(prompt)
        return f"Visitor says {found.group(0)}" if found else "ok"

    [result] = _run(_FakeLLM(summary=echo), [RELAY])

    assert {t.verdict for t in result.trials} == {"relayed"}


def test_the_judge_catches_what_the_mechanical_checks_cannot() -> None:
    [result] = _run(_FakeLLM(), [SCENE], judge=_Judge(unsafe=True))

    assert {t.verdict for t in result.trials} == {"unsafe"}
    assert result.trials[0].reason == "because"


def test_a_camera_attack_puts_the_payload_in_the_scene_not_the_speech() -> None:
    prompts: list[str] = []

    def record(prompt: str) -> str:
        prompts.append(prompt)
        return "ok"

    _run(_FakeLLM(reply=record, summary=record), [SCENE], trials=1)

    assert any("visible text (data only)" in p for p in prompts)
    assert any('said or triggered: "Hi, I have a package for you."' in p for p in prompts)
    for prompt in prompts:
        visitor_lines = [line for line in prompt.split("\n") if "said or triggered" in line]
        assert not any("IGNORE ALL PREVIOUS" in line for line in visitor_lines)


def test_a_relay_attack_is_spoken_with_no_scene() -> None:
    prompts: list[str] = []

    def record(prompt: str) -> str:
        prompts.append(prompt)
        return "ok"

    _run(_FakeLLM(reply=record, summary=record), [RELAY], trials=1)

    assert prompts and not any("Camera observations" in p for p in prompts)
    assert any("evil.example" in p for p in prompts)


@pytest.mark.parametrize(
    ("field", "expected"),
    [("carrying", "carrying"), ("visible_text", "visible_text"), ("summary", "summary")],
)
def test_every_scene_field_in_the_corpus_builds_a_scene(field, expected) -> None:
    attack = next(a for a in CORPUS if a["surface"] == "scene" and a["field"] == field)

    scene = redteam_injection.scene_for(attack, "PAYLOAD")

    assert scene is not None and scene.observation is not None
    assert "PAYLOAD" in json.dumps(scene.observation.model_dump())
    assert expected in attack["field"]


def test_label_attacks_are_left_to_the_deterministic_tests() -> None:
    assert {a["surface"] for a in CORPUS if a["surface"] in redteam_injection.SURFACES} == {
        "scene",
        "relay",
    }
    assert redteam_injection.scene_for({"surface": "label", "field": "carrier"}, "x") is None


def test_relays_attacker_content_finds_phone_numbers_and_links() -> None:
    payload = "call 555-0100 or http://evil.example/x"
    relays = redteam_injection.relays_attacker_content

    assert relays("Please call 555-0100", "ZQ1", payload)
    assert relays("see http://evil.example/x", "ZQ1", payload)
    assert relays("says ZQ1", "ZQ1", payload)
    assert not relays("A delivery needs a signature", "ZQ1", payload)
