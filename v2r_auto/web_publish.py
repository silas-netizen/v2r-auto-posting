"""API-free publishing orchestration over semantic browser UI operations.

The browser implementation is deliberately injected.  This module decides
which screens and fields to use, while the adapter owns selectors, waits, and
all interaction with the V2R web application.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, Sequence

from .content import CommentNode
from .images import strip_placeholders
from .models import AffiliateJob, ImmediateJob, JobStatus


AFFILIATE_CAFE_DELAYS = {
    "씨씨앙": timedelta(hours=4),
    "양평맘": timedelta(hours=20),
    "쌍둥이맘 모여라": timedelta(hours=22),
}
AFFILIATE_DAILY_BOARDS = {
    "씨씨앙": "자유 수다방",
    "양평맘": "이모저모 이야기",
    "쌍둥이맘 모여라": "가족업체 자유게시판",
}


class WebPublishError(RuntimeError):
    """A web-publishing request cannot be completed safely."""


class WebPublishBrowser(Protocol):
    """Semantic UI operations required by :class:`WebPublisher`.

    Implementations must interact with visible browser state only.  In
    particular, ``register`` must click the UI registration control and return
    the completion URL observed after that action.
    """

    def first_available_account(
        self,
        cafe: str,
        board: str,
        account_type: str,
    ) -> str:
        """Return the first enabled account shown by the destination UI."""

    def open_writer(self) -> None:
        """Open a fresh article writer."""

    def select_destination(
        self,
        cafe: str,
        account: str,
        board: str,
        prefix: str,
    ) -> None:
        """Select cafe, account, board, and optional prefix in that order."""

    def select_publish_mode(self, mode: str) -> None:
        """Select ``scheduled`` or ``immediate`` in the writer UI."""

    def set_schedule(self, scheduled_at: datetime) -> None:
        """Fill the visible schedule control."""

    def fill_article(
        self,
        title: str,
        body: str,
        tags: Sequence[str],
    ) -> None:
        """Fill title, body, and tags without submitting."""

    def upload_images(self, paths: Sequence[str]) -> None:
        """Attach prepared images through the visible SmartEditor file chooser."""

    def register(self) -> str:
        """Submit the current UI form and return its completion URL."""

    def open_article(self, completion_url: str) -> None:
        """Open an article from its completion URL."""

    def reserve_revision(self, scheduled_at: datetime) -> None:
        """Open the revision writer and set its reservation time."""

    def reserve_comment(
        self,
        text: str,
        *,
        parent_text: str | None,
        scheduled_at: datetime,
    ) -> None:
        """Reserve one root comment or reply through the article UI."""


def _usable_account(value: str) -> str:
    return value.strip()


def assign_first_available_accounts(
    jobs: Sequence[AffiliateJob | ImmediateJob],
    browser: WebPublishBrowser,
) -> list[AffiliateJob | ImmediateJob]:
    """Fill only blank sheet accounts using the first account offered by UI.

    Explicit sheet accounts are authoritative and are never validated,
    replaced, or reordered here.
    """

    assigned: list[AffiliateJob | ImmediateJob] = []
    for job in jobs:
        if job.status != JobStatus.PENDING or _usable_account(job.account):
            continue
        account = _usable_account(
            browser.first_available_account(
                job.cafe,
                job.board,
                job.account_type,
            )
        )
        if not account:
            raise WebPublishError(
                f"{job.cafe} / {job.board} 화면에 사용 가능한 작성계정이 없습니다"
            )
        job.account = account
        assigned.append(job)
    return assigned


class WebPublisher:
    """Coordinate affiliate and self-owned publishing through browser UI."""

    def __init__(self, browser: WebPublishBrowser):
        self.browser = browser

    def _account(self, job: AffiliateJob | ImmediateJob) -> str:
        explicit = _usable_account(job.account)
        if explicit:
            return explicit
        assign_first_available_accounts([job], self.browser)
        return job.account

    def _new_article(
        self,
        *,
        cafe: str,
        account: str,
        board: str,
        prefix: str,
        title: str,
        body: str,
        tags: Sequence[str],
        mode: str,
        scheduled_at: datetime | None,
    ) -> None:
        self.browser.open_writer()
        self.browser.select_destination(cafe, account, board, prefix)
        self.browser.select_publish_mode(mode)
        if mode == "scheduled":
            if scheduled_at is None:
                raise WebPublishError("예약 발행 시간이 준비되지 않았습니다")
            self.browser.set_schedule(scheduled_at)
        self.browser.fill_article(title, body, tuple(tags))

    @staticmethod
    def _reserve_comments(
        browser: WebPublishBrowser,
        comments: Sequence[CommentNode],
        published_at: datetime,
    ) -> None:
        minute_offset = 5

        def reserve(nodes: Sequence[CommentNode], parent_text: str | None = None) -> None:
            nonlocal minute_offset
            for node in nodes:
                browser.reserve_comment(
                    node.text,
                    parent_text=parent_text,
                    scheduled_at=published_at + timedelta(minutes=minute_offset),
                )
                minute_offset += 1
                reserve(node.children, node.text)

        reserve(comments)

    def publish_affiliate(
        self,
        job: AffiliateJob,
        *,
        dry_run: bool = False,
    ) -> str:
        """Publish a scheduled daily post and reserve its revision and comments.

        A dry run fills the daily-post form but does not register anything;
        consequently it cannot enter the revision or comment screens.
        """

        if job.daily_post is None:
            raise WebPublishError("배정된 일상 글이 없습니다")
        if job.daily_scheduled_at is None:
            raise WebPublishError("일상 글 예약 시간이 준비되지 않았습니다")
        try:
            delay = AFFILIATE_CAFE_DELAYS[job.cafe]
            daily_board = AFFILIATE_DAILY_BOARDS[job.cafe]
        except KeyError as exc:
            raise WebPublishError(f"지원하지 않는 제휴 카페입니다: {job.cafe}") from exc

        account = self._account(job)
        daily_prefix = "일상" if job.cafe == "씨씨앙" else ""
        self._new_article(
            cafe=job.cafe,
            account=account,
            board=daily_board,
            prefix=daily_prefix,
            title=job.daily_post.title,
            body=job.daily_post.body,
            tags=(),
            mode="scheduled",
            scheduled_at=job.daily_scheduled_at,
        )
        if dry_run:
            return ""

        daily_url = self.browser.register()
        if not daily_url:
            raise WebPublishError("일상 글 등록 후 완료 URL을 확인하지 못했습니다")
        job.daily_post_url = daily_url
        self.browser.open_article(daily_url)

        revision_at = job.daily_scheduled_at + delay
        self.browser.reserve_revision(revision_at)
        self.browser.fill_article(
            job.title,
            strip_placeholders(job.body),
            tuple(job.tags),
        )
        image_paths = [
            str(item.local_path)
            for item in job.prepared_images
            if getattr(item, "local_path", None)
        ]
        if image_paths:
            self.browser.upload_images(image_paths)
        revision_url = self.browser.register()
        if not revision_url:
            raise WebPublishError("수정 글 등록 후 완료 URL을 확인하지 못했습니다")
        job.revision_url = revision_url

        if job.comments:
            self.browser.open_article(revision_url)
            self._reserve_comments(self.browser, job.comments, revision_at)
        return revision_url

    def publish_immediate(
        self,
        job: ImmediateJob,
        *,
        dry_run: bool = False,
    ) -> str:
        """Publish or reserve one self-owned article and its UI comments."""

        account = self._account(job)
        immediate = job.publish_immediately
        mode = "immediate" if immediate else "scheduled"
        scheduled_at = None if immediate else job.scheduled_at
        self._new_article(
            cafe=job.canonical_cafe_name or job.cafe,
            account=account,
            board=job.canonical_board_name or job.board,
            prefix=job.canonical_head_name or job.prefix,
            title=job.title,
            body=strip_placeholders(job.body),
            tags=job.tags,
            mode=mode,
            scheduled_at=scheduled_at,
        )
        image_paths = [
            str(item.local_path)
            for item in job.prepared_images
            if getattr(item, "local_path", None)
        ]
        if image_paths:
            self.browser.upload_images(image_paths)
        if dry_run:
            return ""

        completion_url = self.browser.register()
        if not completion_url:
            raise WebPublishError("글 등록 후 완료 URL을 확인하지 못했습니다")
        job.post_url = completion_url
        if job.comments:
            self.browser.open_article(completion_url)
            comment_start = scheduled_at or datetime.now(timezone.utc)
            self._reserve_comments(self.browser, job.comments, comment_start)
        return completion_url
