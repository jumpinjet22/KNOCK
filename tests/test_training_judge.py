from datetime import UTC, datetime

from knock.core.audit import AuditEntry
from knock.core.scene import SceneContext, SceneObservation
from knock.core.training_judge import (
    AggregatedJudgeResult,
    JudgeAxisScores,
    aggregate_scores,
    build_classification_dpo_pairs,
    build_dpo_pairs,
    classify_scenario_voice,
    group_by_intent,
    group_by_scenario,
    score_candidate,
)


def _entry(
    text: str = "Hi, I have a package for you",
    response_text: str = "Thanks, leave it by the door.",
    intent: str | None = "delivery",
    scene: SceneContext | None = None,
) -> AuditEntry:
    return AuditEntry(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        text=text,
        response_text=response_text,
        matched_flags={},
        matched_rule_ids=[],
        allowed=True,
        reason="normal",
        intent=intent,
        scene=scene,
    )


def _scores(
    visitor_voice: int = 8,
    category_correct: int = 8,
    safety_compliant: int = 8,
    natural_quality: int = 8,
    judge_model: str = "judge-a",
) -> JudgeAxisScores:
    return JudgeAxisScores(
        visitor_voice=visitor_voice,
        category_correct=category_correct,
        safety_compliant=safety_compliant,
        natural_quality=natural_quality,
        reason="looks fine",
        judge_model=judge_model,
    )


# -- classify_scenario_voice ----------------------------------------------------


class _FakeVoiceProvider:
    name = "fake-voice"

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class _FailingProvider:
    name = "failing"

    def generate(self, prompt: str) -> str:
        raise RuntimeError("ollama is down")


def test_classify_scenario_voice_accepts_visitor_reply() -> None:
    provider = _FakeVoiceProvider("VISITOR")
    assert classify_scenario_voice(provider, "I have a package for you") == "visitor"


def test_classify_scenario_voice_rejects_not_visitor_reply() -> None:
    provider = _FakeVoiceProvider("NOT_VISITOR")
    assert classify_scenario_voice(provider, "We're not interested, thanks") == "not_visitor"


def test_classify_scenario_voice_fails_open_on_exception() -> None:
    assert classify_scenario_voice(_FailingProvider(), "anything") == "visitor"


def test_classify_scenario_voice_fails_open_on_unparseable_reply() -> None:
    provider = _FakeVoiceProvider("uhh not sure")
    assert classify_scenario_voice(provider, "anything") == "visitor"


# -- score_candidate --------------------------------------------------------------


class _FakeJudgeProvider:
    name = "fake-judge"

    def __init__(self, response: str) -> None:
        self.response = response

    def generate(self, prompt: str) -> str:
        return self.response


def test_score_candidate_parses_valid_json() -> None:
    # visitor_voice is deliberately NOT part of this bundled JSON anymore
    # -- see training_judge.py's module docstring on why it's scored via
    # its own isolated call instead. Passed explicitly here since this
    # test is about the other three axes' parsing.
    raw = (
        '{"category_correct": 8, "safety_compliant": 10, "natural_quality": 7, '
        '"reason": "solid reply"}'
    )
    result = score_candidate(
        _FakeJudgeProvider(raw),
        "judge-a",
        "visitor text",
        "delivery",
        "response text",
        ["delivery"],
        visitor_voice_score=9,
    )
    assert result is not None
    assert result.visitor_voice == 9
    assert result.category_correct == 8
    assert result.safety_compliant == 10
    assert result.natural_quality == 7
    assert result.judge_model == "judge-a"


def test_score_candidate_handles_json_wrapped_in_extra_text() -> None:
    raw = (
        "Sure, here is my answer:\n"
        '{"category_correct": 5, "safety_compliant": 5, "natural_quality": 5, "reason": "ok"}\n'
        "hope that helps"
    )
    result = score_candidate(
        _FakeJudgeProvider(raw),
        "judge-a",
        "text",
        "delivery",
        "response",
        ["delivery"],
        visitor_voice_score=5,
    )
    assert result is not None
    assert result.category_correct == 5


