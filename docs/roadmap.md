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

**Known gap, found live** (2026-10): "remembered" here means stored in
`SessionState` (`history`, `last_intent`), not actually *used* yet.
`classify_intent()`/`_refine_unknown_intent()` run on each turn's own
text alone -- a clarifying question's follow-up reply is classified in
isolation, with no access to what was asked or what the visitor said
before. Concretely: if turn 1 is ambiguous ("anybody here like dogs?"),
gets a clarifying reply, and the visitor's turn 2 answer confirms a
real intent ("yeah, I'm a dog walker, offering my services"), turn 2
classifies correctly on its own merits -- but that's circumstantial,
not because the system used the conversation so far to interpret it.
A genuinely context-aware classifier (e.g. feeding recent turns into
`_classification_prompt`) is a real architecture change, not yet
started.

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
- ONVIF cameras (doorbells, plus PTZ aiming -- see below)
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
Two separate jobs live here: an ONVIF **bridge** (a camera or doorbell that
starts visits, like UniFi does), and **PTZ aiming** (KNOCK pointing a
pan/tilt/zoom camera at something during a visit).

**What it gets to reuse.** Most of the hard parts already exist and aren't
UniFi-specific:
- `core/conversation_runner.py` -- the whole greet/listen/respond/notify
  loop, driven by hooks (`listen`, `speak`, `capture_scene`, ...) a bridge
  fills in.
- The RTSP(S) audio capture and the remux clip recorder
  (`record_rtsp_clip`) work on any RTSP URL, and ONVIF's `GetStreamUri`
  hands one out.
- The structured camera context (`core/scene.py`) and the package alert
  flow (`integrations/package_alerts.py`).
- Python library: `onvif-zeep-async`, the same one Home Assistant's ONVIF
  integration uses. It covers device/media/events/PTZ services and
  PullPoint event subscriptions.

**Worth flagging before building either part:** every bridge so far talks to
a hub (Protect, Frigate, Home Assistant). ONVIF is the first that logs into
cameras directly, so it stores a credential per camera. Same written-with-
care posture as the other secrets in the config store, and ONVIF users
should be the camera's lowest role that still works (Operator, never
Administrator).

#### ONVIF bridge (doorbells and cameras)

- **Stage 1: notify-only bridge.** Discover the device, subscribe to its
  events (PullPoint), and on a trigger send a snapshot (`GetSnapshotUri`,
  digest auth) plus a vision description as a notification -- the same
  one-shot shape as the Frigate bridge today. Useful on its own.
- **Stage 2: a doorbell-press mapping.** "Button pressed" isn't
  standardized; Reolink, Amcrest, Hikvision and Dahua each name it
  differently. Ship a per-vendor table of event topics, plus a setting to
  name the topic directly for anything not in the table. Without it, stage
  1 only knows about motion, not rings.
- **Stage 3: listening.** RTSP audio into STT through the runner's
  `listen` hook -- existing code, new stream URL.
- **Stage 4: talking back through go2rtc.** ONVIF Profile T's audio
  backchannel is messy to implement directly. go2rtc already speaks it
  (plus several vendors' own two-way audio) and is already the planned
  talkback path for Frigate, so one solution covers both. Its own optional
  container, the same way Ollama is.
- **Stage 5: package camera.** A dual-lens doorbell shows up as a second
  media profile. Package detection itself isn't an ONVIF standard, so it
  comes from the vision model, feeding the existing package alert flow.

#### PTZ aiming: letting KNOCK point a camera

**ONVIF-only by design.** PTZ aiming has to work for anyone with an ONVIF
PTZ camera -- no UniFi Protect, Frigate, Home Assistant or any other system
required. Everything Stage A needs is plain ONVIF, straight to the camera:
- **Moving:** the PTZ service (`GotoPreset`, `GetPresets`, `Stop`).
- **Looking:** the media service's `GetSnapshotUri` for a still (digest
  auth), or `GetStreamUri` for an RTSP stream when a frame from video is
  needed.
- **Detection:** KNOCK's own vision model, not the camera's analytics.

