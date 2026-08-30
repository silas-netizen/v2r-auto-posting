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
KEYWORD_HEADERS = ("키워드", "검색어", "검색 키워드")
STATUS_HEADERS = ("노출상태", "노출 상태")
SEARCH_URL_HEADERS = ("통합검색", "통합 검색", "네이버 통합검색", "네이버통합검색")
POST_URL_HEADERS = ("작성 글", "작성글", "작성 글 링크", "작성글링크")
CAFE_HEADERS = ("카페/ID", "카페 / ID", "카페ID", "카페명", "카페")
VOLUME_HEADERS = ("키워드 검색량", "#키워드검색량", "# 키워드 검색량", "검색량")
EXPOSED_VOLUME_HEADERS = ("노출된 검색량", "#노출된검색량", "# 노출된 검색량", "노출 검색량")
EDITED_HEADERS = ("최종 편집 일시", "최종편집일시", "최종 수정 일시")
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
    current_cafe: str = ""
    cafe_property: str = ""
    cafe_type: str = ""
    volume_property: str = ""
    volume_type: str = ""
    exposed_volume_property: str = ""
    exposed_volume_type: str = ""
    edited_property: str = ""


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


def parse_keyword_lines(text: str) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        keyword = strip_parenthetical(line).strip()
        key = compact_text(keyword)
        if not keyword or key in seen:
            continue
        seen.add(key)
        keywords.append(keyword)
    return keywords


def match_selected_rows(
    rows: list[ExposureRow], selected: list[str]
) -> tuple[list[ExposureRow], list[str]]:
    grouped: dict[str, list[ExposureRow]] = {}
    for row in rows:
        grouped.setdefault(compact_text(strip_parenthetical(row.keyword)), []).append(row)
    matched: list[ExposureRow] = []
    missing: list[str] = []
    used: set[str] = set()
    for keyword in selected:
        found = grouped.get(compact_text(keyword), [])
        if not found:
            missing.append(keyword)
            continue
        for row in found:
            if row.page_id in used:
                continue
            matched.append(row)
            used.add(row.page_id)
    return matched, missing


def cafe_id_name(value: str) -> str:
    text = (value or "").strip()
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    return compact_text(text)


def cafe_name_only(value: str) -> str:
    text = (value or "").strip()
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    return text


def status_option(status: str, options: list[str] | tuple[str, ...] = ()) -> str:
    wanted = compact_text(status)
    if not wanted:
        return status
    for name in options:
        text = str(name or "").strip()
        if text and compact_text(text) == wanted:
            return text
    return status


def cafe_name_option(cafe_name: str, options: list[str] | tuple[str, ...] = ()) -> str:
    """카페/ID를 바꿀 때는 이름만 있는 옵션을 고른다. 뒤에 /아이디가 붙은 값은 쓰지 않는다."""
    cafe = cafe_name_only(cafe_name)
    if not cafe:
        return ""
    want = compact_text(cafe)
    for opt in options:
        text = str(opt or "").strip()
        if text and "/" not in text and compact_text(text) == want:
            return text
    return cafe


def preserve_cafe_id(current: str, cafe_name: str) -> str:
    existing = (current or "").strip()
    cafe = (cafe_name or "").strip()
    if not cafe:
        return existing
    if existing and cafe_id_name(existing) == compact_text(cafe):
        return existing
    return cafe_name_only(cafe)


def cafe_id_for_check(
    current: str, status: str, found_cafe: str
) -> tuple[str, str | None]:
    existing = (current or "").strip()
    if status != STATUS_EXPOSED:
        return existing, None
    found = cafe_name_only(found_cafe or "")
    if not found:
        return existing, None
    if existing and cafe_id_name(existing) == compact_text(found):
        return existing, None
    return found, found


def keyword_tool_query(keyword: str) -> str:
    return re.sub(r"\s+", "", strip_parenthetical(keyword or ""))


def is_keyword_tool_placeholder(value: str) -> bool:
    """광고주센터 키워드 도구 칸. 실제 문구는 '한줄에 하나씩 입력하세요.'이다."""
    return "한줄에하나씩" in compact_text(value)


def volume_from_result_cells(cells: list[str], keyword: str) -> int | None:
    want = compact_text(keyword)
    texts = [str(cell or "").strip() for cell in cells if str(cell or "").strip()]
    if len(texts) < 2:
        return None
    names = [compact_text(cell.split("\n")[0]) for cell in texts]
    if want not in names:
        return None
    start = names.index(want) + 1
    numbers = [parse_qc_count(cell.split("\n")[-1]) for cell in texts[start:]]
    numbers = [item for item in numbers if item is not None]
    if len(numbers) >= 2:
        return numbers[0] + numbers[1]
    if numbers:
        return numbers[0]
    return None


