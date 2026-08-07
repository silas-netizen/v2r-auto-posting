"""Loading posts from Markdown files with YAML front matter."""

from __future__ import annotations

import os
from collections.abc import Iterable

import frontmatter

from .models import Post


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


def load_post(path: str) -> Post:
    """Load a single Markdown (front matter) file into a :class:`Post`."""
    with open(path, encoding="utf-8") as handle:
        parsed = frontmatter.load(handle)

    metadata = parsed.metadata or {}
    title = str(metadata.get("title") or _title_from_filename(path))

    return Post(
        title=title,
        body=parsed.content.strip(),
        tags=_as_list(metadata.get("tags")),
        categories=_as_list(metadata.get("categories")),
        status=str(metadata.get("status", "publish")),
        source_path=path,
    )


def load_posts(directory: str) -> list[Post]:
    """Load every ``.md``/``.markdown`` file in ``directory`` (sorted by name)."""
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"Posts directory not found: {directory}")

    posts: list[Post] = []
    for name in sorted(os.listdir(directory)):
        if name.lower().endswith((".md", ".markdown")):
            posts.append(load_post(os.path.join(directory, name)))
    return posts


def _title_from_filename(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return base.replace("-", " ").replace("_", " ").strip().title()
