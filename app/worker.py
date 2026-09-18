from __future__ import annotations

import json
import os
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
from .browser.playwright_v2r import PlaywrightV2RBrowser
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
        browser_factory=None,
        owner: str = "",
    ):
        self.root = root
        self.store = store or JobStore(root / "data" / "v2r.sqlite")
        self.router = router or ModelRouter()
        self.telegram = telegram or TelegramChannel()
        self.slack = slack or SlackChannel()
        self.browser_factory = browser_factory or self._default_browser_factory
        self.owner = owner or f"pc-{uuid4().hex[:8]}"
        self.config_dir = root / "config"

    def _default_browser_factory(self):
        if os.environ.get("V2R_BROWSER", "").strip().casefold() == "playwright":
            return PlaywrightV2RBrowser(
                self.root / "browser-profile",
                self.root / "data" / "downloads",
            )
        return RecordingBrowser()

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
            browser = self.browser_factory()
            if not isinstance(browser, PlaywrightV2RBrowser):
                return {"login": "검증 브라우저에서는 로그인 창을 열지 않습니다"}
            browser.open_login()
            return {"login": "실행 PC의 V2R 로그인 창을 열었습니다"}
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
        if spec.source and not spec.manuscripts:
            manuscripts = [
                item for item in manuscripts if item.source == spec.source
            ]
        if spec.task == "publish_brand" and not spec.manuscripts:
            manuscripts = [
                item for item in manuscripts if item.source != "랜덤일상"
            ]
        manuscripts = [
            item
            for item in manuscripts
            if not self.store.publication_exists(
                source_key=item.source,
                row_number=item.source_row,
                content_hash=item.content_hash,
            )
        ]
        if spec.count:
            manuscripts = manuscripts[: spec.count]
        if not manuscripts:
            raise SourceError(
                "발행할 신규 원고가 없습니다. 완료 이력의 원고는 재발행하지 않습니다."
            )
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
        daily_pool = (
            self._cached_manuscripts("랜덤일상")
            if spec.task == "publish_brand"
            else []
        )
        if spec.task == "publish_brand" and len(daily_pool) < len(manuscripts):
            raise SourceError(
                "제휴 원본 일상 글이 부족합니다. 랜덤일상 원본을 먼저 동기화하세요."
            )
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
            slot = {
                    "title": manuscript.title,
                    "body": manuscript.body,
                    "cafe": cafe,
                    "board": board,
                    "account": account.login_id,
                    "source_key": manuscript.source,
                    "source_row": manuscript.source_row,
                    "content_hash": manuscript.content_hash,
                    "scheduled_at": scheduled_at,
                    "revision_at": (
                        revision_at(cafe, scheduled_at)
                        if cafe in AFFILIATE_BOARDS
                        else None
                    ),
                    "comments": self._scheduled_comments(
                        manuscript.comments,
                        scheduled_at,
                    ),
                }
            if spec.task == "publish_brand":
                daily = daily_pool[index]
                slot.update(
                    {
                        "workflow": "affiliate",
                        "daily": {
                            "title": daily.title,
                            "body": daily.body,
                        },
                    }
                )
            planned.append(slot)
        browser = self.browser_factory()
        try:
            results = publish_planned_slots(browser, planned, dry_run=spec.dry_run)
        finally:
            close = getattr(browser, "close", None)
            if callable(close):
                close()
        if not spec.dry_run:
            for item in results:
                self.store.record_history(
                    cafe=item["cafe"],
                    board=item["board"],
                    title=item["title"],
                    body=item["body"],
                    url=item.get("url") or "",
                )
                self.store.mark_publication(
                    source_key=item.get("source_key") or "",
                    row_number=int(item.get("source_row") or 0),
                    content_hash=item.get("content_hash") or "",
                    status="registered",
                    url=item.get("url") or "",
                    account=item["account"],
                    cafe=item["cafe"],
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

    @staticmethod
    def _scheduled_comments(
        comments: list[dict[str, Any]],
        published_at: datetime,
    ) -> list[dict[str, Any]]:
        schedule = plan_comment_tree(published_at)
        prepared: list[dict[str, Any]] = []
        for index, comment in enumerate(comments[: len(schedule)]):
            text = str(comment.get("text") or "").strip()
            if not text:
                continue
            prepared.append(
                {
                    **comment,
                    "text": text,
                    "scheduled_at": schedule[index]["scheduled_at"],
                }
            )
        return prepared

    def _cached_manuscripts(self, source_name: str = "") -> list[Manuscript]:
        manuscripts: list[Manuscript] = []
        for ref in self._source_refs():
            cached = self.store.load_source(ref.name)
            if not cached:
                continue
            payload = cached.get("payload") or {}
            for item in payload.get("manuscripts") or []:
                if source_name and item.get("source") != source_name:
                    continue
                manuscripts.append(Manuscript(**item))
        return manuscripts

    def _load_accounts(self) -> list[Account]:
        for ref in self._source_refs():
            cached = self.store.load_source(ref.name)
            if not cached:
                continue
            rows = (cached.get("payload") or {}).get("accounts") or []
            if rows:
                return [Account(**item) for item in rows]
        cached = self.store.load_source("계정시트")
        if cached:
            rows = (cached.get("payload") or {}).get("accounts") or []
            if rows:
                return [Account(**item) for item in rows]
        path = self.config_dir / "accounts.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [Account(**item) for item in payload]
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
