#!/usr/bin/env python3
"""Stage 2 of the local synthetic training-data pipeline: run a scenario
file through one or more local Ollama models via the real Orchestrator,
recording every response to the real audit log for review in the web
UI's Training page (see docs/roadmap.md's "Model distillation / LoRA
training workflow").

Not part of the automated test suite -- a manual, hands-on dev tool, like
scripts/smoke_test_providers.py. Run this on a machine with a real GPU and
a local Ollama server (NOT production's inference host -- point
--ollama-host/--ollama-port at your own machine), then review/export the
results:

    uvicorn knock.api.app:app --reload   # separate terminal
    cd web && npm run dev                # separate terminal
    open http://localhost:5173, complete first-run setup if prompted,
    go to /training, review (pick the best response per scenario when
    multiple models answered the same one), export.

Usage:
    python scripts/generate_training_data.py --list-models
    python scripts/generate_training_data.py \\
        --models qwen2.5:14b-instruct,llama3.1:8b-instruct \\
        [--scenarios scripts/training_scenarios.txt] \\
        [--ollama-host 127.0.0.1] [--ollama-port 11434] [--ollama-timeout 120.0] \\
        [--repeats 1] [--audit-log PATH]

Each (scenario x model) pair becomes its own audit entry, so the same
visitor line answered by several models shows up as separate cards with
identical visitor text and different candidate responses in the Training
page -- approve whichever is best, reject the rest. A scenario that trips
the deterministic policy engine (occupancy/schedule/unlock probe, or an
emergency) gets `intent=None` and is excluded from Training entirely,
same as any other blocked/escalated request -- that's correct, not a bug.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

from knock.config import OllamaConfig
from knock.core.audit import JSONLAuditLog
from knock.core.events import VisitorEvent
from knock.core.orchestrator import Orchestrator
from knock.providers.llm.ollama import OllamaProvider


def load_scenarios(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


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


def run_model(
    model: str,
    scenarios: list[str],
    *,
    host: str,
    port: int,
    timeout: float,
    repeats: int,
    audit_log: JSONLAuditLog,
) -> list[dict[str, object]]:
    provider = OllamaProvider(
        config=OllamaConfig(host=host, port=port, model=model, timeout=timeout)
    )
    orchestrator = Orchestrator(llm_provider=provider, audit_log=audit_log)
    results: list[dict[str, object]] = []
    try:
        for i, scenario in enumerate(scenarios):
            for rep in range(repeats):
                t0 = time.monotonic()
                event = VisitorEvent(text=scenario, timestamp=datetime.now(UTC))
                decision = orchestrator.respond(event)
                elapsed = time.monotonic() - t0
                print(
                    f"[{model}] {i + 1:>3}/{len(scenarios)} rep {rep + 1}/{repeats} "
                    f"({elapsed:5.1f}s) intent={decision.intent!r:24} reason={decision.reason!r} "
                    f"text={scenario!r}"
                )
                results.append(
                    {
                        "model": model,
                        "scenario": scenario,
                        "intent": decision.intent,
                        "reason": decision.reason,
                        "timestamp": event.timestamp.isoformat(),
                        # Lets the judge stage compute the same content-hash
                        # example_key() training.py uses, directly from a
                        # manifest row -- without this, joining a manifest
                        # row back to its audit entry means a fragile
                        # (scenario, timestamp) match instead.
                        "response_text": decision.text,
                    }
                )
    finally:
        provider.close()
    return results


def print_agreement_report(results: list[dict[str, object]], repeats: int) -> None:
    if repeats <= 1:
        return
    print("\n=== Same-model/same-scenario agreement (repeats > 1) ===")
    grouped: dict[tuple[str, str], list[object]] = defaultdict(list)
    for row in results:
        grouped[(row["model"], row["scenario"])].append(row["intent"])  # type: ignore[index]
    inconsistent = 0
    for (model, scenario), intents in grouped.items():
        counts = Counter(intents)
        if len(counts) > 1:
            inconsistent += 1
            print(f"INCONSISTENT [{model}]: {scenario!r} -> {dict(counts)}")
    print(
        f"\n{len(grouped)} (model, scenario) pairs, {inconsistent} inconsistent "
        f"across {repeats} repeats each"
    )


def write_manifest(results: list[dict[str, object]], audit_log_path: Path) -> Path:
    manifest_path = audit_log_path.with_name(f"{audit_log_path.stem}.manifest.jsonl")
    with manifest_path.open("a", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row))
            handle.write("\n")
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", help="comma-separated Ollama model names")
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=Path(__file__).parent / "training_scenarios.txt",
        help="path to a scenario file (one visitor line per line, # comments ignored)",
    )
    parser.add_argument("--ollama-host", default="127.0.0.1")
    parser.add_argument("--ollama-port", type=int, default=11434)
    parser.add_argument(
        "--ollama-timeout",
        type=float,
        default=120.0,
        help="a larger teacher model's cold-load-then-generate can exceed the usual 30s default",
    )
    parser.add_argument(
        "--repeats", type=int, default=1, help="re-run each scenario N times per model"
    )
    parser.add_argument(
        "--audit-log", type=Path, default=None, help="override the default audit.jsonl path"
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

    scenarios = load_scenarios(args.scenarios)
    if not scenarios:
        print(f"No scenarios found in {args.scenarios}", file=sys.stderr)
        return 1
    print(f"Loaded {len(scenarios)} scenarios from {args.scenarios}")

    audit_log = JSONLAuditLog(args.audit_log) if args.audit_log else JSONLAuditLog()
    print(f"Recording to {audit_log.path}")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    all_results: list[dict[str, object]] = []
    for model in models:
        all_results.extend(
            run_model(
                model,
                scenarios,
                host=args.ollama_host,
                port=args.ollama_port,
                timeout=args.ollama_timeout,
                repeats=args.repeats,
                audit_log=audit_log,
            )
        )

    blocked = sum(1 for row in all_results if row["intent"] is None)
    total = len(all_results)
    print(
        f"\n{total} total calls across {len(models)} model(s). "
        f"{blocked} were policy-blocked/escalated and won't appear in Training (intent=None)."
    )

    print_agreement_report(all_results, args.repeats)

    manifest_path = write_manifest(all_results, audit_log.path)
    print(
        f"\nWrote a model-provenance manifest to {manifest_path} "
        "(not part of the audit log itself)."
    )
    print("Open the web UI's Training page to review and export.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
