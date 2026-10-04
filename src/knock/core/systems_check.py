"""Voice-triggered diagnostic: saying "systems check" (or "system check") as
the first thing to the doorbell runs a real functional test of whatever's
actually configured on that bridge, then speaks a short result back -- "All
systems operational," or names what's down.

Deliberately not part of rules.json/PolicyEngine -- that vocabulary
(block/escalate) is for classifying visitor *safety* behavior, a different
concept from an operator diagnostic command, and mixing the two would make
rules.json harder to reason about for both purposes. Trigger matching below
is a simple normalized phrase match, not LLM-interpreted, consistent with
how every other safety/operator-adjacent phrase match in this codebase
works.

STT isn't separately tested here: correctly transcribing the trigger phrase
*is* the STT test. TTS isn't either: speaking this module's own summary
back *is* the TTS test. Both happen at the bridge call site, not in here.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from knock.providers.llm.base import LLMProvider
from knock.providers.vision.base import VisionProvider

_TRIGGER_PHRASES = {"systems check", "system check"}
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")
_WHITESPACE = re.compile(r"\s+")


def is_systems_check_trigger(text: str) -> bool:
    normalized = _WHITESPACE.sub(" ", _NON_ALNUM.sub("", text.lower())).strip()
    return normalized in _TRIGGER_PHRASES


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


async def run_systems_check(
    *,
    llm_provider: LLMProvider | None,
    vision_provider: VisionProvider | None = None,
    snapshot_fetcher: Callable[[], Awaitable[bytes | None]] | None = None,
    notify: Callable[[str], None] | None = None,
) -> list[CheckResult]:
    """Only the providers a caller passes are checked, so each bridge only
    tests what it actually has configured (e.g. HomeAssistantBridge has no
    camera, so it never passes vision_provider/snapshot_fetcher). One
    provider failing doesn't stop the others from being checked.
    """
    results: list[CheckResult] = []

    if llm_provider is not None:
        try:
            llm_provider.generate("Reply with the single word: ok")
            results.append(CheckResult("LLM", True))
        except Exception as exc:  # noqa: BLE001 - each check is independent
            results.append(CheckResult("LLM", False, str(exc)))

    if vision_provider is not None and snapshot_fetcher is not None:
        try:
            snapshot = await snapshot_fetcher()
            if snapshot is None:
                results.append(CheckResult("Vision", False, "no snapshot returned"))
            else:
                vision_provider.describe(snapshot)
                results.append(CheckResult("Vision", True))
        except Exception as exc:  # noqa: BLE001 - each check is independent
            results.append(CheckResult("Vision", False, str(exc)))

    if notify is not None:
        try:
            notify("KNOCK systems check: all good.")
            results.append(CheckResult("Notifications", True))
        except Exception as exc:  # noqa: BLE001 - each check is independent
            results.append(CheckResult("Notifications", False, str(exc)))

    return results


def summarize_systems_check(results: list[CheckResult]) -> str:
    """A plain deterministic string builder, not LLM-phrased -- keeps this
    fast and always-correct regardless of model behavior.
    """
    if not results:
        return "No systems configured to check."
    failed = [result.name for result in results if not result.ok]
    if not failed:
        return "All systems operational."
    if len(failed) == len(results):
        return f"{', '.join(failed)} check failed."
    return f"{', '.join(failed)} check failed, everything else is fine."
