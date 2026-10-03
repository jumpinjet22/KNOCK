RESPONSES = {
    "delivery": "Thanks. You can leave the package by the door.",
    "delivery_signature_required": "Okay, I'll let the homeowner know. Give me a sec.",
    "food_delivery": "Thanks, I'll let them know right away so they can come get it.",
    "emergency": "If this is an emergency, call local emergency services now.",
    "religious_soliciting": "Thanks, but we're not interested in religious materials today.",
    "political_soliciting": (
        "Thanks, but we don't discuss politics or take campaign materials at the door."
    ),
    "soliciting": "Sorry, we don't accept solicitations here. Please don't leave anything.",
    "service_appointment": "Thanks, I'll let them know you're here for your appointment.",
    "person_lookup": "I'll pass along that you're looking for them.",
    "official_visit": "I'll make sure the household is aware you're here.",
    "suspicious_activity": "I've let the household know you're here.",
    "unknown": "Sorry, I can't help with that right now.",
    "blocked_request": "Sorry, I can't share that information.",
}


def response_for(intent: str) -> str:
    return RESPONSES.get(intent, RESPONSES["unknown"])
