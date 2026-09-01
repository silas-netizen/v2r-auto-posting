from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .affiliate_api import AffiliateDailyPending, AffiliateRunStopped
from .browser import V2RBrowser
from .cafe_catalog import TEST_CAFE_IDS
from .daily_posts import assign_daily_posts
from .history import HistoryStore
from .models import (
    AffiliateJob,
    DailyPost,
    ImmediateJob,
    JobStatus,
    PostJob,
    RunResult,
)
from .photo_washer import needs_photo_wash
from .report import write_report
from .state import JobStateStore


@dataclass(slots=True)
class RunOptions:
    dry_run: bool = True
    delay_seconds: int = 30
    skip_duplicates: bool = True


def assign_immediate_schedules(
    jobs: list[ImmediateJob],
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
    publish_immediately: bool = False,
) -> None:
    """Keep each cafe's reserved posts 5–15 minutes apart."""
    now = now or datetime.now(timezone.utc)
    rng = rng or random.SystemRandom()
    last_by_cafe: dict[int, datetime] = {}
    for job in jobs:
        if job.status != JobStatus.PENDING:
            continue
        job.publish_immediately = (
            publish_immediately or job.cafe_id in TEST_CAFE_IDS
        )
        if job.publish_immediately:
            job.scheduled_at = None
            continue
        anchor = max(now, last_by_cafe.get(job.cafe_id, now))
        job.scheduled_at = anchor + timedelta(minutes=rng.randint(5, 15))
        last_by_cafe[job.cafe_id] = job.scheduled_at


