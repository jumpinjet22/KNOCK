#!/usr/bin/env python3
"""Stage 3 of the local synthetic training-data pipeline: a multi-model,
mixed-family judge ensemble scores every pending candidate response along
four axes (visitor voice, category correctness, safety compliance,
natural quality) instead of a single flat accept/reject verdict.

Replaces scripts/auto_review_training_data.py -- same place in the
pipeline, but scores rather than decides: results are written as
TrainingMetadata (knock.core.training.TrainingMetadataStore), which the
Training page's human review UI shows alongside each entry, and which
powers DPO pair construction (training.export_dpo_pairs_jsonl). The one
exception: a voice_veto (the scenario itself isn't genuine visitor
speech -- a structural defect, not a subjective quality call) is
auto-rejected outright, group-wide across every candidate response that
shares that same visitor text. There's nothing for a human to usefully
weigh in on there, and this directly closes the bug class found live
tonight where resident-voice scenarios kept getting approved by a
reviewer skimming categories, not transcripts.

Not part of the automated test suite -- a manual, hands-on dev tool, like
scripts/smoke_test_providers.py.

Usage:
    python scripts/judge_training_data.py \\
        --judge-models qwen2.5:14b,gpt-oss:20b,deepseek-r1:14b \\
        [--ollama-host 127.0.0.1] [--ollama-port 11434] [--ollama-timeout 120.0] \\
        [--audit-log PATH] [--metadata-store PATH] [--dry-run] [--limit N] \\
        [--include-reviewed] [--rejudge] [--calibration-samples 3]

By default only scores entries still at review status "pending" that
have no judge metadata yet. --include-reviewed also scores already-
approved/rejected entries that predate this pipeline (so they become
eligible for DPO pair construction, which needs judge metadata
regardless of review status) -- a human's existing decision is never
overwritten by this, see main()'s voice-veto write-back. --rejudge
scores entries that already have judge metadata too (e.g. after a
judge-prompt fix) instead of skipping them -- correction history is
preserved, only the judge scores are replaced.

Before the real run, times --calibration-samples real candidates against
EVERY judge model (reusing those same scores in the real pass rather than
wasting them) and prints a projected ETA from the measured per-model
pace -- a mixed-family ensemble can span a 0.8B model and a 27B one, and
guessing a single "calls per second" badly undersells how long the
slowest model in the list will actually take.

Checkpoints metadata to disk after EVERY judge's pass, not just once at
the end: a long multi-judge run (the whole reason for calibrating an ETA
in the first place) can get killed by an unrelated time limit (e.g. a
background task's own ceiling) partway through -- previously, that meant
losing the *entire* run's work, including judges that had already fully
finished. Each checkpoint aggregates whatever judges have scored so far,
so an interruption only costs progress since the last completed judge,
not the whole run.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Mapping
from pathlib import Path

from knock.config import OllamaConfig
from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.orchestrator import _LLM_CLASSIFIABLE_INTENTS
from knock.core.training import (
    TrainingMetadata,
    TrainingMetadataStore,
    TrainingReview,
    TrainingReviewStore,
    example_key,
)
from knock.core.training_judge import (
    JudgeAxisScores,
    aggregate_scores,
    score_candidate,
    score_visitor_voice,
)
from knock.providers.llm.ollama import OllamaProvider

_VALID_INTENTS = [*_LLM_CLASSIFIABLE_INTENTS, "unknown"]


def _format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def calibrate(
    judges: list[tuple[str, OllamaProvider]],
    sample_entries: list[AuditEntry],
    scores_by_key: dict[str, list[JudgeAxisScores]],
) -> dict[str, float]:
    """Times `sample_entries` against every judge model and returns each
    model's average seconds/call. The scores produced here are written
    straight into `scores_by_key` -- real candidates, real scores, reused
    in the main pass below rather than thrown away, so calibrating an ETA
    costs nothing beyond the time it would have taken anyway.
    """
    print(f"\n=== Calibrating: {len(sample_entries)} sample prompt(s) per judge model ===")
    per_model_seconds: dict[str, float] = {}
    for judge_name, provider in judges:
        elapsed_total = 0.0
        for entry in sample_entries:
            t0 = time.monotonic()
            score = score_candidate(
                provider,
                judge_name,
                entry.text,
                entry.intent or "unknown",
                entry.response_text,
                _VALID_INTENTS,
                # Fixed value -- isolates axis-call timing from the
                # separately-estimated voice-check cost.
                visitor_voice_score=9,
            )
            elapsed_total += time.monotonic() - t0
            if score is not None:
                scores_by_key[example_key(entry)].append(score)
        avg = elapsed_total / len(sample_entries)
        per_model_seconds[judge_name] = avg
        print(f"  {judge_name}: {avg:5.2f}s/call avg (loaded + {len(sample_entries)} sample(s))")
    return per_model_seconds


def project_eta(
    per_model_seconds: dict[str, float],
    judges: list[tuple[str, OllamaProvider]],
    total_candidates: int,
    calibrated_count: int,
    distinct_scenario_count: int,
) -> float:
    remaining_per_judge = max(0, total_candidates - calibrated_count)
    axis_seconds = sum(per_model_seconds[name] * remaining_per_judge for name, _ in judges)
    # Voice-check cost is a rough proxy, not separately calibrated -- it's
    # a shorter prompt than the axis call, so this errs conservative
    # (slightly overestimates), using the first judge's own measured pace.
    voice_seconds = per_model_seconds[judges[0][0]] * distinct_scenario_count
    return axis_seconds + voice_seconds


def _checkpoint(
    *,
    candidates: list[AuditEntry],
    scores_by_key: dict[str, list[JudgeAxisScores]],
    existing_metadata: dict[str, TrainingMetadata],
    metadata_store: TrainingMetadataStore,
    review_store: TrainingReviewStore,
    original_status: Mapping[str, str],
    judges_so_far: int,
    total_judges: int,
    dry_run: bool,
) -> None:
    """Aggregates whatever judges have scored so far and writes it to
    disk -- called after every judge's pass, not just the last one, so an
    interruption only costs progress since the last checkpoint. Minority-
    veto scores are safe to checkpoint early: more judges can only ever
    *add* a veto, never remove one already found, so an early voice_veto
    is never a false positive, only possibly incomplete until the last
    judge weighs in.
    """
    if dry_run:
        return
    group_rejected = 0
    already_decided_skipped = 0
    for entry in candidates:
        key = example_key(entry)
        result = aggregate_scores(scores_by_key[key])
        existing_corrections = existing_metadata.get(key, TrainingMetadata()).corrections
        metadata_store.set(key, TrainingMetadata(judge=result, corrections=existing_corrections))
        if result.voice_veto:
            if original_status[key] == "pending":
                review_store.set(key, TrainingReview(status="rejected"))
                group_rejected += 1
            else:
                already_decided_skipped += 1
    print(
        f"  [checkpoint {judges_so_far}/{total_judges} judges] wrote metadata for "
        f"{len(candidates)} candidates ({group_rejected} newly auto-rejected via voice veto"
        + (
            f", {already_decided_skipped} already-decided left untouched"
            if already_decided_skipped
            else ""
        )
        + ")"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--judge-models", required=True, help="comma-separated Ollama model names (2+ recommended)"
    )
    parser.add_argument("--ollama-host", default="127.0.0.1")
    parser.add_argument("--ollama-port", type=int, default=11434)
    parser.add_argument("--ollama-timeout", type=float, default=120.0)
    parser.add_argument("--audit-log", type=Path, default=None)
    parser.add_argument("--metadata-store", type=Path, default=None)
    parser.add_argument(
        "--dry-run", action="store_true", help="print verdicts without writing metadata/reviews"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="only judge the first N eligible entries"
    )
    parser.add_argument(
        "--include-reviewed",
        action="store_true",
        help=(
            "also score already-approved/rejected entries that were never judged (e.g. ones "
            "reviewed by hand before this pipeline existed) -- needed for DPO pair construction, "
            "which requires judge metadata regardless of review status. A human's existing "
            "approved/rejected decision is never overwritten by this (see the voice-veto "
            "write-back below); only the metadata gets attached."
        ),
    )
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help=(
            "re-score entries that already have judge metadata too (e.g. after a judge-prompt "
            "fix) instead of skipping anything already judged. Existing correction history is "
            "preserved -- only the judge scores themselves are overwritten."
        ),
    )
    parser.add_argument(
        "--calibration-samples",
        type=int,
        default=3,
        help="real candidates to time against every judge before the full run, to project an ETA",
    )
    args = parser.parse_args()

    judge_names = [m.strip() for m in args.judge_models.split(",") if m.strip()]
    if len(judge_names) < 2:
        parser.error("--judge-models needs at least 2 models for a real ensemble")

    audit_log = JSONLAuditLog(args.audit_log) if args.audit_log else JSONLAuditLog()
    review_store = TrainingReviewStore()
    metadata_store = (
        TrainingMetadataStore(args.metadata_store)
        if args.metadata_store
        else TrainingMetadataStore()
    )
    reviews = review_store.all()
    existing_metadata = metadata_store.all()

    entries = audit_log.recent(limit=100_000)
    entries.reverse()  # oldest first, for a stable/readable progress order
    eligible_statuses = (
        {"pending", "approved", "rejected"} if args.include_reviewed else {"pending"}
    )
    candidates = [
        entry
        for entry in entries
        if entry.intent is not None
        and reviews.get(example_key(entry), TrainingReview()).status in eligible_statuses
        and (args.rejudge or example_key(entry) not in existing_metadata)
    ]
    if args.limit is not None:
        candidates = candidates[: args.limit]
    # Captured now, before any write-back below -- the voice-veto
    # auto-reject must check what a human *already* decided, never what
    # this same run just set for another candidate sharing the scenario.
    original_status = {
        example_key(entry): reviews.get(example_key(entry), TrainingReview()).status
        for entry in candidates
    }
    print(f"{len(candidates)} eligible, unjudged entries to score (of {len(entries)} total)")
    if not candidates:
        return 0

    judges = [
        (
            name,
            OllamaProvider(
                config=OllamaConfig(
                    host=args.ollama_host,
                    port=args.ollama_port,
                    model=name,
                    timeout=args.ollama_timeout,
                )
            ),
        )
        for name in judge_names
    ]

    # visitor_voice is a property of the shared scenario text, not of any
    # one candidate response -- scored once per distinct text (via the
    # first judge model) and reused across every candidate and every
    # judge that shares it, rather than re-asking per candidate/judge.
    voice_score_cache: dict[str, int] = {}
    distinct_texts = list({entry.text for entry in candidates})

    scores_by_key: dict[str, list[JudgeAxisScores]] = {example_key(e): [] for e in candidates}
    try:
        calibration_count = min(max(0, args.calibration_samples), len(candidates))
        calibration_entries = candidates[:calibration_count]
        per_model_seconds = (
            calibrate(judges, calibration_entries, scores_by_key) if calibration_count else {}
        )
        if per_model_seconds:
            eta = project_eta(
                per_model_seconds, judges, len(candidates), calibration_count, len(distinct_texts)
            )
            print(
                f"\nProjected remaining time: ~{_format_duration(eta)} "
                f"(plus model load time for judges not yet warmed up)"
            )

        # Judge-major, not candidate-major: load each judge model once and
        # run it against every candidate before moving to the next judge,
        # rather than swapping models on every single candidate. Ollama
        # only keeps one model resident in VRAM at a time, so the naive
        # candidate-major order reloads every judge from disk on every
        # candidate -- live-measured via Ollama's own logs during a
        # 628-candidate run as a ~17s/candidate model swap cycle (~3 hours
        # total), the same antipattern generate_scenarios.py was already
        # fixed for in an earlier PR. This mirrors that fix: each judge
        # model loads from disk exactly once for the whole run.
        for judge_idx, (judge_name, provider) in enumerate(judges):
            if judge_idx == 0:
                print(f"Scoring visitor voice for {len(distinct_texts)} distinct scenario(s)...")
                for text in distinct_texts:
                    voice_score_cache[text] = score_visitor_voice(provider, text)

            already_scored = calibration_count if judge_idx == 0 else 0
            remaining = candidates[already_scored:]
            print(
                f"\nJudging {len(remaining)} candidate(s) with {judge_name}"
                + (
                    f" ({already_scored} already scored during calibration)"
                    if already_scored
                    else ""
                )
                + "..."
            )
            for i, entry in enumerate(remaining):
                t0 = time.monotonic()
                intent = entry.intent or "unknown"
                score = score_candidate(
                    provider,
                    judge_name,
                    entry.text,
                    intent,
                    entry.response_text,
                    _VALID_INTENTS,
                    visitor_voice_score=voice_score_cache[entry.text],
                )
                elapsed = time.monotonic() - t0
                if score is not None:
                    scores_by_key[example_key(entry)].append(score)
                print(
                    f"[{judge_name}] {i + 1:>4}/{len(remaining)} ({elapsed:5.1f}s) -- "
                    f"{entry.text!r}"
                )

            _checkpoint(
                candidates=candidates,
                scores_by_key=scores_by_key,
                existing_metadata=existing_metadata,
                metadata_store=metadata_store,
                review_store=review_store,
                original_status=original_status,
                judges_so_far=judge_idx + 1,
                total_judges=len(judges),
                dry_run=args.dry_run,
            )
    finally:
        for _name, provider in judges:
            provider.close()

    print("\n=== Final aggregated results ===")
    group_rejected = 0
    for i, entry in enumerate(candidates):
        key = example_key(entry)
        result = aggregate_scores(scores_by_key[key])
        if result.voice_veto and original_status[key] == "pending":
            group_rejected += 1
        tag = "VOICE_VETO" if result.voice_veto else ("SAFETY_VETO" if result.safety_veto else "ok")
        print(
            f"{i + 1:>4}/{len(candidates)} {tag:12} "
            f"voice={result.visitor_voice_avg:.1f} cat={result.category_correct_avg:.1f} "
            f"safety={result.safety_compliant_avg:.1f} "
            f"quality={result.natural_quality_avg:.1f} "
            f"disagreement={result.disagreement:.1f} -- {entry.text!r}"
        )

    if args.dry_run:
        print("\n(--dry-run: no metadata/reviews were actually written)")
    else:
        print(
            f"\nAuto-rejected {group_rejected} entries via group-wide voice veto (all checkpoints)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
