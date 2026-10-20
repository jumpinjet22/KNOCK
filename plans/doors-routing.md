# Plan: "Doors" routing, with zero change to current production

**For:** the local Claude Code instance that has access to the production
machines. **Written by:** the cloud session that designed it. Read the
whole thing before starting. **Stop and report** whenever a stop condition
(section 8) hits. Don't push past it.

---

## 1. Goal

Today every visit is routed by a single UniFi camera ID (`device_id`). The
camera that triggered the visit (or the camera a Home Assistant knock sensor
maps to) is used for **everything**: listening (mic), talking (speaker),
snapshots/vision, the package camera, the visit session, and the
`(camera: <id>)` text in notifications.

Add a **door** concept: a named door with its own triggers and its own
choice of devices, so a door can listen on one camera, talk through
another, and take pictures from a third, with notifications that say
"Front door" instead of a camera ID.

Motivating setup (the remote site):

- UniFi cameras with mics and speakers (confirm `has_speaker` per camera in
  Protect).
- A **dumb wired doorbell** that will get a Home Assistant sensor
  (Shelly/ESPHome/vibration sensor). That sensor triggers through the
  existing `knock_sensors` path.
- Home Assistant on site.
- KNOCK runs at the remote site. The AI services (Ollama, vision, Whisper,
  Kokoro) live on the local AI box, reached over a routed WireGuard
  site-to-site tunnel (~30 ms RTT, <1 ms jitter).

### Non-goals (do NOT build these here)

- PTZ aiming, SIP door stations, HA media-player speakers, a doorbell video
  call. A door's config may *reserve* room for them later, but nothing in
  this plan implements them.
- Any change to the conversation logic, policy, intents, or prompts.

---

## 2. The safety contract (non-negotiable)

1. **No `doors` configured means behavior is byte-for-byte identical to
   today.** KNOCK derives "implicit doors", one per camera, from the
   existing settings (`UnifiConfig.trigger_camera_ids`,
   `HomeAssistantConfig.knock_sensors`, and any camera that triggers).
   For implicit doors, all of these stay exactly as they are now:
   - session IDs: `unifi-<sanitized device_id>` (see `_default_session_id`
     in `src/knock/integrations/unifi.py`)
   - package notification tags: `knock-package-<device_id>` (see
     `package_notification_tag` in `src/knock/integrations/homeassistant.py`)
   - notification text, including the `(camera: <device_id>)` suffixes in
     `UnifiBridge._handle_turn_notifications`
   - package-alert state keyed by device ID (`PackageAlerts` in
     `src/knock/integrations/package_alerts.py`)
   - audit log, package log, and History entries
2. **Saved settings are never rewritten or migrated.** `doors` is a new,
   optional config key. Nothing reads, renames, or deletes existing keys on
   behalf of doors.
3. **Existing tests are frozen.** No existing test function may be edited
   or deleted. If the refactor seems to need that, it's a behavior change:
   stop (section 8).
4. **Production changes only when the human bumps the image tag.** Merging
   to `main` deploys nothing (compose pins an exact `sha-…` image).

---

## 3. Pre-flight (do this before writing any code)

Record the baseline so anything can be compared against it and rolled back.

1. **Production image tag.** On the production box:
   `docker ps --format '{{.Names}}  {{.Image}}'`. Write the exact
   `ghcr.io/jumpinjet22/knock:sha-…` tag in the report. That's the rollback
   target.
2. **Back up KNOCK's data volume** (settings, auth, audit log, sessions,
   package log, media). The compose project is `knock`, so the volume is
   usually `knock_knock-data`. Check with `docker volume ls`.
   ```bash
   docker run --rm -v knock_knock-data:/data -v "$PWD":/backup alpine \
     tar czf /backup/knock-data-pre-doors-$(date +%Y%m%d).tgz -C /data .
   ```
   Keep the archive off the production box as well.
3. **Tag the code baseline:** `git fetch origin && git tag pre-doors-baseline origin/main`
   (local tag only, unless the human asks to push it).
4. **Recommend branch protection** to the human if it isn't on yet: GitHub
   → Settings → Branches → protect `main` (require a PR and an approval). A
   PR was merged by accident before; this prevents a repeat.
5. **Branch:** `git checkout -b feature/doors origin/main`. All work
   happens here. Never commit to `main` directly, and never force-push
   `main`.

---

## 4. Implementation phases

Each phase ends with the **gate** in section 5. Commit per step, with
small, descriptive commits.

### Phase 0: Pin current behavior with characterization tests (no src changes)

