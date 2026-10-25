import json
from datetime import UTC, datetime

import pytest

from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.scenario_filter import scenario_is_junk
from knock.core.training import (
    TrainingReview,
    TrainingReviewStore,
    example_key,
    export_training_jsonl,
)


@pytest.mark.parametrize(
    "junk",
    [
        "Here are five realistic things a visitor might say at a smart doorbell:",
        "1. I have a package for you",
        "Sure thing, let them know I'm outside!",
        "Hi there, appreciate you stopping by, I'll be at the door in a sec to pick up the food",
        '"We\'re good, just leave it by the door, thanks."',
        "No thanks, we don't take campaign stuff at the door",
        "Thanks, drop it off on the left side of the porch if you could",
    ],
)
def test_homeowner_voice_and_preambles_are_junk(junk: str) -> None:
    assert scenario_is_junk(junk)


@pytest.mark.parametrize(
    "ok",
    [
        "I have a package for you",
        "DoorDash delivery for apartment 4B",
        "Hi, I'm here about the scheduled maintenance",
        "Can I speak to Jon?",
        "Hello, I'm your neighbor, just stopping by to say hi",
    ],
)
def test_real_visitor_lines_are_kept(ok: str) -> None:
    assert not scenario_is_junk(ok)


def test_export_can_drop_replies_a_filter_replaced(tmp_path, monkeypatch) -> None:
    log = JSONLAuditLog(tmp_path / "audit.jsonl")
    kept = AuditEntry(
        timestamp=datetime.now(UTC),
        text="a",
        response_text="Model words.",
        allowed=True,
        reason="normal",
        intent="delivery",
        raw_response_text="Model words.",
    )
    swapped = AuditEntry(
        timestamp=datetime.now(UTC),
        text="b",
        response_text="Thanks, I'll pass that along.",
        allowed=True,
        reason="normal",
        intent="visitation",
        raw_response_text="I'll check with the homeowner.",
        replaced_by="policy",
    )
    for entry in (kept, swapped):
        log.record(entry)
    store = TrainingReviewStore(tmp_path / "reviews.json")
    for entry in (kept, swapped):
        store.set(example_key(entry), TrainingReview(status="approved"))

    everything = export_training_jsonl(log, store)
    filtered = export_training_jsonl(log, store, drop_replaced=True)

    assert len(everything.splitlines()) == 4
    outputs = [json.loads(line)["output"] for line in filtered.splitlines()]
    assert "Model words." in outputs
    assert "Thanks, I'll pass that along." not in outputs
    assert "visitation" in outputs  # the intent label of the swapped entry is kept
    assert len(outputs) == 3


def test_orchestrator_records_the_models_words_and_why_they_were_replaced(tmp_path) -> None:
    from knock.core.events import VisitorEvent
    from knock.core.orchestrator import Orchestrator

    class _LLM:
        def generate(self, prompt: str) -> str:
            return "I will notify you shortly."

    log = JSONLAuditLog(tmp_path / "a.jsonl")
    Orchestrator(llm_provider=_LLM(), audit_log=log).respond(  # type: ignore[arg-type]
        VisitorEvent(source="x", text="I'm here for maintenance", timestamp=datetime.now(UTC))
    )
    entry = log.recent(limit=1)[0]
    assert entry.raw_response_text == "I will notify you shortly."
    assert entry.replaced_by == "confused"
    assert entry.response_text != entry.raw_response_text
