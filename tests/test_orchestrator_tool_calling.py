"""Covers the opt-in tool-calling classification/notification-extraction
path (Orchestrator.use_tool_calling=True) -- plain-text classification
behavior is already covered by test_orchestrator.py and untouched by this
feature.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.providers.llm.ollama import ChatToolResult, ToolCall


def _event(text: str) -> VisitorEvent:
    return VisitorEvent(source="test", text=text, timestamp=datetime.now(UTC))


class _FakeToolCallingLLMProvider:
    """Conforms to LLMProvider (`generate`) plus the duck-typed
    `chat_with_tools` Orchestrator looks for -- not a real OllamaProvider
    subclass, matching this repo's existing per-file fake-provider
    convention (e.g. test_unifi_bridge.py's own `_SequencedLLMProvider`).
    """

    name = "fake-tool-llm"

    def __init__(
        self,
        chat_result: ChatToolResult | None = None,
        *,
        phrasing: str = "Thanks, noted.",
        raise_on_chat: Exception | None = None,
    ) -> None:
        self._chat_result = chat_result or ChatToolResult()
        self._phrasing = phrasing
        self._raise_on_chat = raise_on_chat
        self.chat_calls: list[tuple[list[dict[str, str]], list[dict[str, Any]]]] = []
        self.config = SimpleNamespace(
            intent_review_confidence_floor=0.5, intent_review_confidence_margin=0.15
        )

    def generate(self, prompt: str) -> str:
        return self._phrasing

    def chat_with_tools(
        self, messages: list[dict[str, str]], tools: list[dict[str, Any]]
    ) -> ChatToolResult:
        self.chat_calls.append((messages, tools))
        if self._raise_on_chat is not None:
            raise self._raise_on_chat
        return self._chat_result


def _classify_result(*candidates: tuple[str, float], flagged: bool = False) -> ChatToolResult:
    tool_calls = [
        ToolCall(
            name="classify_intent",
            arguments={
                "top_3": [{"category": cat, "confidence": conf} for cat, conf in candidates]
            },
        )
    ]
    if flagged:
        tool_calls.append(ToolCall(name="flag_for_review", arguments={"summary": "not sure"}))
    return ChatToolResult(tool_calls=tool_calls)


# -- classification ----------------------------------------------------------


def test_confident_single_winner_sets_intent_without_review() -> None:
    provider = _FakeToolCallingLLMProvider(_classify_result(("food_delivery", 0.95)))
    orchestrator = Orchestrator(llm_provider=provider, use_tool_calling=True)

    decision = orchestrator.respond(_event("some unrecognized phrasing"))

    assert decision.intent == "food_delivery"
    assert decision.needs_review is False


def test_low_confidence_top_candidate_triggers_review() -> None:
    provider = _FakeToolCallingLLMProvider(_classify_result(("visitation", 0.3)))
    orchestrator = Orchestrator(llm_provider=provider, use_tool_calling=True)

    decision = orchestrator.respond(_event("some unrecognized phrasing"))

    assert decision.intent == "visitation"
    assert decision.needs_review is True


def test_thin_margin_between_top_two_triggers_review() -> None:
    provider = _FakeToolCallingLLMProvider(
        _classify_result(("food_delivery", 0.6), ("visitation", 0.55))
    )
    orchestrator = Orchestrator(llm_provider=provider, use_tool_calling=True)

    decision = orchestrator.respond(_event("some unrecognized phrasing"))

    assert decision.needs_review is True
    assert decision.review_candidates == ["food_delivery", "visitation"]


def test_model_calling_flag_for_review_forces_review_even_at_high_confidence() -> None:
    provider = _FakeToolCallingLLMProvider(_classify_result(("official_visit", 0.95), flagged=True))
    orchestrator = Orchestrator(llm_provider=provider, use_tool_calling=True)

    decision = orchestrator.respond(_event("some unrecognized phrasing"))

    assert decision.intent == "official_visit"
    assert decision.needs_review is True
    assert decision.review_summary == "not sure"


def test_malformed_or_missing_classify_intent_tool_call_falls_back_to_unknown_with_review() -> None:
    provider = _FakeToolCallingLLMProvider(ChatToolResult(tool_calls=[], content=""))
    orchestrator = Orchestrator(llm_provider=provider, use_tool_calling=True)

    decision = orchestrator.respond(_event("some unrecognized phrasing"))

    assert decision.intent == "unknown"
    assert decision.needs_review is True


def test_tool_calling_disabled_by_default_never_calls_chat_with_tools() -> None:
    provider = _FakeToolCallingLLMProvider(_classify_result(("food_delivery", 0.95)))
    orchestrator = Orchestrator(llm_provider=provider)  # use_tool_calling defaults False

    decision = orchestrator.respond(_event("some unrecognized phrasing"))

    assert provider.chat_calls == []
    assert decision.needs_review is False
    # Plain-text path ran instead: classification + response generation,
    # both via generate() with the fake's canned phrasing.
    assert decision.text


def test_chat_with_tools_exception_falls_back_to_plain_text_path() -> None:
    provider = _FakeToolCallingLLMProvider(raise_on_chat=RuntimeError("ollama is down"))
    orchestrator = Orchestrator(llm_provider=provider, use_tool_calling=True)

    decision = orchestrator.respond(_event("some unrecognized phrasing"))

    assert decision.needs_review is False
    assert decision.intent == "unknown"  # fake's generate() reply doesn't match any category


# -- notification detail extraction -------------------------------------------


def _details_result(**fields: str) -> ChatToolResult:
    return ChatToolResult(
        tool_calls=[ToolCall(name="extract_notification_details", arguments=fields)]
    )


def test_extract_notification_details_uses_structured_fields_when_tool_calling_enabled() -> None:
    provider = _FakeToolCallingLLMProvider(
        _details_result(
            summary="Officer Jenkins, Springfield PD",
            organization="Springfield PD",
            visitor_name="Officer Jenkins",
        )
    )
    orchestrator = Orchestrator(llm_provider=provider, use_tool_calling=True)

    details = orchestrator.extract_notification_details(
        "I'm Officer Jenkins with Springfield PD", "official_visit", fallback="Someone is here."
    )

    assert details.summary == "Officer Jenkins, Springfield PD"
    assert details.organization == "Springfield PD"
    assert details.visitor_name == "Officer Jenkins"
    assert details.stated_purpose is None


def test_extract_notification_details_falls_back_when_tool_calling_disabled() -> None:
    provider = _FakeToolCallingLLMProvider(
        _details_result(summary="should not be used"), phrasing="A plain summary"
    )
    orchestrator = Orchestrator(llm_provider=provider)  # use_tool_calling defaults False

    details = orchestrator.extract_notification_details(
        "some text", "delivery", fallback="A delivery is here."
    )

    assert provider.chat_calls == []
    assert details.summary == "A plain summary"


def test_extract_notification_details_falls_back_when_chat_with_tools_raises() -> None:
    provider = _FakeToolCallingLLMProvider(raise_on_chat=RuntimeError("ollama is down"))
    orchestrator = Orchestrator(llm_provider=provider, use_tool_calling=True)

    details = orchestrator.extract_notification_details(
        "some text", "delivery", fallback="A delivery is here."
    )

    assert details.summary == "Thanks, noted."  # fake's generate() fallback phrasing


def test_extract_notification_details_falls_back_when_no_usable_tool_call() -> None:
    provider = _FakeToolCallingLLMProvider(ChatToolResult(tool_calls=[], content=""))
    orchestrator = Orchestrator(llm_provider=provider, use_tool_calling=True)

    details = orchestrator.extract_notification_details(
        "some text", "delivery", fallback="A delivery is here."
    )

    assert details.summary == "Thanks, noted."