def assign_next_affiliate_daily_schedule(
    job: AffiliateJob,
    last_by_cafe: dict[str, datetime],
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> datetime:
    now = now or datetime.now(timezone.utc)
    rng = rng or random.SystemRandom()
    anchor = max(now, last_by_cafe.get(job.cafe, now))
    scheduled_at = anchor + timedelta(minutes=rng.randint(5, 15))
    job.daily_scheduled_at = scheduled_at
    last_by_cafe[job.cafe] = scheduled_at
    return scheduled_at


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
        pause_event: threading.Event | None = None,
    ) -> tuple[RunResult, Path]:
        started_at = datetime.now()
        pause_event = pause_event or threading.Event()
        pause_logged = False

        def wait_control() -> None:
            nonlocal pause_logged
            if stop_event.is_set():
                raise AffiliateRunStopped("사용자가 중지함")
            if pause_event.is_set() and not pause_logged:
                self.logger.info("일시정지됨: 다시 시작을 기다립니다")
                pause_logged = True
            while pause_event.is_set():
                if stop_event.wait(0.2):
                    raise AffiliateRunStopped("사용자가 중지함")
            if pause_logged:
                self.logger.info("다시 시작: 중단된 제휴 작업을 이어서 처리합니다")
                pause_logged = False
        for job in jobs:
            if needs_photo_wash(job) and not job.photo_wash_prepared:
                job.status = JobStatus.FAILED
                job.message = (
                    "포토워셔 세탁 준비가 없습니다. "
                    "2. 데이터 확인부터 다시 실행하세요"
                )
        state_records: dict[int, tuple[str, dict]] = {}
        saved_affiliate_urls: dict[int, set[str]] = {}
        if self.state and not dry_run:
            for job in jobs:
                record = self.state.load_or_create(source_sheet_url, job)
                state_records[id(job)] = (record["job_key"], record)
                if record["stage"] == "COMPLETED":
                    source_ids = {
                        str(record.get("daily_source_id") or ""),
                        str(record.get("revision_source_id") or ""),
                    } - {""}
                    saved_affiliate_urls[id(job)] = {
                        "https://v2r.daboja.im/nc/articleDetail/" + source_id
                        for source_id in source_ids
                    }
                    if job.completion_url:
                        saved_affiliate_urls[id(job)].add(job.completion_url)
                elif record.get("account") and not job.account:
                    job.account = record["account"]
                if record.get("daily_scheduled_at"):
                    job.daily_scheduled_at = datetime.fromisoformat(
                        str(record["daily_scheduled_at"]).replace("Z", "+00:00")
                    )

        self.browser.ensure_v2r_login(email, password)
        if not dry_run:
            for job in jobs:
                if job.completion_url:
                    saved_affiliate_urls.setdefault(id(job), set()).add(
                        job.completion_url
                    )
            all_saved_urls = {
                url
                for urls in saved_affiliate_urls.values()
                for url in urls
            }
            probe_results = (
                self.browser.probe_v2r_source_urls(
                    all_saved_urls
                )
                if all_saved_urls
                else {}
            )
            recovered_count = 0
            unknown_count = 0
            for job in jobs:
                saved_urls = saved_affiliate_urls.get(id(job), set())
                if not saved_urls:
                    continue
                states = [probe_results.get(url) for url in saved_urls]
                record_info = state_records.get(id(job))
                if any(state is True for state in states):
                    if self.state and record_info:
                        self.state.reset_sources(
                            record_info[0],
                            job.account,
                            "V2R에서 삭제된 완료 글 자동 초기화",
                        )
                        record_info[1].update(
                            {
                                "stage": "ACCOUNT_ASSIGNED",
                                "daily_source_id": "",
                                "daily_scheduled_at": "",
                                "revision_source_id": "",
                            }
                        )
                    job.completion_url = ""
                    job.revision_url = ""
                    job.daily_scheduled_at = None
                    job.daily_written_at = None
                    job.status = JobStatus.PENDING
                    job.message = ""
                    recovered_count += 1
                    continue
                job.status = JobStatus.SKIPPED
                if any(state is None for state in states):
                    job.message = (
                        "V2R 링크 확인 지연으로 기존 완료 기록 유지"
                    )
                    unknown_count += 1
                else:
                    job.message = "작업 DB에서 이미 완료됨"
            if recovered_count:
                self.logger.warning(
                    "현재 jobs.db 관련 삭제 source %s건을 초기화하고 재발행합니다",
                    recovered_count,
                )
            if unknown_count:
                self.logger.warning(
                    "현재 jobs.db 링크 %s건은 상태 확인 불가로 완료 기록을 유지합니다",
                    unknown_count,
                )
        self.browser.start_affiliate_api_run(jobs)
        assigned_jobs = self.browser.assign_affiliate_accounts(jobs)
        if not dry_run:
            self.browser.refresh_affiliate_account_grades(jobs)
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

        last_daily_scheduled: dict[str, datetime] = {}
        schedule_now = datetime.now(timezone.utc)
        for job in jobs:
            if job.status != JobStatus.PENDING:
                continue
            record_info = state_records.get(id(job))
            has_daily_source = bool(
                record_info
                and record_info[1].get("daily_source_id")
            )
            schedule_is_usable = bool(
                job.daily_scheduled_at
                and (
                    has_daily_source
                    or job.daily_scheduled_at
                    >= schedule_now + timedelta(minutes=2)
                )
            )
            if not schedule_is_usable:
                if job.daily_scheduled_at is not None:
                    self.logger.warning(
                        "행 %s source 없는 지난 일상 예약시간을 다시 계산합니다: %s",
                        job.row_number,
                        job.daily_scheduled_at.isoformat(),
                    )
                job.daily_scheduled_at = None
                assign_next_affiliate_daily_schedule(
                    job,
                    last_daily_scheduled,
                    now=schedule_now,
                )
                if self.state and record_info:
                    serialized = (
                        job.daily_scheduled_at.isoformat()
                        .replace("+00:00", "Z")
                    )
                    self.state.update(
                        record_info[0],
                        daily_scheduled_at=serialized,
                    )
                    record_info[1]["daily_scheduled_at"] = serialized
            else:
                previous = last_daily_scheduled.get(job.cafe)
                if (
                    previous is None
                    or job.daily_scheduled_at > previous
                ):
                    last_daily_scheduled[job.cafe] = job.daily_scheduled_at

        runtime_resumes: dict[int, dict] = {
            id(job): (
                state_records[id(job)][1]
                if id(job) in state_records
                else {}
            )
            for job in jobs
        }
        total = len(jobs)
        retry_count = 0

        def emit_status() -> None:
            if not status:
                return
            status(
                {
                    "pending": sum(job.status == JobStatus.PENDING for job in jobs),
                    "success": sum(job.status == JobStatus.SUCCESS for job in jobs),
                    "reserved": sum(
                        job.status == JobStatus.RESERVED for job in jobs
                    ),
                    "failed": sum(job.status == JobStatus.FAILED for job in jobs),
                    "skipped": sum(job.status == JobStatus.SKIPPED for job in jobs),
                    "retrying": retry_count,
                }
            )

        emit_status()
        last_cafe_started: dict[str, float] = {}
        for index, job in enumerate(jobs, start=1):
            progress(index - 1, total)
            try:
                wait_control()
            except AffiliateRunStopped:
                job.status = JobStatus.SKIPPED
                job.message = "사용자가 중지함"
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
                try:
                    wait_control()
                except AffiliateRunStopped:
                    job.status = JobStatus.SKIPPED
                    job.message = "사용자가 중지함"
                    break
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
                    try:
                        wait_control()
                    except AffiliateRunStopped:
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
                    resume = runtime_resumes[id(job)]
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
                        wait_control=wait_control if not dry_run else None,
                    )
                    job.status = JobStatus.SUCCESS
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
                except AffiliateDailyPending as exc:
                    job.status = JobStatus.RESERVED
                    job.message = str(exc)
                    self.logger.warning(
                        "행 %s 일상 예약은 유지하고 다음 실행에서 다시 확인: %s",
                        job.row_number,
                        exc,
                    )
                    break
                except AffiliateRunStopped:
                    job.status = JobStatus.SKIPPED
                    job.message = "사용자가 중지함"
                    break
                except Exception as exc:
                    if "DELETED_NAVER_CAFE_ARTICLE_SOURCE" in str(exc):
                        self.browser.reset_deleted_affiliate_sources(
                            job,
                            resume,
                        )
                        assign_next_affiliate_daily_schedule(
                            job,
                            last_daily_scheduled,
                            now=datetime.now(timezone.utc),
                        )
                        serialized = (
                            job.daily_scheduled_at.isoformat()
                            .replace("+00:00", "Z")
                        )
                        resume["daily_scheduled_at"] = serialized
                        if self.state and job_key:
                            self.state.reset_sources(
                                job_key,
                                job.account,
                                "V2R에서 삭제된 source 자동 초기화",
                            )
                            self.state.update(
                                job_key,
                                daily_scheduled_at=serialized,
                            )
                        retry_count += 1
                        emit_status()
                        self.logger.warning(
                            "행 %s 삭제된 과거 source를 초기화하고 "
                            "새 예약 쌍으로 재시도합니다",
                            job.row_number,
                        )
                        continue
                    if "NOT_START_AT_PAST_TIME" in str(exc):
                        previous_schedule = job.daily_scheduled_at
                        assign_next_affiliate_daily_schedule(
                            job,
                            last_daily_scheduled,
                            now=datetime.now(timezone.utc),
                        )
                        serialized = (
                            job.daily_scheduled_at.isoformat()
                            .replace("+00:00", "Z")
                        )
                        resume.clear()
                        resume.update(
                            {
                                "stage": "ACCOUNT_ASSIGNED",
                                "account": job.account,
                                "daily_source_id": "",
                                "daily_scheduled_at": serialized,
                                "revision_source_id": "",
                            }
                        )
                        if self.state and job_key:
                            self.state.reset_sources(
                                job_key,
                                job.account,
                                "예약시간이 지나 미래 시간으로 다시 계산",
                            )
                            self.state.update(
                                job_key,
                                daily_scheduled_at=serialized,
                            )
                        retry_count += 1
                        emit_status()
                        self.logger.warning(
                            "행 %s 지난 예약시간 자동 재계산 후 재시도: %s → %s",
                            job.row_number,
                            previous_schedule,
                            job.daily_scheduled_at,
                        )
                        continue
                    reason, retryable = self.browser.classify_affiliate_failure(exc)
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


