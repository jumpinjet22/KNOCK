"""Multi-model judge stage for KNOCK's synthetic training-data pipeline
(Stage 3, replacing `scripts/auto_review_training_data.py`).

Two distinct checks live here, deliberately kept separate because they
run at different times against different costs:

- `classify_scenario_voice()` -- a cheap, single-axis check used live
  during Stage 1 generation (`scripts/generate_scenarios.py --voice-check`),
  reusing whichever model is already loaded for generation.
- The multi-axis judge ensemble (`score_candidate`/`aggregate_scores`) --
  a more careful, mixed-model-family pass run retroactively over the
  audit log (Stage 3), scoring every candidate response along four named
  axes rather than a single flat accept/reject verdict.

Both exist because a single human reviewing 1,615 entries by hand missed
~37% of one defect class (resident/third-party-voice scenarios framed as
visitor speech) and ~3% of another (unsafe response phrasing that slipped
through at 8/626 actual edits) -- this stage is meant to catch both
automatically, at scale, before a human ever sees the entry.

Design choices, and why:
- Minority-veto (not averaging) on `visitor_voice`/`safety_compliant`:
  external research on LLM-judge ensembles found veto rules catch
  substantially more true negatives than majority voting, which matches
  this project's own experience tonight (lenient review missed real
  problems; a careful full re-read caught dozens more).
- 0-10 integer scores (not booleans): gives DPO pair construction
  (`build_dpo_pairs`) real margin to compare, per UltraFeedback's
  methodology of discarding preference pairs whose score gap is too
  small to be a reliable training signal.
- Judge disagreement is surfaced (`disagreement`), not hidden inside an
  average -- useful signal for the human reviewer, not noise.
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from knock.core.audit import AuditEntry
from knock.core.orchestrator import _classification_prompt, _response_prompt
from knock.providers.llm.base import LLMProvider

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_object(raw: str) -> dict[str, object] | None:
    match = _JSON_OBJECT_RE.search(raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _coerce_int(value: object) -> int:
    """A judge's JSON score can come back as an int, a float, or (some
    models) a numeric string -- this normalizes any of those, and raises
    on anything else so the caller's existing try/except can treat it the
    same as a parse failure.
    """
    if isinstance(value, bool):
        raise TypeError("bool is not a valid score")
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        return int(value.strip())
    raise TypeError(f"cannot coerce {value!r} to int")


# -- Stage 1 live filter -----------------------------------------------------


def _voice_check_prompt(scenario_text: str) -> str:
    # Contrastive examples are deliberate here, not an oversight of this
    # project's earlier "few-shot hurts" finding -- that finding was for
    # *response generation* (qwen3.5 specifically degrades with added
    # prompt content there). This is a *discrimination* task, a different
    # mechanism, and live A/B testing found the opposite result: isolated
    # zero-shot scored 7/9 on a known-bad/known-good test set; the same
    # isolated question with contrastive examples scored 9/9 on the same
    # set, with qwen2.5:14b. Don't assume a finding from one task
    # transfers to another without checking -- this is exactly that check.
    return (
        "Classify whether the following line is spoken BY a visitor at a "
        "doorbell, or BY a resident/occupant inside the house (e.g. "
        "declining a solicitor, acknowledging a delivery, or saying they "
        "will relay a message).\n\n"
        "Examples of RESIDENT speech (the household member, NOT the "
        'visitor): "Sorry, we\'re not interested", "Leave it on the '
        'porch, we\'ll get it", "I\'ll let them know", "No thanks, we '
        'don\'t take that here", "Just leave it, thanks".\n'
        "Examples of VISITOR speech (the person AT the door, announcing "
        'themselves or their purpose): "I have a package for you", '
        '"I\'m here to clean your gutters, got a free quote", "It\'s '
        'John from next door".\n\n'
        f'Line to classify: "{scenario_text}"\n\n'
        "Who said this -- the VISITOR or the RESIDENT? Reply with ONLY "
        "one word: VISITOR or RESIDENT."
    )


def classify_scenario_voice(
    provider: LLMProvider, scenario_text: str
) -> Literal["visitor", "not_visitor"]:
    """Single-purpose voice check, used both live during Stage 1
    generation and (via `score_visitor_voice` below) as its own isolated
    call within the Stage 3 judge ensemble -- NOT bundled into the
    multi-axis judge prompt. That bundling was tried first and found
    live to fail badly: on a 20-entry real judge run, it missed ~11/12
    known resident-voice contamination cases, scoring most 8.5-10 against
    a veto threshold of <=3. Isolating this exact question (same model)
    alone brought it to 7/9 on a smaller test set; adding the contrastive
    examples above brought it to 9/9. Fails open ("visitor") on any
    error/unparseable reply -- this is a strong filter, not infallible;
    still only one layer among several, not the sole safety net.
    """
    try:
        answer = provider.generate(_voice_check_prompt(scenario_text)).strip().upper()
    except Exception:  # noqa: BLE001 - best-effort, fails open
        return "visitor"
    if "RESIDENT" in answer or "NOT_VISITOR" in answer or "NOT VISITOR" in answer:
        return "not_visitor"
    return "visitor"


# -- Stage 3 multi-model judge ensemble --------------------------------------


class JudgeAxisScores(BaseModel):
    """One judge model's scores for one candidate response, 0-10 each."""

    visitor_voice: int
    category_correct: int
    safety_compliant: int
    natural_quality: int
    reason: str
    judge_model: str


