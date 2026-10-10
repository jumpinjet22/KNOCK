#!/usr/bin/env python3
"""Stage 4 of the local synthetic training-data pipeline: attempts a
cheap correction for entries the judge ensemble (Stage 3,
scripts/judge_training_data.py) flagged as a *response-level* reject --
right category, bad phrasing only.

Scope is deliberately narrow: only entries with `voice_veto=False` (the
visitor-text scenario itself is genuine) and `safety_veto=True` (the
response phrasing itself is the problem) are eligible. A voice-vetoed
entry was already auto-rejected group-wide by Stage 3 -- "fixing" a
resident-voice scenario into real visitor speech is a full regeneration,
Stage 1's job, not a correction.

Each correction attempt is re-run through the exact same judge ensemble a
fresh candidate would face (not a cheaper check) before being trusted,
capped at --max-attempts. If still vetoed after the cap, the entry is
left `pending` with its full correction history recorded in
TrainingMetadata -- a human reviewer sees what was tried and why it
didn't pass, rather than it silently disappearing.

Not part of the automated test suite -- a manual, hands-on dev tool, like
scripts/smoke_test_providers.py.

Usage:
    python scripts/correct_training_data.py \\
        --corrector-model qwen2.5:14b \\
        --judge-models qwen2.5:14b,gpt-oss:20b,deepseek-r1:14b \\
        [--max-attempts 2] \\
        [--ollama-host 127.0.0.1] [--ollama-port 11434] [--ollama-timeout 120.0] \\
        [--audit-log PATH] [--metadata-store PATH] [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
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
from knock.core.training_correction import CorrectionAttempt, correct_response
from knock.core.training_judge import (
    JudgeAxisScores,
    aggregate_scores,
    combined_judge_reasons,
    score_candidate,
)
from knock.providers.llm.ollama import OllamaProvider

_VALID_INTENTS = [*_LLM_CLASSIFIABLE_INTENTS, "unknown"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--corrector-model", required=True)
    parser.add_argument(
        "--judge-models", required=True, help="comma-separated Ollama model names (2+ recommended)"
    )
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--ollama-host", default="127.0.0.1")
    parser.add_argument("--ollama-port", type=int, default=11434)
    parser.add_argument("--ollama-timeout", type=float, default=120.0)
    parser.add_argument("--audit-log", type=Path, default=None)
    parser.add_argument("--metadata-store", type=Path, default=None)
    parser.add_argument(
        "--dry-run", action="store_true", help="print attempts without writing metadata"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="only attempt the first N eligible entries"
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
    metadata = metadata_store.all()

    entries = audit_log.recent(limit=100_000)
    entries.reverse()  # oldest first, for a stable/readable progress order
    eligible = []
    for entry in entries:
        if entry.intent is None:
            continue
        review = reviews.get(example_key(entry), TrainingReview())
        if review.status != "pending":
            continue
        meta = metadata.get(example_key(entry))
        if meta is None or meta.judge is None:
            continue
        judge = meta.judge
        if judge.voice_veto or not judge.safety_veto:
            continue
        eligible.append((entry, meta))
    if args.limit is not None:
        eligible = eligible[: args.limit]
    print(f"{len(eligible)} salvageable response-level rejects to attempt correcting")

    def make_provider(model: str) -> OllamaProvider:
        return OllamaProvider(
            config=OllamaConfig(
                host=args.ollama_host,
                port=args.ollama_port,
                model=model,
                timeout=args.ollama_timeout,
            )
        )

    corrector = make_provider(args.corrector_model)
    judges = [(name, make_provider(name)) for name in judge_names]

    # Round-batched, not per-entry: generating/re-judging one entry at a
    # time (the original shape here) meant corrector <-> judge-1 <->
    # judge-2 <-> judge-3 reloaded from Ollama's single VRAM slot on every
    # single entry -- confirmed live, the same ~17-20s swap cycle already
    # fixed in judge_training_data.py. Each attempt round now loads the
    # corrector once for every still-pending entry, then loads each judge
    # once for every still-pending entry's new attempt, instead of
    # swapping per entry. Total model loads for the whole run: at most
    # max_attempts * (1 + len(judges)), regardless of how many entries are
    # eligible.
    @dataclass
    class _EntryState:
        entry: AuditEntry
        meta: TrainingMetadata
        current_response: str
        current_reason: str
        attempts: list[CorrectionAttempt] = field(default_factory=list)
        accepted: bool = False
        done: bool = False

    states = [
        _EntryState(
            entry=entry,
            meta=meta,
            current_response=entry.response_text,
            current_reason=combined_judge_reasons(meta.judge) if meta.judge else "",
        )
        for entry, meta in eligible
    ]

    try:
        for attempt_num in range(1, args.max_attempts + 1):
            pending = [s for s in states if not s.done]
            if not pending:
                break

            print(f"\n--- Attempt {attempt_num}: generating with {args.corrector_model} ---")
            corrected_by_state: dict[int, str] = {}
            for i, s in enumerate(pending):
                corrected_by_state[id(s)] = correct_response(
                    corrector,
                    s.entry.text,
                    s.entry.intent or "unknown",
                    s.current_response,
                    s.current_reason,
                )
                print(f"  {i + 1:>4}/{len(pending)} generated -- {s.entry.text!r}")

            scores_by_state: dict[int, list[JudgeAxisScores]] = {id(s): [] for s in pending}
            for judge_name, judge_provider in judges:
                print(f"--- Attempt {attempt_num}: re-judging with {judge_name} ---")
                for i, s in enumerate(pending):
                    score = score_candidate(
                        judge_provider,
                        judge_name,
                        s.entry.text,
                        s.entry.intent or "unknown",
                        corrected_by_state[id(s)],
                        _VALID_INTENTS,
                    )
                    if score is not None:
                        scores_by_state[id(s)].append(score)
                    print(f"  {i + 1:>4}/{len(pending)} -- {s.entry.text!r}")

            for s in pending:
                corrected = corrected_by_state[id(s)]
                result = aggregate_scores(scores_by_state[id(s)])
                accepted = not result.voice_veto and not result.safety_veto
                s.attempts.append(
                    CorrectionAttempt(
                        attempt=attempt_num,
                        original_response=s.current_response,
                        corrected_response=corrected,
                        judge_reason=s.current_reason,
                        rejudged=result,
                        accepted=accepted,
                    )
                )
                if accepted:
                    s.accepted = True
                    s.done = True
                else:
                    s.current_response = corrected
                    s.current_reason = combined_judge_reasons(result) or s.current_reason
                    if attempt_num == args.max_attempts:
                        s.done = True
    finally:
        corrector.close()
        for _name, provider in judges:
            provider.close()

    accepted_count = 0
    print("\n=== Results ===")
    for i, s in enumerate(states):
        tag = "ACCEPTED" if s.accepted else "STILL FAILS"
        accepted_count += int(s.accepted)
        print(
            f"{i + 1:>4}/{len(states)} {tag} after {len(s.attempts)} attempt(s) -- {s.entry.text!r}"
        )
        if args.dry_run:
            continue
        key = example_key(s.entry)
        s.meta.corrections = s.attempts
        if s.accepted and s.attempts:
            s.meta.judge = s.attempts[-1].rejudged
        metadata_store.set(key, s.meta)

    if args.dry_run:
        print("\n(--dry-run: no metadata was actually written)")
    else:
        print(f"\n{accepted_count}/{len(eligible)} corrected successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
