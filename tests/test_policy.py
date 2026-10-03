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


def test_schedule_probe_is_blocked() -> None:
    decision = PolicyEngine().evaluate("What's your schedule like this week?")
    assert decision.allowed is False
    assert decision.reason == "blocked_request"
    assert decision.flags == ["schedule"]


def test_the_word_scheduled_does_not_false_positive_the_schedule_block() -> None:
    # Found via live testing: the schedule rule's phrase used to be the bare
    # word "schedule", which matched as a substring inside "scheduled" --
    # so a technician saying "here for the scheduled furnace tune-up" got
    # hard-blocked with "Sorry, I can't share that information" instead of
    # being recognized as a normal service_appointment. The rule phrase is
    # "your schedule" now, specific enough not to collide with "scheduled".
    decision = PolicyEngine().evaluate("Here for the scheduled furnace tune-up")
    assert decision.allowed is True
    assert decision.reason == "normal"
    assert decision.flags == []


def test_normal_text_is_allowed_with_no_flags() -> None:
    decision = PolicyEngine().evaluate("Hi, I have a package for you")
    assert decision.allowed is True
    assert decision.reason == "normal"
    assert decision.flags == []
    assert decision.matched_rule_ids == []
