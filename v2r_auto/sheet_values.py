from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import datetime
from urllib.parse import parse_qs, parse_qsl, urlparse


_TIME_RE = re.compile(
    r"(?P<year>\d{4})\D+(?P<month>\d{1,2})\D+(?P<day>\d{1,2})"
    r"(?:\D+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?"
)
_HYPERLINK_RE = re.compile(r'(?is)^=\s*HYPERLINK\s*\(\s*"([^"]+)"')
_FULLWIDTH = str.maketrans("０１２３４５６７８９，．　", "0123456789,. ")
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060\xad"), None)
_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


def csv_sheet_cell(
    rows: list[list[str]], row_number: int, column_index: int
) -> str:
    if row_number < 1 or row_number > len(rows):
        return ""
    row = rows[row_number - 1]
    if column_index < 0 or column_index >= len(row):
        return ""
    return row[column_index]


def column_index_from_letter(column: str) -> int:
    number = 0
    for letter in (column or "").upper():
        if letter < "A" or letter > "Z":
            raise ValueError(f"올바르지 않은 시트 열입니다: {column}")
        number = number * 26 + (ord(letter) - ord("A") + 1)
    return number - 1


def parse_sheet_table(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text or "")))


def looks_like_html(text: str) -> bool:
    head = (text or "").lstrip()[:200].casefold()
    return (
        head.startswith("<!doctype")
        or head.startswith("<html")
        or "<html" in head[:80]
    )


def spreadsheet_id_from_url(sheet_url: str) -> str:
    match = _SHEET_ID_RE.search(sheet_url or "")
    if not match:
        raise ValueError("not a google sheet url")
    return match.group(1)


def sheet_gid_from_url(sheet_url: str) -> str:
    parsed = urlparse(sheet_url or "")
    gid = (parse_qs(parsed.query).get("gid") or ["0"])[0]
    fragment = parsed.fragment or ""
    if fragment.startswith("gid="):
        gid = fragment[4:].split("&", 1)[0]
    return gid or "0"


def sheet_csv_export_url(sheet_url: str) -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id_from_url(sheet_url)}"
        f"/export?format=csv&gid={sheet_gid_from_url(sheet_url)}"
    )


def apply_sheet_overlay(
    table: list[list[str]],
    written: dict[tuple[int, int], str],
) -> list[list[str]]:
    for (row_number, column_index), value in written.items():
        if row_number < 1 or column_index < 0:
            continue
        while len(table) < row_number:
            table.append([])
        row = table[row_number - 1]
        while len(row) <= column_index:
            row.append("")
        row[column_index] = value
    return table


def sheet_plain_text(value: str) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = text.translate(_FULLWIDTH)
    text = text.translate(_INVISIBLE)
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip()
    if text.startswith("'"):
        text = text[1:]
    text = _unwrap_formula(text).strip()
    return text


NEARBY_SHEET_ROW_SPAN = 5


def sheet_keywords_match(actual: str, expected: str) -> bool:
    left = _compact_text(sheet_plain_text(actual)).casefold()
    right = _compact_text(sheet_plain_text(expected)).casefold()
    return bool(left) and left == right


def nearby_sheet_row_numbers(remembered: int, span: int = NEARBY_SHEET_ROW_SPAN) -> list[int]:
    """Check the remembered row first, then one up/down, then farther.

    A deleted sheet row shifts later keywords up. The observed miss was
    exactly one row; scan a few neighbours and refuse anything farther.
    """
    seen: list[int] = []
    remembered = int(remembered)
    span = max(0, int(span))
    for delta in range(0, span + 1):
        candidates = (remembered,) if delta == 0 else (remembered - delta, remembered + delta)
        for number in candidates:
            if number < 2 or number in seen:
                continue
            seen.append(number)
    return seen


def pick_nearby_keyword_row(
    reads: dict[int, str | None],
    expected: str,
    remembered: int,
    span: int = NEARBY_SHEET_ROW_SPAN,
) -> int:
    for number in nearby_sheet_row_numbers(remembered, span=span):
        if number not in reads:
            continue
        text = reads[number]
        if text is None:
            continue
        if sheet_keywords_match(text, expected):
            return number
    raise ValueError(
        f"시트에서 키워드를 화면으로 확인하지 못했습니다: {expected} "
        f"(기억한 행 {remembered})"
    )


def sheet_cell_values_match(actual: str, expected: str) -> bool:
    left = sheet_plain_text(actual)
    right = sheet_plain_text(expected)
    if left == right:
        return True
    if not left and not right:
        return True
    if _compact_text(left) and _compact_text(left) == _compact_text(right):
        return True
    if _numbers_match(left, right):
        return True
    if _datetimes_match(actual, expected):
        return True
    return _urls_match(actual, expected)


def sheet_write_confirmed(
    actual: str,
    expected: str,
    *,
    ui_value: str | None = None,
) -> bool:
    """A write is done only when the cell itself shows the new value.

    Public CSV can lag. That is allowed only after the open sheet cell
    already matches. Two different timestamps are not a success.
    """
    if ui_value is not None and sheet_cell_values_match(ui_value, expected):
        return True
    return sheet_cell_values_match(actual, expected)


def parse_sheet_number(value: str) -> float | None:
    text = sheet_plain_text(value).replace(",", "").replace(" ", "")
    if not text or text in {"-", "—", "–"}:
        return None
    if any(mark in text for mark in (":", "/", "-")) and not re.fullmatch(
        r"-?\d+(\.\d+)?", text
    ):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_sheet_int(value: str) -> int:
    number = parse_sheet_number(value)
    if number is None:
        return 0
    return int(number)


def parse_sheet_datetime(value: str) -> datetime | None:
    text = sheet_plain_text(value)
    if not text:
        return None
    afternoon = "오후" in text
    morning = "오전" in text
    cleaned = text.replace("오후", " ").replace("오전", " ")
    match = _TIME_RE.search(cleaned)
    if not match:
        return None
    hour = int(match.group("hour") or 0)
    if afternoon and hour < 12:
        hour += 12
    if morning and hour == 12:
        hour = 0
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            hour,
            int(match.group("minute") or 0),
            int(match.group("second") or 0),
        )
    except ValueError:
        return None


def _unwrap_formula(text: str) -> str:
    match = _HYPERLINK_RE.match(text)
    if match:
        return match.group(1).strip()
    if text.startswith("="):
        inner = text[1:].strip()
        if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in {"\"", "'"}:
            return inner[1:-1]
    return text


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _numbers_match(actual: str, expected: str) -> bool:
    left = parse_sheet_number(actual)
    right = parse_sheet_number(expected)
    if left is None or right is None:
        return False
    return left == right


def _datetimes_match(actual: str, expected: str) -> bool:
    left = parse_sheet_datetime(actual)
    right = parse_sheet_datetime(expected)
    if left is None or right is None:
        return False
    return left == right


def _urls_match(actual: str, expected: str) -> bool:
    left = _url_key(actual)
    right = _url_key(expected)
    return left is not None and right is not None and left == right


def _url_key(value: str) -> tuple | None:
    text = sheet_plain_text(value)
    if not re.match(r"https?://", text, re.I):
        return None
    parsed = urlparse(text)
    query = tuple(
        sorted(
            (unicodedata.normalize("NFC", key), unicodedata.normalize("NFC", val))
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        )
    )
    return (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query)
