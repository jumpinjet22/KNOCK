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

# Phase 4 — Session and State Management

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
- ESPHome

## Definitions

### MQTT

MQTT is a lightweight messaging system commonly used in smart home environments.

### Frigate

Frigate is an open-source AI camera system.

### Home Assistant

Home Assistant is a local smart home automation platform.

### ONVIF

ONVIF is a common camera communication standard.

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

## Notes

This phase happens after the text system is stable.

Voice systems add significant complexity.

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

## Important

Vision processing is optional.

KNOCK should still function without GPUs or advanced vision models.

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

Right now the focus is:

1. Clean architecture
2. Stable foundation
3. Local text harness
4. Safety system
5. Provider interfaces

The project should become stable and understandable before adding advanced AI features.
