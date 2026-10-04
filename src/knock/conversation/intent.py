_RELIGIOUS_KEYWORDS = [
    "bible",
    "church",
    "gospel",
    "jehovah",
    "ministry",
    "scripture",
    "god's word",
    "good news of",
    "congregation",
]
_POLITICAL_KEYWORDS = [
    "campaign",
    "vote",
    "election",
    "petition",
    "candidate",
    "ballot",
    "running for",
    "city council",
]
_SOLICITING_KEYWORDS = [
    "selling",
    "sell you",
    "subscription",
    "magazine",
    "fundraiser",
    "survey",
    "special offer",
    "free estimate",
    "free quote",
    "door-to-door",
]
_FOOD_KEYWORDS = [
    "pizza",
    "doordash",
    "grubhub",
    "uber eats",
    "postmates",
    "food delivery",
    "deliver food",
    "deliver your food",
    "takeout",
    "take-out",
]
_RIDE_KEYWORDS = [
    "uber",
    "lyft",
    "rideshare",
    "your ride",
    "ride is here",
    "here for your ride",
]
_SIGNATURE_KEYWORDS = ["sign", "signature", "initial"]
# Found via a real production misclassification: "[Name] service company...
# is here" (a technician announcing themselves with no explicit job detail
# like "AC" or "repair") was left entirely to the LLM safety net, which
# guessed "delivery" instead of "service_appointment" -- "service company"
# on its own doesn't contain any word the generic delivery keyword check
# below would ever match, so there was nothing to stop it falling all the
# way to "unknown" and then being misclassified there. A deterministic
# catch here means a wrong LLM guess can't happen for this common phrasing
# at all.
_SERVICE_APPOINTMENT_KEYWORDS = [
    "service company",
    "service appointment",
    "scheduled appointment",
    "here for the appointment",
    "here for my appointment",
    "maintenance visit",
    "repair appointment",
]


def classify_intent(text: str) -> str:
    lowered = text.lower()
    # Checked before the generic package/delivery branch below -- food is
    # time-sensitive (it'll sit out getting cold) and often needs a hand-off
    # or payment, unlike a package that can just be left at the door, so it
    # needs its own intent rather than falling through to "delivery".
    if any(k in lowered for k in _FOOD_KEYWORDS):
        return "food_delivery"
    if any(k in lowered for k in ["package", "delivery", "deliver", "amazon", "ups", "fedex"]):
        # A signature requirement means no one can just leave it at the door
        # -- that's the opposite of the generic delivery response, so it
        # needs its own intent rather than falling through to "delivery".
        if any(k in lowered for k in _SIGNATURE_KEYWORDS):
            return "delivery_signature_required"
        return "delivery"
    # Checked after food/delivery on purpose: "uber eats" already matches
    # _FOOD_KEYWORDS above and returns food_delivery first, so by the time a
    # bare "uber"/"lyft" reaches this check, it only ever means an actual ride.
    if any(k in lowered for k in _RIDE_KEYWORDS):
        return "ride_arrived"
    # Via Orchestrator.respond(), PolicyEngine's rules.json already matches
    # (and now, after a pressure-test pass, matches more broadly than) the
    # two checks below, so text reaching here through that path never
    # contains a bare phrase these two already caught -- see policy.py.
    # They're kept anyway: classify_intent() is a public function other
    # callers can use directly without going through PolicyEngine first,
    # and both have their own direct unit tests pinning this exact
    # behavior (test_classifies_help_keywords_as_emergency,
    # test_classifies_occupancy_probes) as part of its contract, not just
    # Orchestrator's.
    if any(k in lowered for k in ["help", "emergency", "fire", "medical"]):
        return "emergency"
    if any(k in lowered for k in ["are you home", "anyone home"]):
        return "occupancy_probe"
    if any(k in lowered for k in _SERVICE_APPOINTMENT_KEYWORDS):
        return "service_appointment"
    if any(k in lowered for k in _RELIGIOUS_KEYWORDS):
        return "religious_soliciting"
    if any(k in lowered for k in _POLITICAL_KEYWORDS):
        return "political_soliciting"
    if any(k in lowered for k in _SOLICITING_KEYWORDS):
        return "soliciting"
    return "unknown"