Any bridge that starts a visit (UniFi, Frigate, Home Assistant, MQTT, the
future ONVIF bridge above) can use the PTZ camera during that visit -- it's
a camera KNOCK *looks through*, not a trigger source. So none of the ONVIF
bridge stages above are prerequisites either.

**Optional shortcuts when another system already has the camera.** If the
same camera is also adopted into UniFi Protect (or another NVR), KNOCK can
take snapshots through that system instead of logging in for them. That's
a convenience, never a requirement: PTZ control always goes direct over
ONVIF, since NVRs generally don't pass pan/tilt through for third-party
cameras, and an ONVIF camera accepts more than one client, so the NVR
keeps recording while KNOCK steers.

**First real camera:** a Hikvision **DS-2DE3304W-DE** (3MP, 4x mini dome
PTZ, no built-in auto-tracking), used as a second set of eyes next to the
UniFi G6 doorbell. It happens to also be adopted into Protect, which makes
it a good test of both paths: ONVIF-only, and the Protect snapshot
shortcut. Camera address and ONVIF login live in Settings, never in code.

Its view covers the walkway, the front yard and the street, so the
natural presets are **walkway** (who's approaching), **street** (vehicles)
and **porch** (packages, close up). The doorbell does the talking; the PTZ
looks at things the doorbell can't see well. For example:
- A vehicle is detected pulling up during a visit: swing to the street,
  and the car becomes part of the visit's camera context and the
  notification.
- A package was detected: zoom on the porch for a closer photo than the
  doorbell's package camera gets.
- Someone is at the edge of the frame: turn to them for a clear photo for
  the notification. Photo only -- no recognition (see Phase 8).

**Camera-side setup first (Hikvision).** The most important safety rails
live on the camera, not in KNOCK's code, so they hold even if KNOCK
misbehaves or crashes mid-move:
- ONVIF is off by default and has its **own user list** (Network ->
  Advanced -> Integration Protocol). Create an Operator-level ONVIF user
  there; the web admin login doesn't apply.
- **Named presets** ("walkway", "street", "porch"). These names are the
  entire vocabulary for aiming.
- **Park action:** the camera returns to a home preset by itself after N
  idle seconds. A hardware-level "go home".
- **PTZ limits and privacy masks:** physical pan/tilt stops plus masked
  areas, so the camera can't look into a neighbor's window regardless of
  what any software asks for.

**Stage A: presets and a guarded `look_at` step.**
- A PTZ provider, same shape as the vision/STT/TTS providers: an interface
  (`goto_preset`, `list_presets`, `stop`) with an ONVIF implementation.
  Works on any ONVIF PTZ, not just Hikvision.
- Settings: host, ONVIF user/password, and the **allowed presets**, each
  with a short description ("street: parked cars and the road"). Presets
  not on the list don't exist as far as KNOCK is concerned.
- **Deciding where to look uses scene signals only, never visitor
  speech.** The inputs are detector/vision output (vehicle detected, a
  package detected, a person at the edge of the doorbell's frame) and
  household actions. A visitor claiming to be a delivery driver must not
  be able to pick the camera's view. Deterministic rules come first
  (vehicle -> street, package -> porch). An optional LLM picker for cases
  the rules don't cover gets a **scene-only prompt** -- the camera
  observations and the preset list, no transcript -- and answers with a
  structured choice from the allowlist.
- **Its own guarded executor, not the existing tool calling.** Today's
  `chat_with_tools` use is structured extraction only: the orchestrator
  parses `classify_intent` / `flag_for_review` /
  `extract_notification_details` out of a single response and never runs
  a tool or sends a result back. So `look_at` is a separate step in the
  visit, not a tool added to those prompts: validate the preset against
  the allowlist and the per-visit move cap, move, wait a fixed settle time
  (~2s -- not ONVIF `MoveStatus`, which some cameras never update, see
  B1), snapshot, run vision, and add the result to the visit's camera
  context (`SceneContext`), tagged with camera and preset. The
  conversation sees it on the next turn and in the notification, not in
  the response already being generated.
- Return to the home preset at the end of every visit (the park action is
  the backstop if this never runs).

**Stage B: following someone (tracking).** Only matters for following a
person who's moving around -- visitors mostly stand at the door, so this
may never be needed. Two routes, cheapest first:
- **B1: Frigate's auto-tracking**, for cameras Frigate supports. Frigate
  already steers ONVIF PTZs from its own person detector, but needs
  relative moves in field-of-view space and a working ONVIF `MoveStatus`.
  **It doesn't apply to the DS-2DE3304W-DE:** Frigate's camera
  compatibility table lists that model by name as not autotracking-capable
  ("MoveStatus won't update even on latest firmware"). It stays here for
  other cameras: check Frigate's table first, then its startup check.
- **B2: KNOCK's own tracking** -- the route for the DS-2DE3304W-DE (and
  any camera B1 rejects), and only if real use shows that presets aren't
  enough. A small person detector gives a bounding
  box; a simple control loop turns "box is right of center" into a
  `ContinuousMove` pan at a speed proportional to the error, with a dead
  zone so it doesn't jitter, optional zoom to keep the person a sensible
  size, and a hard time limit. `ContinuousMove` is supported by almost
  every ONVIF PTZ and the loop steers off the video, not `MoveStatus`, so
  this works where B1 can't. The detector only runs
  while tracking during a visit, never continuously.

**Rules for every stage:**
- **A visitor never steers the camera** -- neither directly ("turn the
  camera left") nor indirectly (claiming to be a delivery driver to get
  the street view). That's the same class of manipulation PolicyEngine
  exists to refuse. Camera moves come only from scene/detector signals or
  the household; the transcript is never an input to a move decision.
- **The camera's own limits and masks are the real boundary**; KNOCK's
  preset allowlist sits on top of them, not instead of them.
- **No moving outside a visit**, and a cap on moves per visit so a
  confused model can't keep the camera sweeping.
- Every move is logged (which preset, why, which visit) alongside the
  visit's audit entry.

**Open questions:**
- Is Frigate in the stack? Only matters for other cameras' B1; the
  DS-2DE3304W-DE goes straight to B2 if tracking is ever needed.
- Does Protect's public API serve snapshots for an adopted third-party
  camera? Only decides whether the optional Protect snapshot shortcut
  works; ONVIF snapshots are the default either way.
- Are ONVIF events reliable enough on Hikvision, or is its native ISAPI
  alert stream worth a dedicated path? Only matters for the bridge, not
  for PTZ aiming.

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
  landline. An ATA (Analog Telephone Adapter) with an **FXO** port -- e.g.
  a Grandstream HT813 -- plugged into that jack turns the existing line
  into a SIP endpoint KNOCK's own code can place/answer calls on and read
  DTMF from, the same way a cordless phone or answering machine already
  can. (Not an FXS-only adapter like the HT801/HT802: those *supply* a
  line for a phone to plug into, so wiring one to the Ooma jack connects
  two line sources together and does nothing.) The provider's own
  cloud/app isn't part of this at all -- it's just the phone line
  underneath, same as any other phone plugged into that jack.

**Built-in SIP client.** Mainstream Python SIP stacks are GPL (pyVoIP,
pjsua2/PJSIP) or unmaintained (aiosip), which doesn't fit an MIT project
that ships a Docker image. KNOCK carries its own small SIP user agent
instead, scoped to what it actually needs: digest-authenticated
REGISTER/INVITE over UDP, G.711 (PCMU/PCMA) RTP audio, DTMF, and BYE. No
video, no transcoding beyond G.711, no full PBX features -- a SIP server
(UniFi Talk, Asterisk, FreeSWITCH, 3CX) or provider does the routing.

**Staged plan:**
- **Stage 1: announcement calls.** For the notification categories that
  matter most (emergency, and optionally approval), KNOCK also rings a
  configured extension or number and speaks the summary through TTS, then
  hangs up. Picking up a phone becomes an alternative to the push
  notification -- not a replacement, the push still goes out.
- **Stage 2: answer with DTMF.** The same call offers keypad choices
  ("press 1 if you're coming, 2 to turn them away"), mapped to the same
  actions as the notification buttons, spoken back to the visitor.
- **Stage 3: live handoff.** Bridge the call's audio to the doorbell's
  talkback/mic so the person who picked up talks to the visitor directly.
- **Stage 4: SIP door stations as a bridge.** Accept inbound calls from
  SIP intercoms (2N, Akuvox, Doorbird, UniFi Access) and run the normal
  conversation over the call audio.

**Rules for every stage:** a visitor can never make KNOCK dial out --
calls only go out from KNOCK's own notification logic, to numbers set in
Settings. And KNOCK never sends a door-release DTMF code: many door
stations and apartment buzzer systems unlock on a keypress, and KNOCK's
policy already refuses "unlock" requests; a SIP bridge answers and hands
off, it never buzzes anyone in.

#### Example implementation: UniFi Talk + ATAs

A concrete home setup using UniFi's phone system as the SIP server:

- **KNOCK as a Talk extension.** Talk accepts third-party SIP devices
  (with caveats: support has shifted between versions, reading SIP
  credentials has at times needed the Pro plan or the CLI, and Ubiquiti
  doesn't troubleshoot third-party devices). KNOCK registers as one with
  its own extension, so Stage 1's announcement call can ring any Talk
  phone -- or a ring group of all of them.
- **The G6 can't do this alone.** Protect doorbells don't speak SIP, so
  there's no native "doorbell rings the desk phones" in UniFi. KNOCK is
  the glue: G6 ring -> KNOCK conversation -> Talk extension call.
- **Analog phones via FXS ATAs.** A Grandstream HT801/HT802 registered in
  Talk turns old desk phones and cordless bases into Talk extensions, so
  KNOCK's call reaches them too, not just UniFi handsets. Watch ring load:
  many handsets on one port may not all ring.
- **Existing lines via FXO ATAs.** An HT813's FXO port brings an analog
  line into Talk: an Ooma base's jack, a landline, or an apartment buzzer
  that rings a phone line (a Stage 4 bridge target -- and exactly where
  the never-send-unlock-DTMF rule matters).
- **Settings to match on both ends:** DTMF method (RFC 2833 vs SIP INFO)
  and codec (G.711) on the ATA, Talk and KNOCK, or keypad choices won't
  register.
- **Switching providers away from Ooma.** Ooma doesn't hand out SIP
  credentials, so with Talk as the phone system the outside line comes
  from a SIP trunk instead -- Talk's own subscription, or a third-party
  provider (Talk ships templates for a handful; others use custom fields,
  unsupported by Ubiquiti). The number can usually be ported from Ooma. On
  a custom trunk, 911 is the provider's responsibility, and an ATA line's
  emergency calling depends on that trunk -- check it before relying on
  any of these phones for emergencies. That swap also makes the FXO/Ooma
  path above unnecessary.

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
- Streaming video understanding

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

### Streaming video understanding

Today's vision is one-shot: on a ring or detection, the UniFi/Frigate
bridge grabs a single snapshot and calls `VisionProvider.describe(image)`.
If that one frame catches the back of someone's head, or the moment
before they set a package down, that's all KNOCK ever "sees." The goal
here is for KNOCK to keep watching while a visitor is actually at the
door.

**What this is, and isn't.** This means sampling frames during an
*active visitor session* and stopping when the session ends. It does not
mean round-the-clock video analysis. Continuous detection is already
Frigate's job, and KNOCK shouldn't duplicate it: the GPU cost and the
privacy cost both go against Local-first and Safety-first.

**No external script needed, and Ollama stays.** Local vision models
(Ollama's included) take images, not live streams. "Streaming" in
practice means KNOCK pulls frames off the camera's RTSP(S) stream itself
and hands them to the model. The plumbing already exists:
`_capture_rtsp_audio` in the UniFi bridge opens the same RTSPS stream
with PyAV for audio, so frame grabbing is a sibling of that, inside the
existing bridges. Ollama's `/api/generate` already accepts a list of
`images`, so sending a few frames per request works with the current
provider.

Worth being honest about: no local model does truly continuous
streaming. Even "video" VLMs sample a clip into frames under the hood.
What a native video model adds is reasoning about motion and order
across those frames (temporal reasoning), not a live feed.

**Staged plan:**

- **Stage A: frame burst on trigger.** Instead of one snapshot, grab N
  frames over ~2-3 seconds, pick the sharpest or most-changed one (a
  cheap PIL diff is enough), and either describe that frame or send
  several in one multi-image request. Smallest change, fixes the
  "bad snapshot" problem, works on today's Ollama setup.
- **Stage B: rolling description during a session.** A per-session
  sampler task at a low rate (e.g. one frame every 2-5 seconds) that
  skips near-duplicate frames and only re-describes on a real change
  ("visitor set down a package," "a second person arrived"). Updates go
  into the session context the orchestrator already uses. Hard cap on
  frames per session; the sampler stops when the session ends.
- **Stage C: native video input (optional).** A new
  `OpenAICompatVisionProvider` that talks to a vLLM server (its own
  optional Docker container, the same way Ollama is today) running a
  video-capable VLM such as Qwen2.5-VL. KNOCK sends a short clip or
  timestamped frame sequence, so the model can follow what happened
  instead of judging separate stills. Tradeoff: this needs an NVIDIA GPU
  with real VRAM and has no CPU fallback, so it stays opt-in and Ollama
  stays the default. Running models in-process inside KNOCK
  (transformers/torch in the main container) is explicitly *not* the
  plan, because it would force GPU dependencies on every install and
  break the swappable-provider design.

**Architecture sketch:**

- A `FrameSource`: a per-camera RTSP frame sampler that reuses the
  `rtsp_transport=tcp` and self-signed-cert handling from
  `_capture_rtsp_audio`.
- An optional `describe_sequence(frames: list[bytes])` on
  `VisionProvider`, with a default that falls back to describing the
  best single frame, so the mock and existing providers keep working
  unchanged.
- Every description, single-frame or sequence, still goes through
  `vision.safety.sanitize_description`. More frames means more chances
  for a model to hallucinate something alarming, not fewer.

**Constraints and risks:**

- Latency: moondream is fast enough for doorbell timing; bigger models
  on small GPUs may not be, especially with several images per request.
- Context windows: multiple images add up fast. The existing 1024px
  downscale in `OllamaVisionProvider` helps, but frame count still needs
  a cap.
- Privacy: frames are held in memory for the session only. No recording
  by default.
- Battery doorbells can't stream (see Phase 6), so they stay on
  single-snapshot vision.

Related: Phase 7's live intercom / live video preview (same RTSP
plumbing), and Phase 9's per-task model routing (the vision model can be
configured separately from the text model).

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
- Calendar integration
- Structured tool-calling classification + rich notifications
- Per-task model routing ("model load balancing")

## Definitions

### Calendar integration

Feed relevant calendar context (e.g. a CalDAV feed, Google Calendar, or a
local ICS file) into the LLM as extra context when it's actually relevant
to the visitor's claim -- the clearest case is corroborating a
`service_appointment` ("I'm here for the AC repair") against a real
scheduled event, so KNOCK can respond with actual confidence instead of
taking a visitor's word for it, and could flag a mismatch (claimed
appointment with nothing on the calendar) as `suspicious_activity`
instead of the normal acknowledgment.

