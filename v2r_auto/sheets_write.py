from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SHEETS_ORIGIN = "https://docs.google.com"
SHEETS_API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets"


def spreadsheet_id_from_url(sheet_url: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_url)
    if not match:
        raise ValueError("올바른 Google Sheets 주소가 아닙니다")
    return match.group(1)


def sheet_gid_from_url(sheet_url: str) -> int:
    match = re.search(r"[?&#]gid=(\d+)", sheet_url)
    if not match:
        return 0
    return int(match.group(1))


def batch_update_url(spreadsheet_id: str) -> str:
    return f"{SHEETS_API_ROOT}/{spreadsheet_id}:batchUpdate"


def sapisid_authorization(
    sapisid: str,
    origin: str = SHEETS_ORIGIN,
    timestamp: int | None = None,
) -> str:
    """Browser-cookie auth header Google APIs accept instead of OAuth."""
    import time

    if not sapisid:
        raise ValueError("Google 로그인 쿠키가 없습니다")
    when = int(time.time() if timestamp is None else timestamp)
    digest = hashlib.sha1(f"{when} {sapisid} {origin}".encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {when}_{digest}"


def cookie_header_and_sapisid(cookies: Iterable[dict[str, Any]]) -> tuple[str, str]:
    parts: list[str] = []
    sapisid = ""
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name:
            continue
        parts.append(f"{name}={value}")
        if name in {"SAPISID", "__Secure-1PAPISID"} and value:
            sapisid = value
    return "; ".join(parts), sapisid


def extract_bearer_tokens(performance_entries: Iterable[dict[str, Any]]) -> list[str]:
    """Collect Google API bearer tokens from Chrome performance logs."""
    tokens: list[str] = []
    seen: set[str] = set()
    for entry in performance_entries:
        try:
            message = entry["message"]
            if isinstance(message, str):
                message = json.loads(message)
            payload = message.get("message", message)
            request = payload.get("params", {}).get("request", {})
            url = str(request.get("url") or "")
            if not any(
                host in url
                for host in (
                    "googleapis.com",
                    "docs.google.com",
                    "clients6.google.com",
                )
            ):
                continue
            headers = request.get("headers") or {}
            auth = ""
            for key, value in headers.items():
                if str(key).casefold() == "authorization":
                    auth = str(value)
                    break
            if not auth.casefold().startswith("bearer "):
                continue
            token = auth.split(None, 1)[1].strip()
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
        except (KeyError, TypeError, json.JSONDecodeError, IndexError):
            continue
    return tokens


def csv_cell_value(rows: list[list[str]], row_number: int, column_index: int) -> str:
    """CSV drops trailing empty cells. A missing column means the cell is empty."""
    if row_number < 1 or len(rows) < row_number:
        return ""
    row = rows[row_number - 1]
    if column_index < 0 or column_index >= len(row):
        return ""
    return row[column_index]


def build_comment_watch_batch_update(plan, sheet_numeric_id: int) -> dict[str, Any]:
    """Replace only the K column, including empty cells that must be cleared."""
    start_column = plan.column_index(plan.mark_header)
    rows = [
        {
            "values": [
                {
                    "userEnteredValue": {
                        "stringValue": row.get(plan.mark_header) or ""
                    }
                }
            ]
        }
        for row in plan.rows
    ]
    return {
        "requests": [
            {
                "updateCells": {
                    "range": {
                        "sheetId": sheet_numeric_id,
                        "startRowIndex": 1,
                        "endRowIndex": 1 + len(plan.rows),
                        "startColumnIndex": start_column,
                        "endColumnIndex": start_column + 1,
                    },
                    "rows": rows,
                    "fields": "userEnteredValue",
                }
            }
        ]
    }


def post_sheets_batch_update(
    spreadsheet_id: str,
    payload: dict[str, Any],
    *,
    bearer: str = "",
    cookie_header: str = "",
    sapisid: str = "",
    opener=urlopen,
    timeout: int = 45,
) -> dict[str, Any]:
    """Write cells through the official Sheets API."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Origin": SHEETS_ORIGIN,
        "Referer": f"{SHEETS_ORIGIN}/",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif sapisid:
        headers["Authorization"] = sapisid_authorization(sapisid)
        if cookie_header:
            headers["Cookie"] = cookie_header
        headers["X-Origin"] = SHEETS_ORIGIN
    else:
        raise ValueError("Google 시트 저장 권한이 없습니다")
    request = Request(
        batch_update_url(spreadsheet_id),
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"시트 저장 API 실패 ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError("시트 저장 API에 연결하지 못했습니다") from exc
    return json.loads(raw) if raw else {}
