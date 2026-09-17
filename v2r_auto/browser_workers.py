from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TypeVar

from .playwright_browser import (
    BrowserConfig,
    PlaywrightBrowser as V2RBrowser,
)


T = TypeVar("T")
R = TypeVar("R")

MIN_BROWSER_WORKERS = 1
MAX_BROWSER_WORKERS = 5


def validate_worker_count(value: int) -> int:
    if not MIN_BROWSER_WORKERS <= value <= MAX_BROWSER_WORKERS:
        raise ValueError(
            f"작업 창 수는 {MIN_BROWSER_WORKERS}개부터 "
            f"{MAX_BROWSER_WORKERS}개까지 선택하세요"
        )
    return value


def partition_jobs_by_cafe(
    jobs: Sequence[T],
    worker_count: int,
) -> list[list[T]]:
    """Balance whole cafe groups without splitting a cafe between workers."""
    worker_count = validate_worker_count(worker_count)
    if not jobs:
        return [[] for _ in range(worker_count)]

    groups: dict[str, list[T]] = {}
    order: list[str] = []
    for job in jobs:
        cafe = str(getattr(job, "cafe", "") or "")
        if cafe not in groups:
            groups[cafe] = []
            order.append(cafe)
        groups[cafe].append(job)

    partitions: list[list[T]] = [[] for _ in range(worker_count)]
    sizes = [0] * worker_count
    for cafe in sorted(order, key=lambda item: len(groups[item]), reverse=True):
        target = min(range(worker_count), key=lambda index: sizes[index])
        partitions[target].extend(groups[cafe])
        sizes[target] += len(groups[cafe])
    return partitions


@dataclass(frozen=True, slots=True)
class WorkerSnapshot:
    worker_id: int
    state: str
    current_row: int | None
    message: str


class BrowserWorker:
    """Own one browser and execute every Selenium call on one fixed thread."""

    def __init__(
        self,
        worker_id: int,
        config: BrowserConfig,
        logger,
        browser_factory: Callable[[BrowserConfig, Any], V2RBrowser] = V2RBrowser,
    ):
        self.worker_id = worker_id
        self.config = config
        self.logger = logger
        self.browser_factory = browser_factory
        self.browser = browser_factory(config, logger)
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"v2r-window-{worker_id + 1}",
        )
        self._state_lock = threading.Lock()
        self._state = "대기"
        self._current_row: int | None = None
        self._message = ""

    def snapshot(self) -> WorkerSnapshot:
        with self._state_lock:
            return WorkerSnapshot(
                worker_id=self.worker_id,
                state=self._state,
                current_row=self._current_row,
                message=self._message,
            )

    def set_state(
        self,
        state: str,
        *,
        current_row: int | None = None,
        message: str = "",
    ) -> None:
        with self._state_lock:
            self._state = state
            self._current_row = current_row
            self._message = message

    def submit(
        self,
        callback: Callable[[V2RBrowser], R],
        *,
        state: str = "작업 중",
        current_row: int | None = None,
    ) -> Future[R]:
        def run() -> R:
            self.set_state(state, current_row=current_row)
            try:
                result = callback(self.browser)
            except Exception as exc:
                self.set_state(
                    "오류",
                    current_row=current_row,
                    message=str(exc)[:200],
                )
                raise
            self.set_state("대기")
            return result

        return self._executor.submit(run)

    def recycle_inline(self) -> None:
        """Recreate this worker browser at a job-safe point on its owner thread."""
        self.set_state("메모리 회수 중")
        self.browser.close()
        self.browser = self.browser_factory(self.config, self.logger)
        self.browser.open_login_window("")
        self.browser.ensure_v2r_login("", "")
        self.set_state("발행 중")

    def close(self) -> None:
        try:
            self.submit(lambda browser: browser.close(), state="종료 중").result()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self.set_state("종료")


