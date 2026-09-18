from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class PublishBrowser(Protocol):
    def open_writer(self) -> None: ...
    def select_destination(self, cafe: str, account: str, board: str) -> None: ...
    def fill_article(self, title: str, body: str) -> None: ...
    def set_schedule(self, scheduled_at: datetime) -> None: ...
    def register(self) -> str: ...
    def verify(self, url: str) -> bool: ...


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


def publish_planned_slots(
    browser: PublishBrowser,
    slots: list[dict],
    *,
    dry_run: bool,
) -> list[dict]:
    results: list[dict] = []
    for slot in slots:
        browser.open_writer()
        browser.select_destination(slot["cafe"], slot["account"], slot["board"])
        browser.fill_article(slot["title"], slot["body"])
        if slot.get("scheduled_at"):
            browser.set_schedule(slot["scheduled_at"])
        if dry_run:
            results.append({**slot, "url": "", "status": "queued"})
            continue
        url = browser.register()
        if not url or not browser.verify(url):
            raise RuntimeError("V2R 등록 결과를 재확인하지 못했습니다")
        results.append({**slot, "url": url, "status": "registered"})
    return results
