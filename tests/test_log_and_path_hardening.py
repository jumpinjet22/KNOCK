"""Hardening flagged by CodeQL: user-controlled text in log lines, and the
static-file handler's path check. Behavior for normal input is unchanged;
these pin the hostile cases.
"""

import importlib
import logging
import os

import pytest
from fastapi.testclient import TestClient

import knock.api.app as app_module
from knock.conversation.policy import PolicyEngine
from knock.core.events import VisitorEvent
from knock.core.logsafe import log_safe
from knock.core.orchestrator import Orchestrator
from knock.core.state import SessionState

# -- log_safe ---------------------------------------------------------------------------------


def test_log_safe_matches_repr_for_ordinary_text() -> None:
    assert log_safe("hello there") == repr("hello there")
    assert log_safe(["a", "b"]) == repr(["a", "b"])
    assert log_safe(None) == "None"


@pytest.mark.parametrize("hostile", ["a\nb", "a\r\nb", "x\ry", "end\n"])
def test_log_safe_never_contains_a_line_break(hostile) -> None:
    assert "\n" not in log_safe(hostile) and "\r" not in log_safe(hostile)


def test_log_safe_escapes_line_breaks_in_objects_whose_repr_has_them() -> None:
    class Weird:
        def __repr__(self) -> str:
            return "line one\nINFO forged: admin logged in\rand a carriage return"

    safe = log_safe(Weird())
    assert "\n" not in safe and "\r" not in safe
    assert "\\n" in safe and "\\r" in safe


def test_a_hostile_visitor_cannot_forge_a_log_line_through_the_orchestrator(caplog) -> None:
    hostile = "hello\nINFO:knock.fake:forged line"
    orchestrator = Orchestrator()
    event = VisitorEvent(source="x", text=hostile, timestamp=__import__("datetime").datetime.now())
    state = SessionState(session_id="s")

    with caplog.at_level(logging.INFO, logger="knock.core.orchestrator"):
        orchestrator.respond(event, state=state)

    for record in caplog.records:
        assert "\n" not in record.getMessage()
    assert any("forged line" in r.getMessage() for r in caplog.records)  # kept, just on one line


def test_a_suppressed_reply_is_logged_on_one_line_in_the_same_form(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="knock.conversation.policy"):
        PolicyEngine().apply_style("Nobody's home right now,\nsorry!")

    [record] = caplog.records
    assert "\n" not in record.getMessage()
    message = record.getMessage()
    assert message.startswith("Response flagged as an unsafe disclosure, suppressing: ")
    assert "right now,\\nsorry!" in message  # the break is shown escaped


# -- static file handler ---------------------------------------------------------------------


@pytest.fixture
def spa(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("knock-spa")
    (dist / "app.js").write_text("real-asset")
    (tmp_path / "secret.txt").write_text("do-not-serve-me")
    sibling = tmp_path / "dist-evil"  # shares the dist directory's name as a prefix
    sibling.mkdir()
    (sibling / "loot.txt").write_text("sibling-loot")
    os.symlink(tmp_path / "secret.txt", dist / "link.txt")
    monkeypatch.setenv("KNOCK_WEB_DIST", str(dist))
    importlib.reload(app_module)
    try:
        yield TestClient(app_module.app)
    finally:
        monkeypatch.delenv("KNOCK_WEB_DIST", raising=False)
        importlib.reload(app_module)


@pytest.mark.parametrize(
    "path",
    [
        "/%2Fetc/passwd",  # absolute path as a URL segment
        "/..%2Fdist-evil/loot.txt",  # sibling dir whose name starts with "dist"
        "/link.txt",  # symlink inside dist that points outside it
    ],
)
def test_requests_that_escape_the_dist_dir_get_the_app_not_the_file(spa, path) -> None:
    resp = spa.get(path)

    assert resp.status_code == 200
    assert resp.text == "knock-spa"


def test_real_files_are_still_served(spa) -> None:
    assert spa.get("/app.js").text == "real-asset"
    assert spa.get("/").text == "knock-spa"
    assert spa.get("/some/client/route").text == "knock-spa"


def test_the_refined_intent_is_always_one_of_the_trusted_names(caplog) -> None:
    from knock.core.orchestrator import _LLM_CLASSIFIABLE_INTENTS

    class _LLM:
        name = "fake"

        def __init__(self, reply: str) -> None:
            self.reply = reply

        def generate(self, prompt: str) -> str:
            return self.reply

    for reply, expected in (
        (_LLM_CLASSIFIABLE_INTENTS[0], _LLM_CLASSIFIABLE_INTENTS[0]),
        ("not-a-real-intent\nINFO forged", "unknown"),
    ):
        refined = Orchestrator(llm_provider=_LLM(reply))._refine_unknown_intent("hi there")
        assert refined.intent == expected
