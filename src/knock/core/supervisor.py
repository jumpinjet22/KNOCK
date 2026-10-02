"""Process supervision for the four bridge subprocesses.

Each bridge (`knock-mqtt-bridge`, `knock-frigate-bridge`, `knock-ha-bridge`,
`knock-unifi-bridge`) is a separate, already-working console script -- this
intentionally does not pull them into the API's own event loop/process.
Two are blocking (`loop_forever()`-style) and two are `asyncio.run()`-based;
rewriting all four to share one event loop would be a rewrite of tested
modules for no real benefit. Instead, each one is spawned as a real
`subprocess.Popen` child, configured via the exact `KNOCK_<NAME>_<FIELD>`
env vars its own `*Config.from_env()` already reads -- zero bridge code
changes needed.

One `BridgeSupervisor` is meant to live for the lifetime of the API
process. This assumes a single `uvicorn` worker: multiple workers would
each spawn their own, duplicate copies of every bridge.
"""

from __future__ import annotations

import collections
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import SecretStr

from knock.config import FrigateConfig, HomeAssistantConfig, MqttConfig, UnifiConfig
from knock.core.config_store import ConfigStore

logger = logging.getLogger(__name__)

BridgeName = Literal["mqtt", "frigate", "homeassistant", "unifi"]
BridgeStatus = Literal["stopped", "starting", "running", "crashed"]

BRIDGE_COMMANDS: dict[BridgeName, str] = {
    "mqtt": "knock-mqtt-bridge",
    "frigate": "knock-frigate-bridge",
    "homeassistant": "knock-ha-bridge",
    "unifi": "knock-unifi-bridge",
}

_ENV_PREFIXES: dict[BridgeName, str] = {
    "mqtt": "KNOCK_MQTT_",
    "frigate": "KNOCK_FRIGATE_",
    "homeassistant": "KNOCK_HA_",
    "unifi": "KNOCK_UNIFI_",
}

_CONFIG_CLASSES: dict[BridgeName, Any] = {
    "mqtt": MqttConfig,
    "frigate": FrigateConfig,
    "homeassistant": HomeAssistantConfig,
    "unifi": UnifiConfig,
}

_MAX_LOG_LINES = 500
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_MAX_SECONDS = 60.0
_MAX_RESTART_ATTEMPTS = 5
_CRASH_LOOP_RESET_SECONDS = 60.0


def _env_value(value: object) -> str | None:
    """Stringify one resolved config field back into its env-var form."""
    if value is None:
        return None
    if isinstance(value, SecretStr):
        secret = value.get_secret_value()
        return secret if secret else None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def build_bridge_env(name: BridgeName, store: ConfigStore) -> dict[str, str]:
    """The env a bridge subprocess should see: the parent's own environment,
    overlaid with every field of its resolved (env > store > default)
    config, flattened back to `KNOCK_<NAME>_<FIELD>`. A field resolved from
    an already-set env var round-trips to the same value; one resolved from
    the store or a hardcoded default becomes newly visible to the child
    exactly as if it had been set directly.
    """
    config_cls = _CONFIG_CLASSES[name]
    prefix = _ENV_PREFIXES[name]
    resolved = config_cls.from_sources(store)

    env = dict(os.environ)
    for field_name in config_cls.model_fields:
        env_var = f"{prefix}{field_name.upper()}"
        value = _env_value(getattr(resolved, field_name))
        if value is None:
            env.pop(env_var, None)
        else:
            env[env_var] = value
    return env


@dataclass
class _BridgeRuntime:
    status: BridgeStatus = "stopped"
    process: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None
    logs: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=_MAX_LOG_LINES)
    )
    total_lines: int = 0
    restart_count: int = 0
    last_start_time: float = 0.0
    manually_stopped: bool = True
    # Bumped on every start()/restart(). A lifecycle thread checks this at
    # each resume point (before respawning, after a process exits) and
    # abandons itself the moment it no longer matches -- otherwise a thread
    # woken from a long crash-loop backoff sleep by a since-superseded
    # start() could race a newer lifecycle thread for the same bridge and
    # spawn a second, duplicate subprocess.
    generation: int = 0


@dataclass
class BridgeInfo:
    name: str
    status: BridgeStatus
    restart_count: int
    pid: int | None