New file: `tests/test_routing_characterization.py`. Only add tests; touch
nothing in `src/`. Cover what exists **today**, using the fakes already in
`tests/test_unifi_bridge.py` / `tests/test_homeassistant_bridge.py` as
patterns (copy helpers; don't import private test helpers across files if
that's fragile):

- A Protect `ring` on camera X: talkback, mic capture (`listen_to_visitor`),
  snapshot (`get_public_api_camera_snapshot`) and session all use X.
  Session ID is `unifi-X`.
- A smart-detect trigger on camera X: same.
- `trigger_camera_ids=["X"]`: a ring on Y is ignored.
- A knock sensor `binary_sensor.k → X` (`HomeAssistantKnockListener`
  → `UnifiBridge.handle_knock`): everything uses X.
- Notification text for each intent branch contains `(camera: X)`
  exactly as today.
- A package alert on X uses tag `knock-package-X`, and package-alert
  presence/departure is keyed by X.
- A SIP call (`_maybe_call_household`) still fires for the configured
  categories with today's text.

These must pass on unmodified `main` code. **Then** run the mutation
check (section 5.4) on two or three of them to prove they bite.

### Phase 1: Door model + implicit-door derivation (pure code, not wired in)

- New module `src/knock/core/doors.py`:
  - `DoorConfig` (pydantic): `key`, `name`, `triggers` (lists of Protect
    camera IDs that may ring/smart-detect for this door, and HA entity IDs),
    `listen_device`, `speak_device`, `look_devices` (ordered: first is the
    main snapshot camera), `call_targets` (optional per-door SIP targets,
    falling back to `SipConfig.call_targets`), plus `implicit: bool`.
  - `Door` (resolved, immutable) with a `session_id` property:
    implicit door → `unifi-<sanitized device>` (reuse
    `_default_session_id`'s logic exactly); explicit door →
    `door-<key>`.
  - `resolve_doors(unifi_config, ha_config, doors_config) -> DoorRegistry`
    with lookups `for_protect_event(device_id) -> Door | None` and
    `for_ha_entity(entity_id) -> Door | None`.
  - **Implicit doors:** when `doors_config` is empty, one door per
    triggering camera, with listen = speak = look[0] = that camera and
    `name` = the camera ID. That way, any text built from the door name
    renders exactly like today's `(camera: X)`.
  - **Mixed mode:** explicit doors claim their triggers. Any camera or
    sensor not claimed by an explicit door still gets an implicit door.
    Nothing that works today may stop working because one explicit door
    was added.
- Config: new `DoorsConfig` in `src/knock/config.py` (a list of
  `DoorConfig`), loaded from a new settings section `doors`, plus env
  `KNOCK_DOORS` (JSON) with the same precedence rules as every other
  `from_sources()`. **Do not** register it in the Settings UI yet.
- Tests in `tests/test_doors.py`: implicit derivation matches today for
  every Phase 0 scenario. Explicit door resolution. Mixed mode. An unknown
  device/entity resolves to nothing. Validation errors (a door with no
  triggers, a speak device that isn't a known camera ID if that's
  checkable, duplicate keys, one trigger claimed by two doors).

### Phase 2: Route visits through a `Door` (the refactor)

- `UnifiBridge.handle_event` / `handle_knock` resolve a `Door` first,
  then run the conversation with it. Thread the door through:
  - `listen_to_visitor(door.listen_device)`
  - `speak_to_visitor(door.speak_device, …)`, plus the greeting, the
    thinking tone and the error sound
  - `_capture_scene` / snapshots: `door.look_devices[0]`. Package-camera
    lens: the same device as today, i.e. the doorbell that has one.
  - session: `door.session_id`
  - notification text: today's `(camera: X)` becomes `(camera: <door name>)`
    for **implicit** doors (identical output, since name = camera ID). For
    explicit doors use a friendlier form, e.g. "at the Front door".
    Implicit doors must keep the exact old string.
  - `PackageAlerts`: keep keying by **device ID** for implicit doors
    (unchanged tags/state). Explicit doors: key by the door's
    `look_devices[0]` so package state stays per physical camera.
  - SIP: `door.call_targets or sip_config.call_targets`.
- The conversation runner (`core/conversation_runner.py`) should need no
  changes. Its hooks are closures, so build them from the door.
- **Gate check that matters most:** the entire existing suite **and**
  Phase 0's characterization tests pass with zero edits.

### Phase 3: Explicit doors end-to-end

- Wire `HomeAssistantKnockListener` to resolve an entity to a door. Today it
  maps entity → camera via `knock_sensors`; keep that working through the
  implicit-door path.
- Tests: a door that listens on A, speaks on B and looks via C really uses
  A/B/C. Two triggers (an HA sensor plus a Protect person detection) on
  one door share **one** visit session (`door-<key>`), so the greeting
  isn't replayed. Per-door SIP targets. Notifications say the door name.

### Phase 4: Dry-run mode (for side-by-side testing next to production)

- `KNOCK_DRY_RUN=1` (and a `dry_run` field on `KnockConfig`): the bridge
  resolves doors and logs exactly what it **would** do, e.g.
  `DRY RUN door=front trigger=binary_sensor.k listen=A speak=B look=[C] session=door-front`,
  and then does **nothing else**: no talkback, no mic capture, no TTS, no
  HA notifications, no SIP calls, no package alerts, no audit entries (so
  History isn't polluted).
- Tests: with dry run on, none of those side effects happen (assert the
  fakes are never called) and the log line is emitted.

### Phase 5 (optional, only if phases 0-4 are solid): Settings UI

- A "Doors" section with pickers (cameras from the existing Protect camera
  list endpoint, HA entities if an endpoint exists, otherwise free text).
  Stays additive, and the page must still render with no doors configured.

---

## 5. Gates (run at the end of every phase; all must pass)

From the repo root, with dev deps installed (`pip install -e ".[dev]"`):

1. `ruff check . && ruff format --check .`
2. `mypy src tests`
3. `pytest -q`: the full suite, not just new files
4. **Mutation check:** for each new safety-relevant test, temporarily break
   the code it guards (e.g. make an implicit door's session ID
   `door-<cam>`, swap listen/speak devices, let dry run still speak).
   Confirm the test fails, then restore the file (`git checkout -- <file>`)
   and confirm it passes again. Report which mutations were tried.
5. **Frozen tests check:** `git diff origin/main -- tests/` must show only
   **added** files/lines. No existing test changed. (Phase 0 adds
   `tests/test_routing_characterization.py`, later phases add
   `tests/test_doors.py` and so on.)
6. If anything under `web/` changed: `cd web && npm ci && npm run lint && npm run build`.

---

## 6. Rollout to production (only after phases 0-4 pass their gates)

1. Push `feature/doors` and open a PR to `main`, **do not merge**. CI must
   be green, including the Docker build.
2. Build a test image from the branch (GitHub Actions → "docker" workflow →
   Run workflow → branch `feature/doors`). It publishes
   `ghcr.io/jumpinjet22/knock:sha-<short-sha>`. Record the tag.
3. **Side-by-side dry run** (production keeps answering the door):
   - Run a second container from the test image with `KNOCK_DRY_RUN=1`, its
     own data volume (never the production volume), a different web port,
     the same Protect/HA connection settings, and **no SIP** enabled.
   - Configure the real `doors` you want on that test instance.
   - Trigger real events (press the doorbell / walk past the cameras). For
     each, compare the dry-run log line with what production actually did
     and with what the door config says it should do.
4. **Swap production to the new image, with NO doors configured.** Edit
   the production compose image tag to the test image, `docker compose up -d`,
   then run the **production checklist**:
   - [ ] a ring/knock gets greeted and answered on the same camera as before
   - [ ] the History page shows the new visit like before, and old visits
         still display
   - [ ] notification text looks exactly as before
   - [ ] the package alert flow still works (or at least no errors in logs)
   - [ ] SIP call still placed for an emergency, if SIP is enabled
   - [ ] no new errors/tracebacks in `docker logs knock` over a day
5. **Only then add the `doors` config to production**, door by door, and
   re-run the checklist for each door.
6. Merge the PR only when the human says so.

---

## 7. Revert procedures (from least to most invasive)

| Level | When | How |
|---|---|---|
| 1. Config | A door behaves wrong | Remove that door (or the whole `doors` section / `KNOCK_DOORS`). KNOCK falls back to implicit doors, i.e. today's behavior. No restart of anything else needed beyond the bridge. |
| 2. Image | Anything breaks with the new image, even without doors | Put the **pre-flight image tag** back in the production compose file, `docker compose up -d`. |
| 3. Data | Should never be needed (nothing migrates data) | Stop the container, restore the pre-flight tarball into the volume, start it. |
| 4. Code | A bad change already merged to `main` | Open a PR that `git revert`s the merge commit. Never rewrite `main`'s history or force-push it. |

After any revert: write down what happened, which level fixed it, and the
relevant log lines in the report.

---

## 8. Stop conditions (stop, revert local work if needed, and report)

- An existing test needs editing or deleting to pass.
- Any implicit-door output differs from today: session ID format, package
  tag format, notification text, audit/History shape.
- Phase 0's characterization tests can't be made to pass on unmodified
  `main` (that means the behavior described here is wrong; report the
  actual behavior instead of "fixing" it).
- A gate fails and the fix isn't obvious and local.
- Anything in the production checklist (section 6.4) regresses: do the
  section 7 revert first, then report.
- The work starts needing changes to conversation logic, prompts, policy
  rules, or anything listed under non-goals.

---

## 9. What to report back

- The baseline production image tag and backup location (pre-flight).
- Per phase: commits, gate results, which mutations were tried.
- The test image tag, dry-run log excerpts vs. what production did, and
  the production checklist results.
- Anything surprising, any stop condition hit, and any revert performed.
