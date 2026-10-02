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
    return "unknown"
