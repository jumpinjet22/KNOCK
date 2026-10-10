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


# -- apply_style's unsafe-disclosure backstop --------------------------------
#
# Found via adversarial testing: a visitor phrasing an entry request in a
# way that dodges PolicyEngine's exact "let me in" rule (e.g. "mind letting
# me inside?") reaches the LLM response-phrasing path, where the model
# sometimes actually replied with an invitation ("feel free to come in!")
# despite the prompt instruction against it. This is the deterministic
# backstop -- same pattern as providers/vision/safety.py's alarming-language
# filter -- that catches it regardless of what the model said.


def test_apply_style_suppresses_an_entry_invitation() -> None:
    styled = PolicyEngine().apply_style("Sure, feel free to come in for a moment!")
    assert styled == "Thanks, I'll pass that along."


def test_apply_style_catches_several_invitation_phrasings() -> None:
    engine = PolicyEngine()
    for phrase in [
        "Come on in, make yourself at home.",
        "You can enter through the side door.",
        "I'll let you in now.",
        "Head on in, it's unlocked.",
    ]:
        assert engine.apply_style(phrase) == "Thanks, I'll pass that along."


def test_apply_style_leaves_a_normal_response_unchanged() -> None:
    styled = PolicyEngine().apply_style("Thanks, I'll pass that along.")
    assert styled == "Thanks, I'll pass that along."


def test_apply_style_catches_literally_offering_to_open_the_door() -> None:
    # Found via a 20-trial live batch test: the literal phrase from the
    # rule this backstop exists to enforce ("never offer to unlock/open
    # the door") wasn't actually in the list -- only paraphrases of it
    # were. A model said "we open the door" outright and nothing caught
    # it until this was added.
    engine = PolicyEngine()
    for phrase in [
        "Thank you for delivering our food, please hold on while we open the door.",
        "Sure, let me unlock the door for you.",
        "No problem, I'll unlock it now.",
    ]:
        assert engine.apply_style(phrase) == "Thanks, I'll pass that along."


def test_apply_style_catches_soft_readiness_confirmations() -> None:
    # Softer than "they'll come"/"let X know", but still confirms a
    # specific person exists and is actively available right now -- found
    # in the same 20-trial batch.
    engine = PolicyEngine()
    for phrase in [
        "Please keep the food outside while I verify that the resident is ready to receive it.",
        "I'll let you know once someone is ready to take your delivery from here.",
    ]:
        assert engine.apply_style(phrase) == "Thanks, I'll pass that along."


def test_apply_style_suppresses_occupancy_confirming_phrasing() -> None:
    # Found via live verification after the entry-invitation fix: this
    # "let them know you're here" pattern is the model's own strong
    # default tendency for this kind of reply, independent of what the
    # prompt's example text suggests -- it was the dominant rejection
    # reason across an entire night's worth of synthetic training-data
    # review, across every teacher model, not a one-off slip.
    engine = PolicyEngine()
    for phrase in [
        "I'll let them know you're here for a signature.",
        "Sure, I'll let them know you stopped by.",
        "I'll let the homeowner know you've arrived.",
        "They'll come down in a minute.",
        "Someone will come grab it from you shortly.",
    ]:
        assert engine.apply_style(phrase) == "Thanks, I'll pass that along."


def test_apply_style_catches_a_direct_occupancy_statement() -> None:
    # The most severe gap found tonight: every *indirect* phrasing
    # ("let them know", "they'll come", "someone is ready") was covered,
    # but a flat, direct statement of presence or absence was somehow
    # never actually in this list. Found live: "no one is home to
    # receive this delivery" went completely unflagged. Revealing
    # absence is just as dangerous as revealing presence.
    engine = PolicyEngine()
    for phrase in [
        "So please know that no one is home to receive this delivery at the moment.",
        "Yes, someone is home right now.",
        "Nobody is home at the moment.",
        "We are home right now, thanks for asking.",
        "They're home, one moment please.",
    ]:
        assert engine.apply_style(phrase) == "Thanks, I'll pass that along."


def test_apply_style_does_not_flag_a_hedged_occupancy_decline() -> None:
    # The safe pattern this backstop must never suppress: explicitly
    # declining to confirm occupancy, rather than stating it outright.
    engine = PolicyEngine()
    for phrase in [
        "I cannot confirm whether anyone is currently available.",
        "I am not able to verify occupancy status.",
    ]:
        assert engine.apply_style(phrase) == phrase


def test_apply_style_catches_person_variants_with_awareness_claim() -> None:
    # A second, weaker model (llama3.1:8b) found still more variants of
    # the "name the person, claim they're aware you're here" pattern --
    # "the person"/"the person inside" used in place of "them"/"the
    # homeowner". The bare "I'll let them know."/"I'll let the person
    # know to expect a signature." variants this test used to include
    # are deliberately NOT here anymore -- see
    # test_apply_style_does_not_flag_a_bare_relay_to_a_third_party --
    # since neither makes any claim about present awareness, just relay
    # mechanism.
    engine = PolicyEngine()
    for phrase in [
        "I'll let the person inside know you're here.",
        "I'll let the person in the house know you're here.",
    ]:
        assert engine.apply_style(phrase) == "Thanks, I'll pass that along."


