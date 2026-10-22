"""Running and remote-controlling baresip, KNOCK's SIP phone.

baresip (https://github.com/baresip/baresip, BSD-3-Clause) is a mature SIP
client. KNOCK runs it as a separate helper process -- not a Python library
linked into KNOCK -- and drives it over its `ctrl_tcp` module: netstring-
framed JSON commands in (`dial`, `hangup`, ...), JSON events out
(`CALL_RINGING`, `CALL_ESTABLISHED`, `CALL_CLOSED`, `REGISTER_OK`, ...).
That keeps all the SIP protocol details (digest auth, retransmits, CANCEL,
NAT, codecs) in a battle-tested implementation.

Call audio comes from baresip's `aufile` module reading one fixed WAV path
(`announcement_path`), which the caller overwrites before each call; when
the file runs out baresip ends the call on its own ("end of file").
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from knock.config import SipConfig

logger = logging.getLogger(__name__)

DEFAULT_BARESIP_DIR_ENV_VAR = "KNOCK_BARESIP_DIR"

# ctrl_tcp has no authentication at all, so it only ever listens on
# loopback -- anything that can reach it can place calls.
CTRL_HOST = "127.0.0.1"

# Characters that would break out of baresip's `<sip:...>;key=value`
# account line (and so could inject extra account parameters).
_UNSAFE_ACCOUNT_CHARS = set(';<>"\n\r')


def default_baresip_dir() -> Path:
    configured = os.environ.get(DEFAULT_BARESIP_DIR_ENV_VAR)
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "knock" / "baresip"


def encode_netstring(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload).encode("utf-8")
    return str(len(data)).encode("ascii") + b":" + data + b","


class NetstringReader:
    """Incrementally splits a byte stream into decoded JSON netstrings."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buffer += data
        messages: list[dict[str, Any]] = []
        while True:
            colon = self._buffer.find(b":")
            if colon < 0:
                break
            length = int(self._buffer[:colon])
            end = colon + 1 + length
            if len(self._buffer) < end + 1:
                break
            messages.append(json.loads(self._buffer[colon + 1 : end]))
            self._buffer = self._buffer[end + 1 :]
        return messages


