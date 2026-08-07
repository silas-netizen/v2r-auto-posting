"""Publisher registry.

A publisher takes a :class:`~v2r_autopost.models.Post` and pushes it to a
destination, returning a :class:`~v2r_autopost.models.PublishResult`.
"""

from __future__ import annotations

from ..config import TargetConfig
from .base import Publisher
from .dryrun import DryRunPublisher
from .wordpress import WordPressPublisher

_REGISTRY: dict[str, type[Publisher]] = {
    "dryrun": DryRunPublisher,
    "console": DryRunPublisher,
    "wordpress": WordPressPublisher,
}


def available_types() -> list[str]:
    return sorted(_REGISTRY)


def build_publisher(target: TargetConfig) -> Publisher:
    """Instantiate the publisher for a given target configuration."""
    try:
        publisher_cls = _REGISTRY[target.type]
    except KeyError as exc:
        raise ValueError(
            f"Unknown target type '{target.type}'. Available: {', '.join(available_types())}"
        ) from exc
    return publisher_cls(name=target.name, **target.options)


__all__ = [
    "Publisher",
    "DryRunPublisher",
    "WordPressPublisher",
    "build_publisher",
    "available_types",
]