def test_apply_style_does_not_flag_a_bare_relay_to_a_third_party() -> None:
    # Reconsidered after discussion: "I'll let the resident know"/"I'll
    # notify the homeowner" only describes the relay *mechanism* -- it
    # doesn't assert anyone's physically present or reachable right now,
    # same information content as SAFE_RESPONSE_FALLBACK itself ("I'll
    # pass that along"). A home having a resident/homeowner isn't a
    # secret; whether someone's inside it *right now* is the actual thing
    # worth protecting, and bare relay phrasing doesn't claim that either
    # way. Previously over-flagged here before this file's "let/notify
    # someone know" rules were narrowed to require an explicit
    # you're-here/you're-there awareness claim alongside them.
    engine = PolicyEngine()
    for phrase in [
        "I'll let the resident know that a package is here.",
        "Thanks, I'll let the family know it's here.",
        "I'll notify the homeowner, give me a second.",
        "Let me notify them, give me a second.",
        "Thanks, I'll let them know.",
        "I'll let the person know to expect a signature.",
    ]:
        assert engine.apply_style(phrase) == phrase


def test_apply_style_catches_let_or_notify_someone_combined_with_awareness_claim() -> None:
    # The actual original defect, preserved: real audited training data
    # contained the verbatim phrase "I'll let the resident know you're
    # here" -- the "you're here"/"you're there" clause is what makes it
    # unsafe (an explicit claim that a named person is aware of this
    # visit right now, which only makes sense if they're home), not the
    # bare relay structure alone.
    engine = PolicyEngine()
    for phrase in [
        "I can't open the door, but I'll let whoever is inside know you're there!",
        "I'll let the resident know you're here.",
        "I'll notify the homeowner that you're here.",
    ]:
        assert engine.apply_style(phrase) == "Thanks, I'll pass that along."


def test_apply_style_still_catches_come_out_regardless_of_relay_wording() -> None:
    # Unaffected by the relay-wording narrowing above -- "come out" is
    # its own direct occupancy-confirmation phrase, matched independently
    # of whatever relay verb precedes it.
    styled = PolicyEngine().apply_style(
        "I'll let the appropriate person know so they can come out to sign for it."
    )
    assert styled == "Thanks, I'll pass that along."


def test_apply_style_does_not_flag_let_you_know() -> None:
    # "I'll let you know" -- addressed back to the visitor themselves, not
    # a third party -- is the established safe relay pattern and must not
    # get swept up by the generic "let ... know" regex above.
    styled = PolicyEngine().apply_style("Thanks, I will let you know when we are ready for pickup.")
    assert styled == "Thanks, I will let you know when we are ready for pickup."


def test_apply_style_does_not_flag_let_me_or_us_know() -> None:
    # Found live while testing a clarifying-question conversation flow:
    # the assistant asking the *visitor* to share info ("could you please
    # let me/us know what you need?") is exactly as safe as "let you
    # know" -- it's the visitor and assistant informing each other, never
    # a third party's presence. Was getting incorrectly suppressed before
    # "me"/"us" were added alongside "you" in the exclusion.
    engine = PolicyEngine()
    for phrase in [
        "Could you please let me know what you need?",
        "Could you please let us know what you need?",
    ]:
        assert engine.apply_style(phrase) == phrase


def test_apply_style_does_not_flag_notify_you_me_or_us() -> None:
    # Same "visitor and assistant informing each other" exclusion as
    # "let you/me/us know" -- "notify you/me/us" never discloses a third
    # party's presence.
    engine = PolicyEngine()
    for phrase in [
        "I'll notify you once it's ready.",
        "notify me if anything changes",
        "please notify us when you arrive",
    ]:
        assert engine.apply_style(phrase) == phrase


def test_apply_style_catches_verifying_with_a_resident() -> None:
    # Found live via the interactive correction feature: unlike bare
    # "notify X" (a one-way relay, safe alone), "verify/check/confirm
    # ... WITH my owner/the homeowner/the resident" implies a live,
    # two-way consultation happening right now -- a presence signal on
    # its own, no awareness-clause pairing needed. A correction attempt
    # produced "please wait at the door until I can verify their
    # identity with my owner" and it passed judge re-scoring untouched.
    engine = PolicyEngine()
    for phrase in [
        "please wait at the door until I can verify their identity with my owner",
        "I need to check that with the homeowner",
        "let me confirm with the resident",
        "I will check with my owner and get back to you",
    ]:
        assert engine.apply_style(phrase) == "Thanks, I'll pass that along."


def test_apply_style_does_not_flag_checking_with_the_visitor() -> None:
    # "with you" -- the visitor themselves, not a third party -- must not
    # get swept up by the structural "with ... owner/homeowner/resident"
    # regex above (it doesn't match "you" at all, but worth pinning).
    styled = PolicyEngine().apply_style("I need to check with you about that")
    assert styled == "I need to check with you about that"


def test_apply_style_still_truncates_to_140_chars() -> None:
    styled = PolicyEngine().apply_style("x" * 200)
    assert len(styled) == 140