def parse_qc_count(value) -> int | None:
    text = str(value or "").replace(",", "").replace(" ", "")
    if not text:
        return None
    folded = text.casefold()
    if folded in {"<10", "< 10"} or folded.startswith("<10") or "미만" in text:
        return 10
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number)


def keywordstool_volume(payload: dict, keyword: str) -> int | None:
    if not isinstance(payload, dict):
        return None
    items = (
        payload.get("keywordList")
        or payload.get("relKeywordList")
        or payload.get("data")
        or []
    )
    if isinstance(items, dict):
        items = (
            items.get("keywordList")
            or items.get("relKeywordList")
            or []
        )
    if not isinstance(items, list):
        return None
    want = compact_text(keyword)
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(
            item.get("relKeyword")
            or item.get("keyword")
            or item.get("hintKeyword")
            or ""
        )
        if compact_text(name) != want:
            continue
        pc = parse_qc_count(
            item.get("monthlyPcQcCnt")
            or item.get("monthlyPcQcCount")
            or item.get("monthlyPcQc")
            or item.get("pcQcCnt")
        )
        mobile = parse_qc_count(
            item.get("monthlyMobileQcCnt")
            or item.get("monthlyMobileQcCount")
            or item.get("monthlyMobileQc")
            or item.get("mobileQcCnt")
        )
        if pc is None and mobile is None:
            return None
        return (pc or 0) + (mobile or 0)
    return None


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


def same_search_query(expected: str, actual: str) -> bool:
    return compact_text(expected) == compact_text(actual)


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


def is_clustered_sub_result(anchor_html: str) -> bool:
    """같은 카페 묶음에서 대표 글 밑에 달린 서브 글인지 본다."""
    end = (anchor_html or "").find(">")
    tag = (anchor_html or "")[: end + 1] if end >= 0 else (anchor_html or "")
    lowered = tag.casefold()
    if 'data-heatmap-target=".series"' in lowered:
        return True
    if "data-heatmap-target='.series'" in lowered:
        return True
    return False


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


def search_result_html(html: str) -> str:
    """Use the 통검 result column only. Header/내 카페 위젯은 빼다."""
    text = html or ""
    match = re.search(r'(?is)<[^>]*\bid=["\']main_pack["\'][^>]*>', text)
    if not match:
        return text
    start = match.start()
    rest = text[start:]
    stop = re.search(
        r'(?is)<(?:div|aside|footer)[^>]*\bid=["\'](?:sub_pack|footer|aside)["\']',
        rest[len(match.group(0)) :],
    )
    if stop:
        return rest[: len(match.group(0)) + stop.start()]
    return rest


def collect_our_cafe_hits(html: str, cafe_names: list[str]) -> list[CafeHit]:
    source = search_result_html(html)
    anchors: list[tuple[int, str, str, str]] = []
    for match in _ANCHOR_RE.finditer(source):
        href = _absolute_url(match.group(1))
        if "cafe.naver.com" not in href.lower():
            continue
        text = _strip_tags(match.group(2))
        anchors.append((match.start(), href, text, match.group(0)))

    our_ids: dict[str, str] = {}
    articles: list[tuple[int, str]] = []
    for start, href, text, tag in anchors:
        identity = cafe_identity(href)
        cafe = matching_cafe_name(text, cafe_names)
        if identity and cafe and not is_cafe_article_url(href):
            our_ids[identity] = cafe
        if is_cafe_article_url(href):
            if is_clustered_sub_result(tag):
                continue
            articles.append((start, href))

    hits: list[CafeHit] = []
    seen: set[str] = set()
    for start, href in articles:
        identity = cafe_identity(href)
        cafe = our_ids.get(identity, "")
        if not cafe:
            continue
        key = article_dedupe_key(href)
        if key in seen:
            continue
        seen.add(key)
        hits.append(CafeHit(url=href, cafe_name=cafe))
    return hits