This has to be designed carefully against Phase 3's schedule-protection
rule: calendar data is exactly the kind of information KNOCK must never
leak back to a visitor (an event title/time is a schedule). The calendar
feed can only ever be read *internally* to inform classification/response
choice -- never quoted, summarized, or referenced in anything said at the
door. Likely implemented as a new read-only provider (mirrors the
LLM/STT/TTS/vision provider pattern) consulted only for the intents where
it's actually appropriate (service_appointment today; maybe
delivery_signature_required or visitation later), not wired into every
response.

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

### Model distillation / LoRA training workflow 🟡 Export built, training itself is external

The review-and-export half is done: a **Training** page lets a person
page through real audit-log interactions, approve or correct the intent/
response for each, and download an instruction-tuning JSONL file (`GET
/api/training/export`) built from the exact same prompts `Orchestrator`
sends the LLM in production (`_classification_prompt`/`_response_prompt`)
-- so a fine-tuned model trains on literally the same input shape it'll
see at inference time. Review state lives in a small separate JSON store
(`TrainingReviewStore`, keyed by a content hash of each audit entry, not
a new field on `AuditEntry` itself) so it works retroactively on every
already-recorded entry without a schema migration.

The actual LoRA training run is deliberately **not** part of KNOCK --
it happens on a separate local machine with a real GPU (this household
runs it on an idle RTX 5070 Ti, specifically *not* the machine serving
production Ollama inference, to avoid contending with real-time doorbell
responses), using whatever training stack the person prefers (PEFT/
transformers, Axolotl, Unsloth, etc.) pointed at the exported file. KNOCK
only produces the training data; it doesn't run or manage the fine-tune.