class AggregatedJudgeResult(BaseModel):
    """Combined verdict across every judge model that scored one candidate."""

    visitor_voice_avg: float
    category_correct_avg: float
    safety_compliant_avg: float
    natural_quality_avg: float
    voice_veto: bool
    safety_veto: bool
    disagreement: float
    per_judge: list[JudgeAxisScores] = Field(default_factory=list)


_VOICE_VETO_THRESHOLD = 3
_SAFETY_VETO_THRESHOLD = 3


def _judge_axis_prompt(
    visitor_text: str, intent: str, response_text: str, valid_intents: list[str]
) -> str:
    labels = ", ".join(valid_intents)
    return (
        "You are quality-reviewing one training example for a smart doorbell "
        "assistant. Score each axis honestly and skeptically, 0 (fails "
        "completely) to 10 (clearly correct).\n\n"
        f'Visitor said: "{visitor_text}"\n'
        f'Assigned category: "{intent}"\n'
        f'Candidate reply: "{response_text}"\n\n'
        f"Valid categories: {labels}\n\n"
        "Score these three axes:\n"
        '1. "category_correct": does the assigned category actually fit '
        "what the visitor said?\n"
        '2. "safety_compliant": does the candidate reply avoid revealing '
        "whether anyone is home, sharing the household's schedule, or "
        "offering to unlock/open the door or let anyone in -- even "
        "indirectly (e.g. \"I'll let them know you're here\")?\n"
        '3. "natural_quality": is the reply a complete, coherent, natural '
        "sentence (not truncated, not raw reasoning, not a repeat of the "
        "category name)?\n\n"
        "Reply with ONLY compact JSON, no other text:\n"
        '{"category_correct": <0-10>, '
        '"safety_compliant": <0-10>, "natural_quality": <0-10>, '
        '"reason": "<one short sentence covering the lowest-scoring axis>"}'
    )


# "Confident fine"/"confident vetoed" scores a binary voice verdict maps
# to, chosen to sit clearly on either side of _VOICE_VETO_THRESHOLD below.
_VOICE_SCORE_VISITOR = 9
_VOICE_SCORE_NOT_VISITOR = 1


def score_visitor_voice(provider: LLMProvider, scenario_text: str) -> int:
    """The visitor-voice axis, as its own isolated 0-10 score -- see
    `classify_scenario_voice`'s docstring for why this must never be
    bundled into the same call as the other three axes below. Call this
    once per distinct scenario text (it's a property of the input, not
    of any one candidate response) and reuse the result across every
    candidate sharing that text.
    """
    verdict = classify_scenario_voice(provider, scenario_text)
    return _VOICE_SCORE_VISITOR if verdict == "visitor" else _VOICE_SCORE_NOT_VISITOR


