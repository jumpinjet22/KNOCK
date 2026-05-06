# KNOCK

KNOCK is a **local-first, privacy-focused door answering platform** for safe visitor conversations.

This repository is the initial clean architecture foundation for a long-term modular system, not a rapid prototype.

## Project Overview

KNOCK is designed to eventually coordinate:
- visitor event intake (doorbell/camera/intercom)
- conversation policy and safety rules
- local and remote AI providers (as swappable modules)
- safe, auditable response decisions
- future hardware integrations

Today, this repo provides a lightweight skeleton with deterministic behavior and test coverage.

## Philosophy

- **Safety-first**: responses must never compromise household security.
- **Local-first**: prioritize operation without cloud dependency.
- **Privacy-first**: minimize sensitive data movement and retention.
- **Modular providers**: each capability (LLM/STT/TTS/vision/input/output) has a clear abstraction boundary.
- **Testability**: core orchestration and policy logic are covered by unit tests.
- **Minimal dependencies**: only essential libraries are included at this stage.

## Architecture Summary

Current flow:

`VisitorEvent -> policy check -> intent classification -> canned response -> ResponseDecision`

Key modules:
- `knock.core`: events, state, orchestrator, response models
- `knock.conversation`: policy, intent, response templates
- `knock.providers`: provider interfaces and mock implementations
- `knock.api`: FastAPI surface (`POST /respond`)
- `knock.cli`: local CLI harness (`python -m knock`)

## Local-First / Privacy-First Notes

- No external AI API calls in the current implementation.
- Mock provider behavior is deterministic and offline.
- No audio/video model pipelines are included yet.

## ⚠ Hardware & Integration Status

Hardware and third-party integrations are **not implemented yet**.

Files under `knock.integrations` are placeholders only for future work:
- UniFi
- Frigate
- Home Assistant
- MQTT bridge behavior

Likewise, no real STT/TTS/vision engines are active yet.

## Quickstart

### 1) Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

### 2) Run tests

```bash
pytest
```

### 3) Run CLI

```bash
python -m knock
```

Example:

- Visitor: `Hi I have an Amazon package`
- Response: `Thanks. You can leave the package by the door.`

### 4) Run API locally

```bash
uvicorn knock.api.app:app --reload
```

Test endpoint:

```bash
curl -X POST http://127.0.0.1:8000/respond \
  -H "Content-Type: application/json" \
  -d '{"source":"doorbell","text":"Hi I have a package","timestamp":"2026-01-01T12:00:00Z"}'
```

### 5) Docker local run

```bash
docker compose -f docker/compose.local.yml up --build
```

## Future Roadmap

See:
- `docs/architecture.md`
- `docs/roadmap.md`
- `docs/hardware.md`

Near-term priorities:
1. richer safety policy and auditing
2. real provider adapters behind existing interfaces
3. hardware input/output bridges
4. session persistence and event replay

## License

MIT