**Synthetic data generation is also built**, as two local, manually-run
scripts (not part of the automated test suite, same posture as
`scripts/smoke_test_providers.py`) -- because a hand-written scenario
list can't capture how chaotic a real doorbell is:

- `scripts/generate_scenarios.py` (Stage 1) uses one or more local Ollama
  models to synthesize a large, varied, randomized set of realistic
  visitor lines per intent category -- reusing `Orchestrator`'s own
  `_INTENT_DESCRIPTIONS`/`_LLM_CLASSIFIABLE_INTENTS` as the category
  taxonomy, so there's one place categories are defined, not two.
  Categories round-robin across every given model so the synthetic input
  distribution isn't biased by a single model's "imagination."
- `scripts/generate_training_data.py` (Stage 2) runs a scenario file
  (Stage 1's output, or the hand-written `scripts/training_scenarios.txt`
  seed set) through one or more teacher models via the real
  `Orchestrator`/`PolicyEngine`, recording every response to the real
  audit log. The same scenario answered by several models shows up as
  separate Training-page cards with identical visitor text and different
  candidate responses -- pick the best, reject the rest.
- The **Training page's Pending tab is a one-at-a-time triage view**
  (not a long scrollable list) -- one focused card, fine-tune the
  response, "Approve & Next"/"Reject & Next" auto-advances, "Back" undoes
  the last decision. This matters beyond just scale: dense multi-item
  review UIs are a real cognitive-load problem for some people (ADHD/
  autism), not just an inconvenience at high volume, so one-at-a-time is
  the default review experience here, not a special "large batch" mode.
  Approved/Rejected tabs keep the original full-list view, since browsing
  already-decided items doesn't have the same problem.

