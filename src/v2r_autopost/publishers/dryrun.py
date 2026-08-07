"""A publisher that prints what *would* be published.

This is the default target: it needs no credentials or network access, which
makes it ideal for local development, CI, and verifying an end-to-end run.
"""

from __future__ import annotations

import sys
from typing import TextIO

from ..models import Post, PublishResult
from .base import Publisher


class DryRunPublisher(Publisher):
    def __init__(
        self, name: str = "console", stream: TextIO | None = None, **_options: object
    ) -> None:
        super().__init__(name)
        self._stream = stream or sys.stdout

    def publish(self, post: Post) -> PublishResult:
        lines = [
            "─" * 60,
            f"[DRY-RUN:{self.name}] would publish (status={post.status})",
            f"  title      : {post.title}",
            f"  tags       : {', '.join(post.tags) or '-'}",
            f"  categories : {', '.join(post.categories) or '-'}",
            f"  preview    : {post.summary()}",
            "─" * 60,
        ]
        self._stream.write("\n".join(lines) + "\n")
        return PublishResult(
            target=self.name,
            post_title=post.title,
            success=True,
            message="dry-run (not actually published)",
        )
