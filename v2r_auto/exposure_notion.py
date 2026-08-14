from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from .exposure import (
    CAFE_HEADERS,
    EXPOSED_VOLUME_HEADERS,
    ExposureRow,
    KEYWORD_HEADERS,
    POST_URL_HEADERS,
    SEARCH_URL_HEADERS,
    STATUS_EXPOSED,
    STATUS_HEADERS,
    VOLUME_HEADERS,
    cafe_name_option,
)

NOTION_VERSION = "2022-06-28"
NOTION_VERSION_DATA_SOURCES = "2025-09-03"
NOTION_VERSION_VIEWS = "2026-03-11"
NOTION_API = "https://api.notion.com/v1"


class NotionError(RuntimeError):
    pass


def _dashed_id(raw: str) -> str:
    compact = (raw or "").replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", compact):
        raise NotionError("노션 데이터베이스 주소를 확인하세요")
    return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"


def parse_database_id(value: str) -> str:
    text = (value or "").strip()
    path_only = text.split("?", 1)[0]
    for pattern in (
        r"/p/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        r"/p/([0-9a-f]{32})",
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        r"([0-9a-f]{32})",
    ):
        match = re.search(pattern, path_only, flags=re.IGNORECASE)
        if match:
            return _dashed_id(match.group(1))
    raise NotionError("노션 데이터베이스 주소를 확인하세요")


def parse_view_id(value: str) -> str:
    raw = (parse_qs(urlparse((value or "").strip()).query).get("v") or [""])[0]
    compact = raw.replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", compact):
        return ""
    return _dashed_id(compact)


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
    if kind == "multi_select":
        return ", ".join(
            str(item.get("name") or "")
            for item in property_value.get("multi_select") or []
            if item.get("name")
        )
    if kind == "status":
        selected = property_value.get("status") or {}
        return str(selected.get("name") or "")
    if kind == "formula":
        formula = property_value.get("formula") or {}
        return str(formula.get("string") or formula.get("url") or "")
    if kind == "number":
        number = property_value.get("number")
        if number is None:
            return ""
        return str(int(number) if float(number).is_integer() else number)
    return ""


def _norm_header(name: str) -> str:
    return (name or "").replace(" ", "").replace("#", "")


def _find_property(schema: dict[str, Any], names: tuple[str, ...]) -> tuple[str, str]:
    wanted = {_norm_header(name) for name in names}
    for name, spec in schema.items():
        if _norm_header(name) in wanted:
            return name, str(spec.get("type") or "")
    raise NotionError("노션에서 열을 찾지 못했습니다: " + ", ".join(names))


def _title_property_name(schema: dict[str, Any]) -> str:
    for name, spec in schema.items():
        if str((spec or {}).get("type") or "") == "title":
            return name
    return ""


def _find_keyword_property(schema: dict[str, Any]) -> tuple[str, str]:
    try:
        return _find_property(schema, KEYWORD_HEADERS)
    except NotionError:
        title_name = _title_property_name(schema)
        if title_name:
            return title_name, "title"
        raise


def _optional_property(schema: dict[str, Any], names: tuple[str, ...]) -> tuple[str, str]:
    try:
        return _find_property(schema, names)
    except NotionError:
        return "", ""


