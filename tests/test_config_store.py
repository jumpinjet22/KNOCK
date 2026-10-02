import json
import stat

from knock.core.config_store import ConfigStore


def test_get_section_returns_empty_dict_when_unset(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    assert store.get_section("ollama") == {}


def test_set_and_get_section_round_trips(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.set_section("ollama", {"host": "192.168.1.1", "port": 11434})

    assert store.get_section("ollama") == {"host": "192.168.1.1", "port": 11434}


def test_set_section_replaces_wholesale(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.set_section("ollama", {"host": "a", "port": 1})
    store.set_section("ollama", {"host": "b"})

    assert store.get_section("ollama") == {"host": "b"}


def test_update_section_merges_into_existing(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.set_section("ollama", {"host": "a", "port": 1})
    store.update_section("ollama", {"port": 2})

    assert store.get_section("ollama") == {"host": "a", "port": 2}


def test_sections_are_independent(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.set_section("ollama", {"host": "a"})
    store.set_section("vision", {"host": "b"})

    assert store.get_section("ollama") == {"host": "a"}
    assert store.get_section("vision") == {"host": "b"}


def test_all_sections_returns_everything(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.set_section("ollama", {"host": "a"})
    store.set_section("vision", {"host": "b"})

    assert store.all_sections() == {"ollama": {"host": "a"}, "vision": {"host": "b"}}


def test_file_is_created_with_owner_only_permissions(tmp_path) -> None:
    path = tmp_path / "config.json"
    ConfigStore(path).set_section("ollama", {"host": "a"})

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == (stat.S_IRUSR | stat.S_IWUSR)


def test_file_contains_a_version_key(tmp_path) -> None:
    path = tmp_path / "config.json"
    ConfigStore(path).set_section("ollama", {"host": "a"})

    data = json.loads(path.read_text())
    assert data["version"] == 1


def test_creates_parent_directory_if_missing(tmp_path) -> None:
    nested = tmp_path / "nested" / "dir"
    ConfigStore(nested / "config.json").set_section("ollama", {"host": "a"})

    assert (nested / "config.json").exists()


def test_tolerates_missing_file(tmp_path) -> None:
    store = ConfigStore(tmp_path / "does-not-exist.json")
    assert store.get_section("ollama") == {}
    assert store.all_sections() == {}


def test_tolerates_corrupt_json(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("not valid json {{{")

    store = ConfigStore(path)
    assert store.get_section("ollama") == {}


def test_default_path_honors_env_var(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOCK_CONFIG_PATH", str(tmp_path / "custom.json"))
    store = ConfigStore()
    assert store.path == tmp_path / "custom.json"
