from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    """One data-defined policy rule.

    A rule contributes `weight` to its `flag`'s confidence whenever any of
    its `phrases` appears in the visitor's text. `action` says what happens
    once that flag crosses the rule set's confidence threshold: "escalate"
    (hand off to a human/emergency response) or "block" (refuse and fall
    back to a safe canned response).
    """

    id: str
    flag: str
    action: str
    phrases: tuple[str, ...]
    weight: float = 1.0


@dataclass(frozen=True)
class RuleSet:
    """A loaded, ready-to-evaluate collection of policy rules."""

    threshold: float
    rules: tuple[Rule, ...]

    @staticmethod
    def from_dict(data: dict) -> RuleSet:
        rules = tuple(
            Rule(
                id=raw["id"],
                flag=raw["flag"],
                action=raw["action"],
                phrases=tuple(phrase.lower() for phrase in raw["phrases"]),
                weight=float(raw.get("weight", 1.0)),
            )
            for raw in data["rules"]
        )
        return RuleSet(threshold=float(data.get("threshold", 1.0)), rules=rules)

    @staticmethod
    def load(path: Path | str) -> RuleSet:
        """Load a rule set from a JSON file at an arbitrary path."""
        raw_text = Path(path).read_text(encoding="utf-8")
        return RuleSet.from_dict(json.loads(raw_text))

    @staticmethod
    def default() -> RuleSet:
        """Load KNOCK's bundled default rule set."""
        raw_text = (
            resources.files("knock.conversation").joinpath("rules.json").read_text(encoding="utf-8")
        )
        return RuleSet.from_dict(json.loads(raw_text))
