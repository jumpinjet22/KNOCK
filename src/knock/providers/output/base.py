from typing import Protocol


class OutputProvider(Protocol):
    name: str

    def send(self, text: str) -> None: ...
