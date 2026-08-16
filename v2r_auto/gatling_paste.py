from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook

from .cafe_catalog import normalized_name
from .content import CommentNode, ContentFormatError, ParsedArticle, parse_article
from .daily_posts import DailyPostSheetError, load_daily_posts
from .models import DailyPost


DAILY_POST_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1vSON0Rej9anDQXcAOXyBrCr50B4MMqZ79FahF4cDPJw/edit?gid=1842684291#gid=1842684291"
)
AFFILIATE_CAFES = {"씨씨앙", "양평맘"}
AFFILIATE_EXACT_BOARDS = {
    "씨씨앙": "자유 수다방",
    "양평맘": "이모저모 이야기💕",
}
KNOWN_EXACT_BOARDS = (
    "자유 수다방",
    "이모저모 이야기💕",
    "웨딩홀탑방기",
)
BOARD_NAME_ALIASES = {
    normalized_name("웨딩홀탐방기"): "웨딩홀탑방기",
}

MASTER_SHEET_NAME = "마스터"
MASTER_HEADER_ROW = 6
MASTER_HEADERS = (
    "링크",
    "타입",
    "제목",
    "내용",
    "크롬번호",
    "아이디",
    "비번",
    "해시태그",
    "말머리",
    "게시판이름",
    "멤버공개",
    "댓글비허용",
    "첨부비디오위치",
    "첨부이미지위치",
    "결과",
    "결과링크",
    "아이피",
    "비고",
    "메모",
)
TYPE_NEW_POST = "새글"
TYPE_EDIT_POST = "글수정"
TYPE_COMMENT = "댓글"
TYPE_REPLY = "대댓글"
COMMENT_ALLOWED = "허용"

BRAND_REQUIRED_COLUMNS = {
    "keyword": "키워드",
    "body": "본문",
    "cafe": "카페명",
    "article_type": "원고유형",
}
BOARD_HEADERS = {"게시판명", "게시판", "메뉴", "메뉴명"}
OPTIONAL_COLUMNS = {
    "account": "작성계정",
    "completion_url": "완료 링크",
    "prefix": "말머리",
    "account_type": "계정유형",
    "image_disabled": "이미지 없음",
}


class GatlingPasteError(ValueError):
    pass


@dataclass(slots=True)
class GatlingBrandJob:
    row_number: int
    keyword: str
    article: ParsedArticle
    cafe: str
    board: str
    account: str = ""
    article_type: str = ""
    prefix: str = ""
    account_type: str = ""
    image_disabled: bool = False
    completion_url: str = ""
    cafe_article_url: str = ""
    daily_post: DailyPost | None = None


@dataclass(slots=True)
class MasterRow:
    link: str | int | float | None = None
    type: str = ""
    title: str = ""
    body: str = ""
    hashtag: str = ""
    prefix: str = ""
    board_name: str = ""
    comment_policy: str = ""
    result_link: str = ""

    def cells(self) -> list[object]:
        return [
            self.link,
            self.type,
            self.title,
            self.body,
            None,
            None,
            None,
            self.hashtag,
            self.prefix,
            self.board_name,
            None,
            self.comment_policy,
            None,
            None,
            None,
            self.result_link or None,
            None,
            None,
            None,
        ]


@dataclass(slots=True)
class GatlingBuildResult:
    rows: list[MasterRow]
    jobs: list[GatlingBrandJob] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def type_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.type] = counts.get(row.type, 0) + 1
        return counts


def is_affiliate_cafe(cafe: str) -> bool:
    return cafe.strip() in AFFILIATE_CAFES


