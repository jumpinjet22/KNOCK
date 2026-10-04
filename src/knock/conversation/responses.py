# "delivery_signature_required"/"food_delivery"/"ride_arrived"/"visitation"/
# "service_appointment" deliberately say "I'll pass that along" rather than
# "I'll let them/the homeowner know you're here" -- the latter confirms a
# specific person exists and is present to receive the message in real
# time, which is exactly the occupancy-confirmation pattern this whole
# system is supposed to avoid. "Pass that along" relays the message without
# claiming anyone is actually home to act on it right now.
RESPONSES = {
    "delivery": "Thanks. You can leave the package by the door.",
    "delivery_signature_required": "Okay, I'll pass that along for a signature. Give me a sec.",
    "food_delivery": "Thanks, I'll pass that along right away.",
    "emergency": "If this is an emergency, call local emergency services now.",
    "religious_soliciting": "Thanks, but we're not interested in religious materials today.",
    "political_soliciting": (
        "Thanks, but we don't discuss politics or take campaign materials at the door."
    ),
    "soliciting": "Sorry, we don't accept solicitations here. Please don't leave anything.",
    "ride_arrived": "Thanks, I'll pass that along.",
    "visitation": "Thanks, I'll pass that along!",
    "service_appointment": "Thanks, I'll pass that along.",
    "person_lookup": "I'll pass along that you're looking for them.",
    "official_visit": "I'll make sure the household is aware you're here.",
    "suspicious_activity": "I've let the household know you're here.",
    "unknown": "Sorry, I can't help with that right now.",
    "blocked_request": "Sorry, I can't share that information.",
}


def response_for(intent: str) -> str:
    return RESPONSES.get(intent, RESPONSES["unknown"])
