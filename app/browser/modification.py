from __future__ import annotations

from datetime import datetime, timedelta

from .publish import PublishBrowser


AFFILIATE_DELAYS_HOURS = {
    "씨씨앙": 4,
    "양평맘": 20,
    "쌍둥이맘": 22,
    "쌍둥이맘 모여라": 22,
}


def revision_at(cafe: str, daily_at: datetime) -> datetime:
    hours = AFFILIATE_DELAYS_HOURS.get(cafe)
    if hours is None:
        raise ValueError(f"수정 간격이 없는 카페입니다: {cafe}")
    return daily_at + timedelta(hours=hours)


def reserve_revision(
    browser: PublishBrowser,
    *,
    cafe: str,
    daily_at: datetime,
    title: str,
    body: str,
    dry_run: bool,
) -> str:
    scheduled = revision_at(cafe, daily_at)
    browser.fill_article(title, body)
    browser.set_schedule(scheduled)
    if dry_run:
        return ""
    url = browser.register()
    if not url or not browser.verify(url):
        raise RuntimeError("수정 글 등록 결과를 재확인하지 못했습니다")
    return url
