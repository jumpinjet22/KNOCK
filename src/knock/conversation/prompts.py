SYSTEM_PROMPT = "Keep responses short, safe, and privacy-preserving."

# Prepended once, to the first response of a new session -- lets a visitor
# know up front they're talking to an AI system that can get details wrong,
# before anything else is said. See Orchestrator.respond().
GREETING = (
    "Hi there, my name is Knock. I use several large language models, "
    "so I might get some details wrong."
)
