from knock.core.orchestrator import _LLM_CLASSIFIABLE_INTENTS
from knock.core.scenario_descriptions import SCENARIO_DESCRIPTIONS


def test_every_classifiable_intent_has_a_scenario_description() -> None:
    missing = [i for i in [*_LLM_CLASSIFIABLE_INTENTS, "unknown"] if i not in SCENARIO_DESCRIPTIONS]
    assert not missing
