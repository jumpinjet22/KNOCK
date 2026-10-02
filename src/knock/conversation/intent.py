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


def classify_intent(text: str) -> str:
    lowered = text.lower()
    if any(k in lowered for k in ["package", "delivery", "amazon", "ups", "fedex"]):
        # A signature requirement means no one can just leave it at the door
        # -- that's the opposite of the generic delivery response, so it
        # needs its own intent rather than falling through to "delivery".
        if "sign" in lowered:
            return "delivery_signature_required"
        return "delivery"
    if any(k in lowered for k in ["help", "emergency", "fire", "medical"]):
        return "emergency"
    if any(k in lowered for k in ["are you home", "anyone home"]):
        return "occupancy_probe"
    if any(k in lowered for k in _RELIGIOUS_KEYWORDS):
        return "religious_soliciting"
    if any(k in lowered for k in _POLITICAL_KEYWORDS):
        return "political_soliciting"
    if any(k in lowered for k in _SOLICITING_KEYWORDS):
        return "soliciting"
    return "unknown"