def exact_board_name(
    sheet_board: str,
    cafe: str,
    extra_exact_names: list[str] | tuple[str, ...] = (),
) -> str:
    """Compare our sheet board without spaces, write the real cafe board name."""
    wanted = (sheet_board or "").strip()
    cafe_name = cafe.strip()
    known: list[str] = []
    default = AFFILIATE_EXACT_BOARDS.get(cafe_name, "")
    if default:
        known.append(default)
    known.extend(KNOWN_EXACT_BOARDS)
    known.extend(name.strip() for name in extra_exact_names if str(name).strip())

    unique_known: list[str] = []
    seen: set[str] = set()
    for name in known:
        key = normalized_name(name)
        if key and key not in seen:
            unique_known.append(name)
            seen.add(key)

    if not wanted:
        if default:
            return default
        raise GatlingPasteError(f"{cafe_name} 게시판명이 비어 있습니다")

    alias = BOARD_NAME_ALIASES.get(normalized_name(wanted))
    if alias:
        return alias

    wanted_key = normalized_name(wanted)
    matches = [name for name in unique_known if normalized_name(name) == wanted_key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise GatlingPasteError(
            f"{cafe_name} 게시판명이 여러 개와 맞습니다: {wanted}"
        )
    if default:
        raise GatlingPasteError(
            f"{cafe_name} 게시판명을 실제 카페 게시판과 맞출 수 없습니다: {wanted}. "
            f"기관총에는 '{default}'처럼 정확한 이름이 필요합니다"
        )
    return wanted


def reply_target_value(node: CommentNode) -> int | float:
    """기관총 대댓글 A열에 원래 쓰이는 대상 번호. URL을 넣지 않는다."""
    if node.depth <= 0:
        raise GatlingPasteError(f"대댓글 대상 번호가 없습니다: {node.label}")
    if node.depth == 1:
        return node.index
    if node.depth in {2, 3}:
        return float(f"{node.index}.{node.depth - 1}")
    raise GatlingPasteError(f"지원하지 않는 댓글 깊이입니다: {node.label}")


def _cell(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _find_header(headers: list[str], candidates: set[str]) -> str:
    return next((header for header in headers if header.strip() in candidates), "")


def load_gatling_brand_jobs(
    path: str | Path,
    *,
    skip_completed: bool = True,
) -> tuple[list[GatlingBrandJob], list[str]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise GatlingPasteError(f"브랜드 시트 파일이 없습니다: {csv_path}")

    skipped: list[str] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = [header for header in (reader.fieldnames or []) if header]
        missing = [
            header
            for header in BRAND_REQUIRED_COLUMNS.values()
            if header not in headers
        ]
        board_header = _find_header(headers, BOARD_HEADERS)
        if not board_header:
            missing.append("게시판명")
        if missing:
            raise GatlingPasteError(
                "브랜드 시트 열을 찾지 못했습니다: " + ", ".join(missing)
            )

        jobs: list[GatlingBrandJob] = []
        for row_number, row in enumerate(reader, start=2):
            keyword = _cell(row.get(BRAND_REQUIRED_COLUMNS["keyword"]))
            source = _cell(row.get(BRAND_REQUIRED_COLUMNS["body"]))
            cafe = _cell(row.get(BRAND_REQUIRED_COLUMNS["cafe"]))
            article_type = _cell(row.get(BRAND_REQUIRED_COLUMNS["article_type"]))
            board = _cell(row.get(board_header))
            if not any((keyword, source, cafe, article_type, board)):
                continue
            if not all((keyword, source, cafe, article_type)):
                skipped.append(f"행 {row_number}: 키워드·본문·카페명·원고유형이 비어 있음")
                continue
            completion_url = _cell(row.get(OPTIONAL_COLUMNS["completion_url"]))
            if skip_completed and completion_url:
                skipped.append(f"행 {row_number}: F열 완료 링크가 있어 건너뜀")
                continue
            try:
                article = parse_article(keyword, source)
            except ContentFormatError as exc:
                skipped.append(f"행 {row_number}: 원고 형식 오류 ({exc})")
                continue
            jobs.append(
                GatlingBrandJob(
                    row_number=row_number,
                    keyword=keyword,
                    article=article,
                    cafe=cafe,
                    board=board,
                    account=_cell(row.get(OPTIONAL_COLUMNS["account"])),
                    article_type=article_type,
                    prefix=_cell(row.get(OPTIONAL_COLUMNS["prefix"])),
                    account_type=_cell(row.get(OPTIONAL_COLUMNS["account_type"])),
                    image_disabled=(
                        _cell(row.get(OPTIONAL_COLUMNS["image_disabled"])).casefold()
                        == "y"
                    ),
                    completion_url=completion_url,
                )
            )
    if not jobs:
        raise GatlingPasteError("붙여넣을 브랜드 원고가 없습니다")
    return jobs, skipped


def assign_daily_posts(
    jobs: list[GatlingBrandJob],
    daily_posts: list[DailyPost],
    rng: random.Random | None = None,
) -> None:
    randomizer = rng or random.SystemRandom()
    for cafe in ("씨씨앙", "양평맘"):
        cafe_jobs = [
            job
            for job in jobs
            if is_affiliate_cafe(job.cafe)
            and job.cafe == cafe
            and job.daily_post is None
        ]
        if not cafe_jobs:
            continue
        candidates = [post for post in daily_posts if post.cafe == cafe]
        if len(candidates) < len(cafe_jobs):
            raise DailyPostSheetError(
                f"{cafe} 일상 글이 부족합니다: 필요 {len(cafe_jobs)}개, "
                f"사용 가능 {len(candidates)}개"
            )
        chosen = randomizer.sample(candidates, len(cafe_jobs))
        for job, post in zip(cafe_jobs, chosen):
            job.daily_post = post


def _article_row(
    *,
    type_name: str,
    title: str,
    body: str,
    job: GatlingBrandJob,
    board_name: str,
    link: str = "",
) -> MasterRow:
    return MasterRow(
        link=link or None,
        type=type_name,
        title=title,
        body=body,
        hashtag=job.keyword,
        prefix=job.prefix,
        board_name=board_name,
        comment_policy=COMMENT_ALLOWED,
    )


def _reply_rows(
    node: CommentNode,
    *,
    keyword: str,
    article_url: str,
) -> list[MasterRow]:
    rows: list[MasterRow] = []
    for child in node.children:
        rows.append(
            MasterRow(
                link=reply_target_value(child),
                type=TYPE_REPLY,
                body=child.text,
                hashtag=keyword,
                result_link=article_url,
            )
        )
        rows.extend(
            _reply_rows(child, keyword=keyword, article_url=article_url)
        )
    return rows


def build_master_rows(
    job: GatlingBrandJob,
    extra_exact_names: list[str] | tuple[str, ...] = (),
) -> list[MasterRow]:
    board_name = exact_board_name(
        job.board,
        job.cafe,
        extra_exact_names=extra_exact_names,
    )
    article_url = (job.cafe_article_url or "").strip()
    rows: list[MasterRow] = []

    if is_affiliate_cafe(job.cafe):
        if job.daily_post is None:
            raise GatlingPasteError(
                f"행 {job.row_number} {job.cafe}는 일상 글을 새글에 넣어야 합니다"
            )
        rows.append(
            _article_row(
                type_name=TYPE_NEW_POST,
                title=job.daily_post.title,
                body=job.daily_post.body,
                job=job,
                board_name=board_name,
            )
        )
        rows.append(
            _article_row(
                type_name=TYPE_EDIT_POST,
                title=job.article.title,
                body=job.article.body,
                job=job,
                board_name=board_name,
                link=article_url,
            )
        )
    else:
        rows.append(
            _article_row(
                type_name=TYPE_NEW_POST,
                title=job.article.title,
                body=job.article.body,
                job=job,
                board_name=board_name,
                link=article_url,
            )
        )

    for comment in job.article.comments:
        rows.append(
            MasterRow(
                link=article_url or None,
                type=TYPE_COMMENT,
                body=comment.text,
                hashtag=job.keyword,
            )
        )
        rows.extend(
            _reply_rows(
                comment,
                keyword=job.keyword,
                article_url=article_url,
            )
        )
    return rows


def build_gatling_master(
    brand_path: str | Path,
    daily_path: str | Path | None = None,
    *,
    skip_completed: bool = True,
    rng: random.Random | None = None,
    extra_exact_names: list[str] | tuple[str, ...] = (),
) -> GatlingBuildResult:
    jobs, skipped = load_gatling_brand_jobs(
        brand_path,
        skip_completed=skip_completed,
    )
    affiliate_jobs = [job for job in jobs if is_affiliate_cafe(job.cafe)]
    if affiliate_jobs:
        if not daily_path:
            raise GatlingPasteError(
                "제휴 카페 원고가 있어 일상 글 시트가 필요합니다"
            )
        assign_daily_posts(jobs, load_daily_posts(daily_path), rng=rng)

    rows: list[MasterRow] = []
    for job in jobs:
        rows.extend(build_master_rows(job, extra_exact_names=extra_exact_names))
    return GatlingBuildResult(rows=rows, jobs=jobs, skipped=skipped)


def write_master_xlsx(path: str | Path, rows: list[MasterRow]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = MASTER_SHEET_NAME
    for column, header in enumerate(MASTER_HEADERS, start=1):
        sheet.cell(MASTER_HEADER_ROW, column, header)
    for offset, row in enumerate(rows):
        for column, value in enumerate(row.cells(), start=1):
            if value is None or value == "":
                continue
            sheet.cell(MASTER_HEADER_ROW + 1 + offset, column, value)
    workbook.save(output)
    return output


def build_and_write_master(
    brand_path: str | Path,
    output_path: str | Path,
    daily_path: str | Path | None = None,
    *,
    skip_completed: bool = True,
    rng: random.Random | None = None,
    extra_exact_names: list[str] | tuple[str, ...] = (),
) -> GatlingBuildResult:
    result = build_gatling_master(
        brand_path,
        daily_path,
        skip_completed=skip_completed,
        rng=rng,
        extra_exact_names=extra_exact_names,
    )
    write_master_xlsx(output_path, result.rows)
    return result
