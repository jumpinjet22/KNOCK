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


def test_classifies_religious_canvassing() -> None:
    assert (
        classify_intent("Do you have a moment to talk about the Bible?") == "religious_soliciting"
    )
    assert classify_intent("We're from the local church ministry") == "religious_soliciting"
    assert classify_intent("I'd like to share the gospel with you") == "religious_soliciting"


def test_classifies_political_canvassing() -> None:
    assert (
        classify_intent("I'm here for the campaign, can I get your vote?") == "political_soliciting"
    )
    assert classify_intent("Would you sign this petition?") == "political_soliciting"


def test_classifies_general_soliciting() -> None:
    assert classify_intent("I'm selling magazine subscriptions") == "soliciting"
    assert classify_intent("We have a special offer on lawn care") == "soliciting"
    assert classify_intent("Can I get you a free quote on gutters?") == "soliciting"


def test_falls_back_to_unknown() -> None:
    assert classify_intent("Do you like jazz?") == "unknown"
