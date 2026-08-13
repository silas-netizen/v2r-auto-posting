from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from html import unescape
from typing import Protocol
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

STATUS_EXPOSED = "노출완"
STATUS_HIDDEN = "밀려남"
DEFAULT_BRAND_MARKERS = (
    "팥순추출물",
    "자연방패 항문세정제",
    "장으뜸 장어즙",
    "코숨핏",
    "그린커피아하바하",
)
DEFAULT_CAFE_NAMES = (
    "씨씨앙",
    "양평맘",
    "러브인썸",
    "마이웨딩드림",
    "우아한갱년기",
)
KEYWORD_HEADERS = ("키워드",)
STATUS_HEADERS = ("노출상태", "노출 상태")
SEARCH_URL_HEADERS = ("통합검색", "통합 검색")
POST_URL_HEADERS = ("작성 글", "작성글", "작성 글 링크", "작성글링크")
_ANCHOR_RE = re.compile(
    r'(?is)<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
)


@dataclass(slots=True)
class ExposureRow:
    page_id: str
    keyword: str
    search_url: str
    post_url: str
    current_status: str
    status_property: str
    status_type: str


@dataclass(slots=True)
class CafeHit:
    url: str
    cafe_name: str


class NaverSearchClient(Protocol):
    def search_integrated(self, keyword: str) -> str:
        """Search on 통합검색 and return the result HTML."""

    def open_post_text(self, url: str) -> str:
        """Open one cafe post and return visible title/body/comment text."""


def compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def strip_parenthetical(keyword: str) -> str:
    text = keyword or ""
    text = re.sub(r"[\(（][^)\）]*[\)）]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_name_list(value: str, defaults: tuple[str, ...]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,/\n]+", value or ""):
        name = part.strip()
        key = compact_text(name)
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names or list(defaults)


def parse_brands(value: str) -> list[str]:
    return parse_name_list(value, DEFAULT_BRAND_MARKERS)


def parse_cafes(value: str) -> list[str]:
    return parse_name_list(value, DEFAULT_CAFE_NAMES)


def naver_search_url(keyword: str) -> str:
    return "https://search.naver.com/search.naver?query=" + quote_plus(keyword.strip())


def matching_cafe_name(text: str, cafe_names: list[str]) -> str:
    compact = compact_text(text)
    for name in cafe_names:
        marker = compact_text(name)
        if marker and marker in compact:
            return name
    return ""


def brand_found(text: str, brands: list[str]) -> str:
    compact = compact_text(text)
    for brand in brands:
        marker = compact_text(brand)
        if marker and marker in compact:
            return brand
    return ""


def _strip_tags(html: str) -> str:
    source = html or ""
    source = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", source)
    source = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", source)
    source = re.sub(r"(?is)<[^>]+>", " ", source)
    return unescape(re.sub(r"\s+", " ", source)).strip()


def _absolute_url(href: str) -> str:
    raw = unescape((href or "").strip())
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("/"):
        return urljoin("https://search.naver.com/", raw)
    return raw


def is_cafe_article_url(url: str) -> bool:
    parsed = urlparse(_absolute_url(url))
    if "cafe.naver.com" not in (parsed.netloc or "").lower():
        return False
    query = parse_qs(parsed.query)
    if query.get("articleid") or query.get("articleId"):
        return True
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return False
    if parts[-1].isdigit() and len(parts) >= 2:
        return True
    lowered = [part.casefold() for part in parts]
    if "articles" in lowered:
        index = lowered.index("articles")
        return index + 1 < len(parts) and parts[index + 1].isdigit()
    return False


def cafe_identity(url: str) -> str:
    parsed = urlparse(_absolute_url(url))
    if "cafe.naver.com" not in (parsed.netloc or "").lower():
        return ""
    query = parse_qs(parsed.query)
    club = (query.get("clubid") or query.get("clubId") or [""])[0]
    if club:
        return f"club:{club}"
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return ""
    lowered = [part.casefold() for part in parts]
    if "cafes" in lowered:
        index = lowered.index("cafes")
        if index + 1 < len(parts):
            return f"cafe:{parts[index + 1]}"
    first = parts[0].casefold()
    if first in {"article", "articleread.nhn", "ca-fe", "f-e"}:
        return ""
    return f"slug:{first}"


def article_dedupe_key(url: str) -> str:
    parsed = urlparse(_absolute_url(url))
    query = parse_qs(parsed.query)
    article = (query.get("articleid") or query.get("articleId") or [""])[0]
    path = parsed.path.rstrip("/").casefold()
    if article:
        club = (query.get("clubid") or query.get("clubId") or [""])[0]
        return f"{club}:{article}"
    return path


