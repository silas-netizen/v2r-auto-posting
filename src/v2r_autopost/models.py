"""Core data models used across the application."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Post:
    """A single piece of content to be published.

    Attributes:
        title: Human readable post title.
        body: Post body. Markdown is expected but any text works.
        tags: Optional list of tags/labels.
        categories: Optional list of categories.
        status: Target publish status (e.g. "publish" or "draft").
        source_path: Where the post was loaded from, for logging/traceability.
    """

    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    status: str = "publish"
    source_path: str | None = None

    def summary(self, width: int = 60) -> str:
        """Return a short one-line summary of the post body."""
        flattened = " ".join(self.body.split())
        if len(flattened) <= width:
            return flattened
        return flattened[: width - 1].rstrip() + "…"


@dataclass
class PublishResult:
    """Outcome of publishing a single post to a single target."""

    target: str
    post_title: str
    success: bool
    url: str | None = None
    message: str = ""

    def format_line(self) -> str:
        status = "OK " if self.success else "FAIL"
        detail = self.url or self.message
        suffix = f" -> {detail}" if detail else ""
        return f"[{status}] {self.target:<10} | {self.post_title}{suffix}".rstrip()
