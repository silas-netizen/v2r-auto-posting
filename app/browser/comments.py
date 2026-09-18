from __future__ import annotations

from datetime import datetime, timedelta


DEFAULT_TREE = (
    ("comment", 0),
    ("comment", 0),
    ("comment", 0),
    ("comment", 0),
    ("comment", 0),
    ("reply", 1),
    ("reply", 1),
    ("reply", 1),
    ("reply", 1),
    ("reply", 1),
    ("reply2", 2),
    ("reply3", 3),
)


def comment_schedule(published_at: datetime, count: int = 12, interval_minutes: int = 3) -> list[datetime]:
    return [
        published_at + timedelta(minutes=interval_minutes * (index + 1))
        for index in range(count)
    ]


def plan_comment_tree(published_at: datetime) -> list[dict[str, object]]:
    times = comment_schedule(published_at, count=len(DEFAULT_TREE))
    return [
        {"role": role, "depth": depth, "scheduled_at": moment}
        for (role, depth), moment in zip(DEFAULT_TREE, times)
    ]
