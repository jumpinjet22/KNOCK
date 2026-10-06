from knock.core.training_correction import correct_response, run_correction_loop
from knock.providers.llm.base import LLMProvider


class _FakeCorrector:
    name = "fake-corrector"

    def __init__(self, response: str = "Corrected reply.") -> None:
        self.response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class _FailingProvider:
    name = "failing"

    def generate(self, prompt: str) -> str:
        raise RuntimeError("down")


def _judge_json(visitor_voice=9, category_correct=9, safety_compliant=9, natural_quality=9) -> str:
    return (
        f'{{"visitor_voice": {visitor_voice}, "category_correct": {category_correct}, '
        f'"safety_compliant": {safety_compliant}, "natural_quality": {natural_quality}, '
        '"reason": "ok"}'
    )


class _FakeJudge:
    def __init__(self, name: str, response: str) -> None:
        self.name = name
        self.response = response

    def generate(self, prompt: str) -> str:
        return self.response


# -- correct_response ----------------------------------------------------------------


def test_correct_response_returns_corrected_text() -> None:
    corrector = _FakeCorrector("I'm sorry, I can't share that.")
    result = correct_response(corrector, "visitor text", "unknown", "bad response", "unsafe")
    assert result == "I'm sorry, I can't share that."


def test_correct_response_falls_back_to_original_on_exception() -> None:
    result = correct_response(_FailingProvider(), "text", "unknown", "original bad", "unsafe")
    assert result == "original bad"


def test_correct_response_falls_back_to_original_on_blank_reply() -> None:
    corrector = _FakeCorrector("   ")
    result = correct_response(corrector, "text", "unknown", "original bad", "unsafe")
    assert result == "original bad"


# -- run_correction_loop -------------------------------------------------------------


def test_run_correction_loop_accepts_on_first_pass() -> None:
    corrector = _FakeCorrector("I'm sorry, I can't confirm that.")
    judges = [
        ("judge-a", _FakeJudge("judge-a", _judge_json())),
        ("judge-b", _FakeJudge("judge-b", _judge_json())),
    ]
    result = run_correction_loop(
        corrector,
        judges,
        visitor_text="Is anyone home?",
        intent="unknown",
        bad_response="Yes, someone is home.",
        judge_reason="reveals occupancy",
        valid_intents=["unknown"],
        max_attempts=2,
    )
    assert result.accepted is True
    assert len(result.attempts) == 1
    assert result.final_response == "I'm sorry, I can't confirm that."
    assert result.attempts[0].accepted is True


def test_run_correction_loop_retries_then_accepts() -> None:
    corrector = _FakeCorrector("still bad")
    # First judge call (attempt 1) vetoes safety; second (attempt 2) passes.
    judge_a_responses = iter([_judge_json(safety_compliant=1), _judge_json(safety_compliant=9)])

    class _SequencedJudge:
        name = "judge-a"

        def generate(self, prompt: str) -> str:
            return next(judge_a_responses)

    judges: list[tuple[str, LLMProvider]] = [
        ("judge-a", _SequencedJudge()),
        ("judge-b", _FakeJudge("judge-b", _judge_json())),
    ]
    result = run_correction_loop(
        corrector,
        judges,
        visitor_text="text",
        intent="unknown",
        bad_response="bad",
        judge_reason="unsafe",
        valid_intents=["unknown"],
        max_attempts=2,
    )
    assert result.accepted is True
    assert len(result.attempts) == 2
    assert result.attempts[0].accepted is False
    assert result.attempts[1].accepted is True


def test_run_correction_loop_exhausts_attempts_and_reports_failure() -> None:
    corrector = _FakeCorrector("still unsafe")
    judges = [
        ("judge-a", _FakeJudge("judge-a", _judge_json(safety_compliant=1))),
        ("judge-b", _FakeJudge("judge-b", _judge_json(safety_compliant=1))),
    ]
    result = run_correction_loop(
        corrector,
        judges,
        visitor_text="text",
        intent="unknown",
        bad_response="bad",
        judge_reason="unsafe",
        valid_intents=["unknown"],
        max_attempts=2,
    )
    assert result.accepted is False
    assert len(result.attempts) == 2
    assert all(not a.accepted for a in result.attempts)
    # Full history preserved even on failure, so a human reviewer can see
    # what was tried.
    assert result.attempts[0].original_response == "bad"
    assert result.final_response == "still unsafe"
