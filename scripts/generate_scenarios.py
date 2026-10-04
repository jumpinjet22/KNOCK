#!/usr/bin/env python3
"""Stage 1 of the local synthetic training-data pipeline: use one or more
local Ollama models to synthesize a large, varied, randomized set of
realistic "visitor at the door" lines, one intent category at a time.

A real doorbell is chaotic -- you don't know who's coming or what they'll
say -- so a small hand-written scenario list can't capture that variety.
This generates many independently-sampled candidates per category instead
(including messy, incomplete, transcript-like phrasing, since real input
here is STT output, not clean text), producing a plain-text scenario file
in exactly the shape scripts/generate_training_data.py (Stage 2) expects.

Not part of the automated test suite -- a manual, hands-on dev tool, like
scripts/smoke_test_providers.py.

Usage:
    python scripts/generate_scenarios.py --list-models
    python scripts/generate_scenarios.py \\
        --models qwen2.5:14b-instruct,llama3.1:8b-instruct \\
        [--categories delivery,food_delivery,visitation]  # default: every known intent + unknown
        [--count-per-category 15] [--batch-size 5] \\
        [--ollama-host 127.0.0.1] [--ollama-port 11434] [--ollama-timeout 120.0] \\
        --output scripts/training_scenarios.generated.txt

Reuses the same category descriptions Orchestrator itself uses to phrase
responses (`_INTENT_DESCRIPTIONS`) so there's one taxonomy, not two.
Every category gets a share of each given model's output, so the synthetic
input distribution isn't biased by a single model's "imagination" -- but
each model is loaded once and runs through every category before handing
off to the next, instead of swapping models on every batch call, since
Ollama only keeps one model resident in VRAM at a time. Minimal automated
hygiene only (dedup, drop degenerate lines) -- real judgment-based
filtering happens later, in the Training page's human review step, not
here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from knock.config import OllamaConfig
from knock.core.orchestrator import _INTENT_DESCRIPTIONS, _LLM_CLASSIFIABLE_INTENTS
from knock.providers.llm.ollama import OllamaProvider

# _INTENT_DESCRIPTIONS["unknown"] is written as a response-phrasing
# instruction ("say something like..."), not a scenario-generation prompt,
# so "unknown" gets its own description here instead of reusing it.
_UNKNOWN_SCENARIO_DESCRIPTION = (
    "vague, off-topic, or small-talk things a visitor might say that don't "
    "fit any specific category -- idle chatter, an unrelated question, "
    "something ambiguous or incomplete"
)

_ALL_CATEGORIES = [*_LLM_CLASSIFIABLE_INTENTS, "unknown"]


def _category_description(category: str) -> str:
    if category == "unknown":
        return _UNKNOWN_SCENARIO_DESCRIPTION
    return _INTENT_DESCRIPTIONS[category]


def _scenario_prompt(description: str, batch_size: int) -> str:
    return (
        f"Generate {batch_size} different realistic things a visitor might say "
        "or that get announced at a smart doorbell, all fitting this situation: "
        f"{description}\n"
        "Make them sound like real speech-to-text transcripts -- vary phrasing, "
        "tone, and directness; some should be informal, incomplete, run-on, or "
        "slightly ambiguous, the way real transcribed speech often is.\n"
        "Reply with exactly one line per example, no numbering, no extra commentary."
    )


def list_models(base_url: str) -> int:
    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Could not reach Ollama at {base_url}: {exc}", file=sys.stderr)
        return 1
    names = [model["name"] for model in response.json().get("models", [])]
    if not names:
        print(f"No models pulled yet on {base_url}.")
        return 0
    print(f"Models available on {base_url}:")
    for name in sorted(names):
        print(f"  {name}")
    return 0


def generate_batch(provider: OllamaProvider, description: str, batch_size: int) -> list[str]:
    text = provider.generate(_scenario_prompt(description, batch_size))
    return [line.strip(" \t-*0123456789.") for line in text.splitlines() if line.strip()]


def _split_evenly(total: int, parts: int) -> list[int]:
    """Splits `total` into `parts` near-equal non-negative ints that sum to it."""
    base, remainder = divmod(total, parts)
    return [base + 1] * remainder + [base] * (parts - remainder)


def generate_all_categories(
    models: list[str],
    categories: list[str],
    count: int,
    batch_size: int,
    *,
    host: str,
    port: int,
    timeout: float,
) -> dict[str, list[tuple[str, str]]]:
    """Returns {category: [(model_name, scenario_text), ...]}, each list deduped
    case-insensitively and dropping degenerate (<3-word) lines.

    Each model is loaded once and generates its full quota across every
    category before the next model is loaded, rather than round-robining
    model-by-model on every batch call: Ollama only keeps one model resident
    in VRAM, so swapping per batch means reloading large models from disk
    over and over for no benefit. Categories still mix contributions from
    every model (in model-major chunks) so the synthetic distribution isn't
    biased by a single model's "imagination."
    """
    collected: dict[str, list[tuple[str, str]]] = {category: [] for category in categories}
    seen: dict[str, set[str]] = {category: set() for category in categories}
    model_quotas = _split_evenly(count, len(models))

    for model, quota in zip(models, model_quotas, strict=True):
        if quota <= 0:
            continue
        provider = OllamaProvider(
            config=OllamaConfig(host=host, port=port, model=model, timeout=timeout)
        )
        try:
            for category in categories:
                target = min(count, len(collected[category]) + quota)
                description = _category_description(category)
                attempts = 0
                max_attempts = max(5, (quota // batch_size + 2) * 2)
                while len(collected[category]) < target and attempts < max_attempts:
                    attempts += 1
                    for line in generate_batch(provider, description, batch_size):
                        if len(line.split()) < 3:
                            continue
                        key = line.lower()
                        if key in seen[category]:
                            continue
                        seen[category].add(key)
                        collected[category].append((model, line))
                        if len(collected[category]) >= target:
                            break
        finally:
            provider.close()
    return collected


def write_output(path: Path, by_category: dict[str, list[tuple[str, str]]]) -> None:
    lines = [
        "# Generated by scripts/generate_scenarios.py -- see that script's docstring.",
        "# One visitor line per line; # comments and blank lines are ignored by",
        "# scripts/generate_training_data.py.",
        "",
    ]
    for category, pairs in by_category.items():
        for model, text in pairs:
            lines.append(f"# -- {category} (model: {model}) --")
            lines.append(text)
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", help="comma-separated Ollama model names")
    parser.add_argument(
        "--categories",
        default=",".join(_ALL_CATEGORIES),
        help=f"comma-separated subset of: {', '.join(_ALL_CATEGORIES)}",
    )
    parser.add_argument("--count-per-category", type=int, default=15)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--ollama-host", default="127.0.0.1")
    parser.add_argument("--ollama-port", type=int, default=11434)
    parser.add_argument("--ollama-timeout", type=float, default=120.0)
    parser.add_argument(
        "--output", type=Path, default=Path("scripts/training_scenarios.generated.txt")
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="list models pulled on the target Ollama server and exit",
    )
    args = parser.parse_args()

    base_url = f"http://{args.ollama_host}:{args.ollama_port}"
    if args.list_models:
        return list_models(base_url)

    if not args.models:
        parser.error("--models is required (or pass --list-models)")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    unknown_categories = [c for c in categories if c not in _ALL_CATEGORIES]
    if unknown_categories:
        parser.error(f"unknown categories: {unknown_categories}; choose from {_ALL_CATEGORIES}")

    by_category = generate_all_categories(
        models,
        categories,
        args.count_per_category,
        args.batch_size,
        host=args.ollama_host,
        port=args.ollama_port,
        timeout=args.ollama_timeout,
    )
    total_written = 0
    for category in categories:
        pairs = by_category[category]
        total_written += len(pairs)
        print(f"{category}: {len(pairs)}/{args.count_per_category} unique scenarios generated")
        if len(pairs) < args.count_per_category:
            print("  (stopped early -- models ran out of new unique lines for this category)")

    write_output(args.output, by_category)
    print(f"\nWrote {total_written} scenarios across {len(categories)} categories to {args.output}")
    print(
        f"Review/edit it, then run: python scripts/generate_training_data.py "
        f"--scenarios {args.output} ..."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
