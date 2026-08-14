from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .affiliate_api import CAFE_DESTINATIONS
from .cafe_catalog import _field, _walk_dicts


MARK_VALUE = "가입"
ID_HEADERS = ("ID", "아이디", "계정", "login_id")
CAFE_LABELS = ("씨씨앙", "양평맘")
PROTECTED_HEADERS = ("김천kb보험",)
ACCOUNT_TEST_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1UgcAvHFCpC5N9joC9T5WCATK834F3XAtRrepFv6XbEs/"
    "edit?gid=218285244#gid=218285244"
)


class JoinMarkerError(ValueError):
    pass


def normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-()]+", "", value or "").strip().casefold()


def clean_cell(value: str | None) -> str:
    """Treat BOM-only and whitespace-only cells as empty."""
    return (value or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip()


def column_letter(index: int) -> str:
    """Convert a 0-based column index to A, B, ..., Z, AA."""
    if index < 0:
        raise JoinMarkerError("열 번호가 올바르지 않습니다")
    result = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def extract_joined_ids(payload: Any) -> set[str]:
    """Collect current cafe members. Dropped or stopped accounts are skipped."""
    ids: set[str] = set()
    for item in _walk_dicts(payload):
        account = str(_field(item, "login_id", "naver_login_id") or "").strip()
        if not account:
            continue
        if item.get("force_drop") or item.get("stop_cafe_member"):
            continue
        ids.add(account.casefold())
    return ids


def fetch_membership(request: Callable[..., Any]) -> dict[str, set[str]]:
    membership: dict[str, set[str]] = {}
    for label in CAFE_LABELS:
        cafe_id = CAFE_DESTINATIONS[label]["cafe_id"]
        payload = request(
            "GET",
            "/naver_cafes/naver_join_cafe",
            query={"cafe_id": cafe_id},
        )
        membership[label] = extract_joined_ids(payload)
    return membership


def _find_header(headers: list[str], aliases: Iterable[str]) -> str:
    wanted = {normalize_header(alias) for alias in aliases}
    for header in headers:
        if normalize_header(header) in wanted:
            return header
    options = ", ".join(headers[:20])
    raise JoinMarkerError(
        "시트에서 열을 찾지 못했습니다: "
        + ", ".join(aliases)
        + (f" / 있는 열: {options}" if options else "")
    )


def load_account_rows(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise JoinMarkerError(f"시트 파일이 없습니다: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = [header for header in (reader.fieldnames or []) if header and header.strip()]
        if not headers:
            raise JoinMarkerError("첫 행에서 열 이름을 찾지 못했습니다")
        rows = []
        for index, row in enumerate(reader, start=2):
            rows.append(
                {
                    "__row": str(index),
                    **{header: clean_cell(row.get(header)) for header in headers},
                }
            )
    return headers, rows


@dataclass(slots=True)
class JoinMarkPlan:
    headers: list[str]
    id_header: str
    cafe_headers: dict[str, str]
    rows: list[dict[str, str]] = field(default_factory=list)

    def marked_count(self, label: str) -> int:
        header = self.cafe_headers[label]
        return sum(1 for row in self.rows if (row.get(header) or "") == MARK_VALUE)

    def both_count(self) -> int:
        headers = [self.cafe_headers[label] for label in CAFE_LABELS if label in self.cafe_headers]
        if len(headers) < 2:
            return 0
        return sum(
            1
            for row in self.rows
            if all((row.get(header) or "") == MARK_VALUE for header in headers)
        )

    def id_count(self) -> int:
        return sum(1 for row in self.rows if (row.get(self.id_header) or "").strip())

    def column_index(self, header: str) -> int:
        return self.headers.index(header)

    def start_cell(self, header: str) -> tuple[str, int]:
        return column_letter(self.column_index(header)), 2

    def column_values(self, header: str) -> list[str]:
        return [(row.get(header) or "") for row in self.rows]

    def tsv_for_headers(self, headers: list[str], rows: list[dict[str, str]] | None = None) -> str:
        lines = []
        for row in rows if rows is not None else self.rows:
            lines.append("\t".join((row.get(header) or "") for header in headers))
        return "\n".join(lines) + ("\n" if lines else "")

    def paste_chunks(
        self,
        headers: list[str],
        chunk_size: int = 400,
    ) -> list[tuple[int, str]]:
        """Split a column paste into start-row + TSV chunks."""
        if chunk_size < 1:
            raise JoinMarkerError("붙여넣기 묶음 크기가 올바르지 않습니다")
        chunks: list[tuple[int, str]] = []
        for start in range(0, len(self.rows), chunk_size):
            piece = self.rows[start : start + chunk_size]
            chunks.append((2 + start, self.tsv_for_headers(headers, piece)))
        return chunks

    def contiguous_cafe_groups(self) -> list[list[str]]:
        ordered = sorted(
            self.cafe_headers.values(),
            key=lambda header: self.column_index(header),
        )
        if not ordered:
            return []
        groups: list[list[str]] = [[ordered[0]]]
        for header in ordered[1:]:
            previous = groups[-1][-1]
            if self.column_index(header) == self.column_index(previous) + 1:
                groups[-1].append(header)
            else:
                groups.append([header])
        return groups

    def summary(self) -> str:
        lines = [
            f"시트 행 {len(self.rows)}개 · 아이디 {self.id_count()}개",
        ]
        for label in CAFE_LABELS:
            if label in self.cafe_headers:
                lines.append(f"{label} 가입 표시 {self.marked_count(label)}칸")
        if len(self.cafe_headers) >= 2:
            lines.append(f"두 카페 모두 가입 {self.both_count()}칸")
        lines.append("아이디가 없는 행은 비웁니다. 김천kb보험 열은 건드리지 않습니다.")
        return "\n".join(lines)


def build_plan(
    headers: list[str],
    rows: list[dict[str, str]],
    membership: dict[str, set[str]],
) -> JoinMarkPlan:
    if not headers:
        raise JoinMarkerError("시트 열 이름이 없습니다")

    id_header = _find_header(headers, ID_HEADERS)
    cafe_headers: dict[str, str] = {}
    for label in CAFE_LABELS:
        cafe_headers[label] = _find_header(headers, (label,))

    planned: list[dict[str, str]] = []
    for row in rows:
        account = clean_cell(row.get(id_header))
        key = account.casefold()
        updated = dict(row)
        for label, header in cafe_headers.items():
            joined = bool(account) and key in membership.get(label, set())
            updated[header] = MARK_VALUE if joined else ""
        planned.append(updated)
    return JoinMarkPlan(
        headers=headers,
        id_header=id_header,
        cafe_headers=cafe_headers,
        rows=planned,
    )


def plan_matches_sheet(
    headers: list[str],
    rows: list[dict[str, str]],
    plan: JoinMarkPlan,
) -> list[str]:
    """Return human-readable mismatches after a write."""
    errors: list[str] = []
    if headers != plan.headers:
        errors.append("시트 열 이름이 바뀌었습니다. 다시 실행하세요")
        return errors
    if len(rows) < len(plan.rows):
        errors.append("시트 행 수가 계획보다 적습니다")
    for expected, actual in zip(plan.rows, rows, strict=False):
        row_number = expected.get("__row", "?")
        for header in plan.cafe_headers.values():
            if clean_cell(actual.get(header)) != clean_cell(expected.get(header)):
                errors.append(
                    f"{row_number}행 {header}: 기대 '{clean_cell(expected.get(header))}' / "
                    f"실제 '{clean_cell(actual.get(header))}'"
                )
                if len(errors) >= 8:
                    return errors
    return errors
