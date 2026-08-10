from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .browser import V2RBrowser
from .daily_posts import assign_daily_posts
from .history import HistoryStore
from .models import AffiliateJob, DailyPost, JobStatus, PostJob, RunResult
from .report import write_report
from .state import JobStateStore


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
            if job.status == JobStatus.FAILED:
                self.logger.error("행 %s 처리 중단: %s", job.row_number, job.message)
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
        state_path: Path | None = None,
    ):
        self.browser = browser
        self.report_dir = report_dir
        self.logger = logger
        self.state = JobStateStore(state_path) if state_path else None

    def run(
        self,
        jobs: list[AffiliateJob],
        email: str,
        password: str,
        dry_run: bool,
        stop_event: threading.Event,
        progress: Callable[[int, int], None],
        daily_posts: list[DailyPost],
        source_sheet_url: str,
        status: Callable[[dict[str, int]], None] | None = None,
    ) -> tuple[RunResult, Path]:
        started_at = datetime.now()
        state_records: dict[int, tuple[str, dict]] = {}
        if self.state and not dry_run:
            for job in jobs:
                record = self.state.load_or_create(source_sheet_url, job)
                state_records[id(job)] = (record["job_key"], record)
                if record["stage"] == "COMPLETED":
                    job.status = JobStatus.SKIPPED
                    job.message = "작업 DB에서 이미 완료됨"
                elif record.get("account") and not job.account:
                    job.account = record["account"]

        self.browser.ensure_v2r_login(email, password)
        self.browser.start_affiliate_api_run(jobs)
        assigned_jobs = self.browser.assign_affiliate_accounts(jobs)
        for job in assigned_jobs:
            try:
                self.browser.update_sheet_cell(
                    source_sheet_url,
                    "D",
                    job.row_number,
                    job.account,
                )
                if self.state and id(job) in state_records:
                    self.state.update(
                        state_records[id(job)][0],
                        stage="ACCOUNT_ASSIGNED",
                        account=job.account,
                    )
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.message = f"D열 작성계정 저장 실패: {exc}"
                self.logger.exception(
                    "행 %s 작성계정은 배정했지만 D열에 저장하지 못했습니다",
                    job.row_number,
                )
        assign_daily_posts(jobs, daily_posts)

        total = len(jobs)
        retry_count = 0

        def emit_status() -> None:
            if not status:
                return
            status(
                {
                    "pending": sum(job.status == JobStatus.PENDING for job in jobs),
                    "success": sum(job.status == JobStatus.SUCCESS for job in jobs),
                    "failed": sum(job.status == JobStatus.FAILED for job in jobs),
                    "skipped": sum(job.status == JobStatus.SKIPPED for job in jobs),
                    "retrying": retry_count,
                }
            )

        emit_status()
        last_cafe_started: dict[str, float] = {}
        last_failure_reason = ""
        consecutive_failures = 0
        circuit_open = False
        for index, job in enumerate(jobs, start=1):
            progress(index - 1, total)
            if circuit_open:
                job.status = JobStatus.SKIPPED
                job.message = "동일 오류 5회 연속 발생으로 전체 작업 일시정지"
                continue
            if stop_event.is_set():
                job.status = JobStatus.SKIPPED
                job.message = "사용자가 중지함"
                continue
            if job.status == JobStatus.SKIPPED:
                self.logger.info("행 %s 건너뜀: %s", job.row_number, job.message)
                continue
            if job.status == JobStatus.FAILED:
                self.logger.error("행 %s 처리 중단: %s", job.row_number, job.message)
                continue

            errors = job.validate()
            if errors:
                job.status = JobStatus.FAILED
                job.message = ", ".join(errors)
                self.logger.error("행 %s 검증 실패: %s", job.row_number, job.message)
                continue

            while True:
                if not dry_run:
                    remaining = 20 - (
                        time.monotonic() - last_cafe_started.get(job.cafe, 0)
                    )
                    if remaining > 0:
                        self.logger.info(
                            "%s 다음 작업까지 %.0f초 대기", job.cafe, remaining
                        )
                        if stop_event.wait(remaining):
                            job.status = JobStatus.SKIPPED
                            job.message = "사용자가 중지함"
                            break
                    last_cafe_started[job.cafe] = time.monotonic()
                self.logger.info(
                    "[%s/%s] 행 %s 제휴 수정 발행 시작: %s",
                    index,
                    total,
                    job.row_number,
                    job.title,
                )
                try:
                    record_info = state_records.get(id(job))
                    job_key = record_info[0] if record_info else ""
                    resume = record_info[1] if record_info else {}
                    if self.state and job_key:
                        self.state.increment_attempt(job_key)

                    def checkpoint(stage: str, **values) -> None:
                        if self.state and job_key:
                            self.state.update(job_key, stage=stage, **values)
                            resume.update(values)
                            resume["stage"] = stage

                    job.revision_url = self.browser.publish_affiliate_revision(
                        job,
                        dry_run,
                        resume=resume,
                        checkpoint=checkpoint if not dry_run else None,
                    )
                    job.status = JobStatus.SUCCESS
                    last_failure_reason = ""
                    consecutive_failures = 0
                    job.message = (
                        "전체 흐름 검증 완료" if dry_run else "수정 발행 완료"
                    )
                    if not dry_run:
                        try:
                            self.browser.update_completion_link(
                                source_sheet_url,
                                job.row_number,
                                job.revision_url,
                            )
                            if self.state and job_key:
                                self.state.update(job_key, stage="COMPLETED")
                        except Exception as sheet_error:
                            job.message = (
                                "수정 발행 완료, F열 완료 링크 입력 실패 - "
                                f"결과 URL: {job.revision_url} / {sheet_error}"
                            )
                            self.logger.exception(
                                "행 %s 발행은 성공했지만 F열 링크 저장 실패",
                                job.row_number,
                            )
                    break
                except Exception as exc:
                    reason, retryable = self.browser.classify_affiliate_failure(exc)
                    if reason == last_failure_reason:
                        consecutive_failures += 1
                    else:
                        last_failure_reason = reason
                        consecutive_failures = 1
                    if consecutive_failures >= 5:
                        retryable = False
                        circuit_open = True
                        reason = f"{reason} (동일 오류 5회 연속, 전체 일시정지)"
                    failed_account = job.account
                    self.logger.error(
                        "행 %s 계정 %s 실패: %s",
                        job.row_number,
                        failed_account,
                        reason,
                    )
                    replacement = (
                        self.browser.replace_failed_affiliate_account(job)
                        if retryable and job.account_type in {"실명", "비실명"}
                        else ""
                    )
                    if replacement:
                        retry_count += 1
                        emit_status()
                        try:
                            self.browser.update_sheet_cell(
                                source_sheet_url,
                                "D",
                                job.row_number,
                                replacement,
                            )
                            if self.state and job_key:
                                self.state.reset_sources(
                                    job_key,
                                    replacement,
                                    reason,
                                )
                                resume.clear()
                                resume.update(
                                    {
                                        "stage": "ACCOUNT_ASSIGNED",
                                        "account": replacement,
                                        "daily_source_id": "",
                                        "revision_source_id": "",
                                    }
                                )
                            self.logger.warning(
                                "행 %s 작성계정 교체 후 재시도: %s → %s",
                                job.row_number,
                                failed_account,
                                replacement,
                            )
                            continue
                        except Exception as sheet_error:
                            reason = f"실패: D열 계정 교체 저장 실패 - {sheet_error}"

                    job.status = JobStatus.FAILED
                    job.message = reason
                    if self.state and job_key:
                        self.state.update(
                            job_key,
                            stage="FAILED",
                            account=job.account,
                            last_error=reason,
                        )
                    if not dry_run:
                        try:
                            self.browser.update_sheet_cell(
                                source_sheet_url,
                                "F",
                                job.row_number,
                                reason,
                            )
                        except Exception:
                            self.logger.exception(
                                "행 %s F열 실패 사유 저장 실패", job.row_number
                            )
                    self.logger.exception(
                        "행 %s 제휴 수정 발행 최종 실패", job.row_number
                    )
                    break
            progress(index, total)
            emit_status()

        result = RunResult(
            started_at=started_at,
            finished_at=datetime.now(),
            dry_run=dry_run,
            jobs=jobs,
        )
        report_path = write_report(result, self.report_dir)
        self.logger.info(
            "제휴 실행 종료: 성공 %s / 실패 %s / 건너뜀 %s / 결과 %s",
            result.succeeded,
            result.failed,
            result.skipped,
            report_path,
        )
        progress(total, total)
        if self.state:
            self.state.close()
        return result, report_path
