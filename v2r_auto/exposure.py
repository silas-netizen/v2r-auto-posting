from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, quote_plus, urlparse

STATUS_EXPOSED = "노출완"
STATUS_HIDDEN = "밀려남"
DEFAULT_BRAND_MARKERS = (
    "팥순추출물",
    "자연방패 항문세정제",
    "장으뜸 장어즙",
    "코숨핏",
    "그린커피아하바하",
)
KEYWORD_HEADERS = ("키워드",)
STATUS_HEADERS = ("노출상태", "노출 상태")
SEARCH_URL_HEADERS = ("통합검색", "통합 검색")
POST_URL_HEADERS = ("작성 글", "작성글", "작성 글 링크", "작성글링크")


@dataclass(slots=True)
class ExposureRow:
    page_id: str
    keyword: str
    search_url: str
    post_url: str
    current_status: str
    status_property: str
    status_type: str


def parse_brands(value: str) -> list[str]:
    brands = []
    seen: set[str] = set()
    for part in re.split(r"[,/\n]+", value or ""):
        brand = part.strip()
        key = _compact(brand)
        if brand and key not in seen:
            seen.add(key)
            brands.append(brand)
    return brands or list(DEFAULT_BRAND_MARKERS)


def naver_search_url(keyword: str) -> str:
    return "https://search.naver.com/search.naver?query=" + quote_plus(keyword.strip())


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def search_result_text(html: str) -> str:
    """Keep the result area and drop scripts, styles, and the search box."""
    source = html or ""
    source = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", source)
    source = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", source)
    source = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", source)
    source = re.sub(r"(?is)<(input|textarea|select)\b[^>]*>.*?</\1>", " ", source)
    source = re.sub(r"(?is)<(input|textarea|select)\b[^>]*>", " ", source)
    match = re.search(
        r'(?is)<(?:div|main)[^>]*(?:id|class)=["\'][^"\']*(?:main_pack|content)[^"\']*["\'][^>]*>',
        source,
    )
    if match:
        source = source[match.start() :]
    source = re.sub(r"(?is)<[^>]+>", " ", source)
    return re.sub(r"\s+", " ", source).strip()


def is_exposed(
    html: str,
    brands: list[str],
    post_url: str = "",
) -> tuple[bool, str]:
    text = search_result_text(html)
    compact = _compact(text)
    for brand in brands:
        marker = _compact(brand)
        if marker and marker in compact:
            return True, brand
    if post_url.strip() and _post_url_in_page(html, post_url):
        return True, "작성 글 주소"
    return False, ""


def _post_url_in_page(html: str, post_url: str) -> bool:
    raw = post_url.strip()
    if not raw:
        return False
    if raw in html:
        return True
    parsed = urlparse(raw)
    article = parse_qs(parsed.query).get("articleid") or parse_qs(parsed.query).get(
        "articleId"
    )
    if article and article[0] and article[0] in html:
        return True
    path = parsed.path.rstrip("/")
    return bool(path) and path in html


class ExposureChecker:
    def __init__(
        self,
        notion,
        fetch_html: Callable[[str], str],
        logger: logging.Logger,
        brands: list[str] | None = None,
        delay_seconds: float = 1.2,
    ):
        self.notion = notion
        self.fetch_html = fetch_html
        self.logger = logger
        self.brands = brands or list(DEFAULT_BRAND_MARKERS)
        self.delay_seconds = delay_seconds

    def inspect_rows(self) -> list[ExposureRow]:
        return self.notion.load_rows()

    def run(
        self,
        rows: list[ExposureRow],
        *,
        dry_run: bool,
        stop_event=None,
        progress=None,
    ) -> list[ExposureRow]:
        total = len(rows)
        for index, row in enumerate(rows, start=1):
            if stop_event is not None and stop_event.is_set():
                self.logger.info("중지 요청으로 노출 검사를 멈춥니다")
                break
            if progress:
                progress(index - 1, total)
            self._check_one(row, dry_run=dry_run)
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            if progress:
                progress(index, total)
        return rows

    def _check_one(self, row: ExposureRow, *, dry_run: bool) -> None:
        url = row.search_url.strip() or naver_search_url(row.keyword)
        self.logger.info("키워드 검색: %s", row.keyword)
        try:
            html = self.fetch_html(url)
        except Exception as exc:
            self.logger.error("네이버 검색 실패 (%s): %s", row.keyword, exc)
            return
        exposed, matched = is_exposed(html, self.brands, row.post_url)
        new_status = STATUS_EXPOSED if exposed else STATUS_HIDDEN
        if exposed:
            self.logger.info("노출완: %s / 식별어 %s", row.keyword, matched)
        else:
            self.logger.info("밀려남: %s / 통합검색에 브랜드 식별어 없음", row.keyword)
        if row.current_status == new_status:
            self.logger.info("상태 유지: %s", new_status)
            return
        if dry_run:
            self.logger.info("검증 모드: %s → %s (노션에 쓰지 않음)", row.current_status, new_status)
            return
        self.notion.update_status(row, new_status)
        row.current_status = new_status
        self.logger.info("노션 노출상태 변경: %s → %s", row.keyword, new_status)


def dump_rows_preview(rows: list[ExposureRow]) -> str:
    return json.dumps(
        [
            {
                "keyword": row.keyword,
                "status": row.current_status,
                "search_url": row.search_url,
            }
            for row in rows[:20]
        ],
        ensure_ascii=False,
        indent=2,
    )
