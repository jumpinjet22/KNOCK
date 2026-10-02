from typing import Protocol


class VisionProvider(Protocol):
    name: str

    def describe(self, image: bytes, prompt: str | None = None) -> str: ...
