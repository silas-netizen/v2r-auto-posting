import pytest

from v2r_autopost.scheduler import run_every


def test_run_every_runs_max_iterations():
    calls = []
    sleeps = []
    count = run_every(
        5.0,
        lambda: calls.append(1),
        max_iterations=3,
        sleep=sleeps.append,
    )
    assert count == 3
    assert len(calls) == 3
    # Sleeps only happen *between* runs, so one fewer than iterations.
    assert sleeps == [5.0, 5.0]


def test_run_every_rejects_non_positive_interval():
    with pytest.raises(ValueError):
        run_every(0, lambda: None, max_iterations=1)
