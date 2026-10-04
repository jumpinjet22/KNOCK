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


# -- paraphrase coverage -- a pressure-test pass found the original rule
# phrases only caught the literal keyword, so asking the exact same thing a
# different way walked straight past the deterministic block/escalate path
# into the LLM-generated response, which is only soft-instructed not to
# leak. rules.json was broadened; these pin that the common paraphrasings
# are now actually caught, not just the original literal phrasing. ---------


def test_occupancy_probe_paraphrases_are_blocked() -> None:
    for text in ["Is anybody home?", "Anybody home?", "Is somebody home right now?"]:
        decision = PolicyEngine().evaluate(text)
        assert decision.allowed is False, text
        assert decision.reason == "blocked_request", text
        assert decision.flags == ["occupancy"], text


def test_schedule_probe_paraphrases_are_blocked() -> None:
    for text in ["When do you usually leave for work?", "What's your daily routine like?"]:
        decision = PolicyEngine().evaluate(text)
        assert decision.allowed is False, text
        assert decision.reason == "blocked_request", text
        assert decision.flags == ["schedule"], text


def test_unlock_probe_paraphrases_are_blocked() -> None:
    for text in ["Can you buzz me in?", "Could you crack the door for me?"]:
        decision = PolicyEngine().evaluate(text)
        assert decision.allowed is False, text
        assert decision.reason == "blocked_request", text
        assert decision.flags == ["unlock"], text


def test_emergency_paraphrases_escalate() -> None:
    for text in [
        "There's a break-in happening next door",
        "He has a gun",
        "Someone's unconscious on the porch",
    ]:
        decision = PolicyEngine().evaluate(text)
        assert decision.allowed is True, text
        assert decision.reason == "emergency", text


def test_break_in_does_not_false_positive_on_taking_a_break() -> None:
    # The broadened emergency phrase is "there's a break-in" / "someone
    # broke in", not bare "break in" -- that would have collided with
    # ordinary phrases like this one, the same class of bug the
    # "scheduled" test above already guards against for a different rule.
    decision = PolicyEngine().evaluate("Just taking a quick break in between deliveries")
    assert decision.allowed is True
    assert decision.reason == "normal"
    assert decision.flags == []
