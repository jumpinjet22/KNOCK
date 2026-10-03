from knock.conversation.intent import classify_intent


def test_classifies_package_keywords_as_delivery() -> None:
    assert classify_intent("I have a package for you") == "delivery"
    assert classify_intent("Amazon delivery") == "delivery"
    assert classify_intent("UPS here") == "delivery"
    assert classify_intent("FedEx drop-off") == "delivery"


def test_a_signature_requirement_overrides_the_generic_delivery_intent() -> None:
    assert classify_intent("I need a signature for this package") == "delivery_signature_required"
    assert classify_intent("Can you sign for this delivery?") == "delivery_signature_required"
    # "initial" is a common real-world phrasing for the same requirement
    # (e.g. UPS/FedEx drivers often say "initial" rather than "sign") that
    # doesn't contain "sign" as a substring -- found via live LLM-refinement
    # probing, where this phrasing matched the generic "delivery" keyword
    # branch and never reached the LLM safety net at all (which only runs
    # when the keyword classifier lands on "unknown").
    assert (
        classify_intent("I need someone to initial for this package real quick")
        == "delivery_signature_required"
    )


def test_classifies_food_keywords_as_food_delivery_not_generic_delivery() -> None:
    # Food is time-sensitive (it sits out getting cold) in a way a package
    # isn't, so it needs its own intent rather than falling through to the
    # generic "leave it at the door" delivery response.
    assert classify_intent("Hi I have a pizza delivery") == "food_delivery"
    assert classify_intent("DoorDash order for you") == "food_delivery"
    assert classify_intent("Grubhub delivery here") == "food_delivery"
    assert classify_intent("Uber Eats order") == "food_delivery"


def test_classifies_deliver_food_phrasing_as_food_delivery() -> None:
    # Live transcripts came back as "I am here to deliver food" -- a real
    # spoken phrasing that matched neither "food delivery" (reversed word
    # order) nor the generic "delivery" keyword (verb "deliver", not the
    # noun), so it silently fell through to "unknown" every time.
    assert classify_intent("I am here to deliver food.") == "food_delivery"
    assert classify_intent("I'm here to deliver food.") == "food_delivery"
    assert classify_intent("I have your food delivery") == "food_delivery"


def test_classifies_deliver_as_generic_delivery() -> None:
    assert classify_intent("I need you to deliver this") == "delivery"


def test_classifies_ride_keywords_as_ride_arrived() -> None:
    assert classify_intent("Your Uber is here") == "ride_arrived"
    assert classify_intent("I'm here for your Lyft") == "ride_arrived"
    assert classify_intent("Your ride is here") == "ride_arrived"


def test_uber_eats_is_food_delivery_not_ride_arrived() -> None:
    # "uber eats" matches _FOOD_KEYWORDS and is checked first -- a bare
    # "uber"/"lyft" only ever reaches the ride check once food/delivery
    # keywords have already ruled themselves out.
    assert classify_intent("I'm your Uber Eats driver") == "food_delivery"


def test_classifies_help_keywords_as_emergency() -> None:
    assert classify_intent("help!") == "emergency"
    assert classify_intent("medical emergency") == "emergency"
    assert classify_intent("fire!") == "emergency"


def test_classifies_occupancy_probes() -> None:
    assert classify_intent("are you home") == "occupancy_probe"
    assert classify_intent("is anyone home") == "occupancy_probe"


def test_classifies_service_company_phrasing_as_service_appointment() -> None:
    # Found via a real production misclassification: a technician
    # announcing themselves as "[Name] service company... is here" with no
    # explicit job detail (no "AC", "repair", etc.) matched no keyword at
    # all, fell to the LLM safety net, and got guessed as "delivery"
    # instead. None of these phrasings overlap with any delivery/food/ride
    # keyword, so this needs its own deterministic catch rather than
    # relying on the LLM to guess right every time.
    assert classify_intent("Weeks Service Company is here.") == "service_appointment"
    assert classify_intent("I'm here for my scheduled appointment") == "service_appointment"
    assert classify_intent("Here for the appointment") == "service_appointment"


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
