from knock.conversation.intent import classify_intent


def test_classifies_package_keywords_as_delivery() -> None:
    assert classify_intent("I have a package for you") == "delivery"
    assert classify_intent("Amazon delivery") == "delivery"
    assert classify_intent("UPS here") == "delivery"
    assert classify_intent("FedEx drop-off") == "delivery"


def test_a_signature_requirement_overrides_the_generic_delivery_intent() -> None:
    assert classify_intent("I need a signature for this package") == "delivery_signature_required"
    assert classify_intent("Can you sign for this delivery?") == "delivery_signature_required"


def test_classifies_help_keywords_as_emergency() -> None:
    assert classify_intent("help!") == "emergency"
    assert classify_intent("medical emergency") == "emergency"
    assert classify_intent("fire!") == "emergency"


def test_classifies_occupancy_probes() -> None:
    assert classify_intent("are you home") == "occupancy_probe"
    assert classify_intent("is anyone home") == "occupancy_probe"


def test_falls_back_to_unknown() -> None:
    assert classify_intent("Do you like jazz?") == "unknown"
