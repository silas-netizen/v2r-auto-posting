from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote


CAFE_HOME_URL = "https://cafe.naver.com/cantsb"
CAFE_ID = 25016228
CAFE_SEARCH_PAGE = (
    "https://cafe.naver.com/ArticleSearchList.nhn"
    f"?search.clubid={CAFE_ID}&search.media=0&search.searchBy=0"
    "&search.defaultValue=1&search.sortBy=date&search.query={query}"
)
CAFE_SEARCH_PAGE_MODERN = (
    "https://cafe.naver.com/f-e/cafes/"
    f"{CAFE_ID}/searches/articles?q={{query}}"
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
    "https://apis.naver.com/cafe-web/cafe-searchui-api/v1/cafes/"
    f"{CAFE_ID}/search/articles?query={{query}}&page={{page}}&perPage=50",
    "https://apis.naver.com/cafe-web/cafe-mobile/CafeSearchArticleList"
    f"?search.clubid={CAFE_ID}&search.query={{query}}&search.page={{page}}"
    "&search.perPage=50&search.searchBy=1",
    "https://apis.naver.com/cafe-web/cafe-search-api/v1.0/cafes/"
    f"{CAFE_ID}/articles?query={{query}}&page={{page}}&size=50",
)
HTML_NICK_PATTERNS = (
    re.compile(r'data-nickname="([^"]+)"'),
    re.compile(r'"writerNickname"\s*:\s*"([^"]+)"'),
    re.compile(r'"nickname"\s*:\s*"([^"]+)"'),
    re.compile(r'class="[^"]*nick[^"]*"[^>]*>\s*([^<]{1,40})\s*<'),
)


class NicknameExcludeError(ValueError):
    pass


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


def cafe_search_url(keyword: str) -> str:
    return CAFE_SEARCH_PAGE.format(query=quote(keyword))


def cafe_search_url_modern(keyword: str) -> str:
    return CAFE_SEARCH_PAGE_MODERN.format(query=quote(keyword))


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


def nicknames_from_html(html: str) -> list[str]:
    found: list[str] = []
    for pattern in HTML_NICK_PATTERNS:
        found.extend(pattern.findall(html or ""))
    return split_nicknames("\n".join(found))


def search_api_urls(keyword: str, page: int) -> list[str]:
    encoded = quote(keyword)
    return [template.format(query=encoded, page=page) for template in SEARCH_API_TEMPLATES]


def payload_has_articles(payload: Any) -> bool:
    if payload is None:
        return False
    if isinstance(payload, list):
        return bool(payload)
    if not isinstance(payload, dict):
        return False
    for key in ("articles", "articleList", "result", "message", "data"):
        if key in payload:
            value = payload[key]
            if isinstance(value, list):
                return bool(value)
            if isinstance(value, dict) and payload_has_articles(value):
                return True
    return bool(nicknames_from_json(payload))


@dataclass(slots=True)
class ExcludeSyncResult:
    keywords: list[str] = field(default_factory=list)
    found: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return "\n".join(
            [
                f"검색어 {len(self.keywords)}개",
                f"카페에서 찾은 닉네임 {len(self.found)}개",
                f"이미 제외된 닉네임 {len(self.already)}개",
                f"새로 넣은 닉네임 {len(self.added)}개",
                f"지금 제외 목록 {len(self.saved)}개",
            ]
        )


@dataclass(slots=True)
class LocalSettings:
    flowmoa_user: str = "earlybirdz"
    keywords: str = ", ".join(DEFAULT_KEYWORDS)
    watch: bool = False
    watch_minutes: int = 30

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
            flowmoa_user=str(data.get("flowmoa_user") or "earlybirdz"),
            keywords=str(data.get("keywords") or ", ".join(DEFAULT_KEYWORDS)),
            watch=bool(data.get("watch")),
            watch_minutes=max(5, int(data.get("watch_minutes") or 30)),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "flowmoa_user": self.flowmoa_user,
                    "keywords": self.keywords,
                    "watch": self.watch,
                    "watch_minutes": self.watch_minutes,
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
