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
- `knock.core`: events, state, orchestrator, response models, session store, audit log
- `knock.conversation`: policy (data-driven rule set), intent, response templates
- `knock.providers`: provider interfaces, mock implementations, and real adapters
- `knock.api`: FastAPI surface (`POST /respond`, `GET /sessions/{id}`)
- `knock.cli`: local CLI harness (`python -m knock`)

## Safety Policy & Audit Trail

`PolicyEngine` no longer hardcodes keyword lists in Python -- it loads a data-defined rule set (`knock.conversation.rules.json` by default) and scores each matching rule's `weight` into a confidence total per flag (`occupancy`, `schedule`, `unlock`, `emergency`). A flag only becomes actionable once its confidence crosses the rule set's `threshold`, so multiple weak, related signals can combine into a real block/escalate decision instead of every rule needing to fire alone. Point `PolicyEngine` at your own rules with `PolicyEngine(rule_set=RuleSet.load("path/to/rules.json"))`.

Every `Orchestrator.respond()` call now also records an `AuditEntry` (timestamp, visitor text, matched flags/confidence, matched rule ids, allowed/reason/intent) to a local-only, append-only JSON-lines file -- nothing in KNOCK transmits this anywhere. The API and CLI both write to `~/.local/share/knock/audit.jsonl` by default, overridable via `KNOCK_AUDIT_LOG`; direct/library use of `Orchestrator()` stays audit-free by default (`NullAuditLog`) unless you pass `audit_log=JSONLAuditLog(...)` explicitly.

## Local-First / Privacy-First Notes

- No *cloud* AI API calls — real providers only talk to services on your own network.
- Mock provider behavior remains deterministic and fully offline (no network calls at all).
- Real providers are opt-in: the orchestrator still uses the mock/canned-response path by default, and nothing in `knock.core`/`knock.conversation` requires a provider to be configured.
- No vision model pipeline is included yet.

## Real Providers (LLM / STT / TTS / Vision)

Beyond the mocks, `knock.providers` now includes adapters for locally-hosted services:

