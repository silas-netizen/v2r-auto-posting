from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .exposure import (
    ExposureRow,
    KEYWORD_HEADERS,
    POST_URL_HEADERS,
    SEARCH_URL_HEADERS,
    STATUS_HEADERS,
)

NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"


class NotionError(RuntimeError):
    pass


def parse_database_id(value: str) -> str:
    text = (value or "").strip()
    dashed = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        text,
        flags=re.IGNORECASE,
    )
    if dashed:
        return dashed.group(1).lower()
    compact = re.search(r"([0-9a-f]{32})", text.replace("-", ""), flags=re.IGNORECASE)
    if not compact:
        raise NotionError("노션 데이터베이스 주소를 확인하세요")
    raw = compact.group(1).lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def _plain_text(property_value: dict[str, Any] | None) -> str:
    if not property_value:
        return ""
    kind = property_value.get("type")
    if kind == "title":
        return "".join(part.get("plain_text") or "" for part in property_value.get("title") or [])
    if kind == "rich_text":
        return "".join(
            part.get("plain_text") or "" for part in property_value.get("rich_text") or []
        )
    if kind == "url":
        return str(property_value.get("url") or "")
    if kind == "select":
        selected = property_value.get("select") or {}
        return str(selected.get("name") or "")
    if kind == "status":
        selected = property_value.get("status") or {}
        return str(selected.get("name") or "")
    if kind == "formula":
        formula = property_value.get("formula") or {}
        return str(formula.get("string") or formula.get("url") or "")
    return ""


def _find_property(schema: dict[str, Any], names: tuple[str, ...]) -> tuple[str, str]:
    wanted = {name.replace(" ", "") for name in names}
    for name, spec in schema.items():
        if name.replace(" ", "") in wanted:
            return name, str(spec.get("type") or "")
    raise NotionError("노션에서 열을 찾지 못했습니다: " + ", ".join(names))


class NotionExposureStore:
    def __init__(
        self,
        token: str,
        database_url: str,
        logger: logging.Logger,
        opener: Callable = urlopen,
    ):
        self.token = token.strip()
        self.database_id = parse_database_id(database_url)
        self.logger = logger
        self.opener = opener
        self._schema: dict[str, Any] | None = None
        self._keyword_name = ""
        self._status_name = ""
        self._status_type = ""
        self._search_url_name = ""
        self._post_url_name = ""

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise NotionError("노션 연결키를 입력하세요")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{NOTION_API}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise NotionError(f"노션 요청 실패 ({exc.code}): {detail[:300]}") from exc
        except URLError as exc:
            raise NotionError(f"노션에 연결하지 못했습니다: {exc.reason}") from exc

    def load_schema(self) -> dict[str, Any]:
        if self._schema is None:
            database = self._request("GET", f"/databases/{self.database_id}")
            self._schema = database.get("properties") or {}
            self._keyword_name, _keyword_type = _find_property(self._schema, KEYWORD_HEADERS)
            self._status_name, self._status_type = _find_property(self._schema, STATUS_HEADERS)
            try:
                self._search_url_name, _search_type = _find_property(
                    self._schema, SEARCH_URL_HEADERS
                )
            except NotionError:
                self._search_url_name = ""
            try:
                self._post_url_name, _post_type = _find_property(self._schema, POST_URL_HEADERS)
            except NotionError:
                self._post_url_name = ""
            if self._status_type not in {"status", "select"}:
                raise NotionError("노출상태 열은 선택형 또는 상태형이어야 합니다")
        return self._schema

    def load_rows(self) -> list[ExposureRow]:
        self.load_schema()
        rows: list[ExposureRow] = []
        cursor = None
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = self._request("POST", f"/databases/{self.database_id}/query", payload)
            for page in data.get("results") or []:
                properties = page.get("properties") or {}
                keyword = _plain_text(properties.get(self._keyword_name)).strip()
                if not keyword:
                    continue
                search_url = (
                    _plain_text(properties.get(self._search_url_name)).strip()
                    if self._search_url_name
                    else ""
                )
                post_url = (
                    _plain_text(properties.get(self._post_url_name)).strip()
                    if self._post_url_name
                    else ""
                )
                rows.append(
                    ExposureRow(
                        page_id=str(page.get("id") or ""),
                        keyword=keyword,
                        search_url=search_url,
                        post_url=post_url,
                        current_status=_plain_text(properties.get(self._status_name)).strip(),
                        status_property=self._status_name,
                        status_type=self._status_type,
                    )
                )
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        if not rows:
            raise NotionError("키워드가 있는 노션 행이 없습니다")
        self.logger.info("노션 키워드 %s건을 읽었습니다", len(rows))
        return rows

    def update_status(self, row: ExposureRow, status: str) -> None:
        key = "status" if row.status_type == "status" else "select"
        self._request(
            "PATCH",
            f"/pages/{row.page_id}",
            {"properties": {row.status_property: {key: {"name": status}}}},
        )
