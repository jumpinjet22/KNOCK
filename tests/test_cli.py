import json

from knock.cli.main import main


def test_cli_loop_persists_session_across_turns(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("KNOCK_SESSION_DIR", str(tmp_path))
    inputs = iter(["Hi I have an Amazon package", "quit"])

    main(get_input=lambda _prompt: next(inputs))

    captured = capsys.readouterr().out
    assert "leave the package" in captured.lower()
    assert "Session saved: " in captured
    assert "(1 turn(s))" in captured

    saved_files = list(tmp_path.glob("*.json"))
    assert len(saved_files) == 1
    saved_state = json.loads(saved_files[0].read_text())
    assert saved_state["turn_count"] == 1
    assert saved_state["last_intent"] == "delivery"


def test_cli_exits_immediately_on_blank_input(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("KNOCK_SESSION_DIR", str(tmp_path))

    main(get_input=lambda _prompt: "")

    captured = capsys.readouterr().out
    assert "Session saved: " in captured
    assert "(0 turn(s))" in captured


def test_cli_handles_eof_gracefully(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("KNOCK_SESSION_DIR", str(tmp_path))

    def _raise_eof(_prompt: str) -> str:
        raise EOFError

    main(get_input=_raise_eof)

    captured = capsys.readouterr().out
    assert "Session saved: " in captured