def collect_our_cafe_hits(html: str, cafe_names: list[str]) -> list[CafeHit]:
    source = html or ""
    anchors: list[tuple[int, str, str]] = []
    for match in _ANCHOR_RE.finditer(source):
        href = _absolute_url(match.group(1))
        if "cafe.naver.com" not in href.lower():
            continue
        text = _strip_tags(match.group(2))
        anchors.append((match.start(), href, text))

    our_ids: dict[str, str] = {}
    all_homes: list[tuple[int, str, str]] = []
    articles: list[tuple[int, str]] = []
    for start, href, text in anchors:
        identity = cafe_identity(href)
        cafe = matching_cafe_name(text, cafe_names)
        if identity and cafe and not is_cafe_article_url(href):
            our_ids[identity] = cafe
        if identity and not is_cafe_article_url(href):
            all_homes.append((start, identity, cafe))
        if is_cafe_article_url(href):
            articles.append((start, href))

    hits: list[CafeHit] = []
    seen: set[str] = set()
    for start, href in articles:
        identity = cafe_identity(href)
        cafe = our_ids.get(identity, "")
        if not cafe:
            nearest_cafe = ""
            nearest = 10_000
            nearest_ours = False
            for home_pos, home_id, home_cafe in all_homes:
                distance = abs(home_pos - start)
                if distance < nearest and distance <= 1200:
                    nearest = distance
                    nearest_cafe = home_cafe
                    nearest_ours = bool(home_cafe)
            if nearest_ours:
                cafe = nearest_cafe
        if not cafe:
            continue
        key = article_dedupe_key(href)
        if key in seen:
            continue
        seen.add(key)
        hits.append(CafeHit(url=href, cafe_name=cafe))
    return hits


class ExposureChecker:
    def __init__(
        self,
        notion,
        naver: NaverSearchClient,
        logger: logging.Logger,
        brands: list[str] | None = None,
        cafe_names: list[str] | None = None,
        delay_seconds: float = 0.5,
    ):
        self.notion = notion
        self.naver = naver
        self.logger = logger
        self.brands = brands or list(DEFAULT_BRAND_MARKERS)
        self.cafe_names = cafe_names or list(DEFAULT_CAFE_NAMES)
        self.delay_seconds = delay_seconds

    def inspect_rows(self) -> list[ExposureRow]:
        return self.notion.load_rows()

    def run(
        self,
        rows: list[ExposureRow],
        *,
        dry_run: bool,
        stop_event=None,
        pause_event=None,
        progress=None,
    ) -> list[ExposureRow]:
        total = len(rows)
        for index, row in enumerate(rows, start=1):
            if stop_event is not None and stop_event.is_set():
                self.logger.info("중지 요청으로 노출 검사를 멈춥니다")
                break
            self._wait_while_paused(pause_event, stop_event)
            if stop_event is not None and stop_event.is_set():
                self.logger.info("중지 요청으로 노출 검사를 멈춥니다")
                break
            keyword = strip_parenthetical(row.keyword)
            if not keyword:
                self.logger.warning("괄호를 빼니 검색어가 없어 건너뜁니다: %s", row.keyword)
                if progress:
                    progress(index, total)
                continue
            if progress:
                progress(index - 1, total)
            self._check_one(row, dry_run=dry_run)
            self._wait_while_paused(pause_event, stop_event)
            if self.delay_seconds:
                self._interruptible_delay(
                    self.delay_seconds, stop_event, pause_event
                )
            if progress:
                progress(index, total)
        return rows

    def _wait_while_paused(self, pause_event, stop_event) -> None:
        if pause_event is None or not pause_event.is_set():
            return
        self.logger.info(
            "일시 중지했습니다. 다시 시작을 누르면 다음 키워드부터 이어서 합니다"
        )
        while pause_event.is_set():
            if stop_event is not None and stop_event.is_set():
                return
            if stop_event is not None:
                stop_event.wait(0.2)
            else:
                time.sleep(0.2)
        if stop_event is None or not stop_event.is_set():
            self.logger.info("검사를 다시 시작합니다")

    def _interruptible_delay(self, seconds: float, stop_event, pause_event) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if stop_event is not None and stop_event.is_set():
                return
            self._wait_while_paused(pause_event, stop_event)
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(0.1)

    def _check_one(self, row: ExposureRow, *, dry_run: bool) -> None:
        keyword = strip_parenthetical(row.keyword)
        self.logger.info("키워드 검색: %s", keyword)
        try:
            html = self.naver.search_integrated(keyword)
        except Exception as exc:
            self.logger.error("네이버 검색 실패 (%s): %s", keyword, exc)
            return
        hits = collect_our_cafe_hits(html, self.cafe_names)
        if not hits:
            self.logger.info("밀려남: %s / 통합검색에 우리 카페 없음", keyword)
            self._apply_status(row, STATUS_HIDDEN, dry_run)
            return
        names = ", ".join(hit.cafe_name for hit in hits)
        self.logger.info("우리 카페 글 %s건: %s", len(hits), names)
        matched = ""
        for hit in hits:
            try:
                post_text = self.naver.open_post_text(hit.url)
            except Exception as exc:
                self.logger.error("카페 글 열기 실패 (%s): %s", hit.url, exc)
                continue
            matched = brand_found(post_text, self.brands)
            if matched:
                self.logger.info(
                    "노출완: %s / %s 글에서 식별어 %s",
                    keyword,
                    hit.cafe_name,
                    matched,
                )
                break
        if not matched:
            self.logger.info("밀려남: %s / 우리 카페 글에 브랜드 식별어 없음", keyword)
            self._apply_status(row, STATUS_HIDDEN, dry_run)
            return
        self._apply_status(row, STATUS_EXPOSED, dry_run)

    def _apply_status(self, row: ExposureRow, new_status: str, dry_run: bool) -> None:
        if row.current_status == new_status:
            self.logger.info("상태 유지: %s", new_status)
            return
        if dry_run:
            self.logger.info(
                "검증 모드: %s → %s (노션에 쓰지 않음)",
                row.current_status or "(비어 있음)",
                new_status,
            )
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
