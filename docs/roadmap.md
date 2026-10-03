# KNOCK Roadmap

# What is KNOCK?

KNOCK is a local-first AI door answering platform.

The long-term goal is to create a system that can:
- answer a doorbell,
- talk to visitors,
- safely handle simple interactions,
- protect privacy,
- and reduce unnecessary interruptions.

KNOCK is designed to work locally whenever possible instead of depending entirely on cloud services.

---

# Core Philosophy

KNOCK is being built around several important ideas.

## 1. Local-first

KNOCK should continue working even if the internet is down.

This means:
- local AI support,
- local processing,
- local storage,
- and local integrations.

Cloud services may be optional later, but they should not be required for basic operation.

---

## 2. Safety-first

KNOCK should never:
- reveal whether someone is home,
- reveal schedules,
- unlock doors,
- expose private information,
- or make unsafe assumptions.

The system should prefer safe boring responses over clever risky responses.

---

## 3. Hardware agnostic

Hardware agnostic means:
"not tied to one brand or device"

KNOCK should work with:
- UniFi Protect,
- Frigate,
- MQTT,
- Home Assistant,
- ONVIF cameras,
- Reolink,
- Amcrest,
- ESPHome,
- and future systems.

The core conversation system should not care which camera or doorbell is connected.

---

## 4. Modular design

Modular means:
"separate pieces that can be replaced independently"

Examples:
- swap Ollama for another LLM,
- swap Whisper for another speech-to-text engine,
- replace MQTT with another event system,
- replace TTS engines without rewriting the whole application.

---

# Development Phases

KNOCK is being built in stages.

The goal is to build a stable foundation first before adding advanced AI features.

---

# Phase 1 — Foundation

## Goal

Build a clean, testable software foundation.

This phase focuses on architecture and development workflow instead of advanced AI.

## Features

- Clean repository structure
- Python project setup
- Docker setup
- GitHub Actions
- Unit testing
- Event schemas
- Response schemas
- Session state models
- Provider interfaces

## Definitions

### Schema

A schema is a structured definition for data.

Example:
A visitor event schema defines:
- visitor text,
- timestamps,
- source device,
- and metadata.

### Provider

A provider is a plugin-like component that performs a task.

Examples:
- LLM provider
- TTS provider
- STT provider
- Vision provider

---

# Phase 2 — Local Conversation Harness

## Goal

Create a text-only local testing environment.

This allows development without:
- cameras,
- microphones,
- GPUs,
- MQTT,
- or Home Assistant.

## Features

- CLI testing mode
- REST API
- Mock providers
- JSON responses
- Conversation testing

## Definitions

### CLI

CLI stands for:
Command Line Interface.

This means interacting with the program through the terminal.

Example:

```bash
python -m knock
```

### REST API

A REST API allows software to communicate over HTTP.

Example:

```http
POST /respond
```

---

# Phase 3 — Policy and Safety Engine

## Goal

Create deterministic safety behavior.

Deterministic means:
"the same input always produces the same result"

The policy engine acts as the system's safety layer.

## Features

- Occupancy protection
- Schedule protection
- Emergency escalation
- Unsafe response filtering
- Fallback responses
- Confidence checks

## Definitions

### Confidence

Confidence is how certain the AI is about a decision.

Example:
- 95% confident = likely safe to auto respond
- 40% confident = ask for clarification or escalate

### Escalation

Escalation means handing control to a human.

Example:
- emergency detected,
- suspicious visitor,
- repeated failed understanding.

---

# Phase 4 — Session and State Management ✅ Done

## Goal

Track conversations across multiple messages.

KNOCK should remember ongoing interactions instead of treating every message as unrelated.

## Features

- Session IDs
- Multi-turn conversations
- Timeout handling
- Cooldown handling
- Context tracking

## Definitions

### Session

A session represents one active visitor interaction.

Example:
A delivery driver speaking three times should still belong to one conversation session.

### Cooldown handling

Distinct from timeout handling (below): timeout is about how long a visit
can go quiet before it's considered over; cooldown is about debouncing an
impatient or accidental double-press of the doorbell button within the
same visit. Without it, a second `ring` moments after the first forced a
full session restart, re-playing the entire greeting mid-conversation. A
`ring` within `UnifiConfig.ring_cooldown_seconds` (default 15s) of the
session's last turn continues the existing session instead; one after the
cooldown but still short of the full idle timeout still restarts, since
that's plausibly a different visitor.

