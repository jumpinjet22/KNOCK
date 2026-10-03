import sys
import time

from knock.config import MqttConfig
from knock.core.config_store import ConfigStore
from knock.core.supervisor import BridgeSupervisor, build_bridge_env


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _sleepy_command(seconds: float = 30.0) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _printing_command(text: str) -> list[str]:
    return [sys.executable, "-c", f"print({text!r}, flush=True)"]


def _failing_command() -> list[str]:
    return [sys.executable, "-c", "import sys; sys.exit(1)"]


# -- env flattening -----------------------------------------------------------------


def test_build_bridge_env_includes_resolved_store_values(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.update_section("mqtt", {"host": "10.0.0.5", "port": 1884, "client_id": "my-knock"})

    env = build_bridge_env("mqtt", store)

    assert env["KNOCK_MQTT_HOST"] == "10.0.0.5"
    assert env["KNOCK_MQTT_PORT"] == "1884"
    assert env["KNOCK_MQTT_CLIENT_ID"] == "my-knock"


def test_build_bridge_env_unwraps_secret_values(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.update_section("mqtt", {"password": "hunter2"})

    env = build_bridge_env("mqtt", store)

    assert env["KNOCK_MQTT_PASSWORD"] == "hunter2"


def test_build_bridge_env_flattens_lists_as_csv(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.update_section("frigate", {"trigger_labels": ["person", "car"]})

    env = build_bridge_env("frigate", store)

    assert env["KNOCK_FRIGATE_TRIGGER_LABELS"] == "person,car"


def test_build_bridge_env_drops_env_var_when_resolved_value_is_none(tmp_path) -> None:
    # No username configured anywhere -- MqttConfig.from_sources() resolves
    # to None, which must not become a stray "KNOCK_MQTT_USERNAME=None" in
    # the child's environment.
    store = ConfigStore(tmp_path / "config.json")

    env = build_bridge_env("mqtt", store)

    assert "KNOCK_MQTT_USERNAME" not in env


# -- lifecycle: start/stop/restart ---------------------------------------------------


def test_start_transitions_to_running(tmp_path) -> None:
    supervisor = BridgeSupervisor(
        ConfigStore(tmp_path / "config.json"),
        commands={"mqtt": _sleepy_command()},
    )
    supervisor.start("mqtt")

    assert _wait_until(lambda: supervisor.describe("mqtt").status == "running")
    info = supervisor.describe("mqtt")
    assert info.pid is not None

    supervisor.stop("mqtt")
    assert _wait_until(lambda: supervisor.describe("mqtt").status == "stopped")


def test_starting_an_already_running_bridge_is_a_no_op(tmp_path) -> None:
    supervisor = BridgeSupervisor(
        ConfigStore(tmp_path / "config.json"),
        commands={"mqtt": _sleepy_command()},
    )
    supervisor.start("mqtt")
    assert _wait_until(lambda: supervisor.describe("mqtt").status == "running")
    pid_before = supervisor.describe("mqtt").pid

    supervisor.start("mqtt")
    time.sleep(0.1)
    assert supervisor.describe("mqtt").pid == pid_before

    supervisor.stop("mqtt")


def test_stop_on_a_never_started_bridge_leaves_it_stopped(tmp_path) -> None:
    supervisor = BridgeSupervisor(
        ConfigStore(tmp_path / "config.json"), commands={"mqtt": _sleepy_command()}
    )
    supervisor.stop("mqtt")
    assert supervisor.describe("mqtt").status == "stopped"


def test_restart_replaces_the_running_process(tmp_path) -> None:
    supervisor = BridgeSupervisor(
        ConfigStore(tmp_path / "config.json"),
        commands={"mqtt": _sleepy_command()},
    )
    supervisor.start("mqtt")
    assert _wait_until(lambda: supervisor.describe("mqtt").status == "running")
    pid_before = supervisor.describe("mqtt").pid

    supervisor.restart("mqtt")
    assert _wait_until(lambda: supervisor.describe("mqtt").status == "running")
    pid_after = supervisor.describe("mqtt").pid

    assert pid_after != pid_before
    supervisor.stop("mqtt")


def test_shutdown_all_stops_every_bridge(tmp_path) -> None:
    supervisor = BridgeSupervisor(
        ConfigStore(tmp_path / "config.json"),
        commands={"mqtt": _sleepy_command(), "unifi": _sleepy_command()},
    )
    supervisor.start("mqtt")
    supervisor.start("unifi")
    assert _wait_until(lambda: supervisor.describe("mqtt").status == "running")
    assert _wait_until(lambda: supervisor.describe("unifi").status == "running")

    supervisor.shutdown_all()

    assert supervisor.describe("mqtt").status == "stopped"
    assert supervisor.describe("unifi").status == "stopped"


# -- crash-loop backoff ---------------------------------------------------------------


def test_crashing_bridge_restarts_with_backoff_then_gives_up(tmp_path) -> None:
    supervisor = BridgeSupervisor(
        ConfigStore(tmp_path / "config.json"),
        commands={"mqtt": _failing_command()},
        restart_base_delay=0.01,
        restart_max_delay=0.05,
        max_restart_attempts=2,
    )
    supervisor.start("mqtt")

    assert _wait_until(lambda: supervisor.describe("mqtt").status == "crashed", timeout=5.0)
    assert supervisor.describe("mqtt").restart_count == 3


def test_crashing_bridge_logs_are_captured(tmp_path) -> None:
    supervisor = BridgeSupervisor(
        ConfigStore(tmp_path / "config.json"),
        commands={"mqtt": _failing_command()},
        restart_base_delay=0.01,
        restart_max_delay=0.05,
        max_restart_attempts=1,
    )
    supervisor.start("mqtt")

    assert _wait_until(lambda: supervisor.describe("mqtt").status == "crashed", timeout=5.0)
    lines, _ = supervisor.tail("mqtt")
    assert any("giving up" in line for line in lines)


# -- logs -------------------------------------------------------------------------------


def test_tail_returns_lines_and_a_cursor(tmp_path) -> None:
    supervisor = BridgeSupervisor(
        ConfigStore(tmp_path / "config.json"),
        commands={"mqtt": _printing_command("hello")},
    )
    supervisor.start("mqtt")

    def _has_output() -> bool:
        lines, _ = supervisor.tail("mqtt")
        return any("hello" in line for line in lines)

    assert _wait_until(_has_output)
    lines, next_after = supervisor.tail("mqtt")
    assert any("hello" in line for line in lines)

    more_lines, _ = supervisor.tail("mqtt", after=next_after)
    assert more_lines == []


def test_mqtt_config_from_sources_is_used_by_build_bridge_env(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    resolved = MqttConfig.from_sources(store)
    env = build_bridge_env("mqtt", store)
    assert env["KNOCK_MQTT_HOST"] == resolved.host


# -- autostart --------------------------------------------------------------------


def test_autostart_defaults_to_false(tmp_path) -> None:
    supervisor = BridgeSupervisor(
        ConfigStore(tmp_path / "config.json"), commands={"mqtt": _sleepy_command()}
    )
    assert supervisor.get_autostart("mqtt") is False
    assert supervisor.describe("mqtt").autostart is False


def test_set_autostart_persists_across_supervisor_instances(tmp_path) -> None:
    store_path = tmp_path / "config.json"
    first = BridgeSupervisor(ConfigStore(store_path), commands={"mqtt": _sleepy_command()})
    first.set_autostart("mqtt", True)

    second = BridgeSupervisor(ConfigStore(store_path), commands={"mqtt": _sleepy_command()})
    assert second.get_autostart("mqtt") is True
    assert second.describe("mqtt").autostart is True


def test_set_autostart_only_affects_the_named_bridge(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    supervisor = BridgeSupervisor(
        store, commands={"mqtt": _sleepy_command(), "unifi": _sleepy_command()}
    )
    supervisor.set_autostart("mqtt", True)

    assert supervisor.get_autostart("mqtt") is True
    assert supervisor.get_autostart("unifi") is False


def test_start_autostart_enabled_starts_only_flagged_bridges(tmp_path) -> None:
    supervisor = BridgeSupervisor(
        ConfigStore(tmp_path / "config.json"),
        commands={"mqtt": _sleepy_command(), "unifi": _sleepy_command()},
    )
    supervisor.set_autostart("mqtt", True)

    supervisor.start_autostart_enabled()

    assert _wait_until(lambda: supervisor.describe("mqtt").status == "running")
    assert supervisor.describe("unifi").status == "stopped"
    supervisor.shutdown_all()


def test_start_autostart_enabled_is_a_no_op_when_nothing_is_flagged(tmp_path) -> None:
    supervisor = BridgeSupervisor(
        ConfigStore(tmp_path / "config.json"), commands={"mqtt": _sleepy_command()}
    )
    supervisor.start_autostart_enabled()
    time.sleep(0.1)
    assert supervisor.describe("mqtt").status == "stopped"
