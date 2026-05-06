from typing import Protocol


class LLMProvider(Protocol):
    name: str

    def generate(self, prompt: str) -> str: ...
