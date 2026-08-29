from __future__ import annotations

import re
from datetime import datetime


_TIME_RE = re.compile(
    r"(?P<year>\d{4})\D+(?P<month>\d{1,2})\D+(?P<day>\d{1,2})"
    r"(?:\D+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?"
)


def csv_sheet_cell(
    rows: list[list[str]], row_number: int, column_index: int
) -> str:
    if row_number < 1 or row_number > len(rows):
        return ""
    row = rows[row_number - 1]
    if column_index < 0 or column_index >= len(row):
        return ""
    return row[column_index]


def sheet_cell_values_match(actual: str, expected: str) -> bool:
    left = (actual or "").strip()
    right = (expected or "").strip()
    if left == right:
        return True
    if _numbers_match(left, right):
        return True
    return _datetimes_match(left, right)


def parse_sheet_number(value: str) -> float | None:
    text = (value or "").strip().replace(",", "").replace(" ", "")
    if not text or text in {"-", "—", "–"}:
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
    text = (value or "").strip()
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
    return datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        hour,
        int(match.group("minute") or 0),
        int(match.group("second") or 0),
    )


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
