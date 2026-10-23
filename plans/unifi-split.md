# Plan: make `unifi.py` only do UniFi things

Follows `plans/doors-routing.md` (Phases 0-2 done). Same safety contract:
with no doors configured, behavior is byte-for-byte what it is today; no
existing test is edited or deleted; production changes only when a human
bumps the image tag.

## Problem

`integrations/unifi.py` is ~1,290 lines and does five jobs:

| Job | Belongs in |
|---|---|
| Protect client, event websocket, trigger filtering by event type | UniFi (stays) |
| Camera audio in (RTSP capture) and out (talkback), snapshots, clips | UniFi (stays) |
| Run a visit: session resume, hooks, turn loop | core |
| Tell the household: per-intent wording, buttons, photos, SIP calls | core |
| Wire up Ollama/Whisper/Kokoro/SIP in `main()` | a neutral entry point |

Frigate and Home Assistant would otherwise have to copy or import the
UniFi bridge to get the same visit behavior.

## Target shape

    connectors (unifi.py, homeassistant.py, frigate.py)
        translate an event into "a trigger on a door"; provide DevicePort
    core/visit.py          VisitRunner: runs a visit for a Door via a DevicePort
    core/household.py      HouseholdNotifier: notification wording, buttons, SIP calls
    core/orchestrator.py   unchanged: what to say, intent, policy

`DevicePort` is a small Protocol: `listen(device)`, `speak(device, text)`,
`play_thinking(device)`, `snapshot(device, package=False)`. `UnifiBridge`
implements it; a Frigate or HA connector can provide its own.

## Steps (each one: all gates green, no test edited, one commit)

1. **`HouseholdNotifier`.** Move `_handle_turn_notifications`,
   `_on_escalate_notify`, `_notify_household`, `_maybe_call_household`,
   `_call_household`, `_approval_actions`, `_coming_to_door_action`,
   `_build_snapshot_url`. `UnifiBridge` keeps one-line delegating methods
   with the same names and signatures, because the frozen Phase 0 tests call
   them directly.
2. **`VisitRunner`.** Move `_run_conversation`, `_start_or_resume_session`,
   `build_event`'s non-UniFi parts, the systems-check interceptor, and the
   package-presence bookkeeping around a visit. Introduce `DevicePort`.
3. **Neutral entry point.** Move provider wiring out of `unifi.main()`
   into one place that builds the orchestrator/providers and the connectors.
4. **Point Frigate and HA at the shared core** (separate PR; only if
   wanted).

Not in scope: orchestrator, prompts, policy, conversation logic. If a step
needs to touch those, stop and ask.

## Gates per step

- `ruff check .`, `ruff format --check .`, `mypy src tests`, `pytest -q`
- `git diff origin/main -- tests/` shows only added files/lines
- Phase 0 characterization tests and `test_doors_routing.py` pass unedited
- A deliberate-breakage check on whatever the step moved (wording, tags,
  SIP text, session ID)
- `wc -l src/knock/integrations/unifi.py` goes down each step
