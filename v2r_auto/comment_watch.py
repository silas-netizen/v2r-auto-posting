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


class CommentWatchError(ValueError):
    pass


def source_id_from_url(url: str) -> str:
    match = SOURCE_ID_PATTERN.search(url or "")
    if not match:
        raise CommentWatchError("완료 링크에서 V2R 글 번호를 찾지 못했습니다")
    return match.group(1)


def cafe_article_url(cafe_id: int | str, article_id: int | str) -> str:
    return (
        "https://cafe.naver.com/ArticleRead.nhn"
        f"?clubid={cafe_id}&articleid={article_id}"
    )


def extra_comment_count(history: dict[str, Any] | None) -> int:
    if not history:
        return 0
    real = int(history.get("real_comment_count") or 0)
    written = int(history.get("write_comment_count") or 0)
    return max(0, real - written)


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


@dataclass(frozen=True, slots=True)
class ArticleView:
    source_id: str
    parent_source_id: str
    dest_status: str
    cafe_id: int | None
    article_id: int | None
    extra_comments: int
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
        extra_comments=extra_comment_count(history),
        title=str(source.get("title") or dest.get("title") or ""),
    )


@dataclass(frozen=True, slots=True)
class WatchDecision:
    action: str
    cafe_url: str
    reason: str


def decide_row(revision: ArticleView, parent: ArticleView | None = None) -> WatchDecision:
    """Decide the K-column value for one F-column V2R revision link."""
    if not revision.parent_source_id:
        return WatchDecision("skip", "", "이전 원본글 없음")
    if revision.dest_status in PUBLISHED_STATUSES:
        return WatchDecision("skip", "", "수정 발행이 이미 끝남")
    if parent is None:
        return WatchDecision("need_parent", "", "원본글을 더 확인해야 함")
    article_id = parent.article_id or revision.article_id
    cafe_id = parent.cafe_id or revision.cafe_id
    if parent.extra_comments > 0 and article_id and cafe_id:
        return WatchDecision(
            "mark",
            cafe_article_url(cafe_id, article_id),
            f"일상 글에 다른 댓글 {parent.extra_comments}개",
        )
    return WatchDecision("clear", "", "일상 글에 다른 댓글 없음")


def inspect_rows(
    rows: list[dict[str, str]],
    fetch_article: Callable[[str], dict[str, Any]],
) -> list[dict[str, str]]:
    """Fill K-column values using V2R article API responses."""
    updated: list[dict[str, str]] = []
    parent_cache: dict[str, ArticleView] = {}
    for row in rows:
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
                f"댓글 있어 카페 링크 {self.mark_count()}개",
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
) -> CommentWatchPlan:
    mark_header = _find_header(headers, MARK_HEADERS)
    inspected = inspect_rows(rows, fetch_article)
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
