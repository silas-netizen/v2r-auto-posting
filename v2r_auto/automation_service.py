from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .browser_workers import (
    BrowserWorkerPool,
    SheetDelegatingBrowser,
    partition_jobs_by_cafe,
    validate_worker_count,
)
from .daily_posts import assign_daily_posts, load_daily_posts
from .images import load_sheet_brand
from .immediate_inputs import (
    ACCOUNT_TEST_SHEET_GID,
    ACCOUNT_TEST_SHEET_ID,
    is_informational_sheet,
    load_account_test_jobs,
    load_brand_immediate_jobs,
    load_daily_excel_jobs,
)
from .models import JobStatus
from .photo_washer import (
    PhotoWashPlan,
    needs_photo_wash,
    prepare_photo_wash_plan,
    preferred_photo_washer_executable,
)
from .playwright_edition import PlaywrightWebBrowser
from .runner import AffiliateRunner, ImmediateRunner
from .sheet import load_affiliate_jobs


DAILY_POST_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1vSON0Rej9anDQXcAOXyBrCr50B4MMqZ79FahF4cDPJw/"
    "edit?gid=1842684291#gid=1842684291"
)
ACCOUNT_TEST_SHEET_URL = (
    f"https://docs.google.com/spreadsheets/d/{ACCOUNT_TEST_SHEET_ID}/"
    f"edit?gid={ACCOUNT_TEST_SHEET_GID}#gid={ACCOUNT_TEST_SHEET_GID}"
)


def app_data_dir(product_name: str = "V2RPlaywrightWeb") -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path.home() / ".local" / "share"
    return root / product_name


@dataclass(frozen=True, slots=True)
class ControlConfig:
    program: str = "affiliate"
    worker_count: int = 3
    sheet_url: str = ""
    excel_path: str = ""
    photo_washer_path: str = ""
    input_mode: str = "brand"
    dry_run: bool = True
    publish_mode: str = "reserved"
    auto_account_limit: int = 10
    immediate_interval_minutes: int = 1
    browser_recycle_jobs: int = 20

    @classmethod
    def from_values(cls, values: dict[str, Any]) -> "ControlConfig":
        program = str(values.get("program", "affiliate"))
        if program not in {"affiliate", "immediate"}:
            raise ValueError("프로그램은 자사 또는 제휴만 선택할 수 있습니다")
        input_mode = str(values.get("input_mode", "brand"))
        if input_mode not in {"brand", "daily", "account_test"}:
            raise ValueError("올바른 자사 입력 모드를 선택하세요")
        publish_mode = str(values.get("publish_mode", "reserved"))
        if publish_mode not in {"reserved", "immediate"}:
            raise ValueError("올바른 발행 방식을 선택하세요")
        worker_count = validate_worker_count(
            int(values.get("worker_count", 3))
        )
        auto_account_limit = int(values.get("auto_account_limit", 10))
        if not 2 <= auto_account_limit <= 10:
            raise ValueError("자동 배정 ID 수는 2개부터 10개까지 선택하세요")
        interval = int(values.get("immediate_interval_minutes", 1))
        if not 1 <= interval <= 15:
            raise ValueError("즉시 발행 간격은 1분부터 15분까지 선택하세요")
        browser_recycle_jobs = int(values.get("browser_recycle_jobs", 20))
        if browser_recycle_jobs not in {0, 10, 20, 30, 50}:
            raise ValueError(
                "브라우저 메모리 회수 주기는 사용 안 함, 10, 20, 30, 50건 중 선택하세요"
            )
        return cls(
            program=program,
            worker_count=worker_count,
            sheet_url=str(values.get("sheet_url", "")).strip(),
            excel_path=str(values.get("excel_path", "")).strip(),
            photo_washer_path=str(values.get("photo_washer_path", "")).strip(),
            input_mode=input_mode,
            dry_run=bool(values.get("dry_run", True)),
            publish_mode=publish_mode,
            auto_account_limit=auto_account_limit,
            immediate_interval_minutes=interval,
            browser_recycle_jobs=browser_recycle_jobs,
        )


