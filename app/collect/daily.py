from __future__ import annotations

from typing import Any

from ..models import Manuscript
from ..providers.router import ModelRouter
from .public import PublicPageReader, PublicReadResult


DAILY_SYSTEM_PROMPT = """\
너는 V2R 일상 글 수집기다. 공개 페이지에서 얻은 제목과 본문만 사용한다.
로그인·페이월 내용은 쓰지 않는다. 출력은 JSON 배열만 반환한다.
각 항목은 title, body, cafe, board 키를 가진다. 본문은 자연스러운 한국어 일상체로 다시 쓴다.
"""


def collect_daily_manuscripts(
    urls: list[str],
    router: ModelRouter,
    *,
    reader: PublicPageReader | None = None,
    cafe: str = "",
    board: str = "",
) -> list[Manuscript]:
    page_reader = reader or PublicPageReader()
    pages: list[PublicReadResult] = []
    for url in urls:
        result = page_reader.fetch(url)
        if result.blocked:
            continue
        if result.title and result.text:
            pages.append(result)

    if not pages:
        return []

    if router.available("claude"):
        payload = router.complete_json(
            "claude",
            system=DAILY_SYSTEM_PROMPT,
            user=_pages_to_prompt(pages, cafe, board),
        )
        return [
            Manuscript(
                title=str(item.get("title") or "").strip(),
                body=str(item.get("body") or "").strip(),
                cafe=str(item.get("cafe") or cafe),
                board=str(item.get("board") or board),
                source="claude-collect",
            )
            for item in payload
            if str(item.get("title") or "").strip() and str(item.get("body") or "").strip()
        ]

    return [
        Manuscript(
            title=page.title,
            body=page.text,
            cafe=cafe,
            board=board,
            source=page.source_kind,
        )
        for page in pages
    ]


def _pages_to_prompt(pages: list[PublicReadResult], cafe: str, board: str) -> str:
    lines = [
        f"대상 카페: {cafe or '미지정'}",
        f"대상 게시판: {board or '미지정'}",
        "아래 공개 자료를 일상 글 후보로 정리하라.",
    ]
    for page in pages:
        lines.append(f"- {page.title} | {page.text} | {page.url}")
    return "\n".join(lines)


def plan_daily_publish(
    manuscripts: list[Manuscript],
    router: ModelRouter,
    *,
    count: int,
    account_count: int,
    window_start: str,
    window_end: str,
    interval_minutes: int,
    start_date: str,
) -> dict[str, Any]:
    selected = manuscripts[:count] if count else manuscripts
    if router.available("claude"):
        return router.complete_object(
            "claude",
            system="너는 V2R 일상 글 발행 계획기다. 허용된 JSON 작업 명세만 반환한다.",
            user=(
                "다음 원고로 publish_daily 작업 JSON을 만들어라. "
                f"count={len(selected)}, accountCount={account_count}, "
                f"windowStart={window_start}, windowEnd={window_end}, "
                f"intervalMinutes={interval_minutes}, startDate={start_date}. "
                "manuscripts에는 title, body, cafe, board만 넣는다."
            ),
        )
    return {
        "task": "publish_daily",
        "count": len(selected),
        "accountMode": "auto",
        "accountCount": account_count,
        "windowStart": window_start,
        "windowEnd": window_end,
        "intervalMinutes": interval_minutes,
        "startDate": start_date,
        "dryRun": True,
        "manuscripts": [
            {
                "title": item.title,
                "body": item.body,
                "cafe": item.cafe,
                "board": item.board,
            }
            for item in selected
        ],
    }
