"""Runs `scripts/generate_scenarios.py`/`scripts/generate_training_data.py`
as a tracked child process, so the Training page's training-mode script
runner panel can kick these off from the browser instead of a terminal.

Deliberately not built on `BridgeSupervisor` (see `knock.core.supervisor`):
these are one-shot batch jobs expected to run to completion and exit, not
long-running daemons that should be crash-loop-restarted on exit -- an
exit code of 0 is success here, not something to respawn.

Assumes the process's current working directory is the repo root (true
both for the Docker image's `WORKDIR /app`, which also has `scripts/`
copied in, and for the documented local dev workflow of running `uvicorn`
from a repo checkout) -- `scripts/<name>.py` is resolved relative to cwd,
not to this installed package's own location (`scripts/` sits outside
`src/`, so it isn't installed as part of the package either way).
"""

from __future__ import annotations

import collections
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

ScriptName = Literal["generate_scenarios", "generate_training_data"]
RunStatus = Literal["idle", "running", "completed", "failed", "stopped"]

_MAX_LOG_LINES = 2000


@dataclass
class _Run:
    script: ScriptName | None = None
    status: RunStatus = "idle"
    process: subprocess.Popen[str] | None = None
    thread: threading.Thread | None = None
    logs: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=_MAX_LOG_LINES)
    )
    total_lines: int = 0
    exit_code: int | None = None
    started_at: float | None = None
    # Bumped on every start(); the reader thread checks this once the
    # process exits and abandons itself if a newer run has since started,
    # the same guard BridgeSupervisor uses for its own lifecycle threads.
    generation: int = 0


@dataclass
class RunState:
    script: ScriptName | None
    status: RunStatus
    exit_code: int | None
    started_at: float | None


class ScriptRunner:
    """Tracks exactly one script run at a time -- these scripts are already
    sequential/single-process by design (see their own docstrings on not
    parallelizing the model loop), and only one local Ollama server is
    being asked to do real GPU work regardless, so there's no reason two
    runs should ever overlap.
    """

    def __init__(self, scripts_dir: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._run = _Run()
        # Overridable for tests, which point this at a tmp directory
        # holding small controllable fake scripts instead of the real
        # ones (which hit a real Ollama server over the network).
        self._scripts_dir = scripts_dir or Path("scripts")

    def state(self) -> RunState:
        with self._lock:
            return RunState(
                script=self._run.script,
                status=self._run.status,
                exit_code=self._run.exit_code,
                started_at=self._run.started_at,
            )

    def tail(self, after: int = 0) -> tuple[list[str], int]:
        with self._lock:
            buf = list(self._run.logs)
            total = self._run.total_lines
        start_index = total - len(buf)
        offset = max(0, after - start_index)
        return buf[offset:], total

    def start(self, script: ScriptName, args: list[str]) -> None:
        with self._lock:
            if self._run.status == "running":
                raise RuntimeError("a script is already running")
            self._run = _Run(script=script, status="running", started_at=time.time())
            self._run.generation += 1
            generation = self._run.generation
            thread = threading.Thread(
                target=self._run_script, args=(script, args, generation), daemon=True
            )
            self._run.thread = thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            process = self._run.process
            if self._run.status == "running":
                self._run.status = "stopped"
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def _append_log(self, line: str) -> None:
        with self._lock:
            self._run.logs.append(line)
            self._run.total_lines += 1

    def _run_script(self, script: ScriptName, args: list[str], generation: int) -> None:
        script_path = self._scripts_dir / f"{script}.py"
        command = [sys.executable, str(script_path), *args]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            with self._lock:
                if self._run.generation != generation:
                    return
                self._run.status = "failed"
            self._append_log(f"[script-runner] failed to start {script}: {exc}")
            return

        with self._lock:
            if self._run.generation != generation:
                process.kill()
                return
            self._run.process = process

        assert process.stdout is not None
        for line in process.stdout:
            with self._lock:
                if self._run.generation != generation:
                    process.kill()
                    return
            self._append_log(line.rstrip("\n"))
        exit_code = process.wait()

        with self._lock:
            if self._run.generation != generation:
                return
            self._run.process = None
            self._run.exit_code = exit_code
            was_stopped = self._run.status == "stopped"
            if not was_stopped:
                self._run.status = "completed" if exit_code == 0 else "failed"