class BaresipControl:
    """Blocking client for baresip's ctrl_tcp interface."""

    def __init__(self, port: int, host: str = CTRL_HOST, connect_timeout: float = 5.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=connect_timeout)
        self._reader = NetstringReader()
        self._pending: list[dict[str, Any]] = []
        self._tokens = 0

    def close(self) -> None:
        self._sock.close()

    def command(self, name: str, params: str = "", timeout: float = 5.0) -> dict[str, Any]:
        """Send one command and wait for its response. Events that arrive
        meanwhile are kept for `next_event`.
        """
        self._tokens += 1
        token = f"knock-{self._tokens}"
        self._sock.sendall(encode_netstring({"command": name, "params": params, "token": token}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in self._receive(deadline - time.monotonic()):
                if message.get("response") and message.get("token") == token:
                    return message
                self._pending.append(message)
        raise TimeoutError(f"baresip didn't answer {name!r}")

    def next_event(self, timeout: float) -> dict[str, Any] | None:
        """The next event, or None if none arrives within `timeout`."""
        deadline = time.monotonic() + timeout
        while True:
            while self._pending:
                message = self._pending.pop(0)
                if message.get("event"):
                    return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._pending.extend(self._receive(remaining))

    def _receive(self, timeout: float) -> list[dict[str, Any]]:
        self._sock.settimeout(max(timeout, 0.01))
        try:
            data = self._sock.recv(65536)
        except TimeoutError:
            return []
        if not data:
            raise ConnectionError("baresip closed the control connection")
        return self._reader.feed(data)


def render_accounts(config: SipConfig) -> str:
    """baresip's `accounts` file: one registered account for KNOCK."""
    password = config.password.get_secret_value()
    for label, value in (
        ("server", config.server),
        ("username", config.username),
        ("password", password),
    ):
        if any(char in _UNSAFE_ACCOUNT_CHARS for char in value):
            raise ValueError(f"SIP {label} can't contain any of ; < > \" or line breaks")
    params = [f"regint={600 if config.registration else 0}", "answermode=manual"]
    if password:
        params.insert(0, f"auth_pass={password}")
    return f"<sip:{config.username}@{config.server}:{config.port}>;{';'.join(params)}\n"


# Where distro packages and source builds put baresip's modules.
_MODULE_DIRS = (
    "/usr/lib/baresip/modules",
    "/usr/local/lib/baresip/modules",
    "/usr/lib/x86_64-linux-gnu/baresip/modules",
    "/usr/lib/aarch64-linux-gnu/baresip/modules",
)


def find_module_path(configured: str = "") -> str | None:
    if configured:
        return configured
    return next((d for d in _MODULE_DIRS if Path(d, "ctrl_tcp.so").exists()), None)


def render_config(config: SipConfig, announcement_path: Path, ctrl_port: int) -> str:
    """baresip's main `config` file. Modules are kept to what announcement
    calls need; no sound card is used (`aubridge,nil` swallows the far
    end's audio).
    """
    listen = f"{config.local_ip or '0.0.0.0'}:{config.local_sip_port}"
    module_path = find_module_path(config.baresip_module_path)
    lines = [f"module_path {module_path}"] if module_path else []
    lines += [
        "module g711.so",
        "module aufile.so",
        "module aubridge.so",
        "module menu.so",
        "module account.so",
        "module ctrl_tcp.so",
        "sip_trans_def udp",
        f"sip_listen {listen}",
        f"rtp_ports {config.rtp_port_low}-{config.rtp_port_high}",
        f"audio_source aufile,{announcement_path}",
        "audio_player aubridge,nil",
        "audio_alert aubridge,nil",
        f"ctrl_tcp_listen {CTRL_HOST}:{ctrl_port}",
    ]
    return "\n".join(lines) + "\n"


class BaresipProcess:
    """Owns one baresip helper process: writes its config, starts it, and
    hands out a connected `BaresipControl`.
    """

    def __init__(
        self,
        config: SipConfig,
        directory: Path | None = None,
        binary: str | None = None,
    ) -> None:
        self.config = config
        self.directory = directory or default_baresip_dir()
        self.binary = binary or config.baresip_path
        self.announcement_path = self.directory / "announcement.wav"
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def write_config(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "config").write_text(
            render_config(self.config, self.announcement_path, self.config.baresip_ctrl_port)
        )
        # It holds the SIP password (baresip needs it in plain text): create it
        # owner-only rather than chmod-ing after the write, which left it
        # world-readable for a moment -- and fchmod an existing file too,
        # since O_CREAT's mode only applies to a new one.
        fd = os.open(self.directory / "accounts", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as accounts:
            os.fchmod(accounts.fileno(), 0o600)
            accounts.write(render_accounts(self.config))

    def start(self, ready_timeout: float = 10.0) -> None:
        if self.running:
            return
        executable = shutil.which(self.binary)
        if executable is None:
            raise FileNotFoundError(
                f"baresip not found ({self.binary!r}) -- install it (e.g. apt install baresip-core)"
            )
        self.write_config()
        log = (self.directory / "baresip.log").open("ab")
        self._process = subprocess.Popen(
            [executable, "-f", str(self.directory)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(
                    f"baresip exited during startup (see {self.directory / 'baresip.log'})"
                )
            try:
                socket.create_connection(
                    (CTRL_HOST, self.config.baresip_ctrl_port), timeout=0.5
                ).close()
                return
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise TimeoutError("baresip didn't open its control port in time")

    def connect(self) -> BaresipControl:
        return BaresipControl(self.config.baresip_ctrl_port)

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None
