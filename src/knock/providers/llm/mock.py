from knock.providers.llm.base import LLMProvider


class MockLLMProvider(LLMProvider):
    name = "mock-llm"

    def generate(self, prompt: str) -> str:
        return f"MOCK_RESPONSE: {prompt[:40]}"
