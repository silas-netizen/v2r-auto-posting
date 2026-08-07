from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


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
class RunResult:
    started_at: datetime
    finished_at: datetime
    dry_run: bool
    jobs: list[PostJob]

    @property
    def succeeded(self) -> int:
        return sum(job.status == JobStatus.SUCCESS for job in self.jobs)

    @property
    def failed(self) -> int:
        return sum(job.status == JobStatus.FAILED for job in self.jobs)

    @property
    def skipped(self) -> int:
        return sum(job.status == JobStatus.SKIPPED for job in self.jobs)