class BrowserWorkerPool:
    """Manage independent Chrome profiles and a serialized Sheet writer."""

    def __init__(
        self,
        *,
        data_dir: Path,
        download_dir: Path,
        worker_count: int,
        logger,
        browser_factory: Callable[[BrowserConfig, Any], V2RBrowser] = V2RBrowser,
    ):
        self.data_dir = data_dir
        self.download_dir = download_dir
        self.worker_count = validate_worker_count(worker_count)
        self.logger = logger
        self.sheet_lock = threading.RLock()
        self._closed = False
        self._coordinator = BrowserWorker(
            worker_id=-1,
            config=BrowserConfig(
                profile_dir=data_dir / "chrome-profiles" / "coordinator",
                download_dir=download_dir / "coordinator",
            ),
            logger=logger,
            browser_factory=browser_factory,
        )
        self.workers = [
            BrowserWorker(
                worker_id=index,
                config=BrowserConfig(
                    profile_dir=(
                        data_dir / "chrome-profiles" / f"worker-{index + 1}"
                    ),
                    download_dir=download_dir / f"worker-{index + 1}",
                ),
                logger=logger,
                browser_factory=browser_factory,
            )
            for index in range(self.worker_count)
        ]
        self._login_workers: tuple[BrowserWorker, ...] = tuple(self.workers)

    @property
    def coordinator(self) -> BrowserWorker:
        return self._coordinator

    def snapshots(self) -> list[WorkerSnapshot]:
        return [
            self.coordinator.snapshot(),
            *(worker.snapshot() for worker in self.workers),
        ]

    def open_login_windows(
        self,
        sheet_url: str = "",
        *,
        worker_limit: int | None = None,
    ) -> None:
        selected_count = self.worker_count if worker_limit is None else worker_limit
        if not 1 <= selected_count <= self.worker_count:
            raise ValueError("로그인 작업 창 수가 올바르지 않습니다")
        self._login_workers = tuple(self.workers[:selected_count])
        if sheet_url:
            self.coordinator.submit(
                lambda browser: browser.open_sheet_login_window(sheet_url),
                state="로그인 대기",
            ).result()
        for worker in self._login_workers:
            worker.submit(
                lambda browser: browser.open_login_window(""),
                state="로그인 대기",
            ).result()
        for worker in self._login_workers:
            worker.set_state("로그인 대기")

    def verify_logins(self) -> None:
        futures = [
            worker.submit(
                lambda browser: browser.verify_current_v2r_login(),
                state="로그인 확인 중",
            )
            for worker in self._login_workers
        ]
        for future in futures:
            future.result()
        for worker in self._login_workers:
            worker.set_state("로그인 완료")

    def run_partitions(
        self,
        partitions: Iterable[Sequence[T]],
        callback: Callable[[int, V2RBrowser, Sequence[T]], R],
    ) -> list[R]:
        futures: list[Future[R]] = []
        for worker, jobs in zip(self.workers, partitions):
            if not jobs:
                continue
            row_number = int(getattr(jobs[0], "row_number", 0) or 0)
            futures.append(
                worker.submit(
                    lambda browser, worker_id=worker.worker_id, rows=jobs: callback(
                        worker_id,
                        browser,
                        rows,
                    ),
                    state="발행 중",
                    current_row=row_number or None,
                )
            )
        return [future.result() for future in futures]

    def call_coordinator(
        self,
        callback: Callable[[V2RBrowser], R],
        *,
        state: str = "준비 중",
    ) -> R:
        return self.coordinator.submit(callback, state=state).result()

    def write_sheet(
        self,
        callback: Callable[[V2RBrowser], R],
    ) -> R:
        with self.sheet_lock:
            return self.call_coordinator(callback, state="시트 기록 중")

    def recycle_worker_inline(self, worker_id: int) -> None:
        if not 0 <= worker_id < len(self.workers):
            raise ValueError(f"작업 창 번호가 올바르지 않습니다: {worker_id}")
        self.workers[worker_id].recycle_inline()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.coordinator.close()
        for worker in self.workers:
            worker.close()


class SheetDelegatingBrowser:
    """Use a worker's V2R window while routing Sheet UI to the coordinator."""

    def __init__(
        self,
        worker_browser: V2RBrowser,
        pool: BrowserWorkerPool,
        worker_id: int | None = None,
    ):
        self._worker_browser = worker_browser
        self._pool = pool
        self._worker_id = worker_id

    def __getattr__(self, name: str) -> Any:
        browser = (
            self._pool.workers[self._worker_id].browser
            if self._worker_id is not None
            else self._worker_browser
        )
        return getattr(browser, name)

    def recycle_worker_browser(self) -> None:
        if self._worker_id is None:
            raise RuntimeError("작업 창 번호가 없어 브라우저를 재시작할 수 없습니다")
        self._pool.recycle_worker_inline(self._worker_id)

    def update_sheet_cell(self, *args: Any, **kwargs: Any) -> Any:
        return self._pool.write_sheet(
            lambda browser: browser.update_sheet_cell(*args, **kwargs)
        )

    def update_completion_link(self, *args: Any, **kwargs: Any) -> Any:
        return self._pool.write_sheet(
            lambda browser: browser.update_completion_link(*args, **kwargs)
        )

    def download_sheet(self, *args: Any, **kwargs: Any) -> Any:
        return self._pool.write_sheet(
            lambda browser: browser.download_sheet(*args, **kwargs)
        )
