from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import asdict
from pathlib import Path

from openpyxl import load_workbook

from .models import Account, Manuscript


GRAY_RGB = {"FFD9D9D9", "FFB7B7B7", "FFCCCCCC", "FF999999", "FF808080"}
CAFE_NAMES = {
    "고요한아침": "고요한 아침",
    "글로시마이": "글로시 마이",
    "웨딩노트": "웨딩 노트",
    "송도포털": "송도포털",
    "헬씨트리": "헬씨 트리",
    "러브인썸": "러브 인썸 (Love in Some)",
    "마이웨딩드림": "마이 웨딩 드림",
}


def manuscript_hash(title: str, body: str) -> str:
    normalized = "\n".join(
        re.sub(r"\s+", " ", value).strip()
        for value in (title, body)
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_adapted_csv(path: Path, *, source_key: str) -> list[Manuscript]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {
            "카페명",
            "게시판명",
            "각색제목",
            "각색본문",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                "각색 원고 필수 열이 없습니다: " + ", ".join(sorted(missing))
            )
        manuscripts: list[Manuscript] = []
        for row_number, row in enumerate(reader, start=2):
            title = (row.get("각색제목") or "").strip()
            body = (row.get("각색본문") or "").strip()
            cafe_raw = re.sub(r"\s+", "", row.get("카페명") or "")
            board = (row.get("게시판명") or "").strip()
            if not title or not body or not cafe_raw or not board:
                continue
            manuscripts.append(
                Manuscript(
                    title=title,
                    body=body,
                    cafe=CAFE_NAMES.get(cafe_raw, cafe_raw),
                    board=board,
                    source=source_key,
                    source_row=row_number,
                    content_hash=manuscript_hash(title, body),
                )
            )
    return manuscripts


def load_account_workbook(path: Path) -> tuple[list[Account], dict[str, int]]:
    workbook = load_workbook(path, read_only=False, data_only=True)
    if "아이디 리스트" not in workbook.sheetnames:
        raise ValueError("계정 파일에서 '아이디 리스트' 시트를 찾지 못했습니다")
    sheet = workbook["아이디 리스트"]
    accounts: list[Account] = []
    counts = {
        "rows": 0,
        "gray_excluded": 0,
        "self_v2r": 0,
        "affiliate_v2r": 0,
    }
    for row_number in range(2, sheet.max_row + 1):
        login_id = str(sheet.cell(row_number, 2).value or "").strip()
        if not login_id:
            continue
        counts["rows"] += 1
        work_type = str(sheet.cell(row_number, 8).value or "").strip()
        linked = str(sheet.cell(row_number, 9).value or "").strip()
        color = str(sheet.cell(row_number, 2).fill.fgColor.rgb or "").upper()
        gray = color in GRAY_RGB or any(
            color.endswith(candidate[-6:]) for candidate in GRAY_RGB
        )
        if gray:
            counts["gray_excluded"] += 1
        if work_type == "자사 카페" and linked == "V2R" and not gray:
            counts["self_v2r"] += 1
        if work_type == "제휴 작업" and linked == "V2R" and not gray:
            counts["affiliate_v2r"] += 1
        accounts.append(
            Account(
                login_id=login_id,
                work_type=work_type,
                linked=linked,
                excluded=gray,
                shade="회색" if gray else "",
            )
        )
    return accounts, counts


def public_account_payload(accounts: list[Account]) -> list[dict[str, object]]:
    """Serialize account assignment fields only; passwords never enter memory."""
    return [asdict(account) for account in accounts]