### Context

Context means information remembered during the conversation.

Example:
- previous responses,
- detected package,
- previous clarification question.

---

# Phase 5 — Provider System

## Goal

Create interchangeable AI and hardware providers.

## Planned providers

### LLM Providers

LLM stands for:
Large Language Model.

Examples:
- Ollama
- OpenAI-compatible APIs
- local models

### STT Providers

STT stands for:
Speech-to-Text.

This converts spoken audio into text.

Examples:
- Whisper
- Faster-Whisper

### TTS Providers

TTS stands for:
Text-to-Speech.

This converts text into spoken audio.

Examples:
- Piper
- Kokoro
- XTTS

### Vision Providers

Vision providers analyze images or video.

Examples:
- package detection,
- person detection,
- visitor description.

---

# Phase 6 — Integration Layer

## Goal

Connect KNOCK to external systems.

## Planned integrations

- MQTT
- Home Assistant
- Frigate
- UniFi Protect
- ONVIF cameras
- ESPHome ✅ Done (via MQTT)
- SIP / VoIP

## Definitions

### MQTT

MQTT is a lightweight messaging system commonly used in smart home environments.

### ESPHome

ESPHome devices speak MQTT natively, so a DIY doorbell needs no
KNOCK-specific code -- just an `mqtt.publish` action wired to the button
press, publishing straight to `KNOCK_MQTT_TOPIC_IN` in the same JSON shape
the MQTT bridge already expects. See the README's MQTT section for a
working example config. A dedicated bridge using ESPHome's own native API
(`aioesphomeapi`) instead of an MQTT broker would be lower-latency, but
isn't built -- flagged here as a possible future refinement, not a gap in
basic support.

### Frigate

Frigate is an open-source AI camera system.

### Home Assistant

Home Assistant is a local smart home automation platform.

### ONVIF

ONVIF is a common camera communication standard. A single ONVIF-generic
bridge can cover a wide range of budget/no-name doorbells at once, the same
way Reolink and Amcrest also expose ONVIF alongside their own local APIs.

### SIP / VoIP

SIP matters for two different reasons here, worth keeping separate:

- **As a bridge, like UniFi/ONVIF.** A lot of apartment/commercial door
  entry hardware (2N, Akuvox, Fermax, Doorbird) is SIP-based rather than a
  consumer IP camera -- a SIP bridge would let KNOCK answer those systems
  too, which matters for the long-term goal's churches/offices/community
  centers beyond a single-family home.
- **As a transport for live intercom's human handoff.** Instead of (or
  alongside) a push notification needing the web app open, KNOCK could
  place an actual SIP/VoIP call to a real phone number during the "hold
  on, let me get someone" fallback (see Phase 7's live intercom mode) --
  so answering doesn't require the app, just picking up a ringing call.

  No separate SIP trunk/provider account is strictly needed if there's
  already a home VoIP box in the house (e.g. Ooma): its base station
  outputs a standard analog phone line on an RJ11 jack, same as an old
  landline. An ATA (Analog Telephone Adapter -- e.g. a Grandstream
  HT801/HT802) wired into that output turns the existing line into a SIP
  endpoint KNOCK's own code can place/answer calls on and read DTMF from,
  the same way a cordless phone or answering machine already can. The
  provider's own cloud/app isn't part of this at all -- it's just the
  phone line underneath, same as any other phone plugged into that jack.

### Battery-powered doorbells

Battery doorbells (most Ring, Blink, Arlo, and Eufy/Reolink's battery
lines) sleep almost all the time and only wake briefly on motion or a
button press, then upload a clip and sleep again. That's a fundamentally
different shape than the always-on, continuously reachable PoE/wired
doorbells KNOCK integrates with today, and it correlates with which
vendors expose a local API at all: wired doorbells tend to have one
(UniFi, ONVIF, Reolink/Amcrest, Hikvision ISAPI), while battery doorbells
tend to be cloud-first by design. Eufy is the notable exception, with a
mature community-run local protocol (`eufy-security-ws`).

See Phase 7's notes -- the short wake window also conflicts with KNOCK's
multi-turn conversation design, independent of which battery brand is used.

### Per-camera trigger selection ✅ Done

