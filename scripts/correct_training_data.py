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
import time
from pathlib import Path

from knock.config import OllamaConfig
from knock.core.audit import JSONLAuditLog
from knock.core.orchestrator import _LLM_CLASSIFIABLE_INTENTS
from knock.core.training import (
    TrainingMetadataStore,
    TrainingReview,
    TrainingReviewStore,
    example_key,
)
from knock.core.training_correction import run_correction_loop
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

    accepted_count = 0
    try:
        for i, (entry, meta) in enumerate(eligible):
            t0 = time.monotonic()
            judge_reason = (
                meta.judge.per_judge[0].reason if meta.judge and meta.judge.per_judge else ""
            )
            result = run_correction_loop(
                corrector,
                judges,
                visitor_text=entry.text,
                intent=entry.intent or "unknown",
                bad_response=entry.response_text,
                judge_reason=judge_reason,
                valid_intents=_VALID_INTENTS,
                max_attempts=args.max_attempts,
            )
            elapsed = time.monotonic() - t0
            tag = "ACCEPTED" if result.accepted else "STILL FAILS"
            accepted_count += int(result.accepted)
            print(
                f"{i + 1:>4}/{len(eligible)} ({elapsed:5.1f}s) {tag} "
                f"after {len(result.attempts)} attempt(s) -- {entry.text!r}"
            )
            if args.dry_run:
                continue
            key = example_key(entry)
            meta.corrections = result.attempts
            if result.accepted and result.attempts:
                meta.judge = result.attempts[-1].rejudged
            metadata_store.set(key, meta)
    finally:
        corrector.close()
        for _name, provider in judges:
            provider.close()

    if args.dry_run:
        print("\n(--dry-run: no metadata was actually written)")
    else:
        print(f"\n{accepted_count}/{len(eligible)} corrected successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
