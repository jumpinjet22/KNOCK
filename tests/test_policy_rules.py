import json

from knock.conversation.policy import PolicyEngine
from knock.conversation.rules import Rule, RuleSet


def test_default_rule_set_loads_from_bundled_json() -> None:
    rule_set = RuleSet.default()
    assert rule_set.threshold == 1.0
    assert {rule.flag for rule in rule_set.rules} == {
        "occupancy",
        "schedule",
        "unlock",
        "emergency",
    }


def test_rule_set_loads_from_an_arbitrary_file(tmp_path) -> None:
    custom = {
        "threshold": 1.0,
        "rules": [
            {
                "id": "custom-greeting",
                "flag": "greeting",
                "action": "block",
                "weight": 1.0,
                "phrases": ["hello there"],
            }
        ],
    }
    path = tmp_path / "custom-rules.json"
    path.write_text(json.dumps(custom))

    rule_set = RuleSet.load(path)

    assert rule_set.rules[0].id == "custom-greeting"
    assert rule_set.rules[0].phrases == ("hello there",)


def test_confidence_accumulates_across_multiple_rules_for_the_same_flag() -> None:
    # Two weak signals for the same flag, neither enough alone, but together
    # they cross the threshold -- this is the graduated-confidence behavior
    # the old flat keyword-OR matching couldn't express.
    rule_set = RuleSet(
        threshold=1.0,
        rules=(
            Rule(
                id="weak-a", flag="suspicious", action="block", phrases=("van outside",), weight=0.6
            ),
            Rule(
                id="weak-b", flag="suspicious", action="block", phrases=("no uniform",), weight=0.6
            ),
        ),
    )
    engine = PolicyEngine(rule_set=rule_set)

    single_signal = engine.evaluate("There's a van outside")
    assert single_signal.confidence["suspicious"] == 0.6
    assert single_signal.flags == []  # below threshold, not actionable alone
    assert single_signal.allowed is True

    both_signals = engine.evaluate("There's a van outside and no uniform")
    # confidence is capped at 1.0 -- it's a bounded score, not a raw sum
    assert both_signals.confidence["suspicious"] == 1.0
    assert both_signals.flags == ["suspicious"]
    assert both_signals.allowed is False
    assert both_signals.reason == "blocked_request"
    assert set(both_signals.matched_rule_ids) == {"weak-a", "weak-b"}


def test_escalate_action_takes_priority_over_block() -> None:
    rule_set = RuleSet(
        threshold=1.0,
        rules=(
            Rule(
                id="block-rule", flag="probe", action="block", phrases=("are you home",), weight=1.0
            ),
            Rule(
                id="escalate-rule", flag="danger", action="escalate", phrases=("fire",), weight=1.0
            ),
        ),
    )
    engine = PolicyEngine(rule_set=rule_set)

    decision = engine.evaluate("Fire! Are you home?")

    assert decision.reason == "emergency"
    assert decision.allowed is True
    assert set(decision.flags) == {"probe", "danger"}