def test_score_candidate_coerces_string_and_float_scores() -> None:
    raw = '{"category_correct": 7.0, "safety_compliant": 9, "natural_quality": 6, "reason": "ok"}'
    result = score_candidate(
        _FakeJudgeProvider(raw),
        "judge-a",
        "text",
        "delivery",
        "response",
        ["delivery"],
        visitor_voice_score=8,
    )
    assert result is not None
    assert result.visitor_voice == 8
    assert result.category_correct == 7


def test_score_candidate_falls_back_to_isolated_voice_check_when_not_provided() -> None:
    # No visitor_voice_score passed -- score_candidate must fall back to
    # calling score_visitor_voice() itself on the same provider, rather
    # than silently defaulting to 0 or erroring.
    raw = '{"category_correct": 8, "safety_compliant": 8, "natural_quality": 8, "reason": "ok"}'
    provider = _FakeJudgeProvider(raw)
    result = score_candidate(provider, "judge-a", "text", "delivery", "response", ["delivery"])
    assert result is not None
    # The fake provider returns the same JSON for every call, including
    # the isolated voice-check call -- that text contains neither
    # "RESIDENT" nor "NOT_VISITOR", so classify_scenario_voice's
    # fail-open default ("visitor") applies, mapping to the fixed
    # "confident visitor" score.
    assert result.visitor_voice == 9


def test_score_candidate_returns_none_on_unparseable_reply() -> None:
    result = score_candidate(
        _FakeJudgeProvider("not json at all"),
        "judge-a",
        "text",
        "delivery",
        "response",
        ["delivery"],
    )
    assert result is None


def test_score_candidate_returns_none_on_exception() -> None:
    result = score_candidate(
        _FailingProvider(), "judge-a", "text", "delivery", "response", ["delivery"]
    )
    assert result is None


# -- aggregate_scores ---------------------------------------------------------------


def test_aggregate_scores_averages_category_and_quality() -> None:
    scores = [
        _scores(category_correct=6, natural_quality=8, judge_model="a"),
        _scores(category_correct=10, natural_quality=6, judge_model="b"),
    ]
    result = aggregate_scores(scores)
    assert result.category_correct_avg == 8.0
    assert result.natural_quality_avg == 7.0


def test_aggregate_scores_minority_veto_on_visitor_voice() -> None:
    # One judge flags it as not-visitor-voice (<=3) -- the whole thing is
    # vetoed even though the other judges scored it highly.
    scores = [
        _scores(visitor_voice=9, judge_model="a"),
        _scores(visitor_voice=9, judge_model="b"),
        _scores(visitor_voice=2, judge_model="c"),
    ]
    result = aggregate_scores(scores)
    assert result.voice_veto is True


def test_aggregate_scores_minority_veto_on_safety() -> None:
    scores = [
        _scores(safety_compliant=9, judge_model="a"),
        _scores(safety_compliant=1, judge_model="b"),
    ]
    result = aggregate_scores(scores)
    assert result.safety_veto is True


def test_aggregate_scores_no_veto_when_all_judges_agree_its_fine() -> None:
    scores = [_scores(judge_model="a"), _scores(judge_model="b")]
    result = aggregate_scores(scores)
    assert result.voice_veto is False
    assert result.safety_veto is False


def test_aggregate_scores_surfaces_disagreement() -> None:
    scores = [
        _scores(category_correct=2, judge_model="a"),
        _scores(category_correct=10, judge_model="b"),
    ]
    result = aggregate_scores(scores)
    assert result.disagreement > 0


def test_aggregate_scores_zero_disagreement_for_single_judge() -> None:
    result = aggregate_scores([_scores(judge_model="a")])
    assert result.disagreement == 0.0


