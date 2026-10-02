SYSTEM_PROMPT = "Keep responses short, safe, and privacy-preserving."

# Prepended once, to the first response of a new session -- lets a visitor
# know up front they're talking to an AI system that can get details wrong,
# before anything else is said. See Orchestrator.respond().
GREETING = (
    "Hi, my name is Knock. I'm a door answering agent that uses large "
    "language models, so I might get some things wrong. How can I help you today?"
)
