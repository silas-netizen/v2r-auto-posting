from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .accounts import assign_accounts
from .alerts.slack import SlackChannel
from .alerts.telegram import TelegramChannel
from .browser.comments import plan_comment_tree
from .browser.modification import revision_at
from .browser.publish import RecordingBrowser, publish_planned_slots
from .collect.daily import collect_daily_manuscripts, plan_daily_publish
from .collect.public import PublicPageReader
from .commands import describe_spec, parse_korean_command
from .duplicate import highest_match
from .models import Account, JobStatus, Manuscript, TaskSpec
from .providers.router import ModelRouter
from .scheduler import plan_slots
from .sources import SourceError, SourceRef, load_source_config, sync_source
from .store import JobStore


KST = ZoneInfo("Asia/Seoul")
AFFILIATE_BOARDS = {
    "씨씨앙": "자유 수다방",
    "양평맘": "이모저모 이야기",
    "쌍둥이맘": "가족업체 자유게시판",
    "쌍둥이맘 모여라": "가족업체 자유게시판",
}


class CommandRuntime:
    def __init__(
        self,
        root: Path,
        *,
        store: JobStore | None = None,
        router: ModelRouter | None = None,
        telegram: TelegramChannel | None = None,
        slack: SlackChannel | None = None,
        browser_factory=RecordingBrowser,
        owner: str = "",
    ):
        self.root = root
        self.store = store or JobStore(root / "data" / "v2r.sqlite")
        self.router = router or ModelRouter()
        self.telegram = telegram or TelegramChannel()
        self.slack = slack or SlackChannel()
        self.browser_factory = browser_factory
        self.owner = owner or f"pc-{uuid4().hex[:8]}"
        self.config_dir = root / "config"

    def handle_text(self, text: str, *, idempotency_key: str = "") -> dict[str, Any]:
        spec = parse_korean_command(text)
        if spec is None:
            if self.router.available("claude"):
                payload = self.router.complete_object(
                    "claude",
                    system=(
                        "너는 V2R 명령 해석기다. 허용 작업만 JSON으로 반환한다. "
                        "허용: publish_daily, publish_brand, publish_info, "
                        "publish_batch, inspect_failures, sync_sources, "
                        "sync_all_sources, collect_daily, status, stop, open_login."
                    ),
                    user=text,
                )
                spec = TaskSpec.from_payload(payload)
                spec.validate()
            else:
                raise ValueError("명령을 규칙으로 해석하지 못했습니다. 더 명확히 적어주세요.")
        if spec.task == "status":
            return self.status()
        if spec.task == "stop":
            cancelled = self.store.cancel_running()
            return {"ok": True, "cancelled": cancelled}
        job = self.store.enqueue(spec, idempotency_key=idempotency_key)
        processed = self.run_once()
        return {
            "ok": True,
            "accepted": describe_spec(spec),
            "job_id": job.id,
            "processed": processed,
        }

    def run_once(self) -> dict[str, Any] | None:
        job = self.store.acquire(self.owner)
        if job is None:
            return None
        spec = TaskSpec.from_payload(job.payload)
        try:
            result = self._execute(spec)
            status = JobStatus.REGISTERED if result.get("registered") else JobStatus.PUBLISHED
            if spec.dry_run or spec.task in {
                "status",
                "sync_sources",
                "sync_all_sources",
                "collect_daily",
                "inspect_failures",
                "open_login",
            }:
                status = JobStatus.PUBLISHED
            finished = self.store.finish(job.id, status, result=result)
            self._notify(f"작업 {finished.id} {finished.status.value}: {describe_spec(spec)}")
            return {"id": finished.id, "status": finished.status.value, "result": result}
        except Exception as exc:
            finished = self.store.finish(
                job.id,
                JobStatus.FAILED,
                error=str(exc),
            )
            self._notify(f"작업 {finished.id} 실패: {exc}")
            raise

    def status(self) -> dict[str, Any]:
        jobs = self.store.list_jobs(10)
        return {
            "ok": True,
            "jobs": [
                {
                    "id": job.id,
                    "task": job.task,
                    "status": job.status.value,
                    "error": job.error,
                }
                for job in jobs
            ],
        }

    def _execute(self, spec: TaskSpec) -> dict[str, Any]:
        if spec.task in {"sync_sources", "sync_all_sources"}:
            return self._sync(all_sources=spec.task == "sync_all_sources")
        if spec.task == "collect_daily":
            return self._collect(spec)
        if spec.task == "inspect_failures":
            return self._inspect()
        if spec.task == "open_login":
            return {"login": "브라우저 프로필에서 수동 로그인 대기"}
        if spec.task.startswith("publish"):
            return self._publish(spec)
        raise ValueError(f"실행기가 아직 이 작업을 처리하지 않습니다: {spec.task}")

    def _sync(self, *, all_sources: bool) -> dict[str, Any]:
        refs = self._source_refs()
        selected = refs if all_sources else refs[:1]
        synced = []
        for ref in selected:
            payload = sync_source(ref, self.store)
            synced.append(
                {
                    "name": ref.name,
                    "manuscripts": len(payload.get("manuscripts") or payload.get("payload", {}).get("manuscripts") or []),
                    "status": payload.get("status", "ok"),
                }
            )
        return {"synced": synced}

    def _collect(self, spec: TaskSpec) -> dict[str, Any]:
        urls = [item.url for item in self._source_refs() if item.kind == "public"]
        if spec.source:
            urls = [spec.source]
        manuscripts = []
        if urls:
            manuscripts = collect_daily_manuscripts(
                urls,
                self.router,
                reader=PublicPageReader(),
                cafe=spec.cafe,
                board=spec.board,
            )
        if not manuscripts:
            manuscripts = self._cached_manuscripts()
        plan = plan_daily_publish(
            manuscripts,
            self.router,
            count=spec.count or len(manuscripts),
            account_count=spec.account_count,
            window_start=spec.window_start,
            window_end=spec.window_end,
            interval_minutes=spec.interval_minutes,
            start_date=spec.start_date,
        )
        planned = TaskSpec.from_payload(plan)
        planned.validate()
        self.store.enqueue(planned)
        return {
            "collected": len(manuscripts),
            "queued_publish": planned.task,
            "count": planned.count,
        }

    def _inspect(self) -> dict[str, Any]:
        failed = [
            {
                "id": job.id,
                "task": job.task,
                "error": job.error,
            }
            for job in self.store.list_jobs(50)
            if job.status in {JobStatus.FAILED, JobStatus.UNCERTAIN}
        ]
        return {"failures": failed, "retryable": [item for item in failed if item["error"]]}

    def _publish(self, spec: TaskSpec) -> dict[str, Any]:
        manuscripts = [Manuscript(**item) for item in spec.manuscripts] if spec.manuscripts else self._cached_manuscripts()
        if spec.count:
            manuscripts = manuscripts[: spec.count]
        if not manuscripts:
            raise SourceError("발행할 원고가 없습니다. 먼저 원본을 동기화하거나 수집하세요.")
        accounts = self._load_accounts()
        work_type = "자사 카페" if spec.task == "publish_info" else (
            "제휴 작업" if spec.task == "publish_brand" else "자사 카페"
        )
        if spec.cafe in {"씨씨앙", "양평맘", "쌍둥이맘", "쌍둥이맘 모여라"}:
            work_type = "제휴 작업"
        selected_accounts = assign_accounts(
            accounts,
            work_type=work_type,
            account_count=spec.account_count or min(1, len(accounts)),
            explicit=spec.accounts or None,
        )
        slots_at = plan_slots(
            count=len(manuscripts),
            start_date=spec.start_date,
            window_start=spec.window_start,
            window_end=spec.window_end,
            interval_minutes=spec.interval_minutes,
        )
        history = self.store.history_corpus()
        planned: list[dict[str, Any]] = []
        for index, manuscript in enumerate(manuscripts):
            verdict = highest_match(manuscript.title, manuscript.body, history)
            if verdict.exact:
                raise ValueError(f"완전 중복 원고입니다: {manuscript.title}")
            if verdict.score >= 0.92:
                raise ValueError(f"유사 원고 검토가 필요합니다: {manuscript.title}")
            account = selected_accounts[index % len(selected_accounts)]
            cafe = manuscript.cafe or spec.cafe or "고요한 아침"
            board = manuscript.board or spec.board or AFFILIATE_BOARDS.get(cafe, "자유게시판")
            scheduled_at = slots_at[index]
            planned.append(
                {
                    "title": manuscript.title,
                    "body": manuscript.body,
                    "cafe": cafe,
                    "board": board,
                    "account": account.login_id,
                    "scheduled_at": scheduled_at,
                    "revision_at": (
                        revision_at(cafe, scheduled_at)
                        if cafe in AFFILIATE_BOARDS
                        else None
                    ),
                    "comments": plan_comment_tree(scheduled_at),
                }
            )
        browser = self.browser_factory()
        results = publish_planned_slots(browser, planned, dry_run=spec.dry_run)
        if not spec.dry_run:
            for item in results:
                self.store.record_history(
                    cafe=item["cafe"],
                    board=item["board"],
                    title=item["title"],
                    body=item["body"],
                    url=item.get("url") or "",
                )
        return {
            "planned": len(results),
            "dry_run": spec.dry_run,
            "registered": sum(1 for item in results if item.get("url")),
            "slots": [
                {
                    "title": item["title"],
                    "account": item["account"],
                    "cafe": item["cafe"],
                    "scheduled_at": item["scheduled_at"].isoformat(),
                    "url": item.get("url", ""),
                }
                for item in results
            ],
        }

    def _source_refs(self) -> list[SourceRef]:
        path = self.config_dir / "sources.json"
        if not path.exists():
            return []
        return load_source_config(path)

    def _cached_manuscripts(self) -> list[Manuscript]:
        manuscripts: list[Manuscript] = []
        for ref in self._source_refs():
            cached = self.store.load_source(ref.name)
            if not cached:
                continue
            payload = cached.get("payload") or {}
            for item in payload.get("manuscripts") or []:
                manuscripts.append(Manuscript(**item))
        return manuscripts

    def _load_accounts(self) -> list[Account]:
        path = self.config_dir / "accounts.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [Account(**item) for item in payload]
        for ref in self._source_refs():
            cached = self.store.load_source(ref.name)
            if not cached:
                continue
            rows = (cached.get("payload") or {}).get("accounts") or []
            if rows:
                return [Account(**item) for item in rows]
        return [
            Account(login_id="demo1", work_type="자사 카페", linked="V2R"),
            Account(login_id="demo2", work_type="자사 카페", linked="V2R"),
            Account(login_id="demo3", work_type="제휴 작업", linked="V2R"),
        ]

    def _notify(self, text: str) -> None:
        if self.telegram.enabled():
            for chat_id in self.telegram.allowed_chat_ids:
                self.telegram.send(chat_id, text)
        if self.slack.enabled():
            self.slack.send(text)
