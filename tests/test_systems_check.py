import asyncio

import pytest

from knock.core.systems_check import (
    is_systems_check_trigger,
    run_systems_check,
    summarize_systems_check,
)

# -- trigger matching ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["systems check", "Systems check.", "SYSTEM CHECK", "system check!", "  systems check  "],
)
def test_is_systems_check_trigger_matches_expected_phrasing(text: str) -> None:
    assert is_systems_check_trigger(text) is True


@pytest.mark.parametrize(
    "text", ["I have a package for you", "", "checking systems", "systems checked"]
)
def test_is_systems_check_trigger_rejects_other_text(text: str) -> None:
    assert is_systems_check_trigger(text) is False


# -- run_systems_check ----------------------------------------------------------


class _OkLLM:
    name = "ok-llm"

    def generate(self, prompt: str) -> str:
        return "ok"


class _FailingLLM:
    name = "failing-llm"

    def generate(self, prompt: str) -> str:
        raise RuntimeError("ollama is down")


class _OkVision:
    name = "ok-vision"

    def describe(self, image: bytes, prompt: str | None = None) -> str:
        return "a person"


class _FailingVision:
    name = "failing-vision"

    def describe(self, image: bytes, prompt: str | None = None) -> str:
        raise RuntimeError("vision model is down")


async def _ok_snapshot() -> bytes | None:
    return b"jpeg-bytes"


async def _no_snapshot() -> bytes | None:
    return None


def _ok_notify(message: str) -> None:
    pass


def _failing_notify(message: str) -> None:
    raise RuntimeError("home assistant is unreachable")


def test_no_providers_configured_returns_no_results() -> None:
    results = asyncio.run(run_systems_check(llm_provider=None))
    assert results == []


def test_all_configured_providers_pass() -> None:
    results = asyncio.run(
        run_systems_check(
            llm_provider=_OkLLM(),
            vision_provider=_OkVision(),
            snapshot_fetcher=_ok_snapshot,
            notify=_ok_notify,
        )
    )
    assert [r.name for r in results] == ["LLM", "Vision", "Notifications"]
    assert all(r.ok for r in results)


def test_one_provider_failing_does_not_stop_the_others() -> None:
    results = asyncio.run(
        run_systems_check(
            llm_provider=_FailingLLM(),
            vision_provider=_OkVision(),
            snapshot_fetcher=_ok_snapshot,
            notify=_ok_notify,
        )
    )
    by_name = {r.name: r.ok for r in results}
    assert by_name == {"LLM": False, "Vision": True, "Notifications": True}


def test_vision_check_fails_when_snapshot_fetch_returns_none() -> None:
    results = asyncio.run(
        run_systems_check(
            llm_provider=None, vision_provider=_OkVision(), snapshot_fetcher=_no_snapshot
        )
    )
    assert len(results) == 1
    assert results[0].name == "Vision"
    assert results[0].ok is False


def test_vision_check_fails_when_describe_raises() -> None:
    results = asyncio.run(
        run_systems_check(
            llm_provider=None, vision_provider=_FailingVision(), snapshot_fetcher=_ok_snapshot
        )
    )
    assert results[0].ok is False


def test_notifications_check_fails_when_notify_raises() -> None:
    results = asyncio.run(run_systems_check(llm_provider=None, notify=_failing_notify))
    assert results[0].ok is False


# -- summarize_systems_check ----------------------------------------------------


def test_summarize_with_no_results() -> None:
    assert summarize_systems_check([]) == "No systems configured to check."


def test_summarize_all_passing() -> None:
    results = asyncio.run(run_systems_check(llm_provider=_OkLLM(), notify=_ok_notify))
    assert summarize_systems_check(results) == "All systems operational."


def test_summarize_one_failure_names_it_and_notes_the_rest_are_fine() -> None:
    results = asyncio.run(
        run_systems_check(
            llm_provider=None,
            vision_provider=_FailingVision(),
            snapshot_fetcher=_ok_snapshot,
            notify=_ok_notify,
        )
    )
    assert summarize_systems_check(results) == "Vision check failed, everything else is fine."


def test_summarize_everything_failing() -> None:
    results = asyncio.run(run_systems_check(llm_provider=_FailingLLM(), notify=_failing_notify))
    assert summarize_systems_check(results) == "LLM, Notifications check failed."
