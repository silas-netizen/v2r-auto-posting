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
JOIN_PASTE_CHUNK_SIZE = 400
JOIN_SPLIT_CHUNK_SIZE = 50
JOIN_MISMATCH_LIMIT = 8
JOIN_CELL_FILL_ROUNDS = 3
JOIN_CELL_FILL_MAX = 80
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


def sheet_row_number(row: dict[str, str]) -> int | None:
    raw = clean_cell(row.get("__row"))
    if not raw.isdigit():
        return None
    return int(raw)


def paste_chunk_end_row(start_row: int, tsv: str) -> int:
    """Inclusive sheet row covered by a trailing-newline TSV block."""
    rows = tsv.count("\n")
    if rows < 1:
        return start_row
    return start_row + rows - 1


def should_split_failed_chunk(tsv: str, min_rows: int = JOIN_SPLIT_CHUNK_SIZE) -> bool:
    return tsv.count("\n") > min_rows


@dataclass(frozen=True, slots=True)
class JoinCellWrite:
    row_number: int
    column: str
    header: str
    value: str


@dataclass(slots=True)
class JoinMarkPlan:
    headers: list[str]
    id_header: str
    cafe_headers: dict[str, str]
    rows: list[dict[str, str]] = field(default_factory=list)
    formula_cells: tuple[str, ...] = ()

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
        chunk_size: int = JOIN_PASTE_CHUNK_SIZE,
        *,
        start_row: int = 2,
        end_row: int | None = None,
    ) -> list[tuple[int, str]]:
        """Split a column paste into start-row + TSV chunks.

        `start_row` / `end_row` are 1-based sheet rows, inclusive.
        Data rows begin at sheet row 2.
        """
        if chunk_size < 1:
            raise JoinMarkerError("붙여넣기 묶음 크기가 올바르지 않습니다")
        first_index = max(0, start_row - 2)
        slice_end = len(self.rows) if end_row is None else min(len(self.rows), max(0, end_row - 1))
        selected = self.rows[first_index:slice_end]
        chunks: list[tuple[int, str]] = []
        for offset in range(0, len(selected), chunk_size):
            piece = selected[offset : offset + chunk_size]
            chunks.append((2 + first_index + offset, self.tsv_for_headers(headers, piece)))
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
        if self.formula_cells:
            lines.append(
                "주의: 씨씨앙·양평맘 열에 수식이 있어 붙여넣기가 지워질 수 있습니다"
            )
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
    formula_cells = tuple(collect_cafe_formulas(rows, cafe_headers))
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
        formula_cells=formula_cells,
    )


def collect_cafe_formulas(
    rows: list[dict[str, str]],
    cafe_headers: dict[str, str],
) -> list[str]:
    """Describe cafe cells that start with `=`, which Sheets will recalc over a paste."""
    hits: list[str] = []
    for row in rows:
        row_number = row.get("__row", "?")
        for header in cafe_headers.values():
            value = clean_cell(row.get(header))
            if value.startswith("="):
                preview = value if len(value) <= 40 else value[:37] + "..."
                hits.append(f"{row_number}행 {header}: {preview}")
    return hits


def require_membership(membership: dict[str, set[str]]) -> None:
    """Refuse to blank the sheet when V2R returned no cafe members at all."""
    joined = sum(len(membership.get(label, set()) or set()) for label in CAFE_LABELS)
    if joined == 0:
        raise JoinMarkerError(
            "V2R에서 씨씨앙·양평맘 가입 아이디를 하나도 읽지 못했습니다. "
            "로그인 준비에서 V2R에 로그인한 뒤 다시 실행하세요. "
            "시트의 가입 표시는 바꾸지 않았습니다."
        )


def format_cafe_formula_error(formula_cells: Iterable[str]) -> str:
    samples = ", ".join(list(formula_cells)[:5])
    return (
        "씨씨앙·양평맘 열에 수식(=로 시작)이 있어 붙여넣기를 하지 않았습니다. "
        "수식을 지우고 일반 글자만 남긴 뒤 다시 실행하세요. "
        f"예: {samples}"
    )


def mismatches_look_like_missed_paste(errors: list[str]) -> bool:
    if not errors:
        return False
    wanted_mark = False
    for error in errors:
        if "시트 열 이름" in error or "행 수" in error:
            return False
        if "실제 ''" not in error and '실제 ""' not in error:
            return False
        if f"기대 '{MARK_VALUE}'" in error:
            wanted_mark = True
    return wanted_mark


