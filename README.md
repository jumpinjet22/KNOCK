<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo/knock-wordmark-dark.svg">
    <img src="docs/assets/logo/knock-wordmark-light.svg" alt="KNOCK" width="280">
  </picture>
</p>

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

- No *cloud* AI API calls — real providers only talk to services on your own network.
- Mock provider behavior remains deterministic and fully offline (no network calls at all).
- Real providers are opt-in: the orchestrator still uses the mock/canned-response path by default, and nothing in `knock.core`/`knock.conversation` requires a provider to be configured.
- No vision model pipeline is included yet.

## Real Providers (LLM / STT / TTS)

Beyond the mocks, `knock.providers` now includes adapters for locally-hosted services:

| Capability | Provider | Protocol | Module |
|---|---|---|---|
| LLM | [Ollama](https://ollama.com) | HTTP REST | `knock.providers.llm.ollama.OllamaProvider` |
| STT | Whisper (e.g. `wyoming-faster-whisper`) | [Wyoming](https://github.com/OHF-Voice/wyoming) | `knock.providers.stt.whisper.WhisperSTTProvider` |
| TTS | Kokoro (Wyoming-wrapped) | [Wyoming](https://github.com/OHF-Voice/wyoming) | `knock.providers.tts.kokoro.KokoroTTSProvider` |

Whisper and TTS run over the Wyoming protocol (the same one used by Home Assistant's local voice pipeline), so these adapters assume a Wyoming TCP server is already running — they don't start one.

Each adapter takes a small config object with localhost defaults, overridable via env vars:

| Var | Default | Notes |
|---|---|---|
| `KNOCK_OLLAMA_HOST` / `_PORT` / `_MODEL` / `_TIMEOUT` | `127.0.0.1` / `11434` / `llama3.2` / `30.0` | Ollama REST API |
| `KNOCK_WHISPER_HOST` / `_PORT` / `_TIMEOUT` | `127.0.0.1` / `10300` / `10.0` | Wyoming STT server |
| `KNOCK_KOKORO_HOST` / `_PORT` / `_VOICE` / `_TIMEOUT` | `127.0.0.1` / `10200` / unset / `10.0` | Wyoming TTS server |

With those services running locally, sanity-check connectivity by hand (this script isn't part of CI, since it needs real services up):

```bash
python scripts/smoke_test_providers.py
# or test a subset:
python scripts/smoke_test_providers.py --skip stt,tts
```

## ⚠ Hardware & Integration Status

Hardware and third-party integrations are **not implemented yet**.

Files under `knock.integrations` are placeholders only for future work:
- UniFi
- Frigate
- Home Assistant
- MQTT bridge behavior

Vision provider is mock-only for now, same as the integrations above.

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

The CLI holds one session across turns (state persists to disk between
messages) until you send a blank line or `quit`:

```
KNOCK CLI -- session 3f9c... (blank line or 'quit' to exit)
Visitor: Hi I have an Amazon package
Response: Thanks. You can leave the package by the door.
Visitor: quit
Session saved: 3f9c... (1 turn(s))
```

### 4) Run API locally

```bash
uvicorn knock.api.app:app --reload
```

Test endpoint — the response carries an `X-Session-Id` header; pass it back
as a `session_id` query param on later calls to continue the same
conversation (state is persisted to disk between requests):

```bash
curl -i -X POST http://127.0.0.1:8000/respond \
  -H "Content-Type: application/json" \
  -d '{"source":"doorbell","text":"Hi I have a package","timestamp":"2026-01-01T12:00:00Z"}'

curl -X GET http://127.0.0.1:8000/sessions/<session-id-from-above>
```

Session files live under `~/.local/share/knock/sessions` by default,
overridable via `KNOCK_SESSION_DIR`.

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
2. ~~real provider adapters behind existing interfaces~~ — done for LLM (Ollama) / STT (Whisper) / TTS (Kokoro), see [Real Providers](#real-providers-llm--stt--tts) above. Vision and `knock.integrations` adapters are still pending.
3. hardware input/output bridges
4. ~~session persistence~~ and event replay — state now persists to disk and is threaded through the API/CLI (see Quickstart above); event replay is still pending

## Brand Assets

The mark and wordmark live under `docs/assets/logo/` as plain SVG (light/dark
variants, plus a self-contained favicon tile tuned for 16px tab icons). The
GitHub Pages favicon (`docs/favicon.ico`) is generated from
`docs/assets/logo/favicon.svg`.

## License

MIT
