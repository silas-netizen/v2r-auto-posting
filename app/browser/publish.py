from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Protocol


class PublishBrowser(Protocol):
    def open_writer(self) -> None: ...
    def select_destination(self, cafe: str, account: str, board: str) -> None: ...
    def fill_article(self, title: str, body: str) -> None: ...
    def set_schedule(self, scheduled_at: datetime) -> None: ...
    def register(self) -> str: ...
    def verify(self, url: str) -> bool: ...
    def open_article(self, url: str) -> None: ...
    def reserve_revision(self, scheduled_at: datetime) -> None: ...
    def reserve_comment(
        self,
        text: str,
        *,
        parent_text: str | None,
        scheduled_at: datetime,
    ) -> None: ...


@dataclass
class RecordingBrowser:
    """Test/dry-run browser that records UI steps without touching V2R."""

    steps: list[str] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)

    def open_writer(self) -> None:
        self.steps.append("open_writer")

    def select_destination(self, cafe: str, account: str, board: str) -> None:
        self.steps.append(f"select:{cafe}:{account}:{board}")

    def fill_article(self, title: str, body: str) -> None:
        self.steps.append(f"fill:{title}")

    def set_schedule(self, scheduled_at: datetime) -> None:
        self.steps.append(f"schedule:{scheduled_at.isoformat()}")

    def register(self) -> str:
        url = f"https://v2r.example/posts/{len(self.registered) + 1}"
        self.registered.append(url)
        self.steps.append(f"register:{url}")
        return url

    def verify(self, url: str) -> bool:
        self.steps.append(f"verify:{url}")
        return url in self.registered

    def open_article(self, url: str) -> None:
        self.steps.append(f"open_article:{url}")

    def reserve_revision(self, scheduled_at: datetime) -> None:
        self.steps.append(f"reserve_revision:{scheduled_at.isoformat()}")

    def reserve_comment(
        self,
        text: str,
        *,
        parent_text: str | None,
        scheduled_at: datetime,
    ) -> None:
        self.steps.append(
            f"comment:{text}:{parent_text or ''}:{scheduled_at.isoformat()}"
        )


def _register_and_verify(browser: PublishBrowser) -> str:
    url = browser.register()
    if not url or not browser.verify(url):
        raise RuntimeError("V2R 등록 결과를 재확인하지 못했습니다")
    return url


def _reserve_comments(browser: PublishBrowser, slot: dict) -> None:
    for comment in slot.get("comments") or []:
        text = str(comment.get("text") or "").strip()
        scheduled_at = comment.get("scheduled_at")
        if not text or not isinstance(scheduled_at, datetime):
            continue
        browser.reserve_comment(
            text,
            parent_text=comment.get("parent_text"),
            scheduled_at=scheduled_at,
        )


def _publish_affiliate(
    browser: PublishBrowser,
    slot: dict,
    *,
    dry_run: bool,
    checkpoint: Callable[[str, dict], None] | None = None,
) -> dict:
    daily = slot.get("daily") or {}
    if not daily.get("title") or not daily.get("body"):
        raise RuntimeError("제휴 발행용 원본 일상 글이 없습니다")
    browser.open_writer()
    browser.select_destination(slot["cafe"], slot["account"], slot["board"])
    browser.fill_article(daily["title"], daily["body"])
    browser.set_schedule(slot["scheduled_at"])
    if dry_run:
        return {
            **slot,
            "daily_url": "",
            "revision_url": "",
            "url": "",
            "status": "queued",
        }
    if checkpoint:
        checkpoint("daily_submitting", slot)
    daily_url = _register_and_verify(browser)
    if checkpoint:
        checkpoint("daily_registered", {**slot, "daily_url": daily_url})
    browser.open_article(daily_url)
    browser.reserve_revision(slot["revision_at"])
    browser.fill_article(slot["title"], slot["body"])
    if checkpoint:
        checkpoint(
            "revision_submitting",
            {**slot, "daily_url": daily_url},
        )
    revision_url = _register_and_verify(browser)
    browser.open_article(revision_url)
    _reserve_comments(browser, slot)
    result = {
        **slot,
        "daily_url": daily_url,
        "revision_url": revision_url,
        "url": revision_url,
        "status": "registered",
    }
    if checkpoint:
        checkpoint("published", result)
    return result


def publish_planned_slots(
    browser: PublishBrowser,
    slots: list[dict],
    *,
    dry_run: bool,
    checkpoint: Callable[[str, dict], None] | None = None,
) -> list[dict]:
    results: list[dict] = []
    for slot in slots:
        if slot.get("workflow") == "affiliate":
            results.append(
                _publish_affiliate(
                    browser,
                    slot,
                    dry_run=dry_run,
                    checkpoint=checkpoint,
                )
            )
            continue
        browser.open_writer()
        browser.select_destination(slot["cafe"], slot["account"], slot["board"])
        browser.fill_article(slot["title"], slot["body"])
        if slot.get("scheduled_at"):
            browser.set_schedule(slot["scheduled_at"])
        if dry_run:
            results.append({**slot, "url": "", "status": "queued"})
            continue
        if checkpoint:
            checkpoint("submitting", slot)
        url = _register_and_verify(browser)
        browser.open_article(url)
        _reserve_comments(browser, slot)
        result = {**slot, "url": url, "status": "registered"}
        results.append(result)
        if checkpoint:
            checkpoint("published", result)
    return results
