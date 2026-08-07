"""A tiny, dependency-free interval scheduler.

The full product would support cron expressions; for the development scaffold
this provides a predictable loop that repeatedly invokes a callback. It is kept
deliberately simple and fully testable (the sleep function is injectable).
"""

from __future__ import annotations

import time
from collections.abc import Callable


def run_every(
    interval_seconds: float,
    job: Callable[[], None],
    *,
    max_iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run ``job`` immediately and then every ``interval_seconds``.

    Args:
        interval_seconds: Delay between runs.
        job: Callable executed each tick.
        max_iterations: Stop after this many runs (``None`` = run forever).
        sleep: Injectable sleep function (used by tests).

    Returns:
        The number of times ``job`` was executed.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    count = 0
    while max_iterations is None or count < max_iterations:
        job()
        count += 1
        if max_iterations is not None and count >= max_iterations:
            break
        sleep(interval_seconds)
    return count
