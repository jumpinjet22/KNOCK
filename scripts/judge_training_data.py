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
        [--audit-log PATH] [--metadata-store PATH] [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from knock.config import OllamaConfig
from knock.core.audit import JSONLAuditLog
from knock.core.orchestrator import _LLM_CLASSIFIABLE_INTENTS
from knock.core.training import (
    TrainingMetadata,
    TrainingMetadataStore,
    TrainingReview,
    TrainingReviewStore,
    example_key,
)
from knock.core.training_judge import (
    AggregatedJudgeResult,
    aggregate_scores,
    score_candidate,
    score_visitor_voice,
)
from knock.providers.llm.ollama import OllamaProvider

_VALID_INTENTS = [*_LLM_CLASSIFIABLE_INTENTS, "unknown"]


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
        "--limit", type=int, default=None, help="only judge the first N pending entries"
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
    candidates = [
        entry
        for entry in entries
        if entry.intent is not None
        and reviews.get(example_key(entry), TrainingReview()).status == "pending"
        and example_key(entry) not in existing_metadata
    ]
    if args.limit is not None:
        candidates = candidates[: args.limit]
    print(f"{len(candidates)} pending, unjudged entries to score (of {len(entries)} total)")

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

    results: dict[str, AggregatedJudgeResult] = {}
    scenario_voice_veto: dict[str, bool] = {}
    # visitor_voice is a property of the shared scenario text, not of any
    # one candidate response -- scored once per distinct text (via the
    # first judge model) and reused across every candidate and every
    # judge that shares it, rather than re-asking per candidate/judge.
    voice_score_cache: dict[str, int] = {}
    try:
        for i, entry in enumerate(candidates):
            t0 = time.monotonic()
            intent = entry.intent or "unknown"
            if entry.text not in voice_score_cache:
                voice_score_cache[entry.text] = score_visitor_voice(judges[0][1], entry.text)
            voice_score = voice_score_cache[entry.text]

            scores = []
            for judge_name, provider in judges:
                score = score_candidate(
                    provider,
                    judge_name,
                    entry.text,
                    intent,
                    entry.response_text,
                    _VALID_INTENTS,
                    visitor_voice_score=voice_score,
                )
                if score is not None:
                    scores.append(score)
            result = aggregate_scores(scores)
            elapsed = time.monotonic() - t0

            key = example_key(entry)
            results[key] = result
            scenario_voice_veto[entry.text] = (
                scenario_voice_veto.get(entry.text, False) or result.voice_veto
            )

            tag = (
                "VOICE_VETO"
                if result.voice_veto
                else ("SAFETY_VETO" if result.safety_veto else "ok")
            )
            print(
                f"{i + 1:>4}/{len(candidates)} ({elapsed:5.1f}s) {tag:12} "
                f"voice={result.visitor_voice_avg:.1f} cat={result.category_correct_avg:.1f} "
                f"safety={result.safety_compliant_avg:.1f} "
                f"quality={result.natural_quality_avg:.1f} "
                f"disagreement={result.disagreement:.1f} -- {entry.text!r}"
            )

        if args.dry_run:
            print("\n(--dry-run: no metadata/reviews were actually written)")
            return 0

        group_rejected = 0
        for entry in candidates:
            key = example_key(entry)
            result = results[key]
            metadata_store.set(key, TrainingMetadata(judge=result))
            # Group-wide: ANY candidate response sharing this visitor text
            # that tripped a voice veto means the scenario itself isn't
            # genuine visitor speech -- every response to it is equally
            # untrustworthy as training data, not just the one judged.
            if scenario_voice_veto.get(entry.text, False):
                review_store.set(key, TrainingReview(status="rejected"))
                group_rejected += 1
    finally:
        for _name, provider in judges:
            provider.close()

    if not args.dry_run:
        print(f"\nAuto-rejected {group_rejected} entries via group-wide voice veto.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
