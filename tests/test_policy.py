from knock.conversation.policy import PolicyEngine


def test_emergency_escalation() -> None:
    decision = PolicyEngine().evaluate("Help, medical emergency")
    assert decision.allowed is True
    assert decision.reason == "emergency"
    assert "emergency" in decision.flags
    assert decision.confidence["emergency"] >= 1.0
    assert decision.matched_rule_ids


def test_occupancy_blocking() -> None:
    decision = PolicyEngine().evaluate("Are you home right now?")
    assert decision.allowed is False
    assert decision.reason == "blocked_request"
    assert decision.flags == ["occupancy"]


def test_normal_text_is_allowed_with_no_flags() -> None:
    decision = PolicyEngine().evaluate("Hi, I have a package for you")
    assert decision.allowed is True
    assert decision.reason == "normal"
    assert decision.flags == []
    assert decision.matched_rule_ids == []
