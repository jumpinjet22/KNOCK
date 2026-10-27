#!/usr/bin/env python3
"""Export a batch of synthetic training data for the "Who's At The Door?"
crowd-review game (see plans/knock-game.md, section 7).

The game is public, so this script is the one gate between KNOCK's data and
the internet. It only ever reads a synthetic audit log:

- `--audit-log` is required, and the default production log
  (`~/.local/share/knock/audit.jsonl`) is refused outright.
- The log must have the `<stem>.manifest.jsonl` that
  `scripts/generate_training_data.py` writes next to it, and only entries
  whose `example_key()` appears in that manifest are exported. An entry
  that got into the log any other way (a real visit, a hand edit) is
  skipped, never published.
- Nothing from an entry's metadata leaves: no timestamps, session ids,
  camera names or zones. Each item carries the visitor line, a short
  player-facing scene line, the generator's intent, candidate replies and
  the judge's averages.

Usage:
    python scripts/export_game_batch.py --audit-log PATH --out batch.json \\
        [--reviews PATH] [--metadata-store PATH] [--batch NAME] \\
        [--max-pairs-per-line 3]

Format (`"format": "knock-game-batch", "version": 1`), one object per item:

    intent mode: {"knock_key", "mode": "intent", "text", "scene",
                  "generator_intent", "gold_answer", "judge"}
    pair mode:   {"knock_key": "pair:<keyA>:<keyB>", "mode": "pair", "text",
                  "scene", "generator_intent", "gold_answer",
                  "replies": [{"knock_key", "text", "judge"}, {...}]}

An intent item's answer is an intent name; a pair item's answer is one
reply's `knock_key` or "both_bad". `gold_answer` is set only where a human
already decided (an approved review for intent items; one reply approved and
the other rejected for pair items).
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.scene import SceneContext
from knock.core.training import (
    VALID_TRAINING_INTENTS,
    TrainingMetadataStore,
    TrainingReview,
    TrainingReviewStore,
    example_key,
)
from knock.core.training_judge import AggregatedJudgeResult

BATCH_FORMAT = "knock-game-batch"
BATCH_VERSION = 1
PRODUCTION_AUDIT_LOG = Path.home() / ".local" / "share" / "knock" / "audit.jsonl"
_MAX_SCENE_CHARS = 200


class ExportRefused(Exception):
    """The audit log isn't one this script is willing to publish from."""


def manifest_path_for(audit_log_path: Path) -> Path:
    # Same naming as generate_training_data.write_manifest().
    return audit_log_path.with_name(f"{audit_log_path.stem}.manifest.jsonl")


def synthetic_keys(audit_log_path: Path) -> set[str]:
    """The `example_key()` of every entry generate_training_data.py wrote,
    computed from its manifest rows. Refuses a log that has none.
    """
    if audit_log_path.expanduser().resolve() == PRODUCTION_AUDIT_LOG.resolve():
        raise ExportRefused(
            f"{audit_log_path} is the production audit log (real visitors); refusing to export it"
        )
    manifest = manifest_path_for(audit_log_path)
    if not manifest.exists():
        raise ExportRefused(
            f"no {manifest.name} next to {audit_log_path}; only logs written by "
            "scripts/generate_training_data.py can be exported"
        )
    keys: set[str] = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "response_text" not in row:
            continue  # manifests from before the field existed can't be matched
        stamp = datetime.fromisoformat(row["timestamp"])
        raw = f"{stamp.isoformat()}|{row['scenario']}|{row['response_text']}"
        keys.add(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16])
    return keys


def _adds_to(summary: str, detail: str | None) -> bool:
    """Whether `detail` says something the summary doesn't already: set, and
    not already in the summary as a whole phrase."""
    if not detail or not detail.strip():
        return False
    pattern = rf"\b{re.escape(detail.strip())}\b"
    return re.search(pattern, summary, re.IGNORECASE) is None


def scene_for_players(scene: SceneContext | None) -> str | None:
    """What the camera saw, as one short line a player can read. Leaves out
    the camera name, zones and capture time on purpose, and any detail the
    summary already mentions ("holding a pizza bag; carrying a pizza bag").
    """
    if scene is None:
        return None
    parts: list[str] = []
    obs = scene.observation
    if obs is not None:
        summary = obs.summary.strip()
        if summary:
            parts.append(summary.rstrip("."))
        if obs.people_count == 0 and _adds_to(summary, "nobody in frame"):
            parts.append("nobody in frame")
        carrying = [thing for thing in obs.carrying if _adds_to(summary, thing)]
        if carrying:
            parts.append("carrying " + ", ".join(carrying))
        if _adds_to(summary, obs.uniform_or_logo):
            parts.append(f"wearing {obs.uniform_or_logo}")
        if _adds_to(summary, obs.vehicle):
            parts.append(f"{obs.vehicle} nearby")
        if _adds_to(summary, obs.visible_text):
            parts.append(f"visible text: '{obs.visible_text}'")
    elif scene.detected_labels:
        parts.append("camera detected: " + ", ".join(scene.detected_labels))
    if not parts:
        return None
    line = "; ".join(parts)
    return line if len(line) <= _MAX_SCENE_CHARS else line[: _MAX_SCENE_CHARS - 3] + "..."


def _judge_summary(judge: AggregatedJudgeResult | None) -> dict[str, object] | None:
    if judge is None:
        return None
    # Averages only; per-judge reasons can quote the prompt back verbatim.
    return {
        "visitor_voice": judge.visitor_voice_avg,
        "category_correct": judge.category_correct_avg,
        "safety_compliant": judge.safety_compliant_avg,
        "natural_quality": judge.natural_quality_avg,
        "safety_veto": judge.safety_veto,
        "disagreement": judge.disagreement,
    }


