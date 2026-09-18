"""Conservative public-page reader for daily-post collection.

This is the README-equivalent collection layer. It only uses documented
public endpoints and stops at login or paywalls. It does not impersonate
TLS fingerprints, solve CAPTCHA, or bypass WAF or authentication.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


LOGIN_HINTS = (
    "로그인",
    "log in",
    "sign in",
    "paywall",
    "구독자만",
    "members only",
)
FEED_HINTS = ("rss", "atom", "feed", "syndication")


class PublicReadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PublicReadResult:
    url: str
    title: str
    text: str
    source_kind: str
    blocked: bool = False
    reason: str = ""
    links: list[str] = field(default_factory=list)


class _HTMLExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.links: list[str] = []
        self.json_ld: list[dict] = []
        self._capture_title = False
        self._capture_ld = False
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {key.lower(): (value or "") for key, value in attrs}
        if tag in {"script", "style"}:
            self._skip = True
            if data.get("type") == "application/ld+json":
                self._capture_ld = True
                self._skip = False
            return
        if tag == "title":
            self._capture_title = True
        if tag == "meta":
            key = data.get("property") or data.get("name")
            if key:
                self.meta[key.lower()] = data.get("content", "")
        if tag == "link" and "rss" in (data.get("type") or "") + (data.get("rel") or ""):
            href = data.get("href")
            if href:
                self.links.append(href)
        if tag == "a":
            href = data.get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip = False
            self._capture_ld = False
        if tag == "title":
            self._capture_title = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._capture_ld:
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                self.json_ld.append(loaded)
            elif isinstance(loaded, list):
                self.json_ld.extend(item for item in loaded if isinstance(item, dict))
            return
        if self._skip:
            return
        if self._capture_title:
            self.title_parts.append(text)
        else:
            self.text_parts.append(text)


class PublicPageReader:
    def __init__(self, opener: Callable = urlopen, timeout: int = 8):
        self.opener = opener
        self.timeout = timeout

    def fetch(self, url: str) -> PublicReadResult:
        request = Request(
            url,
            headers={"User-Agent": "V2RPublicReader/1.0"},
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                final_url = response.geturl()
        except HTTPError as exc:
            if exc.code in {401, 403}:
                return PublicReadResult(
                    url=url,
                    title="",
                    text="",
                    source_kind="blocked",
                    blocked=True,
                    reason="authentication required",
                )
            raise PublicReadError(f"공개 페이지를 읽지 못했습니다: {exc}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise PublicReadError(f"공개 페이지를 읽지 못했습니다: {exc}") from exc

        text = raw.decode("utf-8", errors="replace")
        lowered = text.casefold()
        if any(hint in lowered for hint in LOGIN_HINTS) and len(text) < 1200:
            return PublicReadResult(
                url=final_url,
                title="",
                text="",
                source_kind="blocked",
                blocked=True,
                reason="authentication required",
            )
        if "rss" in content_type or "atom" in content_type or _looks_like_feed(text):
            return self._from_feed(final_url, text)
        return self._from_html(final_url, text)

    def _from_html(self, url: str, html: str) -> PublicReadResult:
        parser = _HTMLExtractor()
        parser.feed(html)
        title = (
            parser.meta.get("og:title")
            or parser.meta.get("twitter:title")
            or " ".join(parser.title_parts)
        )
        description = (
            parser.meta.get("og:description")
            or parser.meta.get("description")
            or ""
        )
        article = ""
        for item in parser.json_ld:
            if item.get("@type") in {"Article", "BlogPosting", "NewsArticle"}:
                article = str(item.get("articleBody") or item.get("description") or "")
                title = title or str(item.get("headline") or "")
                break
        body = article or description or " ".join(parser.text_parts[:40])
        links = [urljoin(url, href) for href in parser.links if _is_feed_link(href)]
        return PublicReadResult(
            url=url,
            title=_clean(title),
            text=_clean(body),
            source_kind="html",
            links=links,
        )

    def _from_feed(self, url: str, xml: str) -> PublicReadResult:
        titles = re.findall(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml, re.I)
        summaries = re.findall(
            r"<(?:summary|description)[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</(?:summary|description)>",
            xml,
            re.I | re.S,
        )
        title = _clean(titles[1] if len(titles) > 1 else (titles[0] if titles else ""))
        text = _clean(summaries[0] if summaries else "")
        return PublicReadResult(
            url=url,
            title=title,
            text=text,
            source_kind="feed",
        )


def _looks_like_feed(text: str) -> bool:
    head = text[:400].casefold()
    return "<rss" in head or "<feed" in head


def _is_feed_link(href: str) -> bool:
    lowered = href.casefold()
    return any(hint in lowered for hint in FEED_HINTS)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()
