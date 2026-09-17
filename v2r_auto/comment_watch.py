from __future__ import annotations

import csv
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .join_marker import clean_cell, column_letter, normalize_header

logger = logging.getLogger(__name__)


SOURCE_ID_PATTERN = re.compile(r"/articleDetail/([0-9A-Za-z]+)")
CAFE_LABELS = ("씨씨앙", "양평맘")
COMPLETION_HEADERS = ("완료 링크", "완료링크")
CAFE_HEADERS = ("카페명", "카페")
MARK_HEADERS = ("일상 글에 댓글", "일상글에댓글")
PUBLISHED_STATUSES = {"SUCCESS", "DONE"}
PUBLISHED_COMMENT_MARK_AT = 13
DEFAULT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1OwR_LSjO1ofOojldtSIqoxv0gieNSMx_t35_5G1VTCc/"
    "edit?gid=0#gid=0"
)
KST = timezone(timedelta(hours=9))
DEFAULT_REPEAT_MINUTES = 60
CAFE_HOME_URL = "https://cafe.naver.com"
OUR_COMMENT_MARKERS = (
    "quilliant",
    "hunnede",
    "prtchht",
    "chocobbn",
    "chenallo",
    "colpith",
)
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
ARTICLE_COMMENT_COUNT_PATTERNS = (
    re.compile(r'"article"\s*:\s*\{[^{}]{0,1200}"commentCount"\s*:\s*(\d+)'),
    re.compile(r'"commentCount"\s*:\s*(\d+)'),
    re.compile(r'"comment_count"\s*:\s*(\d+)'),
    re.compile(r"댓글\s*<[^>]*>\s*(\d+)"),
    re.compile(r">댓글\s*(\d+)<"),
)
COMMENT_LIST_ITEM_PATTERN = re.compile(
    r'class="[^"]*(?:CommentItem|comment_item)[^"]*"',
    re.IGNORECASE,
)


class CommentWatchError(ValueError):
    pass


def parse_repeat_minutes(value: str, *, default: int = DEFAULT_REPEAT_MINUTES) -> int:
    text = (value or "").strip()
    if not text:
        return default
    if not text.isdigit():
        raise ValueError("반복 간격은 분만 넣어 주세요. 예: 60")
    number = int(text)
    if number < 1:
        raise ValueError("반복 간격은 1분 이상으로 넣어 주세요")
    if number > 24 * 60:
        raise ValueError("반복 간격은 1440분(하루) 이하로 넣어 주세요")
    return number


