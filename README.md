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

**Training data export:** the web UI's **Training** page turns that audit log into fine-tuning material -- page through real interactions, approve or correct each one's intent/response, then download an instruction-tuning JSONL file (`GET /api/training/export`) built from the exact prompts `Orchestrator` sends the LLM in production. Review decisions live in a separate small file (`~/.local/share/knock/training_reviews.json`, overridable via `KNOCK_TRAINING_REVIEWS`), keyed by a content hash so it works on entries recorded before this feature existed. KNOCK only produces the file -- actually running a LoRA fine-tune against it is a separate, external step on your own hardware. See `scripts/generate_scenarios.py`/`scripts/generate_training_data.py` for generating synthetic review material locally with one or more Ollama models.

Set `KNOCK_TRAINING_MODE=1` to collapse the web UI down to just the Training page (nav, and every other route redirects to `/training`) -- meant for a single-purpose machine (e.g. a scratch box only ever used to review synthetic training data) where the rest of KNOCK's UI is just noise. **This also skips login entirely** -- `require_auth` short-circuits to a fixed `training-mode` user with no session/cookie needed at all, so there's no setup/login screen standing between the one person who can already reach the machine and the app. This is a real, deliberate removal of authentication, not just a UI simplification: **never set this env var on anything network-exposed** -- anyone who can reach the port can use the whole app, including the script runner below. In this mode the Training page also gets a small panel to run `generate_scenarios.py`/`generate_training_data.py` directly from the browser (live status + log tail) instead of a terminal -- only one script runs at a time, and it assumes the process's cwd is the repo root (true for both the Docker image and the documented local dev workflow), since `scripts/` isn't installed as part of the Python package.

**Self-introduction:** the first response of a new session (`SessionState.turn_count == 0`) is prefixed with a one-time greeting (`knock.conversation.prompts.GREETING`) that identifies KNOCK as an AI system and flags that it can get details wrong -- so a visitor knows what they're talking to before anything else is said. Later turns in the same session skip it. Emergency escalations never get it (immediacy matters more there than a self-introduction); calling `Orchestrator()` with no session state (stateless/library use) never greets either, since there's no session to track a "first turn" against.

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

**Vision safety:** the default prompt explicitly asks the model to describe only what's literally visible and never speculate about danger, intent, or weapons -- but that's an instruction a model can ignore, not a guarantee. Every description from `OllamaVisionProvider` also passes through `knock.providers.vision.safety.sanitize_description`, a deterministic keyword backstop that swaps anything mentioning weapons/explosives/threats for a generic, safe fallback sentence before it can reach a response. It's deliberately over-inclusive (false positives suppress benign descriptions sometimes) -- per the project's safety-first stance, a boring fallback beats a speculative, alarming one. This does not replace real threat detection; it only keeps a vision model's unreliable self-generated language from being spoken or displayed verbatim.

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

