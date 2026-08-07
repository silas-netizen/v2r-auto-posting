from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .models import JobStatus, PostJob


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


NORMALIZED_ALIASES = {
    key: {_normalize(alias) for alias in aliases} for key, aliases in ALIASES.items()
}


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


def load_jobs(path: str | Path, defaults: SheetDefaults | None = None) -> list[PostJob]:
    defaults = defaults or SheetDefaults()
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"시트 파일이 없습니다: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = [header.strip() for header in (reader.fieldnames or []) if header]
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
        for row_number, row in enumerate(reader, start=2):
            title = (row.get(mapping["title"]) or "").strip()
            body = (row.get(mapping["body"]) or "").strip()
            if not title and not body:
                continue

            status_value = (row.get(mapping.get("status", ""), "") or "").strip()
            job = PostJob(
                row_number=row_number,
                keyword=(row.get(mapping.get("keyword", ""), "") or "").strip(),
                title=title,
                body=body,
                cafe=(row.get(mapping.get("cafe", ""), "") or defaults.cafe).strip(),
                board=(row.get(mapping.get("board", ""), "") or defaults.board).strip(),
                account=(row.get(mapping.get("account", ""), "") or defaults.account).strip(),
                publish_at=(row.get(mapping.get("publish_at", ""), "") or "").strip(),
                tags=_split_tags(row.get(mapping.get("tags", ""), "") or ""),
                comments=[
                    (row.get(header) or "").strip()
                    for header in comment_headers
                    if (row.get(header) or "").strip()
                ],
            )
            if _is_completed(status_value):
                job.status = JobStatus.SKIPPED
                job.message = "시트에서 이미 완료로 표시됨"
            jobs.append(job)

    if not jobs:
        raise SheetSchemaError("처리할 글이 없습니다")
    return jobs