def parse_daily_time(value: str) -> tuple[int, int]:
    text = (value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise ValueError("매일 실행 시각은 시:분으로 넣어 주세요. 예: 09:00")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("매일 실행 시각은 00:00부터 23:59까지입니다")
    return hour, minute


def now_kst(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(KST)
    if now.tzinfo is None:
        return now.replace(tzinfo=KST)
    return now.astimezone(KST)


def next_daily_wait_seconds(
    hour: int,
    minute: int,
    now: datetime | None = None,
    *,
    allow_grace: bool = False,
    grace_seconds: int = 60,
) -> float:
    current = now_kst(now)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if current < target:
        return (target - current).total_seconds()
    elapsed = (current - target).total_seconds()
    if allow_grace and elapsed <= grace_seconds:
        return 0.0
    target += timedelta(days=1)
    return (target - current).total_seconds()


def next_cycle_wait_seconds(
    *,
    repeat_enabled: bool,
    repeat_minutes: int,
    daily_enabled: bool,
    daily_time: tuple[int, int] | None,
    first_cycle: bool,
    now: datetime | None = None,
) -> float | None:
    """Seconds to wait before the next pass. None means the job should stop."""
    if first_cycle:
        if daily_enabled and daily_time is not None:
            return next_daily_wait_seconds(
                daily_time[0],
                daily_time[1],
                now=now,
                allow_grace=True,
            )
        return 0.0
    if repeat_enabled:
        return float(max(1, repeat_minutes) * 60)
    if daily_enabled and daily_time is not None:
        return next_daily_wait_seconds(
            daily_time[0],
            daily_time[1],
            now=now,
            allow_grace=False,
        )
    return None


def format_wait_remaining(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def wait_with_events(
    seconds: float,
    *,
    stop_event,
    pause_event=None,
    on_tick=None,
    step: float = 1.0,
) -> bool:
    """Wait while honoring stop/pause. Return False if stopped."""
    remaining = max(0.0, float(seconds))
    chunk = max(0.05, float(step))
    while remaining > 0:
        if stop_event.is_set():
            return False
        if pause_event is not None and pause_event.is_set():
            if on_tick is not None:
                on_tick(remaining, paused=True)
            if stop_event.wait(timeout=chunk):
                return False
            continue
        if on_tick is not None:
            on_tick(remaining, paused=False)
        slept = min(chunk, remaining)
        if stop_event.wait(timeout=slept):
            return False
        remaining -= slept
    return True


def v2r_article_is_gone(exc: BaseException) -> bool:
    """True when V2R says that cafe article source no longer exists."""
    text = str(exc)
    markers = (
        "DELETED_NAVER_CAFE_ARTICLE_SOURCE",
        "DELETED_NAVER_CAFE_ARTICLE",
        "NAVER_CAFE_ARTICLE_NOT_FOUND",
        "ARTICLE_SOURCE_NOT_FOUND",
        "NAVER_CAFE_ARTICLE_SOURCE_NOT_FOUND",
    )
    return any(marker in text for marker in markers)


def _is_login_error(exc: BaseException) -> bool:
    text = str(exc)
    return (
        "네이버 로그인 화면이 열렸습니다" in text
        or "NAVER_LOGIN_REQUIRED" in text
        or "로그인이 풀렸" in text
    )


def user_facing_watch_error(exc: BaseException) -> str:
    if v2r_article_is_gone(exc):
        return "V2R에서 이미 지워진 글이 있어 이 줄은 건너뛰고 나머지를 확인합니다"
    text = str(exc)
    if "시트 표시를 확인하지 못했습니다" in text:
        return (
            "이번에 다른 회원 댓글이 없어 K열을 비워야 하는데, "
            "예전에 넣어 둔 카페 링크가 남아 있습니다. "
            "새 프로그램을 받으면 그 칸을 직접 비웁니다"
        )
    if "TOKEN_ERROR" in text or "로그인 정보" in text:
        return "V2R 로그인이 풀렸습니다. 다시 로그인한 뒤 확인해 주세요"
    if "네트워크" in text:
        return "V2R 연결이 불안정합니다. 잠시 후 다시 확인해 주세요"
    if isinstance(exc, CommentWatchError):
        return text
    return "댓글을 확인하는 중 문제가 났습니다. 잠시 후 다시 확인해 주세요"


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


def cafe_article_ready(html: str) -> bool:
    text = html or ""
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


def page_requires_cafe_login(html: str, url: str = "") -> bool:
    """Only a real login page counts. Logged-in cafe HTML still mentions nid.naver.com."""
    current = (url or "").casefold()
    if any(hint in current for hint in LOGIN_URL_HINTS):
        return True
    if cafe_article_ready(html):
        return False
    lowered = (html or "").casefold()
    return any(hint.casefold() in lowered for hint in LOGIN_PAGE_HINTS)


def _article_comment_count(html: str) -> int:
    """Use the article's own 댓글 N. Do not count writer/author nicknames."""
    text = html or ""
    for pattern in ARTICLE_COMMENT_COUNT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        nearby = text[max(0, match.start() - 12) : match.start()]
        if "비허용" in nearby:
            continue
        return int(match.group(1))
    return 0


def _v2r_comments_in_list(html: str) -> int:
    found = 0
    for match in COMMENT_LIST_ITEM_PATTERN.finditer(html or ""):
        start = match.start()
        chunk = (html or "")[start : start + 400].casefold()
        if "v2r" in chunk or any(marker in chunk for marker in OUR_COMMENT_MARKERS):
            found += 1
    return found


@dataclass(frozen=True, slots=True)
class CafeCommentView:
    other_count: int
    total_count: int
    our_count: int = 0


def cafe_comment_view(html: str, url: str = "") -> CafeCommentView:
    """Read the cafe article comment counts from one page."""
    if page_requires_cafe_login(html, url):
        raise CommentWatchError(
            "네이버 로그인 화면이 열렸습니다. "
            "이 프로그램 크롬에서 네이버에 로그인한 뒤 다시 확인해 주세요. "
            "카페 창을 따로 열어둘 필요는 없습니다"
        )
    total = _article_comment_count(html)
    our = _v2r_comments_in_list(html)
    return CafeCommentView(
        other_count=max(0, total - our),
        total_count=total,
        our_count=our,
    )


def other_member_comment_count(html: str, url: str = "") -> int:
    """Count cafe comments from other members. Ignore V2R-written comments."""
    return cafe_comment_view(html, url).other_count


def is_published_dest(status: str) -> bool:
    return str(status or "").upper() in PUBLISHED_STATUSES


def _as_comment_view(value: CafeCommentView | int) -> CafeCommentView:
    if isinstance(value, CafeCommentView):
        return value
    count = int(value)
    return CafeCommentView(other_count=count, total_count=count)


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
    published: bool = False


def decide_row(revision: ArticleView, parent: ArticleView | None = None) -> WatchDecision:
    """Decide whether this row's cafe post should be opened.

    일상 글: 수정 글이 아직 예약. 다른 회원 댓글이 있으면 링크.
    발행 글: 수정 글이 SUCCESS/DONE. 같은 카페 글을 다시 열고,
    댓글이 13개 이상이거나 우리 닉네임이 아닌 댓글이 있으면 링크.
    """
    if not revision.parent_source_id:
        return WatchDecision("skip", "", "이전 원본글 없음")
    if parent is None:
        return WatchDecision("need_parent", "", "원본글을 더 확인해야 함")
    article_id = parent.article_id or revision.article_id
    cafe_id = parent.cafe_id or revision.cafe_id
    if not article_id or not cafe_id:
        return WatchDecision("skip", "", "카페 글 번호를 찾지 못함")
    published = is_published_dest(revision.dest_status)
    return WatchDecision(
        "open_cafe",
        cafe_article_url(cafe_id, article_id),
        (
            "발행된 글의 댓글을 확인해야 함"
            if published
            else "카페에서 다른 회원 댓글을 확인해야 함"
        ),
        cafe_id=cafe_id,
        article_id=article_id,
        published=published,
    )


def apply_cafe_result(
    decision: WatchDecision, comments: CafeCommentView | int
) -> WatchDecision:
    if decision.action != "open_cafe":
        return decision
    view = _as_comment_view(comments)
    if decision.published:
        if view.total_count >= PUBLISHED_COMMENT_MARK_AT or (
            view.our_count > 0 and view.other_count > 0
        ):
            reason = (
                f"발행 후 댓글 {view.total_count}개"
                if view.total_count >= PUBLISHED_COMMENT_MARK_AT
                else f"발행 후 다른 회원 댓글 {view.other_count}개"
            )
            return WatchDecision(
                "mark",
                decision.cafe_url,
                reason,
                cafe_id=decision.cafe_id,
                article_id=decision.article_id,
                published=True,
            )
        return WatchDecision(
            "clear",
            "",
            "발행 후 다른 회원 댓글 없음",
            cafe_id=decision.cafe_id,
            article_id=decision.article_id,
            published=True,
        )
    if view.other_count > 0:
        return WatchDecision(
            "mark",
            decision.cafe_url,
            f"카페에서 다른 회원 댓글 {view.other_count}개",
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
    check_cafe_comments: Callable[[int, int], CafeCommentView | int],
    should_stop: Callable[[], bool] | None = None,
) -> list[dict[str, str]]:
    """Fill K-column values by opening the daily cafe post."""
    updated: list[dict[str, str]] = []
    parent_cache: dict[str, ArticleView | None] = {}
    for row in rows:
        if should_stop and should_stop():
            raise CommentWatchError("확인을 중지했습니다")
        item = dict(row)
        mark_header = item["__mark_header"]
        item[mark_header] = ""
        if item.get("__skip_reason"):
            updated.append(item)
            continue
        try:
            revision = parse_article_view(fetch_article(item["__source_id"]))
        except Exception as exc:
            if v2r_article_is_gone(exc):
                item["__action"] = "skip"
                item["__reason"] = "V2R에서 글이 삭제됨"
                logger.info(
                    "완료 링크 글이 V2R에서 삭제되어 이 줄은 건너뜁니다: %s",
                    item["__source_id"],
                )
                updated.append(item)
                continue
            raise
        decision = decide_row(revision)
        if decision.action == "need_parent":
            parent_id = revision.parent_source_id
            if parent_id not in parent_cache:
                try:
                    parent_cache[parent_id] = parse_article_view(
                        fetch_article(parent_id)
                    )
                except Exception as exc:
                    if v2r_article_is_gone(exc):
                        parent_cache[parent_id] = None
                    else:
                        raise
            parent = parent_cache[parent_id]
            if parent is None:
                item["__action"] = "skip"
                item["__reason"] = "V2R에서 원글이 삭제됨"
                item["__title"] = revision.title
                logger.info(
                    "완료 링크의 원글이 V2R에서 삭제되어 이 줄은 건너뜁니다: %s",
                    parent_id,
                )
                updated.append(item)
                continue
            decision = decide_row(revision, parent)
        if decision.action == "open_cafe" and decision.cafe_id and decision.article_id:
            try:
                other_count = check_cafe_comments(decision.cafe_id, decision.article_id)
            except Exception as exc:
                if _is_login_error(exc):
                    raise
                item["__action"] = "skip"
                item["__reason"] = "카페 글을 열 수 없음"
                item["__title"] = revision.title
                logger.info(
                    "카페 글 %s를 열 수 없어 이 줄은 건너뜁니다",
                    decision.article_id,
                )
                updated.append(item)
                continue
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

    def leftover_mark_cells(
        self, sheet_rows: list[dict[str, str]]
    ) -> list[tuple[int, str]]:
        leftovers: list[tuple[int, str]] = []
        for expected, actual in zip(self.rows, sheet_rows, strict=False):
            wanted = clean_cell(expected.get(self.mark_header))
            got = clean_cell(actual.get(self.mark_header))
            if wanted == got:
                continue
            leftovers.append((int(expected["__row"]), wanted))
        return leftovers

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
    check_cafe_comments: Callable[[int, int], CafeCommentView | int],
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
