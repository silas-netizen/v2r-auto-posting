from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote


DEFAULT_CAFE_URL = "https://cafe.naver.com/cantsb"
DEFAULT_CAFE_ID = 25016228
DEFAULT_CAFE_SLUG = "cantsb"
CAFE_HOME_URL = DEFAULT_CAFE_URL
CAFE_ID = DEFAULT_CAFE_ID
NAVER_LOGIN_URL = "https://nid.naver.com/nidlogin.login"
NAVER_LOGIN_COOKIES = ("NID_AUT", "NID_SES")
RESERVED_CAFE_SLUGS = {
    "articlelist.nhn",
    "articlesearchlist.nhn",
    "articlewrite.nhn",
    "ca-cafes",
    "ca-fe",
    "ca-fes",
    "cafeprofileview.nhn",
    "f-e",
    "managehome.nhn",
}
MISSING_PAGE_HINTS = (
    "페이지를 찾을 수 없습니다",
    "서비스에 접속할 수 없습니다",
)
DEFAULT_KEYWORDS = ("팥순", "자연방패", "장으뜸")
FLOWMOA_MEMBERSHIP_URL = "https://flowmoa.com/index.php?view=moa-membership"
FLOWMOA_HOME_URL = "https://flowmoa.com/"
LOGIN_URL_HINTS = (
    "nid.naver.com/nidlogin",
    "nid.naver.com/login",
)
LOGIN_PAGE_HINTS = (
    "로그인이 필요",
    "로그인 후 이용",
    "로그인해주세요",
    "로그인 해주세요",
)
NICKNAME_KEYS = (
    "nickname",
    "nickName",
    "writerNickname",
    "memberNickname",
    "authorNickname",
    "nick",
)
SEARCH_API_TEMPLATES = (
    "https://apis.cafe.naver.com/search/v2/cafes/"
    "{cafe_id}/search/articles?query={query}&perPage=50&page={page}"
    "&menuId=0&ta={ta}&views=MEMBER_LEVEL,COUNT,SALE_INFO,CAFE_MENU",
    "https://apis.naver.com/cafe-web/cafe-searchui-api/v1/cafes/"
    "{cafe_id}/search/articles?query={query}&page={page}&perPage=50"
    "&ta={ta}",
    "https://apis.naver.com/cafe-web/cafe-mobile/CafeSearchArticleList"
    "?search.clubid={cafe_id}&search.query={query}&search.page={page}"
    "&search.perPage=50&search.searchBy={search_by}",
)


@dataclass(frozen=True, slots=True)
class SearchScope:
    label: str
    ta: str
    search_by: int


SEARCH_SCOPES = (
    SearchScope("글 + 댓글", "ARTICLE_COMMENT", 0),
    SearchScope("댓글내용", "COMMENT", 4),
)
WRITER_SCOPE = SearchScope("글작성자", "WRITER", 3)
MAX_SEARCH_PAGES = 200
ARTICLE_ID_KEYS = ("articleId", "articleid", "article_id")
ARTICLE_LIST_KEYS = ("articleList", "articles", "articleItems")
PAGE_INFO_LAST_KEYS = (
    "lastNavigationPageNumber",
    "lastPage",
    "totalPages",
    "pageCount",
    "endPage",
)
PAGE_INFO_TOTAL_KEYS = (
    "totalArticleCount",
    "totalCount",
    "total",
    "articleCount",
)
HTML_ARTICLE_ID_PATTERNS = (
    re.compile(r"/articles/(\d+)", re.I),
    re.compile(r"[?&](?:articleid|articleId)=(\d+)", re.I),
)
HTML_PAGINATION_BLOCKS = (
    re.compile(
        r'(?is)<(?:div|ul|nav|table)[^>]*(?:prev-next|pagination|paginator|Nnavi|page_wrap)[^>]*>.*?</(?:div|ul|nav|table)>'
    ),
)
HTML_SEARCH_REGIONS = (
    re.compile(
        r'(?is)<table[^>]*(?:article-board|board-box|article-table|board-list)[^>]*>.*?</table>'
    ),
    re.compile(
        r'(?is)<(?:div|ul)[^>]*(?:ArticleList|article_list|Search_Article|search_list)[^>]*>.*?</(?:div|ul)>'
    ),
)
HTML_NICK_PATTERNS = (
    re.compile(r'data-nickname="([^"]+)"'),
    re.compile(r'"writerNickname"\s*:\s*"([^"]+)"'),
    re.compile(r'"nickname"\s*:\s*"([^"]+)"'),
    re.compile(r'class="[^"]*nick[^"]*"[^>]*>\s*([^<]{1,40})\s*<'),
)
CAFE_ID_PATTERNS = (
    re.compile(r"/cafes/(\d+)"),
    re.compile(r"(?:clubid|cafeid)=(\d+)", re.I),
    re.compile(r'"cafeId"\s*:\s*"?(\d+)'),
    re.compile(r'"clubId"\s*:\s*"?(\d+)'),
    re.compile(r"g_sClubId\s*=\s*['\"]?(\d+)"),
)