`UnifiConfig.trigger_camera_ids` scopes which camera(s) actually start a
conversation -- empty (the default) means every camera on the console,
matching the original behavior. Settings > UniFi Protect renders a real
camera picker (checkboxes with each camera's actual name, pulled live from
the console) rather than a raw device-id text field.

---

# Phase 7 — Audio Pipeline

## Goal

Add real voice interaction.

## Features

- Speech recognition
- Voice responses
- Doorbell talkback
- Multi-voice support
- Streaming audio
- Live intercom mode

## Definitions

### Live intercom mode

A human-operated alternative to KNOCK's AI-mediated conversation: let a
person talk through the doorbell's speaker and hear its microphone live,
from the web UI, the way UniFi Protect's own app already does -- without
needing a separate app. This bypasses the orchestrator/LLM entirely; it's
raw two-way audio passthrough, not a conversation KNOCK is phrasing.

Two concrete ways this plugs into what's already built:

- **Reachable from a notification.** The signature-required/food-delivery
  notifications already carry action buttons ("I'm on my way" / "Turn them
  away") that embed the camera's device id in the action identifier itself
  (see `HomeAssistantActionListener`/`ACTION_DEVICE_ID_SEP`). A "Talk now"
  action is the same mechanism pointed at a live session instead of a
  canned phrase -- tap the notification, get a live connection to that
  camera immediately.
- **KNOCK's main fallback for "I don't know what to do."** Today an intent
  the LLM can't confidently resolve just gets a canned brush-off ("Sorry, I
  can't help with that right now"). A much better fallback is KNOCK telling
  the visitor "Hold on, let me get someone," firing a notification with a
  "Talk now" action, and handing the actual conversation to a human in real
  time -- rather than leaving the visitor stuck with a dead-end line. This
  is probably the strongest argument for building this sooner rather than
  later: it upgrades every current "I can't help with that" moment into a
  real human handoff, not just a nicety on top of the AI path.

Technically this is a different problem than the request-response
talkback already built (`speak_to_visitor`/`_capture_rtsp_audio`): those
are batch capture-then-respond, not a continuous live stream. Live audio
in a browser means real-time streaming (WebRTC or similar) in both
directions, which is the audio counterpart to the live video preview
already flagged as future work -- true WebRTC/HLS restreaming was
explicitly called out as out of scope for the first version of that.

SIP (Phase 6) is the other transport option for this same handoff -- a
real phone call instead of a web UI session, so answering doesn't need
the app open. The two aren't exclusive; which one a given household wants
is a settings choice, not an either/or architecture decision.

## Notes

This phase happens after the text system is stable.

Voice systems add significant complexity.

A battery-powered doorbell's short wake window may not fit KNOCK's
multi-turn conversation loop (greet, listen, think, speak, listen again).
Supporting one well may need a separate, single-exchange interaction mode
-- capture one message and respond once, rather than a live back-and-forth
-- instead of forcing the existing conversational loop onto a device that
can't stay awake for it.

---

# Phase 8 — Vision System

## Goal

Add optional image and video understanding.

## Possible features

- Package detection
- Visitor descriptions
- Vehicle detection
- Safety analysis
- Event summaries
- Known visitor recognition

## Important

Vision processing is optional.

KNOCK should still function without GPUs or advanced vision models.

## Definitions

### Known visitor recognition

A step beyond generic visitor descriptions: recognize a specific,
previously-tagged person and either skip the interaction entirely
(household members, trusted regulars) or greet them by name instead of
running the normal classify-and-respond pipeline. This needs actual face
recognition, not just scene description -- a different vision capability
than today's generic describe-the-image providers.

See Phase 9's "Visitor profiles" for the policy side (who gets ignored,
who gets greeted, managed from Settings). Worth flagging early: this means
storing biometric data (faces) locally, which needs the same
written-with-care posture secrets already get in the config store, not
just another settings field.

---

# Phase 9 — Advanced Features

## Possible future features

- Multi-node inference
- Distributed AI workers
- Memory systems
- Visitor profiles
- Smart routing
- Multi-door support
- Church/business concierge mode
- Accessibility support
- Bridge plugin architecture
- Model distillation / LoRA training workflow
- Installable web app (PWA / "Chrome app")
- Self-hosted OIDC sign-in (Authentik, etc.)

## Definitions

### Self-hosted OIDC sign-in (Authentik, etc.)

