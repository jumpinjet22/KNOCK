from knock.core.session import ConversationContext


class PolicyEngine:
    """Safety-first policy checks."""

    def evaluate(self, text: str) -> tuple[bool, str, list[str]]:
        lowered = text.lower()
        flags: list[str] = []

        if any(k in lowered for k in ["are you home", "is anyone home", "home right now"]):
            flags.append("occupancy")
        if any(k in lowered for k in ["schedule", "when do you leave", "what time are you away"]):
            flags.append("schedule")
        if any(k in lowered for k in ["unlock", "open the door", "let me in"]):
            flags.append("unlock")
        if any(k in lowered for k in ["help", "emergency", "fire", "medical", "police"]):
            flags.append("emergency")

        if "emergency" in flags:
            return True, "emergency", flags
        if flags:
            return False, "blocked_request", flags
        return True, "normal", flags

    def apply_style(self, response: str) -> str:
        return response.strip()[:140]

    def build_context(self, text: str, intent: str, flags: list[str]) -> ConversationContext:
        return ConversationContext(visitor_text=text, intent=intent, policy_flags=flags)
