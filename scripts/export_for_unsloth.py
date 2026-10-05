#!/usr/bin/env python3
"""Converts KNOCK's training export (`knock.core.training.export_training_jsonl`,
`{task, instruction, output}` per line) into the ChatML `messages` shape
Unsloth's CLI (`unsloth train --format-type chatml`) expects:

    {"messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}]}

The system turn is `knock.conversation.prompts.SYSTEM_PROMPT` -- the exact
same string `OllamaProvider.generate()` sends in production -- so the
fine-tuned adapter trains on literally the same turn structure it'll see
at inference, not a different shape invented just for training.

Not part of the automated test suite -- a manual, hands-on dev tool, like
scripts/generate_training_data.py. Usage:

    python scripts/export_for_unsloth.py [--output PATH] [--limit N]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knock.conversation.prompts import SYSTEM_PROMPT
from knock.core.audit import JSONLAuditLog
from knock.core.training import TrainingReviewStore, export_training_jsonl

DEFAULT_OUTPUT = Path("scripts/unsloth_training_data.generated.jsonl")


def convert(exported_jsonl: str) -> list[dict[str, list[dict[str, str]]]]:
    records = []
    for line in exported_jsonl.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        records.append(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": record["instruction"]},
                    {"role": "assistant", "content": record["output"]},
                ]
            }
        )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=10000)
    args = parser.parse_args()

    exported = export_training_jsonl(
        JSONLAuditLog(), TrainingReviewStore(), limit=args.limit
    )
    records = convert(exported)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")

    print(f"Wrote {len(records)} chatml records to {args.output}")


if __name__ == "__main__":
    main()