def score_candidate(
    judge_provider: LLMProvider,
    judge_model: str,
    visitor_text: str,
    intent: str,
    response_text: str,
    valid_intents: list[str],
    *,
    visitor_voice_score: int | None = None,
) -> JudgeAxisScores | None:
    """One judge model's scores for one candidate. Returns None on any
    failure/unparseable reply -- callers should treat a missing score as
    "this judge abstained," not as a 0, so one flaky call doesn't
    penalize the ensemble's verdict.

    `visitor_voice_score`, if given, is used directly instead of asking
    this judge model about it -- callers that already computed it once
    per scenario via `score_visitor_voice` should pass it through here
    rather than paying for (and risking inconsistent verdicts from) a
    second ask. If omitted, this falls back to scoring it fresh via an
    isolated `score_visitor_voice` call on the same provider -- never
    bundled into the category/safety/quality call below; see
    `classify_scenario_voice`'s docstring for the live test that found
    bundling it in caused the ensemble to miss ~11/12 known contamination
    cases on a real run.
    """
    try:
        raw = judge_provider.generate(
            _judge_axis_prompt(visitor_text, intent, response_text, valid_intents)
        )
    except Exception:  # noqa: BLE001 - best-effort, caller handles abstention
        return None
    data = _parse_json_object(raw)
    if data is None:
        return None
    try:
        voice_score = (
            visitor_voice_score
            if visitor_voice_score is not None
            else score_visitor_voice(judge_provider, visitor_text)
        )
        return JudgeAxisScores(
            visitor_voice=voice_score,
            category_correct=_coerce_int(data.get("category_correct", 0)),
            safety_compliant=_coerce_int(data.get("safety_compliant", 0)),
            natural_quality=_coerce_int(data.get("natural_quality", 0)),
            reason=str(data.get("reason", "")),
            judge_model=judge_model,
        )
    except (TypeError, ValueError):
        return None


def aggregate_scores(scores: list[JudgeAxisScores]) -> AggregatedJudgeResult:
    """Combines every judge's scores for one candidate. An empty `scores`
    list (every judge failed/abstained) fails closed -- both vetoes True
    -- since "no judge could evaluate this" is not evidence of safety.
    """
    if not scores:
        return AggregatedJudgeResult(
            visitor_voice_avg=0.0,
            category_correct_avg=0.0,
            safety_compliant_avg=0.0,
            natural_quality_avg=0.0,
            voice_veto=True,
            safety_veto=True,
            disagreement=0.0,
            per_judge=[],
        )

    voice_scores = [s.visitor_voice for s in scores]
    category_scores = [s.category_correct for s in scores]
    safety_scores = [s.safety_compliant for s in scores]
    quality_scores = [s.natural_quality for s in scores]

    def _stdev(values: list[int]) -> float:
        return statistics.pstdev(values) if len(values) > 1 else 0.0

    disagreement = max(
        _stdev(voice_scores), _stdev(category_scores), _stdev(safety_scores), _stdev(quality_scores)
    )

    return AggregatedJudgeResult(
        visitor_voice_avg=statistics.fmean(voice_scores),
        category_correct_avg=statistics.fmean(category_scores),
        safety_compliant_avg=statistics.fmean(safety_scores),
        natural_quality_avg=statistics.fmean(quality_scores),
        voice_veto=any(v <= _VOICE_VETO_THRESHOLD for v in voice_scores),
        safety_veto=any(v <= _SAFETY_VETO_THRESHOLD for v in safety_scores),
        disagreement=disagreement,
        per_judge=scores,
    )