class ImmediateRunner:
    """Run immediate API posts from either a brand Sheet or daily Excel."""

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
        jobs: list[ImmediateJob],
        *,
        dry_run: bool,
        stop_event: threading.Event,
        progress: Callable[[int, int], None],
        source_sheet_url: str = "",
        status: Callable[[dict[str, int]], None] | None = None,
        pause_event: threading.Event | None = None,
        publish_immediately: bool = False,
        auto_account_limit: int = 10,
        immediate_interval_minutes: int = 1,
    ) -> tuple[RunResult, Path]:
        if not 1 <= immediate_interval_minutes <= 15:
            raise ValueError("즉시 발행 간격은 1분부터 15분까지 선택하세요")
        started_at = datetime.now()
        pause_event = pause_event or threading.Event()
        for job in jobs:
            if needs_photo_wash(job) and not job.photo_wash_prepared:
                job.status = JobStatus.FAILED
                job.message = (
                    "포토워셔 세탁 준비가 없습니다. "
                    "2. 데이터 확인부터 다시 실행하세요"
                )
        self.browser.ensure_v2r_login("", "")
        if not dry_run:
            saved_urls_by_job: dict[int, str] = {}
            for job in jobs:
                if job.source_kind == "account_test":
                    continue
                history_record = self.history.get(job) or {}
                saved_url = (
                    job.completion_url
                    or history_record.get("url", "")
                )
                if not saved_url:
                    continue
                saved_urls_by_job[id(job)] = saved_url
            source_states: dict[str, dict[str, str]] = {}
            if saved_urls_by_job:
                if hasattr(self.browser, "inspect_immediate_source_urls"):
                    source_states = self.browser.inspect_immediate_source_urls(
                        set(saved_urls_by_job.values())
                    )
                else:
                    probe_results = self.browser.probe_v2r_source_urls(
                        set(saved_urls_by_job.values())
                    )
                    source_states = {
                        url: {
                            "state": (
                                "deleted"
                                if deleted is True
                                else "present"
                                if deleted is False
                                else "unknown"
                            )
                        }
                        for url, deleted in probe_results.items()
                    }
            recovered_urls: set[str] = set()
            recovered_deleted = 0
            recovered_failed = 0
            unknown_count = 0
            for job in jobs:
                saved_url = saved_urls_by_job.get(id(job), "")
                if not saved_url:
                    continue
                state = source_states.get(saved_url, {}).get("state", "unknown")
                if state == "unknown":
                    unknown_count += 1
                    continue
                if state == "present":
                    continue
                recovered_urls.add(saved_url)
                if state == "deleted":
                    recovered_deleted += 1
                else:
                    recovered_failed += 1
                job.completion_url = ""
                job.status = JobStatus.PENDING
                job.message = ""
            removed_local = self.history.remove_urls(recovered_urls)
            if recovered_deleted or recovered_failed:
                self.logger.warning(
                    "현재 작업 관련 과거 source 복구: 삭제 %s건 / 실패 %s건 / "
                    "로컬 완료 이력 제거 %s건",
                    recovered_deleted,
                    recovered_failed,
                    removed_local,
                )
            if unknown_count:
                self.logger.warning(
                    "현재 작업 링크 %s건은 상태 확인 불가로 기존 중복 방지를 유지합니다",
                    unknown_count,
                )
        if auto_account_limit == 10:
            self.browser.prepare_immediate_jobs(jobs)
        else:
            self.browser.prepare_immediate_jobs(
                jobs,
                auto_account_limit=auto_account_limit,
            )
        if not dry_run and hasattr(
            self.browser,
            "refresh_immediate_account_grades",
        ):
            self.browser.refresh_immediate_account_grades(jobs)
        assign_immediate_schedules(
            jobs,
            publish_immediately=publish_immediately,
        )

        if source_sheet_url:
            for job in jobs:
                if job.source_kind != "brand" or job.status != JobStatus.PENDING:
                    continue
                try:
                    self.browser.update_sheet_cell(
                        source_sheet_url,
                        "D",
                        job.row_number,
                        job.account,
                    )
                except Exception as exc:
                    self.logger.warning(
                        "행 %s D열 작성계정 1차 저장 실패: %s "
                        "(발행은 계속하고 완료 후 다시 시도)",
                        job.row_number,
                        exc,
                    )

        retrying = 0

        def emit_status() -> None:
            if status:
                status(
                    {
                        "pending": sum(
                            job.status == JobStatus.PENDING for job in jobs
                        ),
                        "success": sum(
                            job.status == JobStatus.SUCCESS for job in jobs
                        ),
                        "reserved": sum(
                            job.status == JobStatus.RESERVED for job in jobs
                        ),
                        "failed": sum(
                            job.status == JobStatus.FAILED for job in jobs
                        ),
                        "skipped": sum(
                            job.status == JobStatus.SKIPPED for job in jobs
                        ),
                        "retrying": retrying,
                    }
                )

        pending_test_results: dict[int, ImmediateJob] = {}

        def queue_account_test_result(job: ImmediateJob) -> None:
            if job.source_kind == "account_test" and not dry_run:
                pending_test_results[job.row_number] = job

        def flush_account_test_results() -> None:
            if not source_sheet_url or not pending_test_results:
                return
            self.logger.info(
                "한줄테스트 %s건 완료: 이제 결과를 시트에 기록합니다",
                len(pending_test_results),
            )
            for job in pending_test_results.values():
                write_account_test_result(job)

        def write_account_test_result(job: ImmediateJob) -> None:
            if (
                dry_run
                or not source_sheet_url
                or job.source_kind != "account_test"
            ):
                return
            result_text = (
                f"성공 ({job.canonical_cafe_name})"
                if job.status == JobStatus.SUCCESS
                else job.message
            )
            values = {
                job.account_test_result_column: result_text
            }
            if job.status == JobStatus.SUCCESS:
                values.update(
                    {
                        job.account_test_link_column: job.post_url,
                        job.account_test_time_column: datetime.now().astimezone().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                    }
                )
            errors: list[str] = []
            for column, value in values.items():
                try:
                    self.browser.update_sheet_cell(
                        source_sheet_url,
                        column,
                        job.row_number,
                        value,
                        max_attempts=1,
                        verify_checks=3,
                    )
                except Exception as exc:
                    errors.append(f"{column}열: {exc}")
            if errors:
                self.logger.error(
                    "행 %s 한줄테스트 결과 시트 저장 실패: %s",
                    job.row_number,
                    " / ".join(errors),
                )

        emit_status()
        total = len(jobs)
        last_cafe_started: dict[int, float] = {}
        last_immediate_started = 0.0

        def wait_if_paused() -> float:
            if not pause_event.is_set():
                return 0.0
            paused_at = time.monotonic()
            self.logger.info("일시정지됨: 다시 시작을 기다립니다")
            while pause_event.is_set():
                if stop_event.wait(0.2):
                    return -1.0
            paused_seconds = time.monotonic() - paused_at
            self.logger.info(
                "다시 시작: 다음 미처리 행부터 계속합니다 (정지 %.0f초)",
                paused_seconds,
            )
            return paused_seconds

        for index, job in enumerate(jobs, start=1):
            progress(index - 1, total)
            if wait_if_paused() < 0:
                job.status = JobStatus.SKIPPED
                job.message = "사용자가 중지함"
            if stop_event.is_set():
                job.status = JobStatus.SKIPPED
                job.message = "사용자가 중지함"
                self.logger.warning(
                    "[%s/%s] 행 %s 건너뜀: %s",
                    index,
                    total,
                    job.row_number,
                    job.message,
                )
                progress(index, total)
                emit_status()
                continue
            if job.status != JobStatus.PENDING:
                log = (
                    self.logger.error
                    if job.status == JobStatus.FAILED
                    else self.logger.info
                )
                log(
                    "[%s/%s] 행 %s %s: %s",
                    index,
                    total,
                    job.row_number,
                    job.status.value,
                    job.message or "사유 없음",
                )
                progress(index, total)
                emit_status()
                queue_account_test_result(job)
                continue
            errors = job.validate()
            if errors:
                job.status = JobStatus.FAILED
                job.message = ", ".join(errors)
                self.logger.error(
                    "[%s/%s] 행 %s 형식 오류: %s",
                    index,
                    total,
                    job.row_number,
                    job.message,
                )
                progress(index, total)
                emit_status()
                continue
            if (
                not dry_run
                and job.source_kind != "account_test"
                and self.history.contains(job)
            ):
                job.status = JobStatus.SKIPPED
                job.message = "이전에 발행한 동일 글"
                self.logger.info(
                    "[%s/%s] 행 %s 건너뜀: %s",
                    index,
                    total,
                    job.row_number,
                    job.message,
                )
                progress(index, total)
                emit_status()
                continue

            while True:
                if not dry_run:
                    immediate_with_interval = (
                        job.publish_immediately
                        and job.source_kind != "account_test"
                    )
                    required_gap = (
                        immediate_interval_minutes * 60
                        if immediate_with_interval
                        else 20
                    )
                    previous_started = (
                        last_immediate_started
                        if immediate_with_interval
                        else last_cafe_started.get(job.cafe_id, 0)
                    )
                    remaining = required_gap - (
                        time.monotonic() - previous_started
                    )
                    if remaining > 0:
                        if immediate_with_interval:
                            self.logger.info(
                                "다음 즉시 발행까지 %.0f초 대기",
                                remaining,
                            )
                        deadline = time.monotonic() + remaining
                        while time.monotonic() < deadline:
                            paused_seconds = wait_if_paused()
                            if paused_seconds < 0:
                                job.status = JobStatus.SKIPPED
                                job.message = "사용자가 중지함"
                                break
                            wait_for = min(0.25, deadline - time.monotonic())
                            if wait_for > 0 and stop_event.wait(wait_for):
                                job.status = JobStatus.SKIPPED
                                job.message = "사용자가 중지함"
                                break
                        if job.status == JobStatus.SKIPPED:
                            self.logger.warning(
                                "[%s/%s] 행 %s 건너뜀: %s",
                                index,
                                total,
                                job.row_number,
                                job.message,
                            )
                            break
                    if immediate_with_interval:
                        last_immediate_started = time.monotonic()
                    else:
                        last_cafe_started[job.cafe_id] = time.monotonic()
                if job.scheduled_at is not None:
                    minimum_start = datetime.now(timezone.utc) + timedelta(minutes=2)
                    if job.scheduled_at < minimum_start:
                        original = job.scheduled_at
                        adjusted = datetime.now(timezone.utc) + timedelta(
                            minutes=random.SystemRandom().randint(5, 15)
                        )
                        shift = adjusted - original
                        for pending_job in jobs[index - 1 :]:
                            if (
                                pending_job.status == JobStatus.PENDING
                                and pending_job.cafe_id == job.cafe_id
                                and pending_job.scheduled_at is not None
                            ):
                                pending_job.scheduled_at += shift
                        self.logger.info(
                            "%s 일시정지 중 지난 예약만 최소 조정: %s → %s",
                            job.canonical_cafe_name,
                            original.astimezone().strftime("%Y-%m-%d %H:%M"),
                            job.scheduled_at.astimezone().strftime("%Y-%m-%d %H:%M"),
                        )
                try:
                    self.logger.info(
                        "[%s/%s] %s 행 %s %s: %s / %s / %s / %s",
                        index,
                        total,
                        job.source_name,
                        job.row_number,
                        (
                            "즉시 발행"
                            if job.publish_immediately
                            else "예약 등록"
                        ),
                        job.canonical_cafe_name,
                        job.canonical_board_name,
                        job.account,
                        job.scheduled_at.astimezone().strftime("%Y-%m-%d %H:%M")
                        if job.scheduled_at
                        else "즉시",
                    )
                    job.post_url = self.browser.publish_immediate(job, dry_run)
                    job.status = (
                        JobStatus.SUCCESS
                        if job.publish_immediately
                        else JobStatus.RESERVED
                    )
                    job.message = (
                        (
                            "즉시 발행 API 검증 완료"
                            if job.publish_immediately
                            else "예약 API 검증 완료"
                        )
                        if dry_run
                        else (
                            "즉시 발행 완료"
                            if job.publish_immediately
                            else "예약 발행 등록 완료"
                        )
                    )
                    if not dry_run:
                        self.history.record(job)
                        queue_account_test_result(job)
                        if source_sheet_url and job.source_kind == "brand":
                            sheet_errors: list[str] = []
                            try:
                                self.browser.update_sheet_cell(
                                    source_sheet_url,
                                    "D",
                                    job.row_number,
                                    job.account,
                                )
                            except Exception as sheet_error:
                                sheet_errors.append(
                                    f"D열 작성계정 저장 실패: {sheet_error}"
                                )
                            try:
                                self.browser.update_sheet_cell(
                                    source_sheet_url,
                                    "F",
                                    job.row_number,
                                    job.post_url,
                                )
                            except Exception as sheet_error:
                                sheet_errors.append(
                                    f"F열 완료 링크 저장 실패: {sheet_error}"
                                )
                            if sheet_errors:
                                job.message += " / " + " / ".join(sheet_errors)
                                self.logger.error(
                                    "행 %s 발행은 완료됐지만 시트 저장 실패: %s",
                                    job.row_number,
                                    " / ".join(sheet_errors),
                                )
                    break
                except Exception as exc:
                    reason, can_replace = self.browser.classify_immediate_failure(exc)
                    replacement = (
                        self.browser.replace_failed_immediate_account(job)
                        if can_replace
                        else ""
                    )
                    if replacement:
                        retrying += 1
                        emit_status()
                        if source_sheet_url and job.source_kind == "brand":
                            try:
                                self.browser.update_sheet_cell(
                                    source_sheet_url,
                                    "D",
                                    job.row_number,
                                    replacement,
                                )
                            except Exception as sheet_error:
                                self.logger.warning(
                                    "행 %s 교체계정 D열 저장 실패: %s "
                                    "(새 계정으로 발행은 계속)",
                                    job.row_number,
                                    sheet_error,
                                )
                        self.logger.warning(
                            "행 %s 작성계정 교체 후 재시도: %s",
                            job.row_number,
                            replacement,
                        )
                        continue
                    job.status = JobStatus.FAILED
                    job.message = reason
                    if source_sheet_url and job.source_kind == "brand" and not dry_run:
                        try:
                            self.browser.update_sheet_cell(
                                source_sheet_url,
                                "F",
                                job.row_number,
                                reason,
                            )
                        except Exception:
                            self.logger.exception(
                                "행 %s 실패 사유 시트 저장 실패",
                                job.row_number,
                            )
                    queue_account_test_result(job)
                    self.logger.exception("행 %s 즉시 발행 실패", job.row_number)
                    break
            progress(index, total)
            emit_status()

        flush_account_test_results()
        result = RunResult(
            started_at=started_at,
            finished_at=datetime.now(),
            dry_run=dry_run,
            jobs=jobs,
        )
        report_path = write_report(result, self.report_dir)
        progress(total, total)
        return result, report_path