def test_aggregate_scores_fails_closed_on_empty_list() -> None:
    # No judge could evaluate this at all -- that's not evidence of
    # safety, so both vetoes are True rather than defaulting to "fine."
    result = aggregate_scores([])
    assert result.voice_veto is True
    assert result.safety_veto is True
    assert result.per_judge == []


# -- grouping -----------------------------------------------------------------------


def test_group_by_scenario_groups_shared_visitor_text() -> None:
    a = _entry(text="Hi, I have a package")
    b = _entry(text="Hi, I have a package", response_text="Different reply")
    c = _entry(text="Pizza delivery")
    groups = group_by_scenario([a, b, c])
    assert len(groups["Hi, I have a package"]) == 2
    assert len(groups["Pizza delivery"]) == 1


def test_group_by_intent_excludes_none_intent() -> None:
    a = _entry(intent="delivery")
    b = _entry(intent=None)
    c = _entry(intent="food_delivery")
    groups = group_by_intent([a, b, c])
    assert "delivery" in groups
    assert "food_delivery" in groups
    assert sum(len(v) for v in groups.values()) == 2


# -- DPO pair construction -----------------------------------------------------------


def _ok_result(
    category_correct: float = 8.0, natural_quality: float = 8.0
) -> AggregatedJudgeResult:
    return AggregatedJudgeResult(
        visitor_voice_avg=9.0,
        category_correct_avg=category_correct,
        safety_compliant_avg=9.0,
        natural_quality_avg=natural_quality,
        voice_veto=False,
        safety_veto=False,
        disagreement=0.0,
        per_judge=[],
    )


def test_build_dpo_pairs_creates_pair_from_same_text_and_intent() -> None:
    good = _entry(response_text="Thanks, leave it by the door.")
    bad = _entry(response_text="I'll let the resident know it's here.")
    candidates = [
        (good, _ok_result(category_correct=9, natural_quality=9)),
        (bad, _ok_result(category_correct=3, natural_quality=3)),
    ]
    pairs = build_dpo_pairs(candidates, margin_threshold=2.0)
    assert len(pairs) == 1
    assert pairs[0].chosen == good.response_text
    assert pairs[0].rejected == bad.response_text


def test_build_dpo_pairs_discards_pairs_below_margin_threshold() -> None:
    a = _entry(response_text="Response A")
    b = _entry(response_text="Response B")
    candidates = [
        (a, _ok_result(category_correct=8, natural_quality=8)),
        (b, _ok_result(category_correct=8.5, natural_quality=8)),
    ]
    pairs = build_dpo_pairs(candidates, margin_threshold=2.0)
    assert pairs == []


def test_build_dpo_pairs_excludes_vetoed_candidates() -> None:
    good = _entry(response_text="Good response")
    vetoed = _entry(response_text="I'll let them know you're here")
    vetoed_result = AggregatedJudgeResult(
        visitor_voice_avg=9.0,
        category_correct_avg=9.0,
        safety_compliant_avg=1.0,
        natural_quality_avg=9.0,
        voice_veto=False,
        safety_veto=True,
        disagreement=0.0,
        per_judge=[],
    )
    candidates = [(good, _ok_result()), (vetoed, vetoed_result)]
    pairs = build_dpo_pairs(candidates, margin_threshold=2.0)
    assert pairs == []


def test_build_dpo_pairs_ignores_candidates_with_different_text_or_intent() -> None:
    a = _entry(text="scenario one", response_text="A")
    b = _entry(text="scenario two", response_text="B")
    candidates = [
        (a, _ok_result(category_correct=9, natural_quality=9)),
        (b, _ok_result(category_correct=2, natural_quality=2)),
    ]
    pairs = build_dpo_pairs(candidates, margin_threshold=2.0)
    assert pairs == []


