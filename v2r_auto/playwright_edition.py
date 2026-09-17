"""Playwright web-only edition adapter for the existing runners.

The runner-facing method names are retained for compatibility, but every
publishing operation delegates to visible Playwright UI actions.  This module
does not import or instantiate either V2R API publisher.
"""

from __future__ import annotations

from .models import JobStatus
from .playwright_browser import PlaywrightBrowser, PlaywrightBrowserConfig
from .web_publish import (
    AFFILIATE_DAILY_BOARDS,
    WebPublisher,
    assign_first_available_accounts,
)


class PlaywrightWebBrowser(PlaywrightBrowser):
    """Expose the existing runner contract through API-free web publishing."""

    def __init__(self, config, logger):
        super().__init__(
            PlaywrightBrowserConfig(
                profile_dir=config.profile_dir,
                download_dir=config.download_dir,
                timeout_seconds=config.timeout_seconds,
            ),
            logger,
        )
        self._web_publisher = WebPublisher(self)
        self._account_fallbacks: dict[tuple[str, str, str], list[str]] = {}
        self._account_indexes: dict[tuple[str, str, str], int] = {}

    def first_available_account(
        self,
        cafe: str,
        board: str,
        account_type: str,
    ) -> str:
        key = (cafe, board, account_type)
        if key not in self._account_fallbacks:
            self._account_fallbacks[key] = super().available_accounts(
                cafe,
                board,
                account_type,
            )
        accounts = self._account_fallbacks[key]
        if not accounts:
            return ""
        index = self._account_indexes.get(key, 0)
        self._account_indexes[key] = index + 1
        return accounts[index % len(accounts)]

    def start_affiliate_api_run(self, jobs=None) -> None:
        del jobs

    def assign_affiliate_accounts(self, jobs):
        assigned = []
        for job in jobs:
            if job.status != JobStatus.PENDING or job.account.strip():
                continue
            account = self.first_available_account(
                job.cafe,
                AFFILIATE_DAILY_BOARDS.get(job.cafe, job.board),
                job.account_type,
            ).strip()
            if not account:
                job.status = JobStatus.FAILED
                job.message = "웹 화면에 사용 가능한 작성계정이 없습니다"
                continue
            job.account = account
            assigned.append(job)
        return assigned

    def refresh_affiliate_account_grades(self, jobs):
        return [
            job
            for job in jobs
            if job.status == JobStatus.PENDING
        ]

    def publish_affiliate_revision(
        self,
        job,
        dry_run,
        resume=None,
        checkpoint=None,
        wait_control=None,
    ) -> str:
        del resume, wait_control
        url = self._web_publisher.publish_affiliate(job, dry_run=dry_run)
        if checkpoint and url:
            checkpoint(
                "REVISION_CREATED",
                daily_source_id=job.daily_post_url.rsplit("/", 1)[-1],
                revision_source_id=url.rsplit("/", 1)[-1],
            )
        return url

    def probe_v2r_source_urls(self, urls):
        # Existing links are preserved when a web-only run cannot establish a
        # definitive deleted/present state without direct API inspection.
        return {url: None for url in urls}

    def reset_deleted_affiliate_sources(self, job, resume) -> None:
        del job, resume

    @staticmethod
    def classify_affiliate_failure(error: Exception) -> tuple[str, bool]:
        return f"실패: {str(error)[:150]}", False

    def replace_failed_affiliate_account(self, job) -> str:
        del job
        return ""

    def prepare_immediate_jobs(self, jobs, auto_account_limit: int = 10) -> None:
        del auto_account_limit
        assign_first_available_accounts(jobs, self)
        for job in jobs:
            if job.status != JobStatus.PENDING:
                continue
            job.cafe_id = job.cafe_id or 1
            job.menu_id = job.menu_id or 1
            job.canonical_cafe_name = job.canonical_cafe_name or job.cafe
            job.canonical_board_name = job.canonical_board_name or job.board
            job.canonical_head_name = job.canonical_head_name or job.prefix

    def refresh_immediate_account_grades(self, jobs):
        return [
            job
            for job in jobs
            if job.status == JobStatus.PENDING
        ]

    def publish_immediate(self, job, dry_run: bool) -> str:
        return self._web_publisher.publish_immediate(job, dry_run=dry_run)

    def inspect_immediate_source_urls(self, urls):
        return {
            url: {"state": "unknown", "reason": "웹 전용판은 기존 링크를 보존합니다"}
            for url in urls
        }

    @staticmethod
    def classify_immediate_failure(error: Exception) -> tuple[str, bool]:
        return f"실패: {str(error)[:150]}", False

    def replace_failed_immediate_account(self, job) -> str:
        del job
        return ""

    @staticmethod
    def consume_failed_immediate_urls() -> set[str]:
        return set()