class UnifiedAutomationBackend:
    """Thread-safe backend used by the localhost HTTPS control page."""

    def __init__(self, logger: logging.Logger | None = None):
        self.data_dir = app_data_dir("V2RPlaywrightWeb")
        self.download_dir = self.data_dir / "downloads"
        self.report_dir = self.data_dir / "reports"
        self.log_dir = self.data_dir / "logs"
        for directory in (
            self.data_dir,
            self.download_dir,
            self.report_dir,
            self.log_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.logger = logger or self._default_logger()
        self.config = ControlConfig()
        self.pool: BrowserWorkerPool | None = None
        self.photo_plan: PhotoWashPlan | None = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.shutdown_event = threading.Event()
        self._lock = threading.RLock()
        self._operation: threading.Thread | None = None
        self._state = "설정 대기"
        self._message = "설정을 저장한 뒤 로그인 창을 열어주세요."
        self._completed = 0
        self._total = 0

    def _default_logger(self) -> logging.Logger:
        logger = logging.getLogger("v2r_auto.web_control")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.FileHandler(
                self.log_dir / "v2r-auto-control.log",
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(levelname)s | %(message)s"
                )
            )
            logger.addHandler(handler)
        return logger

    def _ensure_pool(self) -> BrowserWorkerPool:
        with self._lock:
            if self.pool is None:
                self.pool = BrowserWorkerPool(
                    data_dir=self.data_dir,
                    download_dir=self.download_dir,
                    worker_count=self.config.worker_count,
                    logger=self.logger,
                    browser_factory=PlaywrightWebBrowser,
                )
            return self.pool

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            workers = (
                [
                    {
                        "worker_id": item.worker_id,
                        "state": item.state,
                        "current_row": item.current_row,
                        "message": item.message,
                    }
                    for item in self.pool.snapshots()
                ]
                if self.pool
                else []
            )
            return {
                "state": self._state,
                "message": self._message,
                "completed": self._completed,
                "total": self._total,
                "workers": workers,
                "configured": {
                    "program": self.config.program,
                    "worker_count": self.config.worker_count,
                    "input_mode": self.config.input_mode,
                },
            }

    def configure(self, values: dict[str, Any]) -> dict[str, Any]:
        config = ControlConfig.from_values(values)
        with self._lock:
            if self._operation and self._operation.is_alive():
                raise RuntimeError("진행 중인 작업이 끝난 뒤 설정을 변경하세요")
            if self.pool and self.pool.worker_count != config.worker_count:
                self.pool.close()
                self.pool = None
            self.config = config
            self.photo_plan = None
            self._state = "설정 완료"
            self._message = (
                f"{'제휴' if config.program == 'affiliate' else '자사'} / "
                f"작업 창 {config.worker_count}개"
            )
        return self.snapshot()

    def action(self, name: str) -> dict[str, Any]:
        if name == "pause":
            self.pause_event.set()
            with self._lock:
                self._state = "일시정지 요청"
                self._message = "각 창이 현재 안전 지점에서 멈춥니다."
            return self.snapshot()
        if name == "resume":
            self.pause_event.clear()
            with self._lock:
                self._state = "작업 중"
                self._message = "작업을 다시 시작했습니다."
            return self.snapshot()
        if name == "stop":
            self.stop_event.set()
            with self._lock:
                self._state = "중지 요청"
                self._message = "현재 단계가 끝나면 안전하게 중지합니다."
            return self.snapshot()
        if name == "shutdown":
            self.stop_event.set()
            self.shutdown_event.set()
            with self._lock:
                self._state = "종료 중"
                self._message = "작업 창과 제어 서버를 종료합니다."
            return self.snapshot()

        actions = {
            "open_login": self._open_login,
            "verify_login": self._verify_login,
            "check_data": self._check_data,
            "start": self._run,
        }
        callback = actions.get(name)
        if callback is None:
            raise ValueError(f"지원하지 않는 제어 버튼입니다: {name}")
        self._launch(name, callback)
        return self.snapshot()

    def _launch(self, name: str, callback) -> None:
        with self._lock:
            if self._operation and self._operation.is_alive():
                raise RuntimeError("다른 작업이 진행 중입니다")
            self.stop_event.clear()
            if name != "start":
                self.pause_event.clear()
            self._state = "준비 중" if name != "start" else "작업 중"
            self._message = "요청을 처리하고 있습니다."

            def operation() -> None:
                try:
                    callback()
                except Exception as exc:
                    self.logger.exception("통합 제어 작업 실패: %s", name)
                    with self._lock:
                        self._state = "오류"
                        self._message = str(exc)

            self._operation = threading.Thread(
                target=operation,
                name=f"v2r-control-{name}",
                daemon=True,
            )
            self._operation.start()

    def _sheet_url_for_login(self) -> str:
        if self.config.program == "affiliate":
            return self.config.sheet_url
        if self.config.input_mode == "account_test":
            return self.config.sheet_url or ACCOUNT_TEST_SHEET_URL
        if self.config.input_mode == "brand":
            return self.config.sheet_url
        return ""

    def _open_login(self) -> None:
        pool = self._ensure_pool()
        worker_limit = 1 if self.config.input_mode == "account_test" else None
        pool.open_login_windows(
            self._sheet_url_for_login(),
            worker_limit=worker_limit,
        )
        with self._lock:
            self._state = "로그인 대기"
            self._message = (
                "시트 창과 열린 각 작업 창에서 V2R 로그인을 완료한 뒤 "
                "'로그인 확인'을 누르세요."
            )

    def _verify_login(self) -> None:
        self._ensure_pool().verify_logins()
        with self._lock:
            self._state = "로그인 완료"
            self._message = "열린 V2R 작업 창의 로그인을 확인했습니다."

    def _load_affiliate_data(self):
        if not self.config.sheet_url:
            raise ValueError("제휴 Google Sheet URL을 입력하세요")
        pool = self._ensure_pool()
        source_csv = pool.write_sheet(
            lambda browser: browser.download_sheet(self.config.sheet_url)
        )
        jobs = load_affiliate_jobs(source_csv)
        daily_csv = pool.write_sheet(
            lambda browser: browser.download_sheet(
                DAILY_POST_SHEET_URL,
                required_headers={"내용", "카페"},
            )
        )
        daily_posts = load_daily_posts(daily_csv)
        try:
            brand = load_sheet_brand(self.config.sheet_url)
        except Exception:
            brand = ""
            self.logger.warning("시트 제목에서 이미지 브랜드를 확인하지 못했습니다")
        for job in jobs:
            job.brand = brand
        return jobs, daily_posts, self.config.sheet_url

    def _load_immediate_data(self):
        pool = self._ensure_pool()
        if self.config.input_mode == "daily":
            if not self.config.excel_path:
                raise ValueError("일상 글 Excel 경로를 입력하세요")
            return load_daily_excel_jobs(self.config.excel_path), "", None
        sheet_url = (
            self.config.sheet_url
            or ACCOUNT_TEST_SHEET_URL
            if self.config.input_mode == "account_test"
            else self.config.sheet_url
        )
        if not sheet_url:
            raise ValueError("Google Sheet URL을 입력하세요")
        csv_path = pool.write_sheet(
            lambda browser: browser.download_sheet(sheet_url)
        )
        if self.config.input_mode == "account_test":
            return load_account_test_jobs(csv_path), sheet_url, None
        informational = is_informational_sheet(sheet_url)
        brand = "" if informational else load_sheet_brand(sheet_url)
        jobs = load_brand_immediate_jobs(
            csv_path,
            brand=brand,
            format_body=informational,
            use_comment_ai=informational,
        )
        return jobs, sheet_url, None

    def _load_data(self):
        if self.config.program == "affiliate":
            return self._load_affiliate_data()
        return self._load_immediate_data()

    def _photo_washer_executable(self) -> Path | None:
        if self.config.photo_washer_path:
            return Path(self.config.photo_washer_path)
        return preferred_photo_washer_executable()

    def _check_data(self) -> None:
        jobs, auxiliary, _sheet_url = self._load_data()
        self.photo_plan = prepare_photo_wash_plan(
            jobs,
            download_dir=self.download_dir / "photo-washer",
            executable=self._photo_washer_executable(),
            logger=self.logger,
        )
        pending = sum(job.status == JobStatus.PENDING for job in jobs)
        with self._lock:
            self._total = len(jobs)
            self._completed = len(jobs) - pending
            self._state = "데이터 확인 완료"
            self._message = (
                f"전체 {len(jobs)}건 / 처리 대상 {pending}건 / "
                f"사진 {self.photo_plan.selected_count}개"
                + (
                    f" / 일상 글 {len(auxiliary)}건"
                    if self.config.program == "affiliate"
                    else ""
                )
            )

    def _progress(self, _worker_id: int, completed: int, total: int) -> None:
        del total
        with self._lock:
            self._completed += max(0, completed)

    def _run(self) -> None:
        pool = self._ensure_pool()
        jobs, auxiliary, sheet_url = self._load_data()
        if self.photo_plan is not None:
            self.photo_plan.apply(jobs, logger=self.logger)
        elif any(needs_photo_wash(job) for job in jobs):
            raise ValueError("사진 세탁 준비가 없습니다. 데이터 확인을 먼저 실행하세요")
        if self.config.program == "affiliate":
            assign_daily_posts(jobs, auxiliary)
        partitions = partition_jobs_by_cafe(jobs, pool.worker_count)
        with self._lock:
            self._total = len(jobs)
            self._completed = 0
            self._state = "작업 중"
            self._message = f"{pool.worker_count}개 작업 창에 원고를 배정했습니다."

        def execute(worker_id, worker_browser, rows):
            browser = SheetDelegatingBrowser(
                worker_browser,
                pool,
                worker_id=worker_id,
            )
            last_progress = 0

            def progress(completed: int, total: int) -> None:
                nonlocal last_progress
                delta = max(0, completed - last_progress)
                last_progress = completed
                self._progress(worker_id, delta, total)
                recycle_jobs = self.config.browser_recycle_jobs
                if (
                    delta
                    and recycle_jobs
                    and completed < total
                    and completed % recycle_jobs == 0
                    and not self.stop_event.is_set()
                ):
                    self.logger.info(
                        "작업 창 %s: %s건 완료 후 브라우저 메모리를 회수합니다",
                        worker_id + 1,
                        completed,
                    )
                    browser.recycle_worker_browser()

            if self.config.program == "affiliate":
                runner = AffiliateRunner(
                    browser=browser,
                    report_dir=self.report_dir,
                    logger=self.logger,
                    state_path=self.data_dir / "affiliate" / "jobs.db",
                )
                return runner.run(
                    jobs=list(rows),
                    email="",
                    password="",
                    dry_run=self.config.dry_run,
                    stop_event=self.stop_event,
                    progress=progress,
                    daily_posts=auxiliary,
                    source_sheet_url=sheet_url,
                    pause_event=self.pause_event,
                )
            runner = ImmediateRunner(
                browser=browser,
                history_path=self.data_dir / "immediate-history.json",
                report_dir=self.report_dir,
                logger=self.logger,
            )
            return runner.run(
                list(rows),
                dry_run=self.config.dry_run,
                stop_event=self.stop_event,
                progress=progress,
                source_sheet_url=sheet_url,
                pause_event=self.pause_event,
                publish_immediately=self.config.publish_mode == "immediate",
                auto_account_limit=self.config.auto_account_limit,
                immediate_interval_minutes=(
                    self.config.immediate_interval_minutes
                ),
            )

        results = pool.run_partitions(partitions, execute)
        failed = sum(result.failed for result, _path in results)
        succeeded = sum(result.succeeded for result, _path in results)
        reserved = sum(result.reserved for result, _path in results)
        with self._lock:
            self._completed = self._total
            self._state = "중지됨" if self.stop_event.is_set() else "작업 완료"
            self._message = (
                f"성공 {succeeded}건 / 예약 {reserved}건 / 실패 {failed}건"
            )

    def close(self) -> None:
        self.stop_event.set()
        if self.pool:
            self.pool.close()
            self.pool = None
