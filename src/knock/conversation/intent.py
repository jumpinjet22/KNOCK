def classify_intent(text: str) -> str:
    lowered = text.lower()
    if any(k in lowered for k in ["package", "delivery", "amazon", "ups", "fedex"]):
        return "delivery"
    if any(k in lowered for k in ["help", "emergency", "fire", "medical"]):
        return "emergency"
    if any(k in lowered for k in ["are you home", "anyone home"]):
        return "occupancy_probe"
    return "unknown"
