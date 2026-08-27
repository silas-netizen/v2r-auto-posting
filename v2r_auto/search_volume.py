from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from .exposure import (
    ExposureRow,
    _ANCHOR_RE,
    _absolute_url,
    _strip_tags,
    article_dedupe_key,
    compact_text,
    is_cafe_article_url,
    is_clustered_sub_result,
    naver_search_url,
    strip_parenthetical,
)

WRITABLE_KEYWORD_TYPES = {"title", "rich_text", "text"}
_NOTE_TAIL_RE = re.compile(r"(\s*[\(（][\s\S]*)$")


@dataclass(slots=True)
class CafeArticlePreview:
    url: str
    title: str


@dataclass(slots=True)
class SpacingFix:
    keyword: str
    source: str


def volume_is_empty(value: str | None) -> bool:
    return not str(value or "").strip()


def empty_volume_rows(rows: list[ExposureRow]) -> list[ExposureRow]:
    return [
        row
        for row in rows
        if row.volume_property and volume_is_empty(row.current_volume)
    ]


def missing_search_url(row: ExposureRow) -> bool:
    return bool(row.search_url_property) and not str(row.search_url or "").strip()


def rows_to_process(rows: list[ExposureRow]) -> list[ExposureRow]:
    """빈 검색량 행, 또는 검색량은 있는데 통합검색 주소만 빠진 행."""
    seen: set[str] = set()
    selected: list[ExposureRow] = []
    for row in rows:
        needs_volume = bool(row.volume_property) and volume_is_empty(row.current_volume)
        if not needs_volume and not missing_search_url(row):
            continue
        if row.page_id in seen:
            continue
        seen.add(row.page_id)
        selected.append(row)
    return selected


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def keep_keyword_notes(original: str, spaced: str) -> str:
    match = _NOTE_TAIL_RE.search(original or "")
    if not match:
        return spaced
    return spaced + match.group(1)


def is_spacing_only_suggestion(keyword: str, suggestion: str) -> bool:
    """Autocomplete may change spaces, never add or drop other words."""
    return bool(spacing_from_autocomplete(keyword, suggestion))


def spacing_from_autocomplete(keyword: str, suggestion: str) -> str:
    line = normalize_spaces((suggestion or "").split("\n")[0])
    if not line:
        return ""
    want = compact_text(strip_parenthetical(keyword))
    got = compact_text(strip_parenthetical(line))
    if not want or got != want:
        return ""
    return strip_parenthetical(line)


def spacing_from_text(keyword: str, source: str) -> str:
    want = compact_text(strip_parenthetical(keyword))
    if not want:
        return ""
    mapping: list[int] = []
    compact_chars: list[str] = []
    for index, char in enumerate(source or ""):
        if char.isspace():
            continue
        mapping.append(index)
        compact_chars.append(char.casefold())
    compact = "".join(compact_chars)
    start = compact.find(want)
    if start < 0:
        return ""
    end = start + len(want) - 1
    extracted = (source or "")[mapping[start] : mapping[end] + 1]
    return normalize_spaces(extracted)


def collect_cafe_article_previews(html: str) -> list[CafeArticlePreview]:
    previews: list[CafeArticlePreview] = []
    seen: set[str] = set()
    for match in _ANCHOR_RE.finditer(html or ""):
        href = _absolute_url(match.group(1))
        if not is_cafe_article_url(href):
            continue
        if is_clustered_sub_result(match.group(0)):
            continue
        title = _strip_tags(match.group(2))
        if not title:
            continue
        key = article_dedupe_key(href)
        if key in seen:
            continue
        seen.add(key)
        previews.append(CafeArticlePreview(url=href, title=title))
    return previews


def first_visible_cafe_title(
    html: str, visible_urls: list[str] | None
) -> str:
    previews = collect_cafe_article_previews(html)
    if visible_urls is not None:
        keys = {article_dedupe_key(url) for url in visible_urls if url}
        previews = [
            item for item in previews if article_dedupe_key(item.url) in keys
        ]
    return previews[0].title if previews else ""


def keyword_spacing_changed(original: str, spaced: str) -> bool:
    return normalize_spaces(strip_parenthetical(original)) != normalize_spaces(
        strip_parenthetical(spaced)
    )


