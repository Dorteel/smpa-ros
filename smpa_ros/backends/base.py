from typing import Protocol


class Backend(Protocol):
    """The text inference contract shared by both backends."""

    def generate(self, prompt: str, *, max_new_tokens: int = 128) -> str:
        ...