def format_join_write_failure(
    errors: list[str],
    *,
    start_cell: str = "",
    start_row: int | None = None,
    end_row: int | None = None,
) -> str:
    lines = ["시트에 가입 표시가 반영되지 않았습니다."]
    if start_cell and start_row is not None and end_row is not None:
        lines.append(f"문제 구간: {start_cell}부터 {end_row}행까지.")
    elif start_row is not None and end_row is not None:
        lines.append(f"문제 구간: {start_row}행부터 {end_row}행까지.")
    if errors:
        lines.append("확인된 칸: " + "; ".join(errors[:5]))
    lines.append("")
    lines.append("이렇게 해 주세요:")
    lines.append("- 시트 필터(깔때기)가 켜져 있으면 끄고 다시 실행하세요.")
    lines.append("- 씨씨앙·양평맘 칸이 수정 불가(보호)이면 보호를 해제한 뒤 다시 실행하세요.")
    lines.append("- Chrome 창을 닫지 말고, 실행 중에 시트 칸을 클릭하지 마세요.")
    lines.append("- 로그인 준비에서 Google 시트 편집 권한을 확인하세요.")
    if mismatches_look_like_missed_paste(errors):
        lines.append(
            "- 붙여넣기가 표가 아니라 위쪽 이름 상자로 들어간 경우가 많습니다. "
            "프로그램을 한 번 더 실행하면 됩니다."
        )
    return "\n".join(lines)


def user_facing_join_error(exc: BaseException) -> str:
    """Turn browser/API exceptions into short Korean instructions."""
    if isinstance(exc, JoinMarkerError):
        return str(exc).strip()
    text = str(exc).strip()
    lowered = f"{type(exc).__name__}: {text}".casefold()
    session_dead = (
        "invalid session",
        "no such window",
        "not connected to devtools",
        "chrome not reachable",
        "disconnected",
        "target window already closed",
        "web view not found",
    )
    if any(token in lowered for token in session_dead):
        return (
            "Chrome 창이 닫혔거나 연결이 끊겼습니다. "
            "프로그램을 다시 실행하고 로그인 준비 후 표시하기를 누르세요."
        )
    if "timeout" in lowered or "timed out" in lowered:
        return "페이지가 너무 오래 걸렸습니다. 인터넷 상태를 확인하고 다시 실행하세요."
    if "accounts.google.com" in lowered or "google 로그인" in lowered:
        return (
            "Google 로그인이 필요합니다. "
            "로그인 준비에서 Google에 로그인한 뒤 다시 실행하세요."
        )
    if "클립보드" in text:
        return "클립보드에 값을 넣지 못했습니다. 다른 프로그램의 클립보드 사용을 끄고 다시 실행하세요."
    if text:
        return text
    return "알 수 없는 오류가 났습니다. 진행 기록을 확인하세요."


def plan_mismatch_cells(
    headers: list[str],
    rows: list[dict[str, str]],
    plan: JoinMarkPlan,
    *,
    start_row: int | None = None,
    end_row: int | None = None,
) -> list[JoinCellWrite]:
    """Every cafe cell that still does not match the plan."""
    cells: list[JoinCellWrite] = []
    if headers != plan.headers:
        return cells
    for expected, actual in zip(plan.rows, rows, strict=False):
        row_number = sheet_row_number(expected)
        if row_number is None:
            continue
        if start_row is not None and row_number < start_row:
            continue
        if end_row is not None and row_number > end_row:
            continue
        for header in plan.cafe_headers.values():
            wanted = clean_cell(expected.get(header))
            if wanted != clean_cell(actual.get(header)):
                cells.append(
                    JoinCellWrite(
                        row_number=row_number,
                        column=column_letter(plan.column_index(header)),
                        header=header,
                        value=wanted,
                    )
                )
    return cells


def format_locked_sheet_error(errors: list[str]) -> str:
    samples = "; ".join(errors[:5]) if errors else ""
    return (
        "시트가 잠겨 있거나 수정 권한이 없어 글을 남길 수 없습니다. "
        "필터를 끄고, 씨씨앙·양평맘 칸 보호를 해제한 뒤 다시 실행하세요."
        + (f" 확인된 칸: {samples}" if samples else "")
    )


def plan_matches_sheet(
    headers: list[str],
    rows: list[dict[str, str]],
    plan: JoinMarkPlan,
    *,
    start_row: int | None = None,
    end_row: int | None = None,
    limit: int = JOIN_MISMATCH_LIMIT,
) -> list[str]:
    """Return human-readable mismatches after a write.

    When `start_row` / `end_row` are set, only those 1-based sheet rows are
    compared. This lets a 400-row paste be checked before the next paste.
    """
    errors: list[str] = []
    if headers != plan.headers:
        errors.append("시트 열 이름이 바뀌었습니다. 다시 실행하세요")
        return errors
    if start_row is None and len(rows) < len(plan.rows):
        errors.append("시트 행 수가 계획보다 적습니다")
    for expected, actual in zip(plan.rows, rows, strict=False):
        row_number = sheet_row_number(expected)
        label = expected.get("__row", "?")
        if start_row is not None and (row_number is None or row_number < start_row):
            continue
        if end_row is not None and (row_number is None or row_number > end_row):
            continue
        for header in plan.cafe_headers.values():
            if clean_cell(actual.get(header)) != clean_cell(expected.get(header)):
                errors.append(
                    f"{label}행 {header}: 기대 '{clean_cell(expected.get(header))}' / "
                    f"실제 '{clean_cell(actual.get(header))}'"
                )
                if len(errors) >= limit:
                    return errors
    return errors
