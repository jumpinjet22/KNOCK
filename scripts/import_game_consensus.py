#!/usr/bin/env python3
"""Import the crowd's verdicts from the "Who's At The Door?" game back into
`training_reviews.json` (see plans/knock-game.md, section 7).

Reads a `consensus-<date>.json` file the game's scorer wrote
(`"format": "knock-game-consensus", "version": 1`):

    {"format": "knock-game-consensus", "version": 1, "day": "2026-10-27",
     "items": [{"knock_key", "mode": "intent" | "pair",
                "status": "settled" | "disputed" | "open",
                "label", "share", "total_weight"}]}

What it writes:

- settled intent item: the entry is `approved`, with `intent_override` when
  the crowd disagreed with the generator.
- settled pair item: the winning reply's entry is `approved`. A "both_bad"
  verdict is treated like a dispute, since someone has to write a better
  reply.
- disputed item: left `pending`, with a comment, so it lands in the
  Training page's needs-attention queue.
- open item: ignored.

It never overwrites a decision a human already made (a review that is
approved or rejected, or that carries an override), and it only touches
keys that exist in the audit log you point it at, so a bad or hostile
consensus file can't invent reviews.

Usage:
    python scripts/import_game_consensus.py consensus-2026-10-27.json \\
        --audit-log PATH [--reviews PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.training import (
    VALID_TRAINING_INTENTS,
    TrainingReview,
    TrainingReviewStore,
    example_key,
)

CONSENSUS_FORMAT = "knock-game-consensus"
CONSENSUS_VERSION = 1


class ConsensusItem(BaseModel):
    knock_key: str
    mode: Literal["intent", "pair"]
    status: Literal["settled", "disputed", "open"]
    label: str | None = None
    share: float = 0.0
    total_weight: float = 0.0


class ConsensusFile(BaseModel):
    format: Literal["knock-game-consensus"]
    version: Literal[1]
    day: str
    items: list[ConsensusItem]


def _human_decided(review: TrainingReview | None) -> bool:
    if review is None:
        return False
    return (
        review.status != "pending"
        or review.intent_override is not None
        or review.response_override is not None
    )


def _crowd_note(day: str, item: ConsensusItem) -> str:
    return (
        f"game {day}: crowd {item.status}, {item.label or 'no label'} "
        f"at {item.share:.0%} of weight {item.total_weight:.1f}"
    )


def plan_reviews(
    consensus: ConsensusFile,
    entries: dict[str, AuditEntry],
    existing: dict[str, TrainingReview],
) -> tuple[dict[str, TrainingReview], list[str]]:
    """The reviews to write, keyed by example_key, plus a line per item that
    was skipped and why.
    """
    planned: dict[str, TrainingReview] = {}
    skipped: list[str] = []

    def put(key: str, review: TrainingReview, item: ConsensusItem) -> None:
        if key not in entries:
            skipped.append(f"{item.knock_key}: {key} is not in the audit log")
        elif _human_decided(existing.get(key)):
            skipped.append(f"{item.knock_key}: {key} already has a human review")
        else:
            planned[key] = review

    for item in consensus.items:
        if item.status == "open":
            continue
        note = _crowd_note(consensus.day, item)
        if item.mode == "intent":
            entry = entries.get(item.knock_key)
            if item.status == "settled" and item.label not in VALID_TRAINING_INTENTS:
                skipped.append(f"{item.knock_key}: {item.label!r} is not a known intent")
                continue
            if item.status == "settled" and entry is not None:
                override = item.label if item.label != entry.intent else None
                review = TrainingReview(status="approved", intent_override=override, comment=note)
            else:
                review = TrainingReview(status="pending", comment=note)
            put(item.knock_key, review, item)
            continue

        parts = item.knock_key.split(":")
        if len(parts) != 3 or parts[0] != "pair":
            skipped.append(f"{item.knock_key}: not a pair key")
            continue
        reply_keys = parts[1:]
        if item.status == "settled" and item.label in reply_keys:
            put(item.label, TrainingReview(status="approved", comment=note), item)
        elif item.status == "settled" and item.label != "both_bad":
            skipped.append(f"{item.knock_key}: {item.label!r} is neither reply")
        else:
            for key in reply_keys:
                put(key, TrainingReview(status="pending", comment=note), item)
    return planned, skipped


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("consensus", type=Path, help="consensus-<date>.json from the game")
    parser.add_argument("--audit-log", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, default=None, help="training_reviews.json")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = parser.parse_args()

    try:
        consensus = ConsensusFile.model_validate_json(args.consensus.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        print(f"Can't read {args.consensus}: {exc}", file=sys.stderr)
        return 1

    entries = {
        example_key(entry): entry for entry in JSONLAuditLog(args.audit_log).recent(limit=1_000_000)
    }
    store = TrainingReviewStore(args.reviews)
    planned, skipped = plan_reviews(consensus, entries, store.all())

    for line in skipped:
        print(f"skip  {line}")
    for key, review in sorted(planned.items()):
        override = f" -> {review.intent_override}" if review.intent_override else ""
        print(f"{review.status:<8} {key}{override}  ({review.comment})")
    if args.dry_run:
        print(f"\nDry run: would write {len(planned)} reviews.")
        return 0
    for key, review in planned.items():
        store.set(key, review)
    approved = sum(1 for review in planned.values() if review.status == "approved")
    print(
        f"\nWrote {len(planned)} reviews ({approved} approved, "
        f"{len(planned) - approved} pending for you) to {store.path}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