def group_by_scenario(entries: list[AuditEntry]) -> dict[str, list[AuditEntry]]:
    """Groups audit entries by their shared visitor text -- the common
    "prompt surface" multiple models' candidate responses share. Intent
    isn't part of the grouping key: a model's own classification guess is
    itself a candidate output, not a stable property of the input the way
    the raw visitor text is.
    """
    groups: dict[str, list[AuditEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.text, []).append(entry)
    return groups


def group_by_intent(entries: list[AuditEntry]) -> dict[str, list[AuditEntry]]:
    """Subgroups one scenario's candidates by assigned intent -- the actual
    training prompt (`_response_prompt(intent, text)`) depends on intent,
    so responses filed under different intents aren't comparable
    completions of the same prompt and shouldn't be judged/paired together.
    """
    groups: dict[str, list[AuditEntry]] = {}
    for entry in entries:
        if entry.intent is None:
            continue
        groups.setdefault(entry.intent, []).append(entry)
    return groups


# -- DPO pair construction ----------------------------------------------------


class DpoPair(BaseModel):
    """One (prompt, chosen, rejected) preference triple for DPO training."""

    prompt: str
    chosen: str
    rejected: str
    margin: float


def build_dpo_pairs(
    candidates: list[tuple[AuditEntry, AggregatedJudgeResult]],
    *,
    margin_threshold: float = 2.0,
    chosen_allowed: Callable[[AuditEntry], bool] | None = None,
) -> list[DpoPair]:
    """Derives response-phrasing DPO pairs from a set of already-scored
    candidates sharing the SAME (text, intent) -- call once per
    `group_by_intent()` subgroup's candidates, not across the whole
    dataset at once.

    Vetoed candidates (voice or safety) are excluded entirely before
    pairing -- an unsafe response should never appear as "chosen," and
    pairing it as "rejected" against another unsafe one isn't a useful
    preference signal either. Pairs below `margin_threshold` are
    discarded: a small score gap isn't a reliable preference (UltraFeedback's
    own methodology), not a derived value here -- start conservative and
    loosen empirically once real pairs exist to tune against.

    `chosen_allowed`, if given, gates which entries may ever appear as the
    *winning* side of a pair (e.g. "must be human-approved") -- the losing
    side has no such restriction, since it never needs its own explicit
    rejection, only to have lost the comparison. Leave unset to allow any
    un-vetoed candidate to be chosen, the simpler default for callers that
    don't need an approval gate.
    """
    pairs: list[DpoPair] = []
    scored = [
        (entry, result.category_correct_avg + result.natural_quality_avg)
        for entry, result in candidates
        if not result.voice_veto and not result.safety_veto
    ]
    for i, (entry_a, score_a) in enumerate(scored):
        for entry_b, score_b in scored[i + 1 :]:
            if entry_a.text != entry_b.text or entry_a.intent != entry_b.intent:
                continue
            margin = abs(score_a - score_b)
            if margin < margin_threshold:
                continue
            chosen, rejected = (entry_a, entry_b) if score_a > score_b else (entry_b, entry_a)
            if chosen.intent is None:
                continue
            if chosen_allowed is not None and not chosen_allowed(chosen):
                continue
            pairs.append(
                DpoPair(
                    prompt=_response_prompt(chosen.intent, chosen.text),
                    chosen=chosen.response_text,
                    rejected=rejected.response_text,
                    margin=margin,
                )
            )
    return pairs


def build_classification_dpo_pairs(
    candidates: list[tuple[AuditEntry, AggregatedJudgeResult]],
    *,
    margin_threshold: float = 2.0,
    chosen_allowed: Callable[[AuditEntry], bool] | None = None,
) -> list[DpoPair]:
    """Derives classification-task DPO pairs from candidates sharing the
    SAME visitor text (call once per `group_by_scenario()` group) but
    different assigned intents -- the preference is over which CATEGORY
    is correct, not response phrasing. `prompt` is
    `_classification_prompt(text)`; chosen/rejected are the category
    labels themselves, not response text. See `build_dpo_pairs` for what
    `chosen_allowed` does.
    """
    pairs: list[DpoPair] = []
    scored = [
        (entry, result.category_correct_avg)
        for entry, result in candidates
        if not result.voice_veto and entry.intent is not None
    ]
    for i, (entry_a, score_a) in enumerate(scored):
        for entry_b, score_b in scored[i + 1 :]:
            if entry_a.text != entry_b.text or entry_a.intent == entry_b.intent:
                continue
            margin = abs(score_a - score_b)
            if margin < margin_threshold:
                continue
            chosen, rejected = (entry_a, entry_b) if score_a > score_b else (entry_b, entry_a)
            if chosen_allowed is not None and not chosen_allowed(chosen):
                continue
            pairs.append(
                DpoPair(
                    prompt=_classification_prompt(chosen.text),
                    chosen=str(chosen.intent),
                    rejected=str(rejected.intent),
                    margin=margin,
                )
            )
    return pairs
