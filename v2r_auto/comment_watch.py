from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .join_marker import clean_cell, column_letter, normalize_header


SOURCE_ID_PATTERN = re.compile(r"/articleDetail/([0-9A-Za-z]+)")
CAFE_LABELS = ("씨씨앙", "양평맘")
COMPLETION_HEADERS = ("완료 링크", "완료링크")
CAFE_HEADERS = ("카페명", "카페")
MARK_HEADERS = ("일상 글에 댓글", "일상글에댓글")
PUBLISHED_STATUSES = {"SUCCESS", "DONE"}
DEFAULT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1J8Nq-UQxLzrt3fOqIkZ2HRskZJTFQlOjmFjIh3wlzBs/"
    "edit?gid=0#gid=0"
)
CAFE_HOME_URL = "https://cafe.naver.com"
OUR_COMMENT_MARKERS = (
    "quilliant",
    "hunnede",
    "prtchht",
    "chocobbn",
    "chenallo",
    "colpith",
)
LOGIN_HINTS = (
    "nid.naver.com",
    "로그인이 필요",
    "로그인 후 이용",
    "로그인해주세요",
    "로그인 해주세요",
    "멤버만 볼 수",
    "카페 멤버만",
    "가입한 회원만",
)
COMMENT_COUNT_PATTERNS = (
    re.compile(r'"commentCount"\s*:\s*(\d+)'),
    re.compile(r'"comment_count"\s*:\s*(\d+)'),
    re.compile(r"댓글\s*<[^>]*>\s*(\d+)"),
    re.compile(r"댓글\s*(\d+)"),
)
COMMENT_NICK_PATTERN = re.compile(
    r'class="[^"]*(?:nickname|nick_name|comment_nickname)[^"]*"[^>]*>([^<]+)',
    re.IGNORECASE,
)
COMMENT_ITEM_PATTERN = re.compile(
    r'class="[^"]*(?:comment_item|CommentItem|comment_box|box_cmt)[^"]*"',
    re.IGNORECASE,
)


class CommentWatchError(ValueError):
    pass


def source_id_from_url(url: str) -> str:
    match = SOURCE_ID_PATTERN.search(url or "")
    if not match:
        raise CommentWatchError("완료 링크에서 V2R 글 번호를 찾지 못했습니다")
    return match.group(1)


def cafe_article_url(cafe_id: int | str, article_id: int | str) -> str:
    return f"https://cafe.naver.com/f-e/cafes/{cafe_id}/articles/{article_id}"


def cafe_article_fallback_url(cafe_id: int | str, article_id: int | str) -> str:
    return (
        "https://cafe.naver.com/ArticleRead.nhn"
        f"?clubid={cafe_id}&articleid={article_id}"
    )


def _find_header(headers: list[str], aliases: Iterable[str]) -> str:
    wanted = {normalize_header(alias) for alias in aliases}
    for header in headers:
        if normalize_header(header) in wanted:
            return header
    options = ", ".join(headers[:20])
    raise CommentWatchError(
        "시트에서 열을 찾지 못했습니다: "
        + ", ".join(aliases)
        + (f" / 있는 열: {options}" if options else "")
    )


def page_requires_cafe_login(html: str) -> bool:
    text = html or ""
    lowered = text.casefold()
    return any(hint.casefold() in lowered for hint in LOGIN_HINTS)


def cafe_article_ready(html: str) -> bool:
    text = html or ""
    if page_requires_cafe_login(text):
        return True
    return any(
        marker in text
        for marker in (
            "commentCount",
            "comment_count",
            "se-main-container",
            "article_container",
            "comment_list",
            "CommentBox",
        )
    ) or bool(re.search(r"댓글\s*\d+", text))


def _visible_comment_count(html: str) -> int:
    counts: list[int] = []
    for pattern in COMMENT_COUNT_PATTERNS:
        for match in pattern.finditer(html or ""):
            nearby = (html or "")[max(0, match.start() - 12) : match.start()]
            if "비허용" in nearby:
                continue
            counts.append(int(match.group(1)))
    return max(counts) if counts else 0


def _comment_nicks(html: str) -> list[str]:
    nicks: list[str] = []
    for match in COMMENT_NICK_PATTERN.finditer(html or ""):
        nick = re.sub(r"\s+", " ", match.group(1)).strip()
        if nick:
            nicks.append(nick)
    return nicks


def _is_our_comment(text: str) -> bool:
    lowered = (text or "").casefold()
    if "v2r" in lowered:
        return True
    return any(marker in lowered for marker in OUR_COMMENT_MARKERS)


