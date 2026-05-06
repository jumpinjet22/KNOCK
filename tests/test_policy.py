from knock.conversation.policy import PolicyEngine


def test_emergency_escalation() -> None:
    allowed, reason, flags = PolicyEngine().evaluate("Help, medical emergency")
    assert allowed is True
    assert reason == "emergency"
    assert "emergency" in flags


def test_occupancy_blocking() -> None:
    allowed, reason, _ = PolicyEngine().evaluate("Are you home right now?")
    assert allowed is False
    assert reason == "blocked_request"
