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
    "takeout",
    "take-out",
]


def classify_intent(text: str) -> str:
    lowered = text.lower()
    # Checked before the generic package/delivery branch below -- food is
    # time-sensitive (it'll sit out getting cold) and often needs a hand-off
    # or payment, unlike a package that can just be left at the door, so it
    # needs its own intent rather than falling through to "delivery".
    if any(k in lowered for k in _FOOD_KEYWORDS):
        return "food_delivery"
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
