"""Per-category situation descriptions for synthetic scenario generation
(`scripts/generate_scenarios.py`) -- deliberately separate from
`knock.core.orchestrator._INTENT_DESCRIPTIONS`, which exists for a
different purpose entirely (telling the *response-generation* LLM how the
doorbell assistant should reply) and embeds quoted example replies like
'say something like "Thanks, but we are not interested..."'.

Splicing that reply-phrasing text into a scenario-generation prompt was a
real, confirmed bug: a generating model has no speaker disambiguation, so
it would parrot the quoted assistant reply back as if it were visitor
speech. Found live -- roughly 37% of one night's generated scenarios were
resident/third-party voice ("We're on the no-contact list, don't bother")
rather than genuine visitor speech, traced to exactly this reuse.

Every string here describes ONLY the visitor's situation -- who they are
and what's happening -- with zero reply/acknowledgment text of any kind.
Same category keys as `knock.core.orchestrator._LLM_CLASSIFIABLE_INTENTS`
plus `"unknown"`, so this stays one taxonomy, not two.
"""

from __future__ import annotations

SCENARIO_DESCRIPTIONS: dict[str, str] = {
    "delivery": (
        "a package delivery -- a carrier (UPS, FedEx, Amazon, USPS, or "
        "similar) dropping off a package, announcing it, or asking where "
        "to leave it"
    ),
    "delivery_signature_required": (
        "a package delivery that requires a signature -- the carrier "
        "mentioning a signature or ID check, or that someone needs to "
        "sign for it in person"
    ),
    "food_delivery": (
        "a food delivery -- a driver from a delivery app (DoorDash, Uber "
        "Eats, Grubhub) or a restaurant delivery (pizza, takeout) "
        "announcing the order has arrived"
    ),
    "religious_soliciting": (
        "someone doing religious canvassing or proselytizing door-to-door "
        "-- a missionary, a church member handing out pamphlets, someone "
        "inviting the household to a service, sharing their faith, or "
        "asking about religious beliefs"
    ),
    "political_soliciting": (
        "someone doing political canvassing -- campaigning for a "
        "candidate, collecting signatures for a ballot petition, or "
        "asking about voting or political views"
    ),
    "soliciting": (
        "a door-to-door salesperson or solicitor -- selling a product, "
        "service, or subscription, offering a free estimate or quote, or "
        "conducting a survey"
    ),
    "ride_arrived": (
        "a rideshare or taxi driver announcing they've arrived to pick "
        "someone up -- an Uber, Lyft, or taxi driver confirming they're "
        "outside or ready for pickup"
    ),
    "visitation": (
        "a personal or social visit -- a friend, family member, or "
        "neighbor stopping by casually, saying hello, or announcing "
        "themselves by name or relationship (not a stranger asking for "
        "someone by name, not a delivery or solicitor)"
    ),
    "service_appointment": (
        "a contractor or technician arriving for a scheduled service "
        "appointment -- a plumber, electrician, HVAC tech, pest control, "
        "or cable/internet installer, mentioning the appointment or the "
        "work they're there to do"
    ),
    "person_lookup": (
        "someone asking for a specific person by name -- looking for "
        "someone who lives there, asking if they're around, or wanting to "
        "speak to them specifically"
    ),
    "official_visit": (
        "someone claiming to be police, a government official, or a "
        "utility/service worker on official business -- identifying "
        "themselves by title or organization and stating why they're "
        "there (an inspection, an investigation, a meter check, etc)"
    ),
    "suspicious_activity": (
        "behavior or language that's concerning but doesn't rise to an "
        "emergency -- someone lingering, peering in windows, casing the "
        "property, making a vague threat, or acting evasive"
    ),
    "unknown": (
        "vague, off-topic, or small-talk things a visitor might say that "
        "don't fit any specific category -- idle chatter, an unrelated "
        "question, something ambiguous or incomplete"
    ),
}
