"""The publishing engine: ties config, content, and publishers together."""

from __future__ import annotations

from .config import AppConfig
from .content import load_posts
from .models import Post, PublishResult
from .publishers import build_publisher


def publish_posts(
    config: AppConfig,
    posts: list[Post] | None = None,
    *,
    force_dryrun: bool = False,
) -> list[PublishResult]:
    """Publish every post to every enabled target.

    Args:
        config: Loaded application config.
        posts: Explicit posts to publish; if ``None`` they are loaded from
            ``config.posts_dir``.
        force_dryrun: When true, ignore configured targets and only use a
            single console dry-run target (safe default for testing).
    """
    if posts is None:
        posts = load_posts(config.posts_dir)

    from .config import TargetConfig

    targets = (
        [TargetConfig(name="console", type="dryrun")]
        if force_dryrun
        else config.enabled_targets()
    )
    publishers = [build_publisher(target) for target in targets]

    results: list[PublishResult] = []
    for post in posts:
        for publisher in publishers:
            results.append(publisher.publish(post))
    return results