class SearchVolumeFiller:
    def __init__(
        self,
        notion,
        naver,
        logger: logging.Logger,
        delay_seconds: float = 0.5,
    ):
        self.notion = notion
        self.naver = naver
        self.logger = logger
        self.delay_seconds = delay_seconds
        self.failed_rows = 0

    def run(
        self,
        rows: list[ExposureRow],
        *,
        dry_run: bool,
        stop_event=None,
        pause_event=None,
        progress=None,
    ) -> list[ExposureRow]:
        targets = rows_to_process(rows)
        skipped = len(rows) - len(targets)
        empty = empty_volume_rows(targets)
        url_only = len(targets) - len(empty)
        if skipped:
            self.logger.info(
                "이미 끝난 키워드 %s건은 건너뜁니다. 빈 검색량 %s건, 통합검색만 비어 있는 행 %s건",
                skipped,
                len(empty),
                url_only,
            )
        if not targets:
            self.logger.info("채울 검색량·통합검색이 없습니다")
            if progress:
                progress(0, 0)
            return targets
        total = len(targets)
        self.failed_rows = 0
        for index, row in enumerate(targets, start=1):
            if stop_event is not None and stop_event.is_set():
                self.logger.info("중지 요청으로 검색량 채우기를 멈춥니다")
                break
            self._wait_while_paused(pause_event, stop_event)
            if stop_event is not None and stop_event.is_set():
                self.logger.info("중지 요청으로 검색량 채우기를 멈춥니다")
                break
            keyword = strip_parenthetical(row.keyword)
            if not keyword:
                self.logger.warning("괄호를 빼니 검색어가 없어 건너뜁니다: %s", row.keyword)
                if progress:
                    progress(index, total)
                continue
            if progress:
                progress(index - 1, total)
            try:
                self._fill_one(row, dry_run=dry_run)
            except Exception as exc:
                self.failed_rows += 1
                self.logger.error(
                    "키워드 처리 실패, 다음으로 이어갑니다: %s (%s)",
                    keyword,
                    exc,
                )
            self._wait_while_paused(pause_event, stop_event)
            if self.delay_seconds:
                self._interruptible_delay(
                    self.delay_seconds, stop_event, pause_event
                )
            if progress:
                progress(index, total)
        if self.failed_rows:
            self.logger.warning(
                "검색량 채우기를 마쳤습니다. %s건 중 %s건은 시트에 다 못 넣었습니다",
                total,
                self.failed_rows,
            )
        return targets

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
            self.logger.info("검색량 채우기를 다시 시작합니다")

    def _interruptible_delay(self, seconds: float, stop_event, pause_event) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if stop_event is not None and stop_event.is_set():
                return
            self._wait_while_paused(pause_event, stop_event)
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(0.1)

    def _fill_one(self, row: ExposureRow, *, dry_run: bool) -> None:
        keyword = strip_parenthetical(row.keyword)
        if not volume_is_empty(row.current_volume):
            search_url = naver_search_url(keyword)
            self.logger.info(
                "검색량은 이미 있어 통합검색 주소만 넣습니다: %s", keyword
            )
            self._write_result(
                row,
                written_keyword=None,
                volume=None,
                volume_found=False,
                search_url=search_url,
                dry_run=dry_run,
            )
            return
        self.logger.info("빈 검색량 키워드: %s", keyword)
        spacing = self._resolve_spacing(row.keyword)
        written_keyword = keep_keyword_notes(row.keyword, spacing.keyword)
        keyword_changed = keyword_spacing_changed(row.keyword, written_keyword)
        if keyword_changed and row.keyword_type not in WRITABLE_KEYWORD_TYPES:
            self.logger.warning(
                "키워드 열 형식이 글자가 아니라 띄어쓰기를 못 바꿉니다: %s",
                row.keyword_type or "(없음)",
            )
            keyword_changed = False
            written_keyword = row.keyword
        if spacing.source and keyword_changed:
            self.logger.info(
                "띄어쓰기 수정(%s): %s → %s",
                spacing.source,
                strip_parenthetical(row.keyword),
                spacing.keyword,
            )
        elif spacing.source:
            self.logger.info("띄어쓰기는 이미 같습니다: %s", spacing.keyword)
        else:
            self.logger.info("띄어쓰기 참고를 못 찾아 키워드는 그대로 둡니다: %s", keyword)
        volume = None
        volume_found = False
        lookup = getattr(self.naver, "lookup_search_volume", None)
        if callable(lookup):
            try:
                volume = lookup(spacing.keyword)
                volume_found = volume is not None
                if volume_found:
                    self.logger.info("키워드 검색량: %s = %s", spacing.keyword, volume)
            except Exception as exc:
                self.logger.error("검색량 조회 실패 (%s): %s", spacing.keyword, exc)
        else:
            self.logger.warning("검색량 조회 기능이 없습니다")
        self._write_result(
            row,
            written_keyword=written_keyword if keyword_changed else None,
            volume=volume,
            volume_found=volume_found,
            search_url=naver_search_url(spacing.keyword),
            dry_run=dry_run,
        )
        if keyword_changed:
            row.keyword = written_keyword
        if volume_found:
            row.current_volume = str(volume)

    def _resolve_spacing(self, keyword: str) -> SpacingFix:
        suggestion = ""
        peek = getattr(self.naver, "peek_first_autocomplete", None)
        if callable(peek):
            try:
                suggestion = peek(keyword) or ""
            except Exception as exc:
                self.logger.warning("자동완성 확인 실패: %s", exc)
        spaced = spacing_from_autocomplete(keyword, suggestion)
        if spaced:
            return SpacingFix(keyword=spaced, source="자동완성")
        if suggestion:
            self.logger.info(
                "자동완성 첫 항목에 다른 단어가 붙어 무시하고 통합검색 카페 글을 봅니다: %s",
                suggestion.split("\n")[0][:40],
            )
        else:
            self.logger.info("자동완성이 없어 통합검색 카페 글을 봅니다")
        try:
            html = self.naver.search_integrated(keyword)
        except Exception as exc:
            self.logger.error("네이버 검색 실패 (%s): %s", keyword, exc)
            return SpacingFix(keyword=strip_parenthetical(keyword), source="")
        visible_urls = None
        lookup_visible = getattr(self.naver, "visible_cafe_article_urls", None)
        if callable(lookup_visible):
            try:
                visible_urls = lookup_visible()
            except Exception as exc:
                self.logger.warning("통검 화면 글 확인 실패: %s", exc)
                visible_urls = None
        title = first_visible_cafe_title(html, visible_urls)
        if not title:
            return SpacingFix(keyword=strip_parenthetical(keyword), source="")
        spaced = spacing_from_text(keyword, title)
        if not spaced:
            self.logger.info(
                "첫 카페 글 제목에 같은 글자가 없습니다: %s",
                title[:60],
            )
            return SpacingFix(keyword=strip_parenthetical(keyword), source="")
        return SpacingFix(keyword=spaced, source="통합검색 카페 글")

    def _write_result(
        self,
        row: ExposureRow,
        *,
        written_keyword: str | None,
        volume: int | None,
        volume_found: bool,
        search_url: str,
        dry_run: bool,
    ) -> None:
        label = getattr(self.notion, "label", "노션")
        if dry_run:
            self.logger.info(
                "검증 모드: %s / 검색량 %s / 통합검색 %s (%s에 쓰지 않음)",
                written_keyword or row.keyword,
                volume if volume_found else "(조회 안 됨)",
                search_url,
                label,
            )
            return
        failed = 0
        if hasattr(self.notion, "update_volume_and_keyword"):
            try:
                failed = (
                    self.notion.update_volume_and_keyword(
                        row,
                        keyword=written_keyword,
                        search_volume=volume,
                        volume_found=volume_found,
                        search_url=search_url,
                    )
                    or 0
                )
            except Exception as exc:
                failed = 1
                self.logger.error(
                    "시트 저장 실패, 다음 키워드로 이어갑니다 (%s): %s",
                    row.keyword,
                    exc,
                )
        if failed:
            self.failed_rows += 1
            self.logger.warning(
                "이 키워드는 시트에 다 못 넣었습니다. 다음으로 이어갑니다: %s",
                row.keyword,
            )
            return
        if written_keyword:
            self.logger.info("%s 키워드 띄어쓰기 변경: %s", label, written_keyword)
        if search_url:
            self.logger.info("%s 통합검색 반영: %s", label, search_url)
            row.search_url = search_url
        if volume_found:
            self.logger.info("%s 검색량 반영: %s = %s", label, row.keyword, volume)
        elif not written_keyword:
            self.logger.warning(
                "검색량을 못 읽어 검색량 칸은 그대로 둡니다: %s", row.keyword
            )
