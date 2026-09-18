from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from .models import Account, Manuscript
from .store import JobStore


EXCLUDED_DOCUMENT_IDS = {"1DLQgLWBo1c4CDkgvH4fjkuDrRM1C03XT"}
SHEET_ID_PATTERN = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
TIMEOUT_SECONDS = 5


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SourceRef:
    name: str
    url: str
    gid: str = ""
    kind: str = "sheet"

    @property
    def document_id(self) -> str:
        match = SHEET_ID_PATTERN.search(self.url)
        if match:
            return match.group(1)
        query = parse_qs(urlparse(self.url).query)
        return (query.get("id") or [""])[0]


def load_source_config(path: Path) -> list[SourceRef]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        SourceRef(
            name=str(item["name"]),
            url=str(item["url"]),
            gid=str(item.get("gid") or ""),
            kind=str(item.get("kind") or "sheet"),
        )
        for item in payload.get("sources", payload if isinstance(payload, list) else [])
    ]


def visualization_csv_url(ref: SourceRef) -> str:
    document_id = ref.document_id
    if not document_id:
        raise SourceError(f"시트 주소를 해석하지 못했습니다: {ref.url}")
    if document_id in EXCLUDED_DOCUMENT_IDS:
        raise SourceError("사용자 제외 원본은 동기화하지 않습니다")
    gid = ref.gid or _gid_from_url(ref.url)
    return (
        f"https://docs.google.com/spreadsheets/d/{document_id}/gviz/tq"
        f"?tqx=out:csv&gid={gid}"
    )


def _gid_from_url(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    if query.get("gid"):
        return query["gid"][0]
    match = re.search(r"gid=(\d+)", url)
    return match.group(1) if match else "0"


def fetch_csv(url: str, *, opener=urlopen) -> str:
    request = Request(url, headers={"User-Agent": "V2RInternal/1.0"})
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8-sig")
    except TimeoutError as exc:
        raise SourceError("원본이 5초 안에 응답하지 않아 마지막 캐시를 유지합니다") from exc
    except (HTTPError, URLError, OSError) as exc:
        raise SourceError(f"원본을 읽지 못했습니다: {exc}") from exc


def parse_daily_rows(csv_text: str) -> list[Manuscript]:
    reader = csv.DictReader(io.StringIO(csv_text))
    manuscripts: list[Manuscript] = []
    for row in reader:
        title = (row.get("제목") or "").strip()
        body = (row.get("본문") or row.get("내용") or "").strip()
        combined = (row.get("C") or row.get("통합원고") or "").strip()
        if not title and combined:
            title, body = _split_combined(combined)
        elif not title and "제목" in body and "본문" in body:
            title, body = _split_combined(body)
        if not title or not body:
            continue
        manuscripts.append(
            Manuscript(
                title=title,
                body=body,
                cafe=(row.get("카페") or row.get("카페명") or row.get("G") or "").strip(),
                board=(row.get("게시판") or row.get("게시판명") or row.get("H") or "").strip(),
                source="sheet",
            )
        )
    return manuscripts


def parse_account_rows(csv_text: str) -> list[Account]:
    reader = csv.DictReader(io.StringIO(csv_text))
    accounts: list[Account] = []
    for row in reader:
        login_id = (row.get("ID") or row.get("아이디") or row.get("B") or "").strip()
        if not login_id:
            continue
        accounts.append(
            Account(
                login_id=login_id,
                work_type=(row.get("작업 구분") or row.get("작업구분") or row.get("H") or "").strip(),
                linked=(row.get("연동") or row.get("I") or "").strip(),
                excluded=_truthy(row.get("제외") or row.get("음영") or ""),
                grade=(row.get("등급") or row.get("J") or "").strip(),
                nickname=(row.get("닉네임") or row.get("카페닉네임") or "").strip(),
                shade=(row.get("음영") or row.get("shade") or "").strip(),
            )
        )
    return accounts


def _split_combined(value: str) -> tuple[str, str]:
    title_match = re.search(r"제목\s*:\s*(.+)", value)
    body_match = re.search(r"본문\s*:\s*([\s\S]+)", value)
    title = title_match.group(1).strip() if title_match else ""
    body = body_match.group(1).strip() if body_match else ""
    if body and title and body.startswith(title):
        body = body[len(title) :].strip()
    if title_match and "본문" in body:
        body = re.split(r"본문\s*:", body, maxsplit=1)[-1].strip()
    return title, body


def _truthy(value: str) -> bool:
    return value.strip().casefold() in {"y", "yes", "true", "1", "회색", "음영"}


def sync_source(
    ref: SourceRef,
    store: JobStore,
    *,
    opener=urlopen,
) -> dict[str, Any]:
    if ref.document_id in EXCLUDED_DOCUMENT_IDS:
        raise SourceError("사용자 제외 원본은 동기화하지 않습니다")
    try:
        csv_text = fetch_csv(visualization_csv_url(ref), opener=opener)
        rows = parse_daily_rows(csv_text) if ref.kind != "accounts" else []
        accounts = parse_account_rows(csv_text) if ref.kind == "accounts" else []
        payload = {
            "name": ref.name,
            "url": ref.url,
            "manuscripts": [item.__dict__ for item in rows],
            "accounts": [item.__dict__ for item in accounts],
        }
        store.save_source(ref.name, payload, "ok")
        return payload
    except SourceError:
        cached = store.load_source(ref.name)
        if cached:
            cached["status"] = "cache"
            return cached
        raise