def other_member_comment_count(html: str) -> int:
    """Count cafe comments from other members. Ignore V2R-written comments."""
    if page_requires_cafe_login(html):
        raise CommentWatchError(
            "네이버 카페에 로그인한 뒤 다시 확인해 주세요. "
            "로그인 준비에서 카페 창을 열어 두세요"
        )
    nicks = _comment_nicks(html)
    if nicks:
        return sum(1 for nick in nicks if not _is_our_comment(nick))
    items = COMMENT_ITEM_PATTERN.findall(html or "")
    if items:
        return len(items)
    return _visible_comment_count(html)


@dataclass(frozen=True, slots=True)
class ArticleView:
    source_id: str
    parent_source_id: str
    dest_status: str
    cafe_id: int | None
    article_id: int | None
    title: str = ""


def parse_article_view(payload: dict[str, Any]) -> ArticleView:
    source = payload.get("naver_cafe_article_source") or {}
    dest = payload.get("naver_cafe_article_destination") or {}
    history = payload.get("naver_cafe_article_history") or {}
    if not isinstance(history, dict):
        history = {}
    parent = str(source.get("parent_source_id") or "").strip()
    cafe_id = dest.get("cafe_id") if dest.get("cafe_id") is not None else history.get("cafe_id")
    article_id = history.get("article_id")
    return ArticleView(
        source_id=str(source.get("source_id") or ""),
        parent_source_id=parent,
        dest_status=str(dest.get("status") or "").upper(),
        cafe_id=int(cafe_id) if cafe_id not in (None, "") else None,
        article_id=int(article_id) if article_id not in (None, "") else None,
        title=str(source.get("title") or dest.get("title") or ""),
    )


@dataclass(frozen=True, slots=True)
class WatchDecision:
    action: str
    cafe_url: str
    reason: str
    cafe_id: int | None = None
    article_id: int | None = None


def decide_row(revision: ArticleView, parent: ArticleView | None = None) -> WatchDecision:
    """Decide whether this revision's daily cafe post should be opened."""
    if not revision.parent_source_id:
        return WatchDecision("skip", "", "이전 원본글 없음")
    if revision.dest_status in PUBLISHED_STATUSES:
        return WatchDecision("skip", "", "수정 발행이 이미 끝남")
    if parent is None:
        return WatchDecision("need_parent", "", "원본글을 더 확인해야 함")
    article_id = parent.article_id or revision.article_id
    cafe_id = parent.cafe_id or revision.cafe_id
    if not article_id or not cafe_id:
        return WatchDecision("skip", "", "카페 글 번호를 찾지 못함")
    return WatchDecision(
        "open_cafe",
        cafe_article_url(cafe_id, article_id),
        "카페에서 다른 회원 댓글을 확인해야 함",
        cafe_id=cafe_id,
        article_id=article_id,
    )


def apply_cafe_result(decision: WatchDecision, other_count: int) -> WatchDecision:
    if decision.action != "open_cafe":
        return decision
    if other_count > 0:
        return WatchDecision(
            "mark",
            decision.cafe_url,
            f"카페에서 다른 회원 댓글 {other_count}개",
            cafe_id=decision.cafe_id,
            article_id=decision.article_id,
        )
    return WatchDecision(
        "clear",
        "",
        "카페에서 다른 회원 댓글 없음",
        cafe_id=decision.cafe_id,
        article_id=decision.article_id,
    )


def inspect_rows(
    rows: list[dict[str, str]],
    fetch_article: Callable[[str], dict[str, Any]],
    check_cafe_comments: Callable[[int, int], int],
    should_stop: Callable[[], bool] | None = None,
) -> list[dict[str, str]]:
    """Fill K-column values by opening the daily cafe post."""
    updated: list[dict[str, str]] = []
    parent_cache: dict[str, ArticleView] = {}
    for row in rows:
        if should_stop and should_stop():
            raise CommentWatchError("확인을 중지했습니다")
        item = dict(row)
        mark_header = item["__mark_header"]
        item[mark_header] = ""
        if item.get("__skip_reason"):
            updated.append(item)
            continue
        revision = parse_article_view(fetch_article(item["__source_id"]))
        decision = decide_row(revision)
        if decision.action == "need_parent":
            parent_id = revision.parent_source_id
            if parent_id not in parent_cache:
                parent_cache[parent_id] = parse_article_view(fetch_article(parent_id))
            decision = decide_row(revision, parent_cache[parent_id])
        if decision.action == "open_cafe" and decision.cafe_id and decision.article_id:
            other_count = check_cafe_comments(decision.cafe_id, decision.article_id)
            decision = apply_cafe_result(decision, other_count)
        item[mark_header] = decision.cafe_url
        item["__action"] = decision.action
        item["__reason"] = decision.reason
        item["__title"] = revision.title
        updated.append(item)
    return updated