def _usable(entry: AuditEntry, judge: AggregatedJudgeResult | None) -> bool:
    if entry.intent is None or entry.intent not in VALID_TRAINING_INTENTS:
        return False
    # A voice veto means the "visitor" line is really the resident talking:
    # bad data for every mode.
    return not (judge is not None and judge.voice_veto)


def build_batch(
    entries: list[AuditEntry],
    allowed_keys: set[str],
    reviews: dict[str, TrainingReview],
    judges: dict[str, AggregatedJudgeResult],
    *,
    batch_name: str | None = None,
    max_pairs_per_line: int = 3,
) -> tuple[dict[str, object], int]:
    """The batch document, plus how many entries were skipped for not being
    in the manifest.
    """
    skipped_unmarked = 0
    usable: list[tuple[str, AuditEntry]] = []
    seen: set[str] = set()
    for entry in entries:
        key = example_key(entry)
        if key not in allowed_keys:
            skipped_unmarked += 1
            continue
        if key in seen or not _usable(entry, judges.get(key)):
            continue
        seen.add(key)
        usable.append((key, entry))
    usable.sort(key=lambda pair: pair[0])

    items: list[dict[str, object]] = []
    for key, entry in usable:
        review = reviews.get(key)
        if review is not None and review.status == "rejected":
            continue  # we don't know whether the intent or the reply was wrong
        gold = None
        if review is not None and review.status == "approved":
            gold = review.intent_override or entry.intent
        items.append(
            {
                "knock_key": key,
                "mode": "intent",
                "text": entry.text,
                "scene": scene_for_players(entry.scene),
                "generator_intent": entry.intent,
                "gold_answer": gold,
                "judge": _judge_summary(judges.get(key)),
            }
        )

    # Pair items: different replies to the same line, scene and intent.
    groups: dict[tuple[str, str | None, str], list[tuple[str, AuditEntry]]] = {}
    for key, entry in usable:
        if not entry.response_text or entry.replaced_by is not None:
            continue  # a filter's fallback line isn't a candidate reply
        assert entry.intent is not None
        group_key = (entry.text, scene_for_players(entry.scene), entry.intent)
        groups.setdefault(group_key, []).append((key, entry))
    for (text, scene, intent), candidates in sorted(groups.items(), key=lambda g: g[1][0][0]):
        distinct: dict[str, tuple[str, AuditEntry]] = {}
        for key, entry in candidates:
            distinct.setdefault(entry.response_text, (key, entry))
        replies = list(distinct.values())
        for (key_a, a), (key_b, b) in itertools.islice(
            itertools.combinations(replies, 2), max_pairs_per_line
        ):
            status_a = reviews[key_a].status if key_a in reviews else "pending"
            status_b = reviews[key_b].status if key_b in reviews else "pending"
            gold = None
            if (status_a, status_b) == ("approved", "rejected"):
                gold = key_a
            elif (status_a, status_b) == ("rejected", "approved"):
                gold = key_b
            items.append(
                {
                    "knock_key": f"pair:{key_a}:{key_b}",
                    "mode": "pair",
                    "text": text,
                    "scene": scene,
                    "generator_intent": intent,
                    "gold_answer": gold,
                    "replies": [
                        {
                            "knock_key": k,
                            "text": e.response_text,
                            "judge": _judge_summary(judges.get(k)),
                        }
                        for k, e in ((key_a, a), (key_b, b))
                    ],
                }
            )

    if batch_name is None:
        digest = hashlib.sha256("|".join(str(i["knock_key"]) for i in items).encode("utf-8"))
        batch_name = digest.hexdigest()[:12]
    batch = {
        "format": BATCH_FORMAT,
        "version": BATCH_VERSION,
        "batch": batch_name,
        "intents": sorted(VALID_TRAINING_INTENTS),
        "items": items,
    }
    return batch, skipped_unmarked


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--audit-log", type=Path, required=True, help="a SYNTHETIC audit log")
    parser.add_argument("--out", type=Path, required=True, help="where to write batch.json")
    parser.add_argument("--reviews", type=Path, default=None, help="training_reviews.json")
    parser.add_argument("--metadata-store", type=Path, default=None)
    parser.add_argument("--batch", default=None, help="batch name (default: a content hash)")
    parser.add_argument("--max-pairs-per-line", type=int, default=3)
    args = parser.parse_args()

    try:
        allowed = synthetic_keys(args.audit_log)
    except ExportRefused as exc:
        print(f"Refusing: {exc}", file=sys.stderr)
        return 2
    if not args.audit_log.exists():
        print(f"No audit log at {args.audit_log}", file=sys.stderr)
        return 1

    entries = JSONLAuditLog(args.audit_log).recent(limit=1_000_000)
    reviews = TrainingReviewStore(args.reviews).all()
    judges = {
        key: meta.judge
        for key, meta in TrainingMetadataStore(args.metadata_store).all().items()
        if meta.judge is not None
    }
    batch, skipped = build_batch(
        entries,
        allowed,
        reviews,
        judges,
        batch_name=args.batch,
        max_pairs_per_line=args.max_pairs_per_line,
    )
    args.out.write_text(json.dumps(batch, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    items = batch["items"]
    assert isinstance(items, list)
    pairs = sum(1 for item in items if item["mode"] == "pair")
    gold = sum(1 for item in items if item["gold_answer"] is not None)
    print(
        f"Wrote {len(items)} items ({len(items) - pairs} intent, {pairs} pair, {gold} gold) "
        f"to {args.out}"
    )
    if skipped:
        print(f"Skipped {skipped} entries not in the manifest (not known to be synthetic).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
