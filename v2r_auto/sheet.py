from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .content import ContentFormatError, parse_article
from .models import AffiliateJob, JobStatus, PostJob


class SheetSchemaError(ValueError):
    pass


ALIASES = {
    "keyword": {"키워드", "검색어", "keyword"},
    "title": {"제목", "글제목", "title", "subject"},
    "body": {"본문", "내용", "글내용", "body", "content"},
    "cafe": {"카페", "카페명", "cafe"},
    "board": {"게시판", "게시판명", "메뉴", "board"},
    "account": {"계정", "아이디", "작성계정", "account", "id"},
    "publish_at": {"예약시간", "발행시간", "예약일시", "publishat", "scheduledat"},
    "tags": {"태그", "해시태그", "tags"},
    "status": {"상태", "발행상태", "완료", "status"},
}


def _normalize(value: str) -> str:
    return re.sub(r"[\s_\-()]+", "", value or "").strip().lower()


def _clean_cell(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


NORMALIZED_ALIASES = {
    key: {_normalize(alias) for alias in aliases} for key, aliases in ALIASES.items()
}

AFFILIATE_COLUMNS = {
    "keyword": "키워드",
    "body": "본문",
    "cafe": "카페명",
    "account": "작성계정",
    "article_type": "원고유형",
    "completion_url": "완료 링크",
}
AFFILIATE_PREFIX_COLUMN = "말머리"
AFFILIATE_ACCOUNT_TYPE_COLUMN = "계정유형"
AFFILIATE_IMAGE_DISABLED_COLUMN = "이미지 없음"
AFFILIATE_REVISION_BOARD_COLUMN = "게시판명"


@dataclass(slots=True)
class SheetDefaults:
    cafe: str = ""
    board: str = ""
    account: str = ""


def _field_map(headers: list[str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for header in headers:
        normalized = _normalize(header)
        for canonical, aliases in NORMALIZED_ALIASES.items():
            if normalized in aliases and canonical not in mapped:
                mapped[canonical] = header
                break
    return mapped


def _split_tags(value: str) -> list[str]:
    parts = re.split(r"[,#\n]+", value or "")
    return [part.strip() for part in parts if part.strip()][:10]


def _comment_headers(headers: list[str]) -> list[str]:
    return [
        header
        for header in headers
        if re.fullmatch(r"(댓글|comment)\s*\d*", header.strip(), flags=re.IGNORECASE)
    ]


def _is_completed(value: str) -> bool:
    return _normalize(value) in {
        "완료",
        "발행완료",
        "성공",
        "done",
        "success",
        "true",
        "y",
    }


def load_jobs(
    path: str | Path,
    defaults: SheetDefaults | None = None,
    selected_row_number: int | None = None,
) -> list[PostJob]:
    """Load all post jobs, or only one Google Sheet row when requested.

    ``selected_row_number`` uses the visible spreadsheet number: the header is row 1,
    so the first original post is row 2.
    """
    defaults = defaults or SheetDefaults()
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"시트 파일이 없습니다: {csv_path}")
    if selected_row_number is not None and selected_row_number < 2:
        raise ValueError("시트 행 번호는 2 이상이어야 합니다")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = [header for header in (reader.fieldnames or []) if header and header.strip()]
        if not headers:
            raise SheetSchemaError("첫 행에서 열 이름을 찾지 못했습니다")

        mapping = _field_map(headers)
        missing = [name for name in ("title", "body") if name not in mapping]
        if missing:
            raise SheetSchemaError(
                "필수 열을 찾지 못했습니다: "
                + ", ".join("제목" if name == "title" else "본문" for name in missing)
            )

        comment_headers = _comment_headers(headers)
        jobs: list[PostJob] = []
        selected_row_found = False
        for row_number, row in enumerate(reader, start=2):
            if selected_row_number is not None and row_number != selected_row_number:
                continue
            if selected_row_number is not None:
                selected_row_found = True
            title = _clean_cell(row.get(mapping["title"]))
            body = _clean_cell(row.get(mapping["body"]))
            if not title and not body:
                continue

            status_value = _clean_cell(row.get(mapping.get("status", ""), ""))
            job = PostJob(
                row_number=row_number,
                keyword=_clean_cell(row.get(mapping.get("keyword", ""), "")),
                title=title,
                body=body,
                cafe=_clean_cell(row.get(mapping.get("cafe", ""), "") or defaults.cafe),
                board=_clean_cell(row.get(mapping.get("board", ""), "") or defaults.board),
                account=_clean_cell(row.get(mapping.get("account", ""), "") or defaults.account),
                publish_at=_clean_cell(row.get(mapping.get("publish_at", ""), "")),
                tags=_split_tags(row.get(mapping.get("tags", ""), "") or ""),
                comments=[
                    _clean_cell(row.get(header))
                    for header in comment_headers
                    if _clean_cell(row.get(header))
                ],
            )
            if _is_completed(status_value):
                job.status = JobStatus.SKIPPED
                job.message = "시트에서 이미 완료로 표시됨"
            jobs.append(job)

    if selected_row_number is not None and not selected_row_found:
        raise SheetSchemaError(f"입력한 시트 행 {selected_row_number}을 찾지 못했습니다")
    if not jobs:
        if selected_row_number is not None:
            raise SheetSchemaError(
                f"시트 행 {selected_row_number}에 처리할 원고가 없습니다"
            )
        raise SheetSchemaError("처리할 글이 없습니다")
    return jobs


def load_affiliate_jobs(
    path: str | Path,
    selected_row_number: int | None = None,
) -> list[AffiliateJob]:
    """Load all complete compact affiliate-cafe tasks from columns A–F."""
    if selected_row_number is not None and selected_row_number < 2:
        raise ValueError("시트 행 번호는 2 이상이어야 합니다")

    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"시트 파일이 없습니다: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = [header for header in (reader.fieldnames or []) if header and header.strip()]
        missing = [header for header in AFFILIATE_COLUMNS.values() if header not in headers]
        if missing:
            raise SheetSchemaError(
                "제휴용 시트 열을 찾지 못했습니다: " + ", ".join(missing)
            )

        jobs: list[AffiliateJob] = []
        selected_row_found = False
        for row_number, row in enumerate(reader, start=2):
            if selected_row_number is not None and row_number != selected_row_number:
                continue
            if selected_row_number is not None:
                selected_row_found = True

            keyword = _clean_cell(row.get(AFFILIATE_COLUMNS["keyword"]))
            source = _clean_cell(row.get(AFFILIATE_COLUMNS["body"]))
            cafe = _clean_cell(row.get(AFFILIATE_COLUMNS["cafe"]))
            account = _clean_cell(row.get(AFFILIATE_COLUMNS["account"]))
            article_type = _clean_cell(row.get(AFFILIATE_COLUMNS["article_type"]))
            prefix = _clean_cell(row.get(AFFILIATE_PREFIX_COLUMN))
            if not keyword and cafe == "씨씨앙":
                keyword = prefix
            missing_required = [
                label
                for label, value in (
                    ("키워드", keyword),
                    ("본문", source),
                    ("카페명", cafe),
                    ("원고유형", article_type),
                )
                if not value
            ]
            if missing_required:
                continue
            try:
                article = parse_article(keyword, source)
            except ContentFormatError as exc:
                raise SheetSchemaError(
                    f"시트 행 {row_number} 원고 형식 오류: {exc}"
                ) from exc

            job = AffiliateJob(
                row_number=row_number,
                keyword=keyword,
                article=article,
                cafe=cafe,
                account=account,
                article_type=article_type,
                prefix=prefix,
                revision_board=_clean_cell(
                    row.get(AFFILIATE_REVISION_BOARD_COLUMN)
                ),
                account_type=_clean_cell(row.get(AFFILIATE_ACCOUNT_TYPE_COLUMN)),
                image_disabled=(
                    _clean_cell(row.get(AFFILIATE_IMAGE_DISABLED_COLUMN)).casefold()
                    == "y"
                ),
                completion_url=_clean_cell(row.get(AFFILIATE_COLUMNS["completion_url"])),
            )
            if job.completion_url and not re.match(
                r"^https?://",
                job.completion_url,
                flags=re.IGNORECASE,
            ):
                job.completion_url = ""
            if not job.account and job.account_type not in {"실명", "비실명"}:
                job.status = JobStatus.SKIPPED
                job.message = "D열 작성계정과 H열 계정유형이 모두 비어 있음"
            if job.completion_url:
                job.status = JobStatus.SKIPPED
                job.message = "F열에 완료 링크가 있어 건너뜀"
            jobs.append(job)

    if selected_row_number is not None and not selected_row_found:
        raise SheetSchemaError(f"입력한 시트 행 {selected_row_number}을 찾지 못했습니다")
    if not jobs:
        raise SheetSchemaError("A~E열이 모두 채워진 처리 대상 원고가 없습니다")
    return jobs