Still open: actually loading a fine-tuned/distilled model back into the
`OllamaConfig.model` setting and comparing its real-world performance
against the current prompt-engineered general-purpose model -- the loop
isn't closed until someone's actually run it and compared.

### Structured tool-calling classification + rich notifications ✅ Done

Classification moved from plain-text generation + exact-string-match
(`OllamaProvider.generate()`) onto real Ollama tool-calling
(`chat_with_tools()`, Ollama's `/api/chat` + a `tools` schema) when the
configured model supports it -- `classify_intent` returns a ranked top-3
list with confidence instead of one guessed word, structurally unable to
hallucinate a category outside the fixed list.

This closed a real gap found while auto-reviewing a night's worth of
synthetic training data: household notifications were only wired up for a
fixed intent whitelist, so the catch-all `unknown` intent had **no
notification path at all** -- a visitor's phrasing that matched neither
the keyword rules nor a confident LLM category silently vanished with zero
alert. Now, when confidence is genuinely low (a deterministic floor/margin
check on the model's own reported confidence, not "whichever tool the
model happened to call"), a `"review"` notification fires with a summary,
the top-3 guesses, and a camera photo -- a `flag_for_review` tool call is
only an additional OR'd signal into that check, never the sole trigger, so
a model that under- or over-reports its own uncertainty still gets caught.

A second tool call, `extract_notification_details`, replaced the old
single-sentence `summarize_for_notification()` across *every* notification
branch (not just the uncertain one) -- same call site, same cost, richer
output: a natural summary plus structured fields (visitor name,
organization, stated purpose, reference number) when the visitor actually
stated them. Every notification also gets a photo now, not just uncertain
ones, picking the doorbell's package camera for delivery-shaped intents
and the main camera for everything else -- delivered via a short-lived,
single-use, unguessable-token link (`core/snapshot_store.py` +
`api/snapshot_routes.py`), since the phone fetches it directly, outside
KNOCK's own session system, and a disk-backed store is required (not an
in-memory dict) because `UnifiBridge` runs as its own OS subprocess,
separate from the FastAPI process serving that route.

`OllamaConfig.use_tool_calling` is tri-state (`bool | None`), not a plain
flag: unset ("auto") runs a two-stage capability check at bridge
startup -- Ollama's own reported model capabilities, then a live
functional smoke test, since a model can claim `"tools"` support without
reliably producing a well-formed call in practice. An explicit `True`/
`False` is a sticky operator override that skips detection entirely, and
an explicit `False` is never silently re-enabled by a later restart's
auto-detection -- only the operator changing it back does that.

A related, lower-stakes voice command landed alongside this: saying
"systems check" at the door (`core/systems_check.py`) runs a real
functional test of whatever's actually configured on that bridge -- pings
the LLM, fetches a real camera snapshot and describes it, sends a real
test notification -- and speaks back "All systems operational" or what's
down. Handled entirely at the bridge level, before `PolicyEngine` or
`orchestrator.respond()` ever sees the transcript, since it's an operator
diagnostic command, not a visitor-behavior classification.

Building this also surfaced and fixed a pre-existing production bug,
unrelated to tool-calling itself: the response-phrasing templates for
several intents (`delivery_signature_required`, `food_delivery`,
`ride_arrived`, `visitation`, `service_appointment`) literally instructed
"I'll let them/the homeowner know you're here" as the example wording --
the same occupancy-confirming pattern that turned out to be the dominant
rejection reason across that night's entire training-data review, except
baked into live production output, not just something models drifted
into on their own. Reworded to relay-style phrasing ("I'll pass that
along") that doesn't confirm anyone's actually home to act on it right
now, in both `conversation/responses.py`'s static fallback text and
`orchestrator.py`'s `_INTENT_DESCRIPTIONS` LLM-prompt hints.

Hard safety constraint preserved throughout: `PolicyEngine.evaluate()`
still makes every block/allow/escalate decision deterministically, before
the LLM is ever touched -- tool-calling only affects classification labels
and notification content, never `PolicyDecision`/`ResponseDecision.escalate`
or anything that could unlock/open a door.

### Per-task model routing ("model load balancing")

Right now every LLM call `Orchestrator` makes -- classification, response
phrasing, notification-detail extraction -- goes through one globally
configured model (`OllamaConfig.model`). But different local models turned
out to have meaningfully different strengths and failure modes on
different *specific* jobs, not uniformly better-or-worse overall (e.g. one
model's structured tool-calling was more reliable while another phrased
more natural responses; a reasoning model's raw `<think>` leakage was a
problem for response phrasing specifically). Idea: let each distinct call
-- classification, response phrasing, notification extraction, vision
description -- be configured to use whichever model is actually best at
that job, instead of forcing one model to do everything. Likely shaped as
a per-task model override on top of the existing global default (fall
back to `OllamaConfig.model` when a task-specific override isn't set),
rather than a new standalone config system.

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

- Phase 4 follow-on, found live: intent classification doesn't actually
  use conversation context yet, despite `SessionState` tracking it --
  each turn is classified on its own text alone, not helped or hurt by
  what was asked or said earlier in the same visit. See Phase 4's
  Context definition for specifics.
- Phase 6 gaps: ONVIF, SIP/VoIP (per-camera trigger selection and basic
  ESPHome support are both done now)
- Phase 7 gaps: live intercom mode, real multi-voice switching
- Phase 8 gaps: known visitor recognition, structured vision detection
  (today's vision is one generic description, not separate categories),
  streaming video understanding (frame sampling during a session)
- Phase 9: everything except self-hosted OIDC sign-in (done)

The project has moved past "stabilize the foundation" into "pick which
advanced feature earns its slot next" -- see each phase's Definitions for
the reasoning behind what's still open and why.