**All four integrations are real: MQTT, Frigate, Home Assistant, and UniFi Protect.** Vision provider too (see [Real Providers](#real-providers-llm--stt--tts--vision) above). What's left: ONVIF/Reolink/Amcrest/ESPHome and the rest of `docs/roadmap.md`'s long-term integration list.

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

#### A DIY doorbell with ESPHome

[ESPHome](https://esphome.io) devices already speak MQTT natively, so a DIY doorbell button needs no KNOCK-specific code -- just an `mqtt.publish` action wired to whatever triggers the press, publishing the exact JSON shape above to `KNOCK_MQTT_TOPIC_IN`:

```yaml
mqtt:
  broker: 192.168.1.50   # same host as KNOCK_MQTT_HOST

binary_sensor:
  - platform: gpio
    pin: GPIO4
    name: "Doorbell Button"
    on_press:
      then:
        - mqtt.publish:
            topic: knock/events
            payload: !lambda |-
              return "{\"text\": \"Doorbell button pressed\", \"source\": \"front-doorbell\"}";
```

This is intentionally the extent of ESPHome "integration": a dedicated bridge using ESPHome's own native API (`aioesphomeapi`) instead of going through an MQTT broker would be lower-latency, but isn't built -- MQTT already gets a DIY doorbell talking to KNOCK today.

### Frigate

`knock.integrations.frigate.FrigateBridge` consumes [Frigate](https://frigate.video)'s own MQTT event stream -- since Frigate already runs its own object detector, KNOCK gets person/vehicle/package labels for free, no separate vision model required for this path (though one can still be plugged in for a richer description, see below). Run it standalone:

```bash
knock-frigate-bridge
```

It subscribes to `<KNOCK_FRIGATE_TOPIC_PREFIX>/events` (default `frigate/events`) and only reacts when a tracked object of a configured label (`KNOCK_FRIGATE_TRIGGER_LABELS`, default `person`) **actually enters a defined zone** (Frigate's `entered_zones`, optionally narrowed further via `KNOCK_FRIGATE_ZONES`) -- not just anything visible in frame, so someone passing by on the sidewalk doesn't trigger a response. Pass a `vision_provider` (e.g. `OllamaVisionProvider`) when constructing `FrigateBridge` yourself to have it fetch the event's snapshot from Frigate's HTTP API and fold a short description into the response; this is optional enrichment -- a vision failure is logged and the plain detection still goes through. Connection settings (`KNOCK_FRIGATE_MQTT_HOST`/`_PORT`/`_TOPIC_PREFIX`/`_TOPIC_OUT`/`_HTTP_HOST`/`_HTTP_PORT`/`_TRIGGER_LABELS`/`_ZONES`/`_CLIENT_ID`/`_USERNAME`/`_PASSWORD`/`_KEEPALIVE`) follow the same env-var pattern as everything else.

### Home Assistant

`knock.integrations.homeassistant.HomeAssistantBridge` connects directly to Home Assistant's WebSocket API (not just MQTT), so it can both react to a trigger entity and call a service back afterward. Authenticate with a [Long-Lived Access Token](https://www.home-assistant.io/docs/authentication/) created in your HA user profile. Run it standalone:

```bash
knock-ha-bridge
```

It subscribes to `state_changed` events and reacts whenever `KNOCK_HA_TRIGGER_ENTITY_ID` (default `binary_sensor.front_doorbell`) genuinely changes state -- any transition, not specifically "became on", since a growing number of HA doorbell buttons are modeled as an `event` entity whose state is a changing timestamp rather than an on/off value. `unknown`/`unavailable` placeholder states (not yet initialized, or the device dropped offline) are never treated as a trigger. If `KNOCK_HA_NOTIFY_SERVICE` is set (e.g. `notify.mobile_app_pixel`), the response text is sent through that HA service afterward via `POST /api/services/<domain>/<service>`.

**Safety note:** this bridge only ever calls whatever service *you* configure via `KNOCK_HA_NOTIFY_SERVICE` -- it ships with no default that unlocks, arms, or disarms anything. That's entirely your own Home Assistant configuration choice. Connection settings (`KNOCK_HA_BASE_URL`/`_TOKEN`/`_TRIGGER_ENTITY_ID`/`_NOTIFY_SERVICE`/`_VERIFY_SSL`) follow the same env-var pattern as everything else; keep HA on plain `http://` on your LAN unless you've got a real (non-self-signed) cert, to avoid TLS verification headaches.

**Different tones per event:** every notification carries one of three categories -- `emergency` (escalation), `approval` (a delivery/appointment with a decision button), or `fyi` (everything else) -- as an Android `channel` (`knock_emergency`/`knock_approval`/`knock_fyi`) and an iOS `interruption-level` (`critical`/`time-sensitive`/`passive`). iOS's `critical` level needs the Home Assistant app's one-time "Critical Notifications" permission granted on your phone, or it's silently treated as normal. Android can't have its sound set programmatically at all -- the first notification of each category creates that channel, then you assign its sound once yourself under Settings > Apps > Home Assistant > Notifications > (channel name).

### UniFi Protect

`knock.integrations.unifi.UnifiBridge` connects to a local UniFi OS console via [`uiprotect`](https://github.com/uilibs/uiprotect)'s realtime event websocket. Create an API key for a **local console user** -- cloud SSO/MFA accounts aren't supported by the underlying library, so a local-only account is required (which fits KNOCK's local-first stance anyway). Run it standalone:

```bash
knock-unifi-bridge
```

It reacts to a doorbell `ring` by default (`KNOCK_UNIFI_TRIGGER_ON`); add smart-detect object types (e.g. `person`, `package`) to also react to those. UniFi itself has no speech-to-text, so the event text is a generic trigger description -- real visitor speech is a future audio-pipeline concern. Pass a `vision_provider` when constructing `UnifiBridge` yourself to have it fetch the triggering camera's snapshot and fold a short description into the response, same best-effort enrichment pattern as the Frigate bridge. Connection settings (`KNOCK_UNIFI_HOST`/`_PORT`/`_API_KEY`/`_VERIFY_SSL`/`_TRIGGER_ON`) follow the same env-var pattern as everything else.

**Listening (speech-to-text):** pass a `stt_provider` (e.g. `WhisperSTTProvider`) to have KNOCK open the triggering camera's RTSPS stream, capture `KNOCK_UNIFI_LISTEN_SECONDS` (default 10s) of audio from its microphone, and transcribe it -- the real transcript becomes the `VisitorEvent` text instead of the generic "Doorbell ring on ..." placeholder. `KNOCK_UNIFI_RTSP_QUALITY` (default `high`) picks which RTSPS stream quality to pull (UniFi exposes several, including a `package` variant on some doorbells). No stream, no audio track, or any capture/transcription failure just falls back to the generic placeholder text -- it never blocks the response.

**Talkback (two-way audio):** pass a `tts_provider` (e.g. `KokoroTTSProvider`) to have it synthesize the response and stream it out to the triggering camera's speaker, using `uiprotect`'s own `TalkbackStream` (PyAV-based UDP streaming -- already a transitive dependency via `uiprotect`, nothing extra to install). A camera with no speaker, or any streaming failure, is logged and skipped -- talkback is an enhancement on top of the text response, never a requirement for it.

Together, these make the loop genuinely two-way -- KNOCK hears what the visitor actually says and speaks a real response back:

```python
from knock.integrations.unifi import UnifiBridge
from knock.providers.stt.whisper import WhisperSTTProvider
from knock.providers.tts.kokoro import KokoroTTSProvider

bridge = UnifiBridge(
    stt_provider=WhisperSTTProvider(),
    tts_provider=KokoroTTSProvider(),
)
```

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

### 5) Docker

Build locally:

```bash
docker compose -f docker/compose.local.yml up --build
```

Or skip building it yourself entirely -- CI (`.github/workflows/docker-publish.yml`) builds and pushes the image to GitHub Container Registry whenever you push a version tag (`v*.*.*`), or on a manual run (`workflow_dispatch`). A **stable** tag (e.g. `v0.1.0`) is also tagged `latest`; a **prerelease** tag (e.g. `v0.1.0-alpha`) is not, so an alpha/beta/rc build can never silently become what `:latest` resolves to -- pull it by its exact version instead:

```bash
git tag v0.1.0-alpha && git push origin v0.1.0-alpha
# once that run finishes:
docker pull ghcr.io/jumpinjet22/knock:0.1.0-alpha
docker run --rm -p 8000:8000 ghcr.io/jumpinjet22/knock:0.1.0-alpha
```

Pull requests still build the image on every PR (to catch a broken `Dockerfile` or a missing packaged file immediately) but never push it -- only a tag push or a manual run does, since those are the only contexts with a usable registry-write token anyway.

Note: GHCR packages publish as **private** by default on their first push, regardless of the repo's own visibility -- after the workflow runs once, you'll need to flip it to public yourself (package settings on GitHub) if you want `docker pull` to work without authenticating.

### Try the full stack

Want to try KNOCK itself without owning UniFi Protect/Frigate/Home Assistant hardware? `docker/compose.demo.yml` runs a complete, self-contained stack -- KNOCK plus a real Ollama (LLM + vision), Wyoming Whisper (speech-to-text), and Wyoming Kokoro (text-to-speech), all pulled from public images, nothing built from source:

```bash
docker compose -f docker/compose.demo.yml up -d
docker compose -f docker/compose.demo.yml logs -f ollama-pull   # first run only -- downloads the LLM/vision models
```

Open `http://localhost:8080`, finish the first-run admin setup, and head to the Debug page -- the Conversation Simulator exercises the full policy/intent/LLM pipeline by typing what a visitor might say, and the Vision/LLM/STT/TTS panels talk to the real providers above directly. No physical doorbell or camera bridge required. See the comments in the compose file for notes on swapping in a larger Ollama model for better intent-classification results.

## Future Roadmap

See:
- `docs/architecture.md`
- `docs/roadmap.md`
- `docs/hardware.md`

Near-term priorities:
1. ~~richer safety policy and auditing~~ — done: data-driven rule set with confidence scoring + a local audit trail, see [Safety Policy & Audit Trail](#safety-policy--audit-trail) above
2. ~~real provider adapters behind existing interfaces~~ — done for LLM (Ollama) / STT (Whisper) / TTS (Kokoro) / Vision (Ollama), see [Real Providers](#real-providers-llm--stt--tts--vision) above.
3. ~~hardware input/output bridges~~ — done for MQTT, Frigate, Home Assistant, and UniFi Protect (`knock-mqtt-bridge`, `knock-frigate-bridge`, `knock-ha-bridge`, `knock-unifi-bridge`, see [Hardware & Integration Status](#-hardware--integration-status) above)
4. ~~session persistence~~ and event replay — state now persists to disk and is threaded through the API/CLI (see Quickstart above); event replay is still pending
5. ~~test/tooling hardening~~ — done: FastAPI/CLI test coverage, mypy, an 80% coverage floor, and a CI job that builds the Docker image, all enforced in CI

## Web UI

A full web UI (`web/`: React + TypeScript + Vite + Tailwind CSS, served by the existing FastAPI backend) configures every setting -- including API keys/tokens -- from the browser, debugs/tests each provider (especially vision, to directly verify it's working), supervises the bridge processes, and previews live camera snapshots. Built on the brand identity under `docs/assets/logo/` (same wordmark, same `ink`/`dusk`/`steel`/`porch`/`mist` palette, self-hosted Big Shoulders Display/IBM Plex fonts -- no runtime Google Fonts CDN call). FastAPI serves the built `web/dist` directly (`GET /{full_path}` in `api/app.py`) with a real SPA fallback and a path-traversal guard; `docker/Dockerfile` builds the frontend in a Node stage and copies the output into the Python runtime image, and CI builds/lints it on every push/PR (`frontend` job in `test.yml`).

- **Config store** (`knock.core.config_store.ConfigStore`) -- a persisted, UI-editable settings file (`~/.local/share/knock/config.json`, `0600`). Every `*Config` class has a `from_sources(store)` alongside its existing `from_env()`: precedence is **env var > stored setting > hardcoded default**, so nothing already deployed via `.env`/systemd/compose needs to change. Secrets (`MqttConfig.password`, `FrigateConfig.password`, `HomeAssistantConfig.token`, `UnifiConfig.api_key`, OAuth/passkey credentials) are `pydantic.SecretStr`, never echoed back to the client.
- **Auth, three ways** -- password (Argon2id hashing via `knock.core.auth`, server-side revocable sessions, CSRF via `starlette-csrf`), Google OAuth sign-in (bring-your-own client id/secret, single-email allow-list, Authlib), and WebAuthn/passkeys (`py_webauthn` + `@simplewebauthn/browser`, no hand-rolled crypto or COSE parsing -- requires HTTPS or exactly `localhost`/`127.0.0.1`, the UI checks `window.isSecureContext` and says so). `POST /respond` and `GET /sessions/{id}` stay exactly as they are -- open, unauthenticated -- since no secrets flow through them; every other API surface added for the web UI is gated behind a logged-in session.
- **Settings pages** for all eight integration/provider config sections, with live Kokoro voice selection (Wyoming `Describe`/`Info` handshake) instead of a blind text field.
- **Debug/test lab** -- authenticated panels that exercise the real, currently-configured vision/LLM/STT/TTS providers from the browser (vision shows raw vs. safety-filtered output side by side, the direct way to confirm the imaging pipeline is behaving), plus a conversation simulator wrapping `Orchestrator.respond()` directly -- a browser version of the CLI REPL for debugging policy/intent without real hardware.
- **Process supervision** (`knock.core.supervisor.BridgeSupervisor`) -- start/stop/restart each of the four bridge subprocesses from the UI, with a bounded log tail and crash-loop backoff that gives up after repeated failures instead of looping forever. The Dockerfile's `tini` entrypoint plus an API shutdown hook keep `docker stop` from orphaning bridge children.
- **Live video preview** -- snapshot polling behind a per-camera rate-limit floor for both UniFi Protect (reusing the same `get_camera_snapshot()` the bridge already calls on a real event) and Frigate (its own `latest.jpg` endpoint), plus a link out to Frigate's own live-view UI.
- **Audit log and session browser** -- `GET /api/audit` and `GET /api/sessions` surface what `Orchestrator.respond()` and the bridges have been recording all along.

## Brand Assets

The mark and wordmark live under `docs/assets/logo/` as plain SVG (light/dark
variants, plus a self-contained favicon tile tuned for 16px tab icons). The
GitHub Pages favicon (`docs/favicon.ico`) is generated from
`docs/assets/logo/favicon.svg`.

## License

MIT