def keep_visible_cafe_hits(
    hits: list[CafeHit], visible_urls: list[str] | None
) -> list[CafeHit]:
    """통검 화면에 실제로 보이는 카페 글만 남긴다. None이면 걸러내지 않는다."""
    if visible_urls is None:
        return hits
    keys = {article_dedupe_key(url) for url in visible_urls if url}
    return [hit for hit in hits if article_dedupe_key(hit.url) in keys]


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
        if not dry_run and hasattr(self.notion, "write_volume_totals"):
            try:
                self.notion.write_volume_totals()
            except Exception as exc:
                self.logger.error("검색량 합 저장 실패: %s", exc)
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
        html_hits = collect_our_cafe_hits(html, self.cafe_names)
        visible_urls = None
        lookup_visible = getattr(self.naver, "visible_cafe_article_urls", None)
        if callable(lookup_visible):
            try:
                visible_urls = lookup_visible()
            except Exception as exc:
                self.logger.warning("통검 화면 글 확인 실패: %s", exc)
                visible_urls = None
        hits = keep_visible_cafe_hits(html_hits, visible_urls)
        if html_hits and visible_urls is not None and len(hits) < len(html_hits):
            kept = {article_dedupe_key(hit.url) for hit in hits}
            hidden_names = ", ".join(
                hit.cafe_name
                for hit in html_hits
                if article_dedupe_key(hit.url) not in kept
            )
            self.logger.info(
                "통검에 가려진 우리 카페 글은 제외합니다: %s",
                hidden_names,
            )
        cafe_name = ""
        status = STATUS_HIDDEN
        if not hits:
            if html_hits and visible_urls is not None:
                self.logger.info(
                    "밀려남: %s / 통검 화면에 우리 카페 글이 보이지 않습니다",
                    keyword,
                )
            else:
                named = matching_cafe_name(_strip_tags(html), self.cafe_names)
                if named:
                    self.logger.warning(
                        "밀려남: %s / 화면에 %s 이름은 보이지만 우리 카페 글 주소를 못 찾았습니다",
                        keyword,
                        named,
                    )
                else:
                    self.logger.info("밀려남: %s / 통합검색에 우리 카페 없음", keyword)
        else:
            names = ", ".join(
                f"{hit.cafe_name} ({hit.url})" for hit in hits
            )
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
                    cafe_name = hit.cafe_name
                    status = STATUS_EXPOSED
                    self.logger.info(
                        "노출완: %s / %s 글에서 식별어 %s / %s",
                        keyword,
                        hit.cafe_name,
                        matched,
                        hit.url,
                    )
                    break
            if status != STATUS_EXPOSED:
                self.logger.info("밀려남: %s / 우리 카페 글에 브랜드 식별어 없음", keyword)
        volume = None
        volume_found = False
        lookup = getattr(self.naver, "lookup_search_volume", None)
        if callable(lookup):
            try:
                volume = lookup(keyword)
                volume_found = volume is not None
                if volume_found:
                    self.logger.info("키워드 검색량: %s = %s", keyword, volume)
            except Exception as exc:
                self.logger.error("검색량 조회 실패 (%s): %s", keyword, exc)
        self._write_result(
            row,
            status,
            cafe_name,
            volume,
            volume_found,
            dry_run,
        )

    def _write_result(
        self,
        row: ExposureRow,
        status: str,
        cafe_name: str,
        volume: int | None,
        volume_found: bool,
        dry_run: bool,
    ) -> None:
        cafe_value, cafe_write = cafe_id_for_check(row.current_cafe, status, cafe_name)
        cafe_log = cafe_write or cafe_value or "(없음)"
        if cafe_write:
            cafe_log = f"변경 {cafe_write}"
        elif cafe_value:
            cafe_log = f"유지 {cafe_value}"
        label = getattr(self.notion, "label", "노션")
        if dry_run:
            self.logger.info(
                "검증 모드: %s → %s / 카페 %s / 검색량 %s (%s에 쓰지 않음)",
                row.current_status or "(비어 있음)",
                status,
                cafe_log,
                volume if volume_found else "(조회 안 됨)",
                label,
            )
            return
        try:
            if hasattr(self.notion, "update_check_result"):
                self.notion.update_check_result(
                    row,
                    status=status,
                    cafe_name=cafe_write,
                    search_volume=volume,
                    volume_found=volume_found,
                )
            elif row.current_status != status:
                self.notion.update_status(row, status)
        except Exception as exc:
            self.logger.error("시트 저장 실패 (%s): %s", row.keyword, exc)
            return
        if row.current_status != status:
            self.logger.info("%s 노출상태 변경: %s → %s", label, row.keyword, status)
        else:
            self.logger.info("상태 유지: %s", status)
        if cafe_write:
            self.logger.info("%s 카페 변경: %s → %s", label, row.keyword, cafe_write)
        row.current_status = status
        row.current_cafe = cafe_value


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