| Capability | Provider | Protocol | Module |
|---|---|---|---|
| LLM | [Ollama](https://ollama.com) | HTTP REST | `knock.providers.llm.ollama.OllamaProvider` |
| STT | Whisper (e.g. `wyoming-faster-whisper`) | [Wyoming](https://github.com/OHF-Voice/wyoming) | `knock.providers.stt.whisper.WhisperSTTProvider` |
| TTS | Kokoro (Wyoming-wrapped) | [Wyoming](https://github.com/OHF-Voice/wyoming) | `knock.providers.tts.kokoro.KokoroTTSProvider` |
| Vision | A vision-capable Ollama model (default `moondream`) | HTTP REST | `knock.providers.vision.ollama.OllamaVisionProvider` |

Whisper and TTS run over the Wyoming protocol (the same one used by Home Assistant's local voice pipeline), so these adapters assume a Wyoming TCP server is already running — they don't start one. Vision reuses the same Ollama server as the LLM (just point it at a vision-capable model pulled into Ollama, e.g. `ollama pull moondream`, `llava`, or `qwen2.5vl` for better accuracy at the cost of latency); it's not wired into the orchestrator's decision flow by default -- the Frigate/UniFi integrations call it directly to enrich a detected event with a short description.

Each adapter takes a small config object with localhost defaults, overridable via env vars:

| Var | Default | Notes |
|---|---|---|
| `KNOCK_OLLAMA_HOST` / `_PORT` / `_MODEL` / `_TIMEOUT` | `127.0.0.1` / `11434` / `llama3.2` / `30.0` | Ollama REST API |
| `KNOCK_WHISPER_HOST` / `_PORT` / `_TIMEOUT` | `127.0.0.1` / `10300` / `10.0` | Wyoming STT server |
| `KNOCK_KOKORO_HOST` / `_PORT` / `_VOICE` / `_TIMEOUT` | `127.0.0.1` / `10200` / unset / `10.0` | Wyoming TTS server |
| `KNOCK_VISION_HOST` / `_PORT` / `_MODEL` / `_TIMEOUT` / `_PROMPT` | `127.0.0.1` / `11434` / `moondream` / `30.0` / (see `config.py`) | Vision-capable Ollama model |

With those services running locally, sanity-check connectivity by hand (this script isn't part of CI, since it needs real services up):

```bash
python scripts/smoke_test_providers.py
# or test a subset:
python scripts/smoke_test_providers.py --skip stt,tts
```

## ⚠ Hardware & Integration Status

Still placeholders:
- UniFi Protect (direct integration)
- Home Assistant (direct integration)

**MQTT and Frigate are both real.**

### MQTT

`knock.integrations.mqtt.MqttBridge` -- the bridge that doesn't need a vendor-specific client, since Home Assistant, Frigate, and most doorbell/camera hardware already speak MQTT. Run it standalone:

```bash
knock-mqtt-bridge
```

It subscribes to `KNOCK_MQTT_TOPIC_IN` (default `knock/events`) for JSON payloads:

```json
{"text": "Hi I have a package", "source": "front-doorbell"}
```

`text` is the only required field; `source`, `timestamp`, and `session_id` all have sensible defaults (repeated messages from the same `source` continue one session automatically). The resulting response is published as JSON to `KNOCK_MQTT_TOPIC_OUT` (default `knock/responses`). Connection settings (`KNOCK_MQTT_HOST`/`_PORT`/`_CLIENT_ID`/`_USERNAME`/`_PASSWORD`/`_KEEPALIVE`) follow the same env-var pattern as the LLM/STT/TTS/Vision providers above.

### Frigate

`knock.integrations.frigate.FrigateBridge` consumes [Frigate](https://frigate.video)'s own MQTT event stream -- since Frigate already runs its own object detector, KNOCK gets person/vehicle/package labels for free, no separate vision model required for this path (though one can still be plugged in for a richer description, see below). Run it standalone:

```bash
knock-frigate-bridge
```

It subscribes to `<KNOCK_FRIGATE_TOPIC_PREFIX>/events` (default `frigate/events`) and only reacts when a tracked object of a configured label (`KNOCK_FRIGATE_TRIGGER_LABELS`, default `person`) **actually enters a defined zone** (Frigate's `entered_zones`, optionally narrowed further via `KNOCK_FRIGATE_ZONES`) -- not just anything visible in frame, so someone passing by on the sidewalk doesn't trigger a response. Pass a `vision_provider` (e.g. `OllamaVisionProvider`) when constructing `FrigateBridge` yourself to have it fetch the event's snapshot from Frigate's HTTP API and fold a short description into the response; this is optional enrichment -- a vision failure is logged and the plain detection still goes through. Connection settings (`KNOCK_FRIGATE_MQTT_HOST`/`_PORT`/`_TOPIC_PREFIX`/`_TOPIC_OUT`/`_HTTP_HOST`/`_HTTP_PORT`/`_TRIGGER_LABELS`/`_ZONES`/`_CLIENT_ID`/`_USERNAME`/`_PASSWORD`/`_KEEPALIVE`) follow the same env-var pattern as everything else.

## Quickstart

### 1) Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

### 2) Run tests

```bash
pytest                                        # tests only
pytest --cov=knock --cov-report=term-missing  # with coverage (CI enforces 80% minimum)
ruff check . && ruff format --check .         # lint + format
mypy src tests                                # type checking
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

CI builds this image on every push/PR (separately from the lint/type/test job) so a broken `Dockerfile` or missing packaged file surfaces immediately.

## Future Roadmap

See:
- `docs/architecture.md`
- `docs/roadmap.md`
- `docs/hardware.md`

Near-term priorities:
1. ~~richer safety policy and auditing~~ — done: data-driven rule set with confidence scoring + a local audit trail, see [Safety Policy & Audit Trail](#safety-policy--audit-trail) above
2. ~~real provider adapters behind existing interfaces~~ — done for LLM (Ollama) / STT (Whisper) / TTS (Kokoro) / Vision (Ollama), see [Real Providers](#real-providers-llm--stt--tts--vision) above. `knock.integrations` adapters (UniFi/Frigate/Home Assistant) are still pending.
3. ~~hardware input/output bridges~~ — done for MQTT and Frigate (`knock-mqtt-bridge`, `knock-frigate-bridge`, see [Hardware & Integration Status](#-hardware--integration-status) above); UniFi Protect and Home Assistant direct integrations are still pending
4. ~~session persistence~~ and event replay — state now persists to disk and is threaded through the API/CLI (see Quickstart above); event replay is still pending
5. ~~test/tooling hardening~~ — done: FastAPI/CLI test coverage, mypy, an 80% coverage floor, and a CI job that builds the Docker image, all enforced in CI

## Brand Assets

The mark and wordmark live under `docs/assets/logo/` as plain SVG (light/dark
variants, plus a self-contained favicon tile tuned for 16px tab icons). The
GitHub Pages favicon (`docs/favicon.ico`) is generated from
`docs/assets/logo/favicon.svg`.

## License

MIT
