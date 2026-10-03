import time

import pytest

from knock.core.script_runner import ScriptRunner


def _write_script(tmp_path, name: str, body: str) -> None:
    (tmp_path / f"{name}.py").write_text(body)


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition never became true")


def test_runs_a_script_to_completion(tmp_path) -> None:
    _write_script(tmp_path, "generate_scenarios", "print('hello from fake script')")
    runner = ScriptRunner(scripts_dir=tmp_path)

    runner.start("generate_scenarios", [])
    _wait_until(lambda: runner.state().status != "running")

    state = runner.state()
    assert state.status == "completed"
    assert state.exit_code == 0
    assert state.script == "generate_scenarios"

    lines, _ = runner.tail()
    assert "hello from fake script" in lines


def test_passes_args_through_to_the_script(tmp_path) -> None:
    _write_script(
        tmp_path,
        "generate_scenarios",
        "import sys\nprint('args:', sys.argv[1:])",
    )
    runner = ScriptRunner(scripts_dir=tmp_path)

    runner.start("generate_scenarios", ["--models", "a,b"])
    _wait_until(lambda: runner.state().status != "running")

    lines, _ = runner.tail()
    assert any("--models" in line and "a,b" in line for line in lines)


def test_nonzero_exit_is_reported_as_failed(tmp_path) -> None:
    _write_script(tmp_path, "generate_scenarios", "import sys\nsys.exit(1)")
    runner = ScriptRunner(scripts_dir=tmp_path)

    runner.start("generate_scenarios", [])
    _wait_until(lambda: runner.state().status != "running")

    state = runner.state()
    assert state.status == "failed"
    assert state.exit_code == 1


def test_starting_while_already_running_raises(tmp_path) -> None:
    _write_script(tmp_path, "generate_scenarios", "import time\ntime.sleep(2)")
    runner = ScriptRunner(scripts_dir=tmp_path)

    runner.start("generate_scenarios", [])
    _wait_until(lambda: runner.state().status == "running")

    with pytest.raises(RuntimeError):
        runner.start("generate_scenarios", [])

    runner.stop()


def test_stop_terminates_a_running_script_and_reports_stopped(tmp_path) -> None:
    _write_script(tmp_path, "generate_scenarios", "import time\ntime.sleep(30)")
    runner = ScriptRunner(scripts_dir=tmp_path)

    runner.start("generate_scenarios", [])
    _wait_until(lambda: runner.state().status == "running")

    runner.stop()
    _wait_until(lambda: runner.state().status != "running", timeout=10.0)

    assert runner.state().status == "stopped"


def test_tail_supports_incremental_reads(tmp_path) -> None:
    _write_script(tmp_path, "generate_scenarios", "print('one')\nprint('two')\nprint('three')")
    runner = ScriptRunner(scripts_dir=tmp_path)

    runner.start("generate_scenarios", [])
    _wait_until(lambda: runner.state().status != "running")

    first_batch, next_after = runner.tail(after=0)
    assert first_batch == ["one", "two", "three"]

    second_batch, _ = runner.tail(after=next_after)
    assert second_batch == []


def test_idle_before_anything_is_started() -> None:
    runner = ScriptRunner()
    state = runner.state()
    assert state.status == "idle"
    assert state.script is None
    assert state.exit_code is None


def test_missing_script_file_is_reported_as_failed(tmp_path) -> None:
    # No generate_scenarios.py written in tmp_path at all -- the python
    # interpreter itself still launches fine, so this exercises the
    # normal nonzero-exit path (python's own "can't open file" error),
    # not the OSError-starting-the-interpreter-itself branch.
    runner = ScriptRunner(scripts_dir=tmp_path)

    runner.start("generate_scenarios", [])
    _wait_until(lambda: runner.state().status != "running")

    state = runner.state()
    assert state.status == "failed"
    assert state.exit_code != 0
