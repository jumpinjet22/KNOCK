#!/usr/bin/env python3
"""Stage 3 (optional) of the local synthetic training-data pipeline: use a
local judge model to pre-review pending Training-page entries, so a human
only has to look at the ones the judge itself isn't confident about.

For every pending audit entry (skips anything already approved/rejected,
and anything with intent=None, which Training already excludes), asks the
judge model two questions: is the assigned intent category actually
correct, and is the candidate response a good, safe, natural doorbell
reply? The judge also reports its own confidence (0-100).

- confidence >= --threshold (default 80): the judge's own call is applied
  directly -- "approved" (with an intent correction recorded via
  TrainingReview.intent_override if the judge disagreed with the original
  label) or "rejected" if the response itself is unsafe/bad/garbled.
- confidence < --threshold: left untouched (still "pending") for a human
  to decide in the Training page -- this is the point: cut a large batch
  down to just the genuinely uncertain cases instead of requiring a human
  to look at everything.

This is a judgment call, not a guarantee -- it uses the same kind of LLM
call it's reviewing, so treat the "approved" pile as a head start, not a
substitute for spot-checking before actually training on it.

Not part of the automated test suite -- a manual, hands-on dev tool, like
scripts/smoke_test_providers.py.

Usage:
    python scripts/auto_review_training_data.py \\
        --judge-model qwen2.5:14b \\
        [--threshold 80] \\
        [--ollama-host 127.0.0.1] [--ollama-port 11434] [--ollama-timeout 120.0] \\
        [--audit-log PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from knock.config import OllamaConfig
from knock.core.audit import JSONLAuditLog
from knock.core.orchestrator import _LLM_CLASSIFIABLE_INTENTS
from knock.core.training import TrainingReview, TrainingReviewStore, example_key
from knock.providers.llm.ollama import OllamaProvider

_VALID_INTENTS = [*_LLM_CLASSIFIABLE_INTENTS, "unknown"]

_JUDGE_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _judge_prompt(visitor_text: str, intent: str, response_text: str) -> str:
    labels = ", ".join(_VALID_INTENTS)
    return (
        "You are quality-reviewing one training example for a smart doorbell "
        "assistant that replies to visitors. Judge it carefully and skeptically.\n\n"
        f'Visitor said: "{visitor_text}"\n'
        f'Assigned category: "{intent}"\n'
        f'Candidate reply: "{response_text}"\n\n'
        f"Valid categories: {labels}\n\n"
        "Check:\n"
        "1. Is the assigned category actually correct for what the visitor said?\n"
        "2. Is the candidate reply a good, natural, safe doorbell reply? It must "
        "NOT reveal whether anyone is home, share the household's schedule, or "
        "offer to unlock/open the door. It must be a complete, coherent sentence "
        "(not truncated, not raw reasoning, not a repeat of the category name).\n\n"
        "Reply with ONLY compact JSON, no other text:\n"
        '{"correct_intent": "<one of the valid categories>", '
        '"response_good": true/false, '
        '"confidence": <integer 0-100, how confident you are in this judgment overall>, '
        '"reason": "<one short sentence>"}'
    )


def _parse_judge_response(raw: str) -> dict[str, object] | None:
    match = _JUDGE_JSON_RE.search(raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--threshold", type=int, default=80)
    parser.add_argument("--ollama-host", default="127.0.0.1")
    parser.add_argument("--ollama-port", type=int, default=11434)
    parser.add_argument("--ollama-timeout", type=float, default=120.0)
    parser.add_argument("--audit-log", type=Path, default=None)
    parser.add_argument(
        "--dry-run", action="store_true", help="print decisions without writing reviews"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="only judge the first N pending entries"
    )
    args = parser.parse_args()

    audit_log = JSONLAuditLog(args.audit_log) if args.audit_log else JSONLAuditLog()
    entries = audit_log.recent(limit=100_000)
    entries.reverse()  # oldest first, for a stable/readable progress order
    store = TrainingReviewStore()
    reviews = store.all()

    candidates = [
        entry
        for entry in entries
        if entry.intent is not None
        and reviews.get(example_key(entry), TrainingReview()).status == "pending"
    ]
    if args.limit is not None:
        candidates = candidates[: args.limit]
    print(f"{len(candidates)} pending entries to judge (of {len(entries)} total)")

    provider = OllamaProvider(
        config=OllamaConfig(
            host=args.ollama_host,
            port=args.ollama_port,
            model=args.judge_model,
            timeout=args.ollama_timeout,
        )
    )

    approved = rejected = deferred = parse_failures = 0
    try:
        for i, entry in enumerate(candidates):
            t0 = time.monotonic()
            raw = provider.generate(
                _judge_prompt(entry.text, entry.intent or "unknown", entry.response_text)
            )
            elapsed = time.monotonic() - t0
            verdict = _parse_judge_response(raw)

            if verdict is None:
                parse_failures += 1
                print(f"{i + 1:>4}/{len(candidates)} ({elapsed:4.1f}s) UNPARSEABLE -- left pending")
                continue

            confidence_raw = verdict.get("confidence", 0)
            confidence = confidence_raw if isinstance(confidence_raw, int) else 0
            correct_intent = str(verdict.get("correct_intent", entry.intent))
            response_good = bool(verdict.get("response_good", False))
            reason = str(verdict.get("reason", ""))

            if confidence < args.threshold:
                deferred += 1
                print(
                    f"{i + 1:>4}/{len(candidates)} ({elapsed:4.1f}s) "
                    f"DEFER (confidence={confidence}) -- {reason}"
                )
                continue

            key = example_key(entry)
            if response_good and correct_intent in _VALID_INTENTS:
                override = correct_intent if correct_intent != entry.intent else None
                review = TrainingReview(status="approved", intent_override=override)
                approved += 1
                tag = "APPROVE" + (f" (intent -> {correct_intent})" if override else "")
            else:
                review = TrainingReview(status="rejected")
                rejected += 1
                tag = "REJECT"

            print(
                f"{i + 1:>4}/{len(candidates)} ({elapsed:4.1f}s) "
                f"{tag} (confidence={confidence}) -- {reason}"
            )
            if not args.dry_run:
                store.set(key, review)
    finally:
        provider.close()

    print(
        f"\n{approved} approved, {rejected} rejected, {deferred} deferred to human review, "
        f"{parse_failures} unparseable judge replies (left pending)."
    )
    if args.dry_run:
        print("(--dry-run: no reviews were actually written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
