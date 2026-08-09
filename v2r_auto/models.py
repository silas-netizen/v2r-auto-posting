from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .content import CommentNode, ParsedArticle


class JobStatus(str, Enum):
    PENDING = "대기"
    VALIDATED = "검증완료"
    SKIPPED = "건너뜀"
    SUCCESS = "완료"
    FAILED = "실패"


@dataclass(slots=True)
class PostJob:
    row_number: int
    keyword: str
    title: str
    body: str
    cafe: str = ""
    board: str = ""
    account: str = ""
    publish_at: str = ""
    tags: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    status: JobStatus = JobStatus.PENDING
    message: str = ""
    post_url: str = ""

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.title.strip():
            errors.append("제목이 없습니다")
        if not self.body.strip():
            errors.append("본문이 없습니다")
        if not self.cafe.strip():
            errors.append("카페가 없습니다")
        if not self.board.strip():
            errors.append("게시판이 없습니다")
        return errors


@dataclass(slots=True)
class AffiliateJob:
    """One affiliate-cafe scheduled-revision task from the compact sheet."""

    row_number: int
    keyword: str
    article: ParsedArticle
    cafe: str
    account: str
    article_type: str
    completion_url: str = ""
    status: JobStatus = JobStatus.PENDING
    message: str = ""
    daily_post_url: str = ""
    revision_url: str = ""

    @property
    def title(self) -> str:
        return self.article.title

    @property
    def body(self) -> str:
        return self.article.body

    @property
    def tags(self) -> list[str]:
        return [self.article.tag]

    @property
    def comments(self) -> list[CommentNode]:
        return self.article.comments

    @property
    def board(self) -> str:
        return "카페별 자동 선택"

    @property
    def post_url(self) -> str:
        return self.revision_url or self.completion_url

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.cafe.strip():
            errors.append("카페명이 없습니다")
        elif self.cafe.strip() not in {"씨씨앙", "양평맘"}:
            errors.append("카페명은 씨씨앙 또는 양평맘만 사용할 수 있습니다")
        if not self.account.strip():
            errors.append("작성계정이 없습니다")
        if self.article_type.strip() not in {"질문형", "후기형"}:
            errors.append("원고유형은 질문형 또는 후기형이어야 합니다")
        return errors


@dataclass(slots=True)
class RunResult:
    started_at: datetime
    finished_at: datetime
    dry_run: bool
    jobs: list[PostJob | AffiliateJob]

    @property
    def succeeded(self) -> int:
        return sum(job.status == JobStatus.SUCCESS for job in self.jobs)

    @property
    def failed(self) -> int:
        return sum(job.status == JobStatus.FAILED for job in self.jobs)

    @property
    def skipped(self) -> int:
        return sum(job.status == JobStatus.SKIPPED for job in self.jobs)
