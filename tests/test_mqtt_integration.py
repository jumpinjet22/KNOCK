"""MQTT and Frigate bridges against a real Mosquitto broker.

The unit tests in test_mqtt_bridge.py / test_frigate_bridge.py call the paho
callbacks directly with a `MagicMock` client, so nothing there proves the
bridges actually connect, subscribe, and publish through a real broker with
the real paho client. These start a password-protected Mosquitto on a free
local port and drive each bridge end to end.

Opt-in (KNOCK_TEST_MQTT=1) since it needs Mosquitto installed.
"""

import getpass
import json
import logging
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator

import paho.mqtt.client as mqtt
import pytest
from pydantic import SecretStr

from knock.config import FrigateConfig, MqttConfig
from knock.core.session_store import JSONFileSessionStore
from knock.integrations.frigate import FrigateBridge
from knock.integrations.mqtt import MqttBridge

pytestmark = pytest.mark.skipif(
    os.environ.get("KNOCK_TEST_MQTT") != "1" or shutil.which("mosquitto") is None,
    reason="opt-in: set KNOCK_TEST_MQTT=1 with mosquitto installed",
)

USER, PASSWORD = "knock", "door-secret"
TIMEOUT = 5.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def broker_port(tmp_path) -> Iterator[int]:
    port = _free_port()
    passwd = tmp_path / "passwd"
    subprocess.run(["mosquitto_passwd", "-b", "-c", str(passwd), USER, PASSWORD], check=True)
    conf = tmp_path / "mosquitto.conf"
    # `user`: when started as root, Mosquitto otherwise drops to a
    # "mosquitto" user that can't read the password file under tmp_path.
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous false\npassword_file {passwd}\n"
        f"persistence false\nuser {getpass.getuser()}\n"
    )
    broker = subprocess.Popen(
        ["mosquitto", "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + TIMEOUT
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        yield port
    finally:
        broker.terminate()
        broker.wait(timeout=5)


class _Listener:
    """A plain paho client subscribed to `topic`, collecting what arrives."""

    def __init__(self, port: int, topic: str) -> None:
        self.messages: queue.Queue[dict] = queue.Queue()
        subscribed = threading.Event()
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self.client.username_pw_set(USER, PASSWORD)
        self.client.on_connect = lambda c, *_: c.subscribe(topic)
        self.client.on_subscribe = lambda *_: subscribed.set()
        self.client.on_message = lambda c, u, msg: self.messages.put(json.loads(msg.payload))
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()
        assert subscribed.wait(TIMEOUT), "listener never subscribed"

    def publish(self, topic: str, payload: dict) -> None:
        self.client.publish(topic, json.dumps(payload)).wait_for_publish(TIMEOUT)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def _start(bridge_client: mqtt.Client, connect) -> threading.Event:
    """Connect a bridge's own paho client in the background; returns its
    subscribed event so a test publishes only once the bridge is listening."""
    subscribed = threading.Event()
    bridge_client.on_subscribe = lambda *_: subscribed.set()
    connect()
    bridge_client.loop_start()
    return subscribed


def test_mqtt_bridge_answers_a_visitor_over_a_real_broker(tmp_path, broker_port) -> None:
    bridge = MqttBridge(
        config=MqttConfig(
            port=broker_port, username=USER, password=SecretStr(PASSWORD), client_id="knock-test"
        ),
        session_store=JSONFileSessionStore(tmp_path),
    )
    listener = _Listener(broker_port, "knock/responses")
    try:
        assert _start(bridge.client, bridge.connect).wait(TIMEOUT), "bridge never subscribed"

        listener.publish("knock/events", {"text": "Hi I have a package", "session_id": "visit-1"})

        reply = listener.messages.get(timeout=TIMEOUT)
        assert "leave the package" in reply["text"].lower()
        assert bridge.session_store.load("visit-1") is not None
    finally:
        bridge.client.loop_stop()
        bridge.disconnect()
        listener.close()


def _frigate_event(event_type: str) -> dict:
    after = {
        "id": "1700000000.123456-abc123",
        "camera": "front_door",
        "label": "person",
        "entered_zones": ["porch"],
        "has_snapshot": False,
    }
    return {"type": event_type, "before": dict(after), "after": after}


def test_frigate_bridge_answers_a_new_detection_over_a_real_broker(tmp_path, broker_port) -> None:
    bridge = FrigateBridge(
        config=FrigateConfig(
            mqtt_port=broker_port,
            username=USER,
            password=SecretStr(PASSWORD),
            client_id="knock-frigate-test",
        ),
        session_store=JSONFileSessionStore(tmp_path),
    )
    listener = _Listener(broker_port, "knock/responses")
    try:
        assert _start(bridge.client, bridge.connect).wait(TIMEOUT), "bridge never subscribed"

        # Frigate publishes "update"/"end" for the same object too -- only
        # "new" should get an answer.
        listener.publish("frigate/events", _frigate_event("update"))
        listener.publish("frigate/events", _frigate_event("new"))

        reply = listener.messages.get(timeout=TIMEOUT)
        assert reply["text"]
        assert bridge.session_store.load("frigate-front_door") is not None
        with pytest.raises(queue.Empty):
            listener.messages.get(timeout=0.5)
    finally:
        bridge.client.loop_stop()
        bridge.disconnect()
        listener.close()


def test_mqtt_bridge_logs_a_refused_connection(tmp_path, broker_port, caplog) -> None:
    caplog.set_level(logging.ERROR, logger="knock.integrations.mqtt")
    bridge = MqttBridge(
        config=MqttConfig(
            port=broker_port, username=USER, password=SecretStr("wrong"), client_id="knock-test"
        ),
        session_store=JSONFileSessionStore(tmp_path),
    )
    subscribed = _start(bridge.client, bridge.connect)
    try:
        deadline = time.monotonic() + TIMEOUT
        while "refused the connection" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.05)

        assert "refused the connection" in caplog.text
        assert not subscribed.is_set()
    finally:
        bridge.client.loop_stop()
        bridge.disconnect()