@dataclass(slots=True)
class CommentWatchPlan:
    headers: list[str]
    mark_header: str
    rows: list[dict[str, str]] = field(default_factory=list)

    def mark_count(self) -> int:
        return sum(1 for row in self.rows if (row.get(self.mark_header) or "").strip())

    def checked_count(self) -> int:
        return sum(1 for row in self.rows if row.get("__source_id"))

    def skip_count(self) -> int:
        return sum(1 for row in self.rows if row.get("__action") == "skip")

    def column_index(self, header: str) -> int:
        return self.headers.index(header)

    def start_cell(self) -> tuple[str, int]:
        return column_letter(self.column_index(self.mark_header)), 2

    def paste_chunks(self, chunk_size: int = 400) -> list[tuple[int, str]]:
        if chunk_size < 1:
            raise CommentWatchError("붙여넣기 묶음 크기가 올바르지 않습니다")
        chunks: list[tuple[int, str]] = []
        for start in range(0, len(self.rows), chunk_size):
            piece = self.rows[start : start + chunk_size]
            lines = [(row.get(self.mark_header) or "") for row in piece]
            chunks.append((2 + start, "\n".join(lines) + ("\n" if lines else "")))
        return chunks

    def summary(self) -> str:
        return "\n".join(
            [
                f"완료 링크 {self.checked_count()}개 확인",
                f"다른 회원 댓글 있어 카페 링크 {self.mark_count()}개",
                f"건너뜀 {self.skip_count()}개",
                "K열만 바꿉니다. 완료 링크와 다른 열은 건드리지 않습니다.",
            ]
        )


def load_watch_rows(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise CommentWatchError(f"시트 파일이 없습니다: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = [header for header in (reader.fieldnames or []) if header and header.strip()]
        if not headers:
            raise CommentWatchError("첫 행에서 열 이름을 찾지 못했습니다")
        completion_header = _find_header(headers, COMPLETION_HEADERS)
        cafe_header = _find_header(headers, CAFE_HEADERS)
        mark_header = _find_header(headers, MARK_HEADERS)
        rows: list[dict[str, str]] = []
        for index, row in enumerate(reader, start=2):
            cafe = clean_cell(row.get(cafe_header))
            completion = clean_cell(row.get(completion_header))
            item = {
                "__row": str(index),
                "__mark_header": mark_header,
                mark_header: clean_cell(row.get(mark_header)),
                cafe_header: cafe,
                completion_header: completion,
            }
            if not completion:
                item["__skip_reason"] = "완료 링크 없음"
            elif cafe not in CAFE_LABELS:
                item["__skip_reason"] = "씨씨앙·양평맘이 아님"
            else:
                try:
                    item["__source_id"] = source_id_from_url(completion)
                except CommentWatchError as exc:
                    item["__skip_reason"] = str(exc)
            rows.append(item)
    return headers, rows


def build_plan(
    headers: list[str],
    rows: list[dict[str, str]],
    fetch_article: Callable[[str], dict[str, Any]],
    check_cafe_comments: Callable[[int, int], int],
    should_stop: Callable[[], bool] | None = None,
) -> CommentWatchPlan:
    mark_header = _find_header(headers, MARK_HEADERS)
    inspected = inspect_rows(
        rows,
        fetch_article,
        check_cafe_comments,
        should_stop=should_stop,
    )
    return CommentWatchPlan(headers=headers, mark_header=mark_header, rows=inspected)


def plan_matches_sheet(
    headers: list[str],
    rows: list[dict[str, str]],
    plan: CommentWatchPlan,
) -> list[str]:
    errors: list[str] = []
    if plan.mark_header not in headers:
        return ["시트에서 일상 글에 댓글 열을 찾지 못했습니다"]
    if len(rows) < len(plan.rows):
        errors.append("시트 행 수가 계획보다 적습니다")
    for expected, actual in zip(plan.rows, rows, strict=False):
        row_number = expected.get("__row", "?")
        wanted = clean_cell(expected.get(plan.mark_header))
        got = clean_cell(actual.get(plan.mark_header))
        if wanted != got:
            errors.append(f"{row_number}행 K: 기대 '{wanted}' / 실제 '{got}'")
            if len(errors) >= 8:
                return errors
    return errors