class NicknameExcludeError(ValueError):
    pass


@dataclass(slots=True)
class CafeTarget:
    home_url: str
    cafe_id: int | None = None
    slug: str = ""

    def with_cafe_id(self, cafe_id: int) -> "CafeTarget":
        return CafeTarget(home_url=self.home_url, cafe_id=cafe_id, slug=self.slug)


def split_keywords(text: str) -> list[str]:
    parts = re.split(r"[,/\n]+", text or "")
    return [part.strip() for part in parts if part.strip()]


def split_nicknames(text: str) -> list[str]:
    parts = re.split(r"[,/\n;]+", text or "")
    seen: set[str] = set()
    nicknames: list[str] = []
    for part in parts:
        nick = clean_nickname(part)
        if not nick or nick.casefold() in seen:
            continue
        seen.add(nick.casefold())
        nicknames.append(nick)
    return nicknames


def clean_nickname(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def join_nicknames(nicknames: Iterable[str]) -> str:
    return ", ".join(split_nicknames("\n".join(nicknames)))


def join_nicknames_lines(nicknames: Iterable[str]) -> str:
    return "\n".join(split_nicknames("\n".join(nicknames)))


def join_nicknames_export(nicknames: Iterable[str]) -> str:
    names = split_nicknames("\n".join(nicknames))
    if not names:
        return ""
    return f"{join_nicknames_lines(names)}\n\n{join_nicknames(names)}\n"


def write_nicknames_file(path: Path, nicknames: Iterable[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(join_nicknames_export(nicknames), encoding="utf-8")
    return path


def write_nicknames_comma_file(path: Path, nicknames: Iterable[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = join_nicknames(nicknames)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")
    return path


def article_url(cafe: CafeTarget, article_id: str) -> str:
    aid = str(article_id or "").strip()
    if not aid:
        raise NicknameExcludeError("글 번호가 없습니다")
    if cafe.slug:
        return f"https://cafe.naver.com/{cafe.slug}/{aid}"
    cafe_id = require_cafe_id(cafe)
    return f"https://cafe.naver.com/f-e/cafes/{cafe_id}/articles/{aid}"


def unique_urls(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    found: list[str] = []
    for raw in urls:
        url = (raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        found.append(url)
    return found


def format_author_links(rows: Iterable[tuple[str, Iterable[str]]]) -> str:
    blocks: list[str] = []
    for nickname, urls in rows:
        nick = clean_nickname(str(nickname))
        links = unique_urls(urls)
        if not nick:
            continue
        if links:
            blocks.append("\n".join([nick, *links]))
        else:
            blocks.append(nick)
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def write_author_links_file(path: Path, rows: Iterable[tuple[str, Iterable[str]]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_author_links(rows), encoding="utf-8")
    return path


def open_nicknames_notepad(path: Path) -> None:
    import subprocess
    import sys

    if sys.platform == "win32":
        subprocess.Popen(["notepad.exe", str(path)])


def merge_nicknames(existing: Iterable[str], incoming: Iterable[str]) -> list[str]:
    return split_nicknames("\n".join([*existing, *incoming]))


def new_nicknames(existing: Iterable[str], incoming: Iterable[str]) -> list[str]:
    known = {clean_nickname(item).casefold() for item in existing if clean_nickname(item)}
    added: list[str] = []
    seen: set[str] = set()
    for item in incoming:
        nick = clean_nickname(item)
        key = nick.casefold()
        if not nick or key in known or key in seen:
            continue
        seen.add(key)
        added.append(nick)
    return added


def normalize_cafe_url(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        raise NicknameExcludeError("카페 주소를 넣어 주세요")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def parse_cafe_address(text: str) -> CafeTarget:
    raw = normalize_cafe_url(text)
    lowered = raw.casefold()
    if "cafe.naver.com" not in lowered:
        raise NicknameExcludeError(
            "네이버 카페 주소를 넣어 주세요. 예: https://cafe.naver.com/cantsb"
        )
    cafe_id = cafe_id_from_page("", raw)
    slug = ""
    match = re.search(r"(?:m\.)?cafe\.naver\.com/([^/?#]+)", raw, re.I)
    if match:
        candidate = match.group(1)
        if candidate.casefold() not in RESERVED_CAFE_SLUGS and not candidate.isdigit():
            slug = candidate
    if slug:
        home_url = f"https://cafe.naver.com/{slug}"
    elif cafe_id:
        home_url = f"https://cafe.naver.com/f-e/cafes/{cafe_id}"
    else:
        home_url = raw.split("?")[0].rstrip("/")
    if cafe_id is None and slug.casefold() == DEFAULT_CAFE_SLUG:
        cafe_id = DEFAULT_CAFE_ID
    return CafeTarget(home_url=home_url, cafe_id=cafe_id, slug=slug)


def cafe_id_from_page(html: str, url: str = "") -> int | None:
    for source in (url, html):
        for pattern in CAFE_ID_PATTERNS:
            match = pattern.search(source or "")
            if match:
                return int(match.group(1))
    return None


def cookies_show_naver_login(cookie_names: Iterable[str]) -> bool:
    names = {name for name in cookie_names}
    return any(name in names for name in NAVER_LOGIN_COOKIES)


def require_cafe_id(cafe: CafeTarget) -> int:
    if not cafe.cafe_id:
        raise NicknameExcludeError(
            "카페 번호를 찾지 못했습니다. 카페 주소를 다시 확인해 주세요"
        )
    return cafe.cafe_id


def cafe_search_url(
    keyword: str,
    cafe: CafeTarget | None = None,
    page: int = 1,
    scope: SearchScope | None = None,
) -> str:
    target = cafe or parse_cafe_address(DEFAULT_CAFE_URL)
    cafe_id = require_cafe_id(target)
    chosen = scope or SEARCH_SCOPES[0]
    return (
        f"https://cafe.naver.com/f-e/cafes/{cafe_id}/menus/0"
        f"?viewType=L&ta={chosen.ta}&page={max(1, int(page))}&q={quote(keyword)}"
    )


def cafe_search_url_modern(
    keyword: str,
    cafe: CafeTarget | None = None,
    page: int = 1,
    scope: SearchScope | None = None,
) -> str:
    target = cafe or parse_cafe_address(DEFAULT_CAFE_URL)
    cafe_id = require_cafe_id(target)
    chosen = scope or SEARCH_SCOPES[0]
    home = target.home_url or f"https://cafe.naver.com/f-e/cafes/{cafe_id}"
    return (
        f"{home}?iframe_url=/ArticleSearchList.nhn"
        f"?search.clubid={cafe_id}&search.media=0&search.searchBy={chosen.search_by}"
        f"&search.defaultValue=1&search.sortBy=date&search.page={max(1, int(page))}"
        f"&search.query={quote(keyword)}"
    )


def page_is_missing(html: str, url: str = "") -> bool:
    current = (url or "").casefold()
    if "/ca-cafes/" in current:
        return True
    return any(hint in (html or "") for hint in MISSING_PAGE_HINTS)


def page_requires_naver_login(html: str, url: str = "") -> bool:
    current = (url or "").casefold()
    if any(hint in current for hint in LOGIN_URL_HINTS):
        return True
    if "cafe.naver.com" in current and "article" in current:
        return False
    lowered = (html or "").casefold()
    return any(hint.casefold() in lowered for hint in LOGIN_PAGE_HINTS)


def nicknames_from_json(payload: Any) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in NICKNAME_KEYS and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return split_nicknames("\n".join(found))


def search_articles_from_payload(payload: Any) -> list[Any]:
    found: list[Any] = []

    def walk(node: Any) -> None:
        if found:
            return
        if isinstance(node, dict):
            for key in ARTICLE_LIST_KEYS:
                value = node.get(key)
                if isinstance(value, list):
                    found.extend(value)
                    return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def is_search_response(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            if any(key in node for key in ARTICLE_LIST_KEYS):
                return True
            if any(key in node for key in PAGE_INFO_LAST_KEYS + PAGE_INFO_TOTAL_KEYS):
                return True
            if node.get("pageInfo") is not None or node.get("page") is not None:
                return True
            return any(walk(value) for value in node.values())
        if isinstance(node, list):
            return any(walk(item) for item in node)
        return False

    return walk(payload)


def nicknames_from_search_payload(payload: Any) -> list[str]:
    articles = search_articles_from_payload(payload)
    if articles:
        return nicknames_from_json(articles)
    if is_search_response(payload):
        return []
    return nicknames_from_json(payload)


def search_result_html(html: str) -> str:
    text = html or ""
    for pattern in HTML_SEARCH_REGIONS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return ""


def nicknames_from_html(html: str) -> list[str]:
    found: list[str] = []
    for pattern in HTML_NICK_PATTERNS:
        found.extend(pattern.findall(html or ""))
    return split_nicknames("\n".join(found))


def nicknames_from_search_html(html: str) -> list[str]:
    region = search_result_html(html)
    if not region:
        return []
    return nicknames_from_html(region)


def article_ids_from_html(html: str) -> list[str]:
    region = search_result_html(html)
    if not region:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for pattern in HTML_ARTICLE_ID_PATTERNS:
        for item in pattern.findall(region):
            if item not in seen:
                seen.add(item)
                found.append(item)
    return found


def last_page_from_html(html: str) -> int | None:
    text = html or ""
    block = ""
    for pattern in HTML_PAGINATION_BLOCKS:
        match = pattern.search(text)
        if match:
            block = match.group(0)
            break
    if not block:
        return None
    pages = [int(item) for item in re.findall(r"(?:[?&](?:search\.)?page=|data-page=[\"']|>)(\d{1,3})(?:[\"'<])", block)]
    if not pages:
        pages = [int(item) for item in re.findall(r">\s*([1-9]\d{0,2})\s*<", block)]
    if not pages:
        return None
    if re.search(r">\s*다음\s*<|aria-label=[\"']다음[\"']", block):
        return None
    return max(pages)


def search_api_urls(
    keyword: str,
    page: int,
    cafe: CafeTarget | int | None = None,
    scope: SearchScope | None = None,
) -> list[str]:
    if isinstance(cafe, int):
        cafe_id = cafe
    elif cafe is not None:
        cafe_id = require_cafe_id(cafe)
    else:
        cafe_id = DEFAULT_CAFE_ID
    chosen = scope or SEARCH_SCOPES[0]
    encoded = quote(keyword)
    return [
        template.format(
            cafe_id=cafe_id,
            query=encoded,
            page=page,
            ta=chosen.ta,
            search_by=chosen.search_by,
        )
        for template in SEARCH_API_TEMPLATES
    ]


def article_ids_from_json(payload: Any) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ARTICLE_ID_KEYS:
                value = node.get(key)
                if value in (None, ""):
                    continue
                item = str(value)
                if item not in seen:
                    seen.add(item)
                    found.append(item)
                break
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def search_page_info(payload: Any) -> tuple[int | None, int | None, bool]:
    last_page: int | None = None
    total: int | None = None
    per_page: int | None = None
    has_more = False

    def read_info(node: dict[str, Any]) -> None:
        nonlocal last_page, total, per_page, has_more
        for key in PAGE_INFO_LAST_KEYS:
            if node.get(key) not in (None, ""):
                last_page = int(node[key])
                break
        for key in PAGE_INFO_TOTAL_KEYS:
            if node.get(key) not in (None, ""):
                total = int(node[key])
                break
        for key in ("perPage", "pageSize"):
            if node.get(key) not in (None, ""):
                try:
                    size = int(node[key])
                except (TypeError, ValueError):
                    size = 0
                if size > 0:
                    per_page = size
                    break
        if node.get("visibleNextButton") or node.get("hasMore"):
            has_more = True

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if any(key in node for key in PAGE_INFO_LAST_KEYS + PAGE_INFO_TOTAL_KEYS) or node.get(
                "visibleNextButton"
            ) is not None:
                read_info(node)
            info = node.get("pageInfo")
            if isinstance(info, dict):
                read_info(info)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    if last_page is None and total is not None:
        size = per_page or 50
        last_page = 1 if total <= 0 else max(1, (total + size - 1) // size)
        if total <= 0:
            has_more = False
    return last_page, total, has_more


def should_stop_search(
    page: int,
    last_page: int | None,
    has_more: bool,
    empty_streak: int,
    new_article_count: int,
) -> bool:
    _ = empty_streak
    if page >= MAX_SEARCH_PAGES:
        return True
    if last_page is not None and page >= last_page and not has_more:
        return True
    if new_article_count > 0:
        return False
    return True


def payload_has_articles(payload: Any) -> bool:
    return bool(search_articles_from_payload(payload))


@dataclass(slots=True)
class ExcludeSyncResult:
    keywords: list[str] = field(default_factory=list)
    found: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)
    author_links: list[tuple[str, list[str]]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"검색어 {len(self.keywords)}개",
            f"카페에서 찾은 닉네임 {len(self.found)}개",
        ]
        if self.author_links:
            total = sum(len(urls) for _nick, urls in self.author_links)
            lines.append(f"글 링크 {total}개")
        return "\n".join(lines)


@dataclass(slots=True)
class LocalSettings:
    cafe_url: str = DEFAULT_CAFE_URL
    flowmoa_user: str = "earlybirdz"
    keywords: str = ", ".join(DEFAULT_KEYWORDS)
    watch: bool = False
    watch_minutes: int = 30
    collect_author_links: bool = False

    @classmethod
    def load(cls, path: Path) -> "LocalSettings":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            cafe_url=str(data.get("cafe_url") or DEFAULT_CAFE_URL),
            flowmoa_user=str(data.get("flowmoa_user") or "earlybirdz"),
            keywords=str(data.get("keywords") or ", ".join(DEFAULT_KEYWORDS)),
            watch=bool(data.get("watch")),
            watch_minutes=max(5, int(data.get("watch_minutes") or 30)),
            collect_author_links=bool(data.get("collect_author_links")),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "cafe_url": self.cafe_url,
                    "flowmoa_user": self.flowmoa_user,
                    "keywords": self.keywords,
                    "watch": self.watch,
                    "watch_minutes": self.watch_minutes,
                    "collect_author_links": self.collect_author_links,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def build_sync_result(
    keywords: Iterable[str],
    found: Iterable[str],
    existing: Iterable[str],
) -> ExcludeSyncResult:
    keyword_list = [item for item in keywords if item]
    found_list = split_nicknames("\n".join(found))
    existing_list = split_nicknames("\n".join(existing))
    added = new_nicknames(existing_list, found_list)
    saved = merge_nicknames(existing_list, found_list)
    already = [nick for nick in found_list if nick not in added]
    return ExcludeSyncResult(
        keywords=keyword_list,
        found=found_list,
        already=already,
        added=added,
        saved=saved,
    )


def require_keywords(text: str) -> list[str]:
    keywords = split_keywords(text)
    if not keywords:
        raise NicknameExcludeError("브랜드 식별 키워드를 넣어 주세요")
    return keywords
