---
layout: default
title: KNOCK
---

# KNOCK

**Local-first, privacy-focused door answering for safer visitor conversations.**

KNOCK is an open-source foundation for building a smart front-door assistant that helps households handle visitor interactions safely—without forcing cloud dependence.

## Why KNOCK?

Most “smart” door systems push audio/video and decision-making into cloud services. KNOCK takes a different path:

- **Safety-first:** policy-gated responses before any conversational output.
- **Local-first:** designed to keep core workflows available offline.
- **Privacy-first:** minimizes sensitive household data movement.
- **Modular-by-design:** swap LLM/STT/TTS/vision/input/output providers as adapters mature.

## Current Status

KNOCK is in an **early foundation phase** with deterministic, testable core behavior.

Today, the implemented flow is:

`VisitorEvent -> policy check -> intent classification -> canned response -> ResponseDecision`

### Included now

- Core orchestration and response decision models
- Conversation policy and intent classification scaffolding
- FastAPI endpoint (`POST /respond`)
- CLI harness for local interaction
- Mock providers and unit tests

### Not yet implemented

- Production hardware bridges (doorbell/intercom/camera)
- Real STT/TTS/vision engines
- Complete integrations for UniFi / Frigate / Home Assistant / MQTT

## Quick Start

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
pytest
python -m knock
```

Run the API:

```bash
uvicorn knock.api.app:app --reload
```

Sample request:

```bash
curl -X POST http://127.0.0.1:8000/respond \
  -H "Content-Type: application/json" \
  -d '{"source":"doorbell","text":"Hi I have a package","timestamp":"2026-01-01T12:00:00Z"}'
```

## Architecture at a glance

- `knock.core` — events, state, orchestrator, response models
- `knock.conversation` — policy, intent, response templates
- `knock.providers` — provider interfaces and mock implementations
- `knock.api` — HTTP service surface
- `knock.cli` — local shell interaction

## Project Roadmap

Near-term priorities:

1. Richer safety policy and auditing
2. Real provider adapters behind current interfaces
3. Hardware input/output bridges
4. Session persistence and event replay

See deeper planning docs in:

- `docs/architecture.md`
- `docs/roadmap.md`
- `docs/hardware.md`

## Contributing

Contributions are welcome—especially around:

- provider adapter implementations
- policy hardening
- integration testing
- deployment patterns for home-lab setups

Open an issue or PR with context, assumptions, and tests.

## License

MIT
