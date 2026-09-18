from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    REGISTERED = "registered"
    PUBLISHED = "published"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


ALLOWED_TASKS = (
    "publish_daily",
    "publish_brand",
    "publish_info",
    "publish_batch",
    "inspect_failures",
    "sync_sources",
    "sync_all_sources",
    "collect_daily",
    "status",
    "stop",
    "open_login",
)


@dataclass(slots=True)
class TaskSpec:
    task: str
    count: int = 0
    account_mode: str = "auto"
    account_count: int = 0
    accounts: list[str] = field(default_factory=list)
    window_start: str = "09:00"
    window_end: str = "18:00"
    interval_minutes: int = 10
    start_date: str = ""
    cafe: str = ""
    board: str = ""
    brand: str = ""
    source: str = ""
    dry_run: bool = True
    notes: str = ""
    manuscripts: list[dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        if self.task not in ALLOWED_TASKS:
            raise ValueError(f"허용되지 않은 작업입니다: {self.task}")
        if self.account_mode not in {"auto", "manual"}:
            raise ValueError("accountMode는 auto 또는 manual만 사용할 수 있습니다")
        if self.count < 0 or self.account_count < 0:
            raise ValueError("count와 accountCount는 0 이상이어야 합니다")
        if self.interval_minutes < 0:
            raise ValueError("intervalMinutes는 0 이상이어야 합니다")

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["accountMode"] = payload.pop("account_mode")
        payload["accountCount"] = payload.pop("account_count")
        payload["windowStart"] = payload.pop("window_start")
        payload["windowEnd"] = payload.pop("window_end")
        payload["intervalMinutes"] = payload.pop("interval_minutes")
        payload["startDate"] = payload.pop("start_date")
        payload["dryRun"] = payload.pop("dry_run")
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TaskSpec":
        return cls(
            task=str(payload.get("task") or ""),
            count=int(payload.get("count") or 0),
            account_mode=str(payload.get("accountMode") or payload.get("account_mode") or "auto"),
            account_count=int(payload.get("accountCount") or payload.get("account_count") or 0),
            accounts=[str(item) for item in payload.get("accounts") or []],
            window_start=str(payload.get("windowStart") or payload.get("window_start") or "09:00"),
            window_end=str(payload.get("windowEnd") or payload.get("window_end") or "18:00"),
            interval_minutes=int(
                payload.get("intervalMinutes") or payload.get("interval_minutes") or 10
            ),
            start_date=str(payload.get("startDate") or payload.get("start_date") or ""),
            cafe=str(payload.get("cafe") or ""),
            board=str(payload.get("board") or ""),
            brand=str(payload.get("brand") or ""),
            source=str(payload.get("source") or ""),
            dry_run=bool(payload.get("dryRun", payload.get("dry_run", True))),
            notes=str(payload.get("notes") or ""),
            manuscripts=list(payload.get("manuscripts") or []),
        )


@dataclass(slots=True)
class JobRecord:
    id: int
    idempotency_key: str
    task: str
    payload: dict[str, Any]
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    lease_owner: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True, slots=True)
class Account:
    login_id: str
    work_type: str
    linked: str
    excluded: bool = False
    grade: str = ""
    nickname: str = ""
    shade: str = ""


@dataclass(frozen=True, slots=True)
class Manuscript:
    title: str
    body: str
    cafe: str = ""
    board: str = ""
    source: str = ""
    keyword: str = ""
    account: str = ""
    comments: list[dict[str, Any]] = field(default_factory=list)
