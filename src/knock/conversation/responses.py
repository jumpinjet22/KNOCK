RESPONSES = {
    "delivery": "Thanks. You can leave the package by the door.",
    "delivery_signature_required": "Okay, I'll let the homeowner know. Give me a sec.",
    "emergency": "If this is an emergency, call local emergency services now.",
    "religious_soliciting": "Thanks, but we're not interested in religious materials today.",
    "political_soliciting": (
        "Thanks, but we don't discuss politics or take campaign materials at the door."
    ),
    "soliciting": "Sorry, we don't accept solicitations here. Please don't leave anything.",
    "unknown": "Sorry, I can't help with that right now.",
    "blocked_request": "Sorry, I can't share that information.",
}


def response_for(intent: str) -> str:
    return RESPONSES.get(intent, RESPONSES["unknown"])
