"""Correction stage for KNOCK's synthetic training-data pipeline (Stage 4):
attempts a cheap fix for *response-level* rejects from the judge ensemble
(`knock.core.training_judge`) -- right category, bad phrasing only.

Deliberately out of scope: *input-level* rejects (the judge's
`voice_veto` -- the scenario itself isn't genuine visitor speech). Those
are discarded by the judge stage before ever reaching here, since
"correcting" a resident-voice scenario into real visitor speech is really
a full regeneration, not a fix -- that's Stage 1's job
(`scripts/generate_scenarios.py`), not this stage's.

A corrected candidate is never trusted on its own say-so: it's re-run
through the full judge ensemble (same multi-model, multi-axis scoring as
any other candidate) before being accepted, capped at a small number of
attempts. If it still can't pass after the cap, the entry is left
`pending` with its full correction history attached, so a human reviewer
sees *why* it couldn't be fixed automatically rather than it silently
disappearing.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from knock.core.training_judge import AggregatedJudgeResult, aggregate_scores, score_candidate
from knock.providers.llm.base import LLMProvider


def _correction_prompt(
    visitor_text: str,
    intent: str,
    bad_response: str,
    judge_reason: str,
    human_note: str | None = None,
) -> str:
    reason_block = f"A reviewer flagged a problem with it: {judge_reason}\n" if judge_reason else ""
    # The human's own instruction, when given, is a direct editorial
    # request (e.g. "make this shorter", "don't mention the dog") -- kept
    # as its own clearly-labeled line rather than folded into
    # `judge_reason` so the model doesn't have to guess which parts of
    # the prompt are an automated score's reasoning vs. an explicit ask.
    note_block = f"The reviewer specifically asked for this: {human_note}\n" if human_note else ""
    return (
        f'A visitor at the door said: "{visitor_text}"\n'
        f"The correct category for this is: {intent}\n\n"
        f'A smart doorbell assistant said this in reply: "{bad_response}"\n'
        f"{reason_block}"
        f"{note_block}\n"
        "Write a corrected reply that fixes this specific problem while "
        "staying a short, natural, polite sentence for the same situation. "
        "Never say whether anyone is home, share the household's schedule, "
        "or offer to unlock/open the door -- and never invite the visitor "
        'inside or say anything like "come in," even warmly or casually.\n'
        "Reply with ONLY the corrected sentence, no explanation, no quotes."
    )


def correct_response(
    provider: LLMProvider,
    visitor_text: str,
    intent: str,
    bad_response: str,
    judge_reason: str,
    human_note: str | None = None,
) -> str:
    """One correction attempt. Returns the corrected text, or the original
    `bad_response` unchanged if generation fails/comes back blank --
    callers should still re-judge the result either way, since an
    unchanged response will simply fail the same check again and surface
    in the correction history as a failed attempt, which is honest.

    `human_note` is a reviewer's own instruction for the rewrite (e.g.
    from the Training page's comment box) -- optional, and additive to
    `judge_reason`, not a replacement for it.
    """
    try:
        corrected = provider.generate(
            _correction_prompt(visitor_text, intent, bad_response, judge_reason, human_note)
        ).strip()
    except Exception:  # noqa: BLE001 - best-effort, caller re-judges regardless
        return bad_response
    return corrected or bad_response


class CorrectionAttempt(BaseModel):
    """One correction attempt's full record -- kept even on failure, so a
    human reviewer can see what was tried and why it still didn't pass.
    """

    attempt: int
    original_response: str
    corrected_response: str
    judge_reason: str
    rejudged: AggregatedJudgeResult | None = None
    accepted: bool = False


class CorrectionResult(BaseModel):
    """The outcome of running `run_correction_loop` on one entry."""

    attempts: list[CorrectionAttempt] = Field(default_factory=list)
    final_response: str
    accepted: bool


def run_correction_loop(
    corrector: LLMProvider,
    judges: Sequence[tuple[str, LLMProvider]],
    *,
    visitor_text: str,
    intent: str,
    bad_response: str,
    judge_reason: str,
    valid_intents: list[str],
    max_attempts: int = 2,
) -> CorrectionResult:
    """Drives the correct -> re-judge -> (accept | retry) loop for one
    response-level reject. `judges` is the same mixed-model-family judge
    pool the main judge stage uses -- a correction is only trusted once it
    clears the exact same bar a fresh candidate would, not a cheaper
    check.

    Stops as soon as an attempt is accepted (no voice/safety veto). If
    `max_attempts` is exhausted without acceptance, returns with
    `accepted=False` and the full attempt history intact -- callers should
    leave that entry `pending` for human review rather than silently
    dropping it, so the reviewer sees exactly what was tried.
    """
    attempts: list[CorrectionAttempt] = []
    current_response = bad_response
    current_reason = judge_reason

    for attempt_num in range(1, max_attempts + 1):
        corrected = correct_response(
            corrector, visitor_text, intent, current_response, current_reason
        )
        scores = []
        for judge_name, judge_provider in judges:
            score = score_candidate(
                judge_provider, judge_name, visitor_text, intent, corrected, valid_intents
            )
            if score is not None:
                scores.append(score)
        rejudged = aggregate_scores(scores)
        accepted = not rejudged.voice_veto and not rejudged.safety_veto
        attempts.append(
            CorrectionAttempt(
                attempt=attempt_num,
                original_response=current_response,
                corrected_response=corrected,
                judge_reason=current_reason,
                rejudged=rejudged,
                accepted=accepted,
            )
        )
        if accepted:
            return CorrectionResult(attempts=attempts, final_response=corrected, accepted=True)
        current_response = corrected
        current_reason = rejudged.per_judge[0].reason if rejudged.per_judge else current_reason

    return CorrectionResult(
        attempts=attempts, final_response=attempts[-1].corrected_response, accepted=False
    )