KNOCK's existing OAuth sign-in (`oauth_routes.py`) is scoped to Google
specifically, since Google's OpenID Connect discovery document
(`server_metadata_url`) lets Authlib auto-configure the
authorization/token/JWKS endpoints for free -- the code's own docstring
calls out a second, non-OIDC provider as real, separate work (hand
specifying every endpoint).

Authentik turns out to be the easy case, not the hard one: it's fully
OIDC-compliant with its own discovery document, so a self-hosted Authentik
instance should fit the same `server_metadata_url` pattern already built
for Google, not the "hand-specify everything" work a non-OIDC provider
like plain GitHub OAuth2 would need. It also fits KNOCK's local-first
stance better than Google does -- sign in through a self-hosted identity
provider instead of a cloud one.

### Installable web app ✅ Done

A hand-written manifest (`web/public/manifest.webmanifest`) and a
deliberately no-op service worker (`web/public/sw.js` -- satisfies the
"has a fetch handler" installability requirement, does no caching at all,
since this is a security-sensitive admin app where serving stale
authenticated content would be a real risk) let the web UI install like a
native app in Chrome and other browsers, no separate extension needed.
Icons generated from the existing brand mark at the required 192/512
sizes, both standard and maskable variants. Requires a secure context
(HTTPS or localhost), same constraint WebAuthn already has.

Still valuable to pair with later: live intercom mode and push
notifications, so answering the door from a phone feels like using an
app, not a website.

### Visitor profiles

The policy side of Phase 8's "known visitor recognition": once a person
can be recognized, something needs to decide what happens next -- ignore
them entirely, greet them by name, or treat them normally. Managed from
Settings as a list of tagged people, each with an ignore-or-greet choice.

### Bridge plugin architecture

Today every bridge (MQTT, Frigate, Home Assistant, UniFi Protect) is
hardcoded into the process supervisor and the settings system. A plugin
architecture would let a third-party package register its own bridge
through Python's standard "entry points" mechanism instead, so installing
a package is enough for it to show up in the Processes and Settings pages
automatically -- no code changes to KNOCK itself.

This matters most for hardware KNOCK doesn't support out of the box yet:
ONVIF-generic, Reolink, Amcrest, battery-powered doorbells, and anything
built on ESPHome.

This is explicitly a later-version item (not part of the current
foundation work) and needs a deliberate decision on the install/trust
model before it ships -- a plugin is arbitrary third-party code running
with access to camera credentials and Home Assistant tokens.

### Model distillation / LoRA training workflow

KNOCK already records every interaction in its audit log. A future
workflow would let a person review real interactions, confirm or correct
the intent and response, and use the approved set to fine-tune a small,
purpose-built model with LoRA -- instead of relying entirely on prompt
engineering against a general-purpose model.

This is lower priority while KNOCK's intents and response style are still
actively changing: prompt changes ship in minutes, but each new intent
under a distilled model would need new training examples and a re-tune.
Worth revisiting once the intent taxonomy and response style stabilize.

---

# Long-Term Goal

The long-term goal is not just:

"AI doorbell"

The long-term goal is:

A safe, local-first conversational interface between people and physical spaces.

That could eventually include:
- homes,
- churches,
- offices,
- makerspaces,
- community centers,
- and accessibility-focused environments.

---

# Current Priority

Phases 1-5 are done: clean architecture, the local text harness, the
deterministic safety engine, session/state management, and the full
provider system -- all with real test coverage, not just scaffolding.

Phase 6 (Integration Layer) and Phase 7 (Audio Pipeline) are mostly done
and proven against real hardware: MQTT, Home Assistant, Frigate, and
UniFi Protect bridges, live two-way talkback, speech recognition, and the
web UI (settings, process supervision, debug tools, Google/Authentik
sign-in) all actually work today, not just on paper.

What's left is genuinely the "advanced features" tier now, not
foundation-building:

- Phase 6 gaps: ONVIF, SIP/VoIP (per-camera trigger selection and basic
  ESPHome support are both done now)
- Phase 7 gaps: live intercom mode, real multi-voice switching
- Phase 8 gaps: known visitor recognition, structured vision detection
  (today's vision is one generic description, not separate categories)
- Phase 9: everything except self-hosted OIDC sign-in (done)

The project has moved past "stabilize the foundation" into "pick which
advanced feature earns its slot next" -- see each phase's Definitions for
the reasoning behind what's still open and why.