def _property_option_names(schema: dict[str, Any], prop_name: str) -> list[str]:
    if not prop_name:
        return []
    spec = schema.get(prop_name) or {}
    kind = str(spec.get("type") or "")
    bucket = spec.get(kind) or {}
    names: list[str] = []
    for option in bucket.get("options") or []:
        name = str((option or {}).get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _keyword_text(
    properties: dict[str, Any], keyword_name: str, title_name: str
) -> str:
    text = _plain_text(properties.get(keyword_name)).strip()
    if text:
        return text
    if title_name and title_name != keyword_name:
        return _plain_text(properties.get(title_name)).strip()
    return ""


@dataclass
class _SourceBind:
    query_path: str
    version: str
    schema: dict[str, Any]
    keyword_name: str
    title_name: str
    status_name: str
    status_type: str
    search_url_name: str = ""
    post_url_name: str = ""
    cafe_name: str = ""
    cafe_type: str = ""
    volume_name: str = ""
    volume_type: str = ""
    exposed_volume_name: str = ""
    exposed_volume_type: str = ""
    source_id: str = ""
    filter: dict[str, Any] | None = None


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
        self.view_id = parse_view_id(database_url)
        self.logger = logger
        self.opener = opener
        self._schema: dict[str, Any] | None = None
        self._sources: list[_SourceBind] = []
        self._scoped_to_view = False
        self._view_name = ""
        self._keyword_name = ""
        self._status_name = ""
        self._status_type = ""
        self._search_url_name = ""
        self._post_url_name = ""
        self._cafe_name = ""
        self._cafe_type = ""
        self._volume_name = ""
        self._volume_type = ""
        self._exposed_volume_name = ""
        self._exposed_volume_type = ""
        self._cafe_options: list[str] = []

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        version: str = NOTION_VERSION,
    ) -> dict[str, Any]:
        if not self.token:
            raise NotionError("노션 연결키를 입력하세요")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{NOTION_API}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": version,
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

    def _try_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        version: str = NOTION_VERSION,
    ) -> dict[str, Any] | None:
        try:
            return self._request(method, path, payload, version=version)
        except NotionError:
            return None

    def load_schema(self) -> dict[str, Any]:
        if self._schema is None:
            self._sources = self._discover_sources()
            if not self._sources:
                raise NotionError("노션에서 키워드 표를 찾지 못했습니다")
            first = self._sources[0]
            self._schema = first.schema
            self._keyword_name = first.keyword_name
            self._status_name = first.status_name
            self._status_type = first.status_type
            self._search_url_name = first.search_url_name
            self._post_url_name = first.post_url_name
            self._cafe_name = first.cafe_name
            self._cafe_type = first.cafe_type
            self._volume_name = first.volume_name
            self._volume_type = first.volume_type
            self._exposed_volume_name = first.exposed_volume_name
            self._exposed_volume_type = first.exposed_volume_type
            options: list[str] = []
            seen: set[str] = set()
            for source in self._sources:
                for name in _property_option_names(source.schema, source.cafe_name):
                    if name not in seen:
                        seen.add(name)
                        options.append(name)
            self._cafe_options = options
        return self._schema

    def _bind_schema(self, schema: dict[str, Any], query_path: str, version: str, source_id: str = "") -> _SourceBind | None:
        try:
            keyword_name, _keyword_type = _find_keyword_property(schema)
            status_name, status_type = _find_property(schema, STATUS_HEADERS)
        except NotionError:
            return None
        if status_type not in {"status", "select"}:
            return None
        search_url_name, _search_type = _optional_property(schema, SEARCH_URL_HEADERS)
        post_url_name, _post_type = _optional_property(schema, POST_URL_HEADERS)
        cafe_name, cafe_type = _optional_property(schema, CAFE_HEADERS)
        volume_name, volume_type = _optional_property(schema, VOLUME_HEADERS)
        exposed_volume_name, exposed_volume_type = _optional_property(
            schema, EXPOSED_VOLUME_HEADERS
        )
        return _SourceBind(
            query_path=query_path,
            version=version,
            schema=schema,
            keyword_name=keyword_name,
            title_name=_title_property_name(schema),
            status_name=status_name,
            status_type=status_type,
            search_url_name=search_url_name,
            post_url_name=post_url_name,
            cafe_name=cafe_name,
            cafe_type=cafe_type,
            volume_name=volume_name,
            volume_type=volume_type,
            exposed_volume_name=exposed_volume_name,
            exposed_volume_type=exposed_volume_type,
            source_id=source_id,
        )

    def _bind_data_source(
        self, source_id: str, view_filter: dict[str, Any] | None = None
    ) -> _SourceBind | None:
        data_source = self._try_request(
            "GET",
            f"/data_sources/{source_id}",
            version=NOTION_VERSION_DATA_SOURCES,
        )
        if not data_source:
            return None
        bind = self._bind_schema(
            data_source.get("properties") or {},
            f"/data_sources/{source_id}/query",
            NOTION_VERSION_DATA_SOURCES,
            source_id=source_id,
        )
        if bind and view_filter:
            bind.filter = view_filter
        return bind

    def _widget_view_ids(self, view: dict[str, Any]) -> list[str]:
        ids: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                child_id = str(node.get("view_id") or "").strip()
                if child_id:
                    ids.append(child_id)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(view.get("configuration") or {})
        return ids

    def _binds_from_view(self, view: dict[str, Any]) -> list[_SourceBind]:
        view_type = str(view.get("type") or "")
        if view_type == "dashboard":
            binds: list[_SourceBind] = []
            seen: set[str] = set()
            for widget_id in self._widget_view_ids(view):
                compact = widget_id.replace("-", "").lower()
                if compact in seen:
                    continue
                seen.add(compact)
                try:
                    dashed = _dashed_id(compact)
                except NotionError:
                    continue
                child = self._try_request(
                    "GET",
                    f"/views/{dashed}",
                    version=NOTION_VERSION_VIEWS,
                )
                if child:
                    binds.extend(self._binds_from_view(child))
            return binds
        source_id = str(view.get("data_source_id") or "").strip()
        if not source_id:
            return []
        bind = self._bind_data_source(source_id, view.get("filter") or None)
        if not bind:
            return []
        self._view_name = str(view.get("name") or "").strip()
        return [bind]

    def _sources_from_url_view(self) -> list[_SourceBind]:
        if not self.view_id:
            return []
        view = self._try_request(
            "GET",
            f"/views/{self.view_id}",
            version=NOTION_VERSION_VIEWS,
        ) or self._try_request(
            "GET",
            f"/views/{self.view_id}",
            version=NOTION_VERSION_DATA_SOURCES,
        )
        if not view:
            return []
        binds = self._binds_from_view(view)
        if binds:
            self._scoped_to_view = True
        return binds

    def _discover_sources(self) -> list[_SourceBind]:
        view_sources = self._sources_from_url_view()
        if view_sources:
            return view_sources
        sources: list[_SourceBind] = []
        seen_queries: set[str] = set()

        def add_source(bind: _SourceBind | None) -> None:
            if not bind or bind.query_path in seen_queries:
                return
            seen_queries.add(bind.query_path)
            sources.append(bind)

        database = self._try_request(
            "GET",
            f"/databases/{self.database_id}",
            version=NOTION_VERSION_DATA_SOURCES,
        )
        if database is None:
            database = self._try_request(
                "GET",
                f"/databases/{self.database_id}",
                version=NOTION_VERSION,
            )

        data_source_ids: list[str] = []
        if database:
            for item in database.get("data_sources") or []:
                source_id = str((item or {}).get("id") or "").strip()
                if source_id:
                    data_source_ids.append(source_id)
            properties = database.get("properties") or {}
            if properties:
                add_source(
                    self._bind_schema(
                        properties,
                        f"/databases/{self.database_id}/query",
                        NOTION_VERSION,
                    )
                )

        for source_id in data_source_ids:
            data_source = self._try_request(
                "GET",
                f"/data_sources/{source_id}",
                version=NOTION_VERSION_DATA_SOURCES,
            )
            if not data_source:
                continue
            add_source(
                self._bind_schema(
                    data_source.get("properties") or {},
                    f"/data_sources/{source_id}/query",
                    NOTION_VERSION_DATA_SOURCES,
                    source_id=source_id,
                )
            )

        if not sources:
            old_database = self._try_request(
                "GET",
                f"/databases/{self.database_id}",
                version=NOTION_VERSION,
            )
            if old_database and old_database.get("properties"):
                add_source(
                    self._bind_schema(
                        old_database.get("properties") or {},
                        f"/databases/{self.database_id}/query",
                        NOTION_VERSION,
                    )
                )

        if not sources:
            for child_id in self._child_database_ids(self.database_id):
                child = self._try_request(
                    "GET",
                    f"/databases/{child_id}",
                    version=NOTION_VERSION_DATA_SOURCES,
                ) or self._try_request(
                    "GET",
                    f"/databases/{child_id}",
                    version=NOTION_VERSION,
                )
                if not child:
                    continue
                for item in child.get("data_sources") or []:
                    source_id = str((item or {}).get("id") or "").strip()
                    if not source_id:
                        continue
                    data_source = self._try_request(
                        "GET",
                        f"/data_sources/{source_id}",
                        version=NOTION_VERSION_DATA_SOURCES,
                    )
                    if not data_source:
                        continue
                    add_source(
                        self._bind_schema(
                            data_source.get("properties") or {},
                            f"/data_sources/{source_id}/query",
                            NOTION_VERSION_DATA_SOURCES,
                            source_id=source_id,
                        )
                    )
                properties = child.get("properties") or {}
                if properties:
                    add_source(
                        self._bind_schema(
                            properties,
                            f"/databases/{child_id}/query",
                            NOTION_VERSION,
                        )
                    )

        if not sources:
            raise NotionError("노션에서 키워드·노출상태 열을 찾지 못했습니다")
        return sources

    def _child_database_ids(self, block_id: str) -> list[str]:
        ids: list[str] = []
        cursor = None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = self._try_request("GET", path, version=NOTION_VERSION)
            if not data:
                break
            for block in data.get("results") or []:
                if block.get("type") == "child_database":
                    child_id = str(block.get("id") or "").strip()
                    if child_id:
                        ids.append(child_id)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return ids

    def _optional_property(self, names: tuple[str, ...]) -> tuple[str, str]:
        return _optional_property(self._schema or {}, names)

    def _property_option_names(self, prop_name: str) -> list[str]:
        return _property_option_names(self._schema or {}, prop_name)

    def load_rows(self) -> list[ExposureRow]:
        self.load_schema()
        rows: list[ExposureRow] = []
        seen_pages: set[str] = set()
        skipped = 0
        pending = list(self._sources)
        seen_queries = {source.query_path for source in pending}
        while pending:
            source = pending.pop(0)
            pages, nested_ids = self._query_pages(source)
            if not self._scoped_to_view:
                for source_id in nested_ids:
                    query_path = f"/data_sources/{source_id}/query"
                    if query_path in seen_queries:
                        continue
                    bind = self._bind_data_source(source_id)
                    if bind:
                        seen_queries.add(query_path)
                        pending.append(bind)
            for page in pages:
                page_id = str(page.get("id") or "")
                if not page_id or page_id in seen_pages:
                    continue
                properties = page.get("properties") or {}
                keyword = _keyword_text(
                    properties, source.keyword_name, source.title_name
                )
                if not keyword:
                    skipped += 1
                    continue
                seen_pages.add(page_id)
                search_url = (
                    _plain_text(properties.get(source.search_url_name)).strip()
                    if source.search_url_name
                    else ""
                )
                post_url = (
                    _plain_text(properties.get(source.post_url_name)).strip()
                    if source.post_url_name
                    else ""
                )
                rows.append(
                    ExposureRow(
                        page_id=page_id,
                        keyword=keyword,
                        search_url=search_url,
                        post_url=post_url,
                        current_status=_plain_text(
                            properties.get(source.status_name)
                        ).strip(),
                        status_property=source.status_name,
                        status_type=source.status_type,
                        current_cafe=(
                            _plain_text(properties.get(source.cafe_name)).strip()
                            if source.cafe_name
                            else ""
                        ),
                        cafe_property=source.cafe_name,
                        cafe_type=source.cafe_type,
                        volume_property=source.volume_name,
                        volume_type=source.volume_type,
                        exposed_volume_property=source.exposed_volume_name,
                        exposed_volume_type=source.exposed_volume_type,
                    )
                )
        if not rows:
            raise NotionError("키워드가 있는 노션 행이 없습니다")
        view_label = f" 보기 '{self._view_name}'" if self._view_name else ""
        if skipped:
            self.logger.info(
                "노션%s 키워드 %s건을 읽었습니다. 키워드가 비어 건너뛴 행 %s건",
                view_label,
                len(rows),
                skipped,
            )
        else:
            self.logger.info("노션%s 키워드 %s건을 읽었습니다", view_label, len(rows))
        return rows

    def _query_pages(self, source: _SourceBind) -> tuple[list[dict[str, Any]], list[str]]:
        pages: list[dict[str, Any]] = []
        nested_ids: list[str] = []
        cursor = None
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            if source.filter:
                payload["filter"] = source.filter
            data = self._request(
                "POST", source.query_path, payload, version=source.version
            )
            for item in data.get("results") or []:
                obj = str(item.get("object") or "")
                if obj == "data_source":
                    source_id = str(item.get("id") or "").strip()
                    if source_id:
                        nested_ids.append(source_id)
                    continue
                if obj == "page" or item.get("properties"):
                    pages.append(item)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return pages, nested_ids

    def update_status(self, row: ExposureRow, status: str) -> None:
        key = "status" if row.status_type == "status" else "select"
        self._request(
            "PATCH",
            f"/pages/{row.page_id}",
            {"properties": {row.status_property: {key: {"name": status}}}},
        )

    def update_check_result(
        self,
        row: ExposureRow,
        *,
        status: str,
        cafe_name: str | None = None,
        search_volume: int | None = None,
        volume_found: bool = False,
    ) -> None:
        key = "status" if row.status_type == "status" else "select"
        properties: dict[str, Any] = {row.status_property: {key: {"name": status}}}
        if row.cafe_property and cafe_name is not None:
            chosen = cafe_name
            if row.cafe_type in {"select", "multi_select"}:
                chosen = cafe_name_option(cafe_name, self._cafe_options)
            written = self._writable_value(row.cafe_type, chosen)
            if written is not None:
                properties[row.cafe_property] = written
        if volume_found and row.volume_property:
            written = self._writable_value(row.volume_type, search_volume)
            if written is not None:
                properties[row.volume_property] = written
        if row.exposed_volume_property:
            if status == STATUS_EXPOSED and volume_found:
                written_exposed = self._writable_value(
                    row.exposed_volume_type, search_volume
                )
            elif status != STATUS_EXPOSED:
                written_exposed = self._writable_value(row.exposed_volume_type, None)
            else:
                written_exposed = None
            if written_exposed is not None:
                properties[row.exposed_volume_property] = written_exposed
        self._request("PATCH", f"/pages/{row.page_id}", {"properties": properties})

    def _writable_value(self, kind: str, value: Any) -> dict[str, Any] | None:
        if kind == "number":
            if value in ("", None):
                return {"number": None}
            return {"number": int(value)}
        if kind == "rich_text":
            if value in ("", None):
                return {"rich_text": []}
            return {"rich_text": [{"text": {"content": str(value)}}]}
        if kind == "title":
            if value in ("", None):
                return {"title": []}
            return {"title": [{"text": {"content": str(value)}}]}
        if kind == "select":
            if value in ("", None):
                return {"select": None}
            return {"select": {"name": str(value)}}
        if kind == "multi_select":
            if value in ("", None):
                return {"multi_select": []}
            return {"multi_select": [{"name": str(value)}]}
        self.logger.warning("노션 열 형식이 달라 값을 쓰지 않습니다: %s", kind)
        return None
