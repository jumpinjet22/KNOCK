"""The shared adversarial corpus (tests/safety_corpus.json) against KNOCK's
deterministic safety layers -- no model involved, so it runs on every PR.

scripts/redteam.py reads the same corpus and sends visitor_attacks to the
real model; this file checks the part that must hold whatever the model
says: the policy blocks what it should, and the apply_style() backstop
suppresses every known unsafe reply on every path through the orchestrator.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from knock.conversation.policy import SAFE_RESPONSE_FALLBACK, PolicyEngine
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.core.state import SessionState

CORPUS = json.loads((Path(__file__).parent / "safety_corpus.json").read_text())
ATTACKS = CORPUS["visitor_attacks"]


class _CompliantLLM:
    """Worst case: a model that does whatever the visitor talked it into."""

    name = "compliant-llm"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def generate(self, prompt: str) -> str:
        return self.reply


@pytest.mark.parametrize("reply", CORPUS["unsafe_replies"])
def test_backstop_suppresses_unsafe_reply(reply) -> None:
    assert PolicyEngine().apply_style(reply) == SAFE_RESPONSE_FALLBACK


@pytest.mark.parametrize("reply", CORPUS["safe_replies"])
def test_backstop_leaves_safe_reply_alone(reply) -> None:
    assert PolicyEngine().apply_style(reply) == reply


@pytest.mark.parametrize(
    "reply",
    [pytest.param(r, marks=pytest.mark.xfail(strict=True)) for r in CORPUS["known_gaps"]],
)
def test_known_gap_is_still_open(reply) -> None:
    # Strict xfail: once the backstop catches one of these, this "unexpectedly
    # passes" and fails CI -- move the reply to unsafe_replies.
    assert PolicyEngine().apply_style(reply) == SAFE_RESPONSE_FALLBACK


@pytest.mark.parametrize("attack", ATTACKS, ids=[a["id"] for a in ATTACKS])
@pytest.mark.parametrize("reply", CORPUS["unsafe_replies"])
def test_compliant_model_cannot_get_an_unsafe_reply_through(attack, reply) -> None:
    orchestrator = Orchestrator(llm_provider=_CompliantLLM(reply))
    state = SessionState(session_id="redteam", updated_at=datetime.now(UTC))
    event = VisitorEvent(source="redteam", text=attack["text"], timestamp=datetime.now(UTC))

    decision = orchestrator.respond(event, state=state)

    assert reply not in decision.text, (attack["id"], decision.text)


def test_corpus_attack_ids_are_unique() -> None:
    ids = [a["id"] for a in ATTACKS]
    assert len(ids) == len(set(ids))
