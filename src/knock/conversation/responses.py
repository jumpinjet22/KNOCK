RESPONSES = {
    "delivery": "Thanks. You can leave the package by the door.",
    "emergency": "If this is an emergency, call local emergency services now.",
    "unknown": "Sorry, I can't help with that right now.",
    "blocked_request": "Sorry, I can't share that information.",
}


def response_for(intent: str) -> str:
    return RESPONSES.get(intent, RESPONSES["unknown"])
