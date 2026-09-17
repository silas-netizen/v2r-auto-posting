from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from v2r_auto.browser_workers import (
    BrowserWorkerPool,
    partition_jobs_by_cafe,
    validate_worker_count,
)


@dataclass
class Job:
    row_number: int
    cafe: str


class FakeBrowser:
    created_profiles: list[Path] = []

    def __init__(self, config, _logger):
        self.config = config
        self.calls: list[tuple[str, str]] = []
        self.thread_ids: list[int] = []
        FakeBrowser.created_profiles.append(config.profile_dir)

    def open_login_window(self, sheet_url=""):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(("open", sheet_url))

    def ensure_v2r_login(self, _email, _password):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(("verify", ""))

    def close(self):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(("close", ""))


def test_worker_count_is_bounded() -> None:
    assert validate_worker_count(1) == 1
    assert validate_worker_count(5) == 5
    with pytest.raises(ValueError):
        validate_worker_count(0)
    with pytest.raises(ValueError):
        validate_worker_count(6)


def test_partition_jobs_keeps_each_cafe_on_one_worker() -> None:
    jobs = [
        Job(2, "가"),
        Job(3, "나"),
        Job(4, "가"),
        Job(5, "다"),
        Job(6, "나"),
    ]

    partitions = partition_jobs_by_cafe(jobs, 3)

    locations: dict[str, set[int]] = {}
    for index, partition in enumerate(partitions):
        for job in partition:
            locations.setdefault(job.cafe, set()).add(index)
    assert all(len(indexes) == 1 for indexes in locations.values())
    assert sorted(job.row_number for rows in partitions for job in rows) == [
        2,
        3,
        4,
        5,
        6,
    ]


def test_pool_uses_unique_profiles_and_thread_bound_browsers(tmp_path: Path) -> None:
    FakeBrowser.created_profiles = []
    pool = BrowserWorkerPool(
        data_dir=tmp_path,
        download_dir=tmp_path / "downloads",
        worker_count=3,
        logger=logging.getLogger("pool-test"),
        browser_factory=FakeBrowser,
    )
    try:
        pool.open_login_windows("https://sheet.example")
        pool.verify_logins()
        snapshots = pool.snapshots()

        assert len(FakeBrowser.created_profiles) == 4
        assert len(set(FakeBrowser.created_profiles)) == 4
        assert snapshots[0].worker_id == -1
        assert all(snapshot.state == "로그인 완료" for snapshot in snapshots)
        assert pool.coordinator.browser.calls[0] == (
            "open",
            "https://sheet.example",
        )
        assert all(
            worker.browser.calls[0] == ("open", "")
            for worker in pool.workers
        )
        for worker in (pool.coordinator, *pool.workers):
            assert len(set(worker.browser.thread_ids)) == 1
    finally:
        pool.close()


def test_pool_runs_partitions_concurrently(tmp_path: Path) -> None:
    pool = BrowserWorkerPool(
        data_dir=tmp_path,
        download_dir=tmp_path / "downloads",
        worker_count=2,
        logger=logging.getLogger("parallel-test"),
        browser_factory=FakeBrowser,
    )
    barrier = threading.Barrier(2)

    def run(worker_id, _browser, rows):
        barrier.wait(timeout=1)
        time.sleep(0.02)
        return worker_id, rows[0].row_number

    try:
        results = pool.run_partitions(
            [[Job(2, "가")], [Job(3, "나")]],
            run,
        )
    finally:
        pool.close()

    assert sorted(results) == [(0, 2), (1, 3)]


def test_worker_recycle_reopens_same_persistent_profile(tmp_path: Path) -> None:
    FakeBrowser.created_profiles = []
    pool = BrowserWorkerPool(
        data_dir=tmp_path,
        download_dir=tmp_path / "downloads",
        worker_count=1,
        logger=logging.getLogger("recycle-test"),
        browser_factory=FakeBrowser,
    )
    worker = pool.workers[0]
    original = worker.browser
    try:
        worker.submit(
            lambda _browser: worker.recycle_inline(),
            state="발행 중",
        ).result()

        replacement = worker.browser
        assert replacement is not original
        assert replacement.config.profile_dir == original.config.profile_dir
        assert original.calls == [("close", "")]
        assert replacement.calls[:2] == [("open", ""), ("verify", "")]
        assert len(set(original.thread_ids + replacement.thread_ids)) == 1
    finally:
        pool.close()