def test_build_dpo_pairs_respects_chosen_allowed_gate() -> None:
    unapproved_good = _entry(response_text="Great response")
    approved_worse = _entry(response_text="Worse response")
    candidates = [
        (unapproved_good, _ok_result(category_correct=10, natural_quality=10)),
        (approved_worse, _ok_result(category_correct=2, natural_quality=2)),
    ]
    pairs = build_dpo_pairs(
        candidates,
        margin_threshold=2.0,
        chosen_allowed=lambda e: e is approved_worse,
    )
    # unapproved_good scored higher but isn't allowed to be "chosen", and
    # approved_worse scored too low to win on its own -- no valid pair.
    assert pairs == []


def test_build_dpo_pairs_ignores_candidates_with_different_scene() -> None:
    # Codex PR review finding, verified before fixing: same text/intent but
    # different real camera context means these aren't comparable
    # completions of the same prompt -- pairing them would train on a
    # prompt the "rejected" side never actually saw.
    same_text = "Hi, I have a package for you"
    seen_package = _entry(
        text=same_text,
        response_text="Thanks, leave it by the door.",
        scene=SceneContext(observation=SceneObservation(package_visible=True)),
    )
    seen_nothing = _entry(
        text=same_text,
        response_text="I'll let the resident know it's here.",
        scene=SceneContext(observation=SceneObservation(package_visible=False)),
    )
    candidates = [
        (seen_package, _ok_result(category_correct=9, natural_quality=9)),
        (seen_nothing, _ok_result(category_correct=2, natural_quality=2)),
    ]
    pairs = build_dpo_pairs(candidates, margin_threshold=2.0)
    assert pairs == []


def test_build_dpo_pairs_pairs_candidates_with_equivalent_scene() -> None:
    # Both None, and both "nothing observed" (the common synthetic-data
    # case, which never attaches a scene at all), must still count as
    # equivalent -- this guard shouldn't block ordinary same-scene pairs.
    same_text = "Hi, I have a package for you"
    good = _entry(text=same_text, response_text="Thanks, leave it by the door.", scene=None)
    bad = _entry(
        text=same_text,
        response_text="I'll let the resident know it's here.",
        scene=SceneContext(observation=SceneObservation()),
    )
    candidates = [
        (good, _ok_result(category_correct=9, natural_quality=9)),
        (bad, _ok_result(category_correct=2, natural_quality=2)),
    ]
    pairs = build_dpo_pairs(candidates, margin_threshold=2.0)
    assert len(pairs) == 1


def test_build_classification_dpo_pairs_ignores_candidates_with_different_scene() -> None:
    same_text = "I have a package"
    correct = _entry(
        text=same_text,
        intent="delivery",
        scene=SceneContext(observation=SceneObservation(package_visible=True)),
    )
    wrong = _entry(
        text=same_text,
        intent="food_delivery",
        scene=SceneContext(observation=SceneObservation(package_visible=False)),
    )
    candidates = [
        (correct, _ok_result(category_correct=9)),
        (wrong, _ok_result(category_correct=2)),
    ]
    pairs = build_classification_dpo_pairs(candidates, margin_threshold=2.0)
    assert pairs == []


def test_build_classification_dpo_pairs_creates_pair_across_intents() -> None:
    correct = _entry(text="I have a package", intent="delivery")
    wrong = _entry(text="I have a package", intent="food_delivery")
    candidates = [
        (correct, _ok_result(category_correct=9)),
        (wrong, _ok_result(category_correct=2)),
    ]
    pairs = build_classification_dpo_pairs(candidates, margin_threshold=2.0)
    assert len(pairs) == 1
    assert pairs[0].chosen == "delivery"
    assert pairs[0].rejected == "food_delivery"


def test_build_classification_dpo_pairs_ignores_same_intent() -> None:
    a = _entry(text="scenario", intent="delivery")
    b = _entry(text="scenario", intent="delivery")
    candidates = [
        (a, _ok_result(category_correct=9)),
        (b, _ok_result(category_correct=2)),
    ]
    pairs = build_classification_dpo_pairs(candidates, margin_threshold=2.0)
    assert pairs == []
