"""Publisher base class."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Post, PublishResult


class Publisher(ABC):
    """Abstract publish target."""

    def __init__(self, name: str, **_options: object) -> None:
        self.name = name

    @abstractmethod
    def publish(self, post: Post) -> PublishResult:
        """Publish a single post and return the result."""

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"{type(self).__name__}(name={self.name!r})"