class BridgeSupervisor:
    def __init__(
        self,
        store: ConfigStore | None = None,
        *,
        commands: dict[BridgeName, list[str]] | None = None,
        restart_base_delay: float = _BACKOFF_BASE_SECONDS,
        restart_max_delay: float = _BACKOFF_MAX_SECONDS,
        max_restart_attempts: int = _MAX_RESTART_ATTEMPTS,
        crash_loop_reset_seconds: float = _CRASH_LOOP_RESET_SECONDS,
    ) -> None:
        self.store = store or ConfigStore()
        self.commands: dict[BridgeName, list[str]] = commands or {
            name: [command] for name, command in BRIDGE_COMMANDS.items()
        }
        self.restart_base_delay = restart_base_delay
        self.restart_max_delay = restart_max_delay
        self.max_restart_attempts = max_restart_attempts
        self.crash_loop_reset_seconds = crash_loop_reset_seconds
        self._lock = threading.RLock()
        self._bridges: dict[BridgeName, _BridgeRuntime] = {
            name: _BridgeRuntime() for name in self.commands
        }

    def _append_log(self, rt: _BridgeRuntime, line: str) -> None:
        with self._lock:
            rt.logs.append(line)
            rt.total_lines += 1

    def describe(self, name: BridgeName) -> BridgeInfo:
        with self._lock:
            rt = self._bridges[name]
            pid = rt.process.pid if rt.process is not None else None
            return BridgeInfo(name=name, status=rt.status, restart_count=rt.restart_count, pid=pid)

    def describe_all(self) -> list[BridgeInfo]:
        return [self.describe(name) for name in self._bridges]

    def tail(self, name: BridgeName, after: int = 0) -> tuple[list[str], int]:
        with self._lock:
            rt = self._bridges[name]
            buf = list(rt.logs)
            total = rt.total_lines
        start_index = total - len(buf)
        offset = max(0, after - start_index)
        return buf[offset:], total

    def start(self, name: BridgeName) -> None:
        with self._lock:
            rt = self._bridges[name]
            if rt.status in ("starting", "running"):
                return
            rt.manually_stopped = False
            rt.restart_count = 0
            rt.generation += 1
            generation = rt.generation
            rt.status = "starting"
            thread = threading.Thread(target=self._lifecycle, args=(name, generation), daemon=True)
            rt.thread = thread
        thread.start()

    def stop(self, name: BridgeName) -> None:
        with self._lock:
            rt = self._bridges[name]
            rt.manually_stopped = True
            process = rt.process
        if process is not None and process.poll() is None:
            self._terminate(process)

    def restart(self, name: BridgeName) -> None:
        with self._lock:
            rt = self._bridges[name]
            rt.manually_stopped = True
            process = rt.process
            thread = rt.thread
        if process is not None and process.poll() is None:
            self._terminate(process)
        if thread is not None:
            thread.join(timeout=10)
        self.start(name)

    def shutdown_all(self) -> None:
        """Terminate every running child. Paired with the Dockerfile's `tini`
        entrypoint: without an init process as PID 1, `docker stop` would
        only ever signal the API process itself and leave these orphaned.
        """
        for name in list(self._bridges):
            self.stop(name)
        for name in list(self._bridges):
            thread = self._bridges[name].thread
            if thread is not None:
                thread.join(timeout=10)

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _lifecycle(self, name: BridgeName, generation: int) -> None:
        rt = self._bridges[name]
        while True:
            with self._lock:
                if rt.generation != generation:
                    return
                if rt.manually_stopped:
                    rt.status = "stopped"
                    return
                env = build_bridge_env(name, self.store)
                try:
                    process = subprocess.Popen(
                        self.commands[name],
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                    )
                except OSError as exc:
                    rt.status = "crashed"
                    self._append_log(rt, f"[supervisor] failed to start {name}: {exc}")
                    return
                rt.process = process
                rt.status = "running"
                rt.last_start_time = time.monotonic()

            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(rt, line.rstrip("\n"))
            process.wait()

            with self._lock:
                if rt.generation != generation:
                    return
                rt.process = None
                if rt.manually_stopped:
                    rt.status = "stopped"
                    return

                if time.monotonic() - rt.last_start_time > self.crash_loop_reset_seconds:
                    rt.restart_count = 0
                rt.restart_count += 1

                if rt.restart_count > self.max_restart_attempts:
                    rt.status = "crashed"
                    self._append_log(rt, f"[supervisor] {name} crashed repeatedly, giving up")
                    return

                backoff = min(
                    self.restart_base_delay * (2 ** (rt.restart_count - 1)),
                    self.restart_max_delay,
                )
                rt.status = "starting"
                self._append_log(
                    rt,
                    f"[supervisor] {name} exited unexpectedly, restarting in "
                    f"{backoff:.0f}s (attempt {rt.restart_count}/{self.max_restart_attempts})",
                )

            time.sleep(backoff)
