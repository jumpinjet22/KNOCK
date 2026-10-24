"""Generated replies that are confused fall back to the canned line (issue #51)."""

import pytest

from knock.conversation.responses import response_for
from knock.core.orchestrator import Orchestrator, _reply_is_confused


class _LLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def generate(self, prompt: str) -> str:
        return self.reply


@pytest.mark.parametrize(
    "bad",
    [
        "Thanks for stopping by; please wait just a moment while I notify you that we are aware.",
        "I'm sorry, but I cannot provide information about real-world locations.",
        "I'll check the camera feed for Jon's order.",
        "I am sorry, but I cannot answer the door for you at this time.",
        "As an AI language model I can't do that.",
    ],
)
def test_confused_replies_fall_back_to_the_canned_line(bad: str) -> None:
    assert _reply_is_confused(bad)
    orchestrator = Orchestrator(llm_provider=_LLM(bad))  # type: ignore[arg-type]
    assert orchestrator._text_for_intent("service_appointment", "I'm here for maintenance") == (
        response_for("service_appointment")
    )


@pytest.mark.parametrize(
    "good",
    [
        "Thanks, I'll pass that along.",
        "Sorry, we don't accept solicitations here. Please don't leave anything.",
        "You can leave the package by the door.",
    ],
)
def test_normal_replies_are_untouched(good: str) -> None:
    assert not _reply_is_confused(good)
    orchestrator = Orchestrator(llm_provider=_LLM(good))  # type: ignore[arg-type]
    assert orchestrator._text_for_intent("delivery", "package") == good


@pytest.mark.parametrize(
    "leak",
    [
        "I can tell them you mentioned Jon, but please wait here as I check if he is available.",
        "Jon is available right now.",
        "Please wait outside while I see.",
        "I'll check whether she is in.",
    ],
)
def test_availability_replies_fall_back_to_the_canned_line(leak: str) -> None:
    orchestrator = Orchestrator(llm_provider=_LLM(leak))  # type: ignore[arg-type]
    assert orchestrator._text_for_intent("person_lookup", "Can I speak to Jon?") == (
        response_for("person_lookup")
    )
