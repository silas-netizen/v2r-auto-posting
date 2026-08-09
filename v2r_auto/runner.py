from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .browser import V2RBrowser
from .history import HistoryStore
from .models import AffiliateJob, JobStatus, PostJob, RunResult
from .report import write_report


@dataclass(slots=True)
class RunOptions:
    dry_run: bool = True
    delay_seconds: int = 30
    skip_duplicates: bool = True


class AutomationRunner:
    def __init__(
        self,
        browser: V2RBrowser,
        history_path: Path,
        report_dir: Path,
        logger: logging.Logger,
    ):
        self.browser = browser
        self.history = HistoryStore(history_path)
        self.report_dir = report_dir
        self.logger = logger

    def run(
        self,
        jobs: list[PostJob],
        email: str,
        password: str,
        options: RunOptions,
        stop_event: threading.Event,
        progress: Callable[[int, int], None],
    ) -> tuple[RunResult, Path]:
        started_at = datetime.now()
        self.browser.ensure_v2r_login(email, password)

        total = len(jobs)
        for index, job in enumerate(jobs, start=1):
            progress(index - 1, total)
            if stop_event.is_set():
                job.status = JobStatus.SKIPPED
                job.message = "사용자가 중지함"
                continue
            if job.status == JobStatus.SKIPPED:
                self.logger.info("행 %s 건너뜀: %s", job.row_number, job.message)
                continue

            errors = job.validate()
            if errors:
                job.status = JobStatus.FAILED
                job.message = ", ".join(errors)
                self.logger.error("행 %s 검증 실패: %s", job.row_number, job.message)
                continue
            if options.skip_duplicates and not options.dry_run and self.history.contains(job):
                job.status = JobStatus.SKIPPED
                job.message = "이전에 발행한 동일 글"
                self.logger.warning("행 %s 중복 글을 건너뜁니다", job.row_number)
                continue

            try:
                self.logger.info("[%s/%s] 행 %s 입력 시작: %s", index, total, job.row_number, job.title)
                self.browser.fill_post(job, dry_run=options.dry_run)
                job.status = JobStatus.SUCCESS
                job.message = "입력 검증 완료" if options.dry_run else "발행 완료"
                if not options.dry_run:
                    try:
                        self.history.record(job)
                    except Exception as history_error:
                        job.message = (
                            "발행 완료, 중복 이력 저장 실패 - 재실행 전 결과를 확인하세요: "
                            f"{history_error}"
                        )
                        self.logger.exception(
                            "행 %s 발행은 완료됐지만 중복 이력을 저장하지 못했습니다",
                            job.row_number,
                        )
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.message = str(exc)
                self.logger.exception("행 %s 처리 실패", job.row_number)

            progress(index, total)
            if index < total and not stop_event.is_set() and options.delay_seconds:
                stop_event.wait(options.delay_seconds)

        result = RunResult(
            started_at=started_at,
            finished_at=datetime.now(),
            dry_run=options.dry_run,
            jobs=jobs,
        )
        report_path = write_report(result, self.report_dir)
        self.logger.info("처리 완료. 결과 파일: %s", report_path)
        progress(total, total)
        return result, report_path


class AffiliateRunner:
    """Runs the fixed affiliate flow for a single selected Sheet row."""

    def __init__(
        self,
        browser: V2RBrowser,
        report_dir: Path,
        logger: logging.Logger,
    ):
        self.browser = browser
        self.report_dir = report_dir
        self.logger = logger

    def run(
        self,
        jobs: list[AffiliateJob],
        email: str,
        password: str,
        dry_run: bool,
        stop_event: threading.Event,
        progress: Callable[[int, int], None],
    ) -> tuple[RunResult, Path]:
        started_at = datetime.now()
        self.browser.ensure_v2r_login(email, password)

        total = len(jobs)
        for index, job in enumerate(jobs, start=1):
            progress(index - 1, total)
            if stop_event.is_set():
                job.status = JobStatus.SKIPPED
                job.message = "사용자가 중지함"
                continue
            if job.status == JobStatus.SKIPPED:
                self.logger.info("행 %s 건너뜀: %s", job.row_number, job.message)
                continue

            errors = job.validate()
            if errors:
                job.status = JobStatus.FAILED
                job.message = ", ".join(errors)
                self.logger.error("행 %s 검증 실패: %s", job.row_number, job.message)
                continue

            try:
                self.logger.info(
                    "[%s/%s] 행 %s 제휴 수정 발행 시작: %s",
                    index,
                    total,
                    job.row_number,
                    job.title,
                )
                job.revision_url = self.browser.publish_affiliate_revision(job, dry_run)
                job.status = JobStatus.SUCCESS
                job.message = "전체 흐름 검증 완료" if dry_run else "수정 발행 완료"
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.message = str(exc)
                self.logger.exception("행 %s 제휴 수정 발행 실패", job.row_number)
            progress(index, total)

        result = RunResult(
            started_at=started_at,
            finished_at=datetime.now(),
            dry_run=dry_run,
            jobs=jobs,
        )
        report_path = write_report(result, self.report_dir)
        self.logger.info("제휴 수정 발행 완료. 결과 파일: %s", report_path)
        progress(total, total)
        return result, report_path
