from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .sheet_values import (
    apply_sheet_overlay,
    column_index_from_letter,
    csv_sheet_cell,
    looks_like_html,
    parse_sheet_int,
    parse_sheet_table,
    sheet_cell_values_match,
    sheet_gid_from_url,
    sheet_plain_text,
    spreadsheet_id_from_url,
)
from .exposure import (
    CAFE_HEADERS,
    EDITED_HEADERS,
    EXPOSED_VOLUME_HEADERS,
    ExposureRow,
    KEYWORD_HEADERS,
    POST_URL_HEADERS,
    SEARCH_URL_HEADERS,
    STATUS_EXPOSED,
    STATUS_HEADERS,
    VOLUME_HEADERS,
    cafe_name_option,
    status_option,
)


class SheetError(RuntimeError):
    pass


def parse_spreadsheet_ref(value: str) -> tuple[str, str]:
    try:
        return spreadsheet_id_from_url(value), sheet_gid_from_url(value)
    except ValueError as exc:
        raise SheetError("구글 시트 주소를 확인하세요") from exc


def sheet_export_url(sheet_id: str, gid: str) -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={gid}"
    )


def column_letter(index: int) -> str:
    if index < 0:
        raise ValueError("열 번호가 올바르지 않습니다")
    number = index + 1
    letters: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def now_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _norm_header(name: str) -> str:
    return sheet_plain_text(name).replace(" ", "").replace("#", "")


def _find_header(headers: list[str], names: tuple[str, ...]) -> tuple[str, int]:
    wanted = {_norm_header(name) for name in names}
    for index, header in enumerate(headers):
        if _norm_header(header) in wanted:
            return column_letter(index), index
    return "", -1


def _sum_column(table: list[list[str]], index: int) -> int:
    if index < 0 or len(table) < 2:
        return 0
    return sum(parse_sheet_int(_cell(row, index)) for row in table[1:])


def _cell(row: list[str], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _sheet_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


@dataclass(frozen=True, slots=True)
class SheetWrite:
    column: str
    value: str


@dataclass
class _SheetBind:
    keyword_col: str
    keyword_idx: int
    status_col: str
    status_idx: int
    search_url_col: str = ""
    search_url_idx: int = -1
    post_url_col: str = ""
    post_url_idx: int = -1
    cafe_col: str = ""
    cafe_idx: int = -1
    volume_col: str = ""
    volume_idx: int = -1
    exposed_volume_col: str = ""
    exposed_volume_idx: int = -1
    edited_col: str = ""
    edited_idx: int = -1


def plan_sheet_writes(
    row: ExposureRow,
    *,
    status: str,
    cafe_name: str | None,
    search_volume: int | None,
    volume_found: bool,
    edited_at: str,
    status_options: list[str] | tuple[str, ...] = (),
    cafe_options: list[str] | tuple[str, ...] = (),
) -> list[SheetWrite]:
    writes: list[SheetWrite] = []
    if row.status_property:
        writes.append(
            SheetWrite(row.status_property, status_option(status, status_options))
        )
    if row.cafe_property and cafe_name is not None:
        writes.append(
            SheetWrite(row.cafe_property, cafe_name_option(cafe_name, cafe_options))
        )
    if volume_found and row.volume_property:
        writes.append(SheetWrite(row.volume_property, _sheet_text(search_volume)))
    if row.exposed_volume_property:
        if status == STATUS_EXPOSED and volume_found:
            writes.append(
                SheetWrite(row.exposed_volume_property, _sheet_text(search_volume))
            )
        elif status != STATUS_EXPOSED:
            writes.append(SheetWrite(row.exposed_volume_property, ""))
    if row.edited_property:
        writes.append(SheetWrite(row.edited_property, edited_at))
    return writes


class SeleniumSheetWriter:
    def __init__(self, browser, logger: logging.Logger):
        self.browser = browser
        self.logger = logger

    def write_cell(
        self, sheet_url: str, column: str, row_number: int, value: str
    ) -> None:
        self.browser.update_sheet_cell(sheet_url, column, row_number, value)


class GoogleSheetExposureStore:
    label = "구글 시트"

    def __init__(
        self,
        sheet_url: str,
        logger: logging.Logger,
        opener: Callable = urlopen,
        writer=None,
        browser=None,
        now: Callable[[], datetime] | None = None,
    ):
        self.sheet_url = (sheet_url or "").strip()
        self.sheet_id, self.gid = parse_spreadsheet_ref(self.sheet_url)
        self.logger = logger
        self.opener = opener
        self.writer = writer
        self.browser = browser
        self._now = now
        self._bind: _SheetBind | None = None
        self._status_options: list[str] = []
        self._cafe_options: list[str] = []
        self._written: dict[tuple[int, int], str] = {}

    def load_rows(self) -> list[ExposureRow]:
        text = self._read_csv()
        table = parse_sheet_table(text)
        if not table:
            raise SheetError("구글 시트에서 열 이름을 찾지 못했습니다")
        headers = [sheet_plain_text(header) for header in table[0]]
        bind = self._bind_headers(headers)
        self._bind = bind
        rows: list[ExposureRow] = []
        skipped = 0
        for number, raw in enumerate(table[1:], start=2):
            keyword = _cell(raw, bind.keyword_idx)
            if not keyword:
                skipped += 1
                continue
            rows.append(
                ExposureRow(
                    page_id=str(number),
                    keyword=keyword,
                    search_url=_cell(raw, bind.search_url_idx),
                    post_url=_cell(raw, bind.post_url_idx),
                    current_status=_cell(raw, bind.status_idx),
                    status_property=bind.status_col,
                    status_type="select",
                    current_cafe=_cell(raw, bind.cafe_idx),
                    cafe_property=bind.cafe_col,
                    cafe_type="select",
                    volume_property=bind.volume_col,
                    volume_type="number",
                    exposed_volume_property=bind.exposed_volume_col,
                    exposed_volume_type="number",
                    edited_property=bind.edited_col,
                )
            )
        if not rows:
            raise SheetError("키워드가 있는 시트 행이 없습니다")
        if skipped:
            self.logger.info(
                "구글 시트 키워드 %s건을 읽었습니다. 키워드가 비어 건너뛴 행 %s건",
                len(rows),
                skipped,
            )
        else:
            self.logger.info("구글 시트 키워드 %s건을 읽었습니다", len(rows))
        return rows

    def update_status(self, row: ExposureRow, status: str) -> None:
        self.update_check_result(row, status=status, cafe_name=None, volume_found=False)

    def update_check_result(
        self,
        row: ExposureRow,
        *,
        status: str,
        cafe_name: str | None = None,
        search_volume: int | None = None,
        volume_found: bool = False,
    ) -> None:
        if self.writer is None:
            raise SheetError("구글 시트에 쓸 브라우저가 없습니다")
        edited_at = now_stamp(self._now() if self._now else None)
        writes = plan_sheet_writes(
            row,
            status=status,
            cafe_name=cafe_name,
            search_volume=search_volume,
            volume_found=volume_found,
            edited_at=edited_at,
            status_options=self._status_options,
            cafe_options=self._cafe_options,
        )
        row_number = int(row.page_id)
        table = apply_sheet_overlay(
            parse_sheet_table(self._read_csv()), self._written
        )
        errors: list[str] = []
        for write in writes:
            current = csv_sheet_cell(
                table, row_number, column_index_from_letter(write.column)
            )
            if sheet_cell_values_match(current, write.value):
                self.logger.info(
                    "시트 %s%s는 이미 같아서 건너뜁니다",
                    write.column,
                    row_number,
                )
                self._remember_write(row_number, write.column, write.value)
                continue
            try:
                self.writer.write_cell(
                    self.sheet_url, write.column, row_number, write.value
                )
                self._remember_write(row_number, write.column, write.value)
            except Exception as exc:
                self.logger.error(
                    "시트 %s%s 저장 실패: %s",
                    write.column,
                    row_number,
                    exc,
                )
                errors.append(f"{write.column}{row_number}: {exc}")
        if errors:
            raise SheetError(
                "시트 일부 칸을 저장하지 못했습니다: " + "; ".join(errors)
            )

    def write_volume_totals(self) -> None:
        if self.writer is None:
            raise SheetError("구글 시트에 쓸 브라우저가 없습니다")
        table = apply_sheet_overlay(
            parse_sheet_table(self._read_csv()), self._written
        )
        if not table:
            raise SheetError("구글 시트에서 열 이름을 찾지 못했습니다")
        bind = self._bind or self._bind_headers(
            [str(header or "").strip() for header in table[0]]
        )
        keyword_total = _sum_column(table, bind.volume_idx)
        exposed_total = _sum_column(table, bind.exposed_volume_idx)
        if bind.volume_idx >= 0:
            self._write_if_changed("P", 1, _sheet_text(keyword_total), table)
            self.logger.info("키워드 검색량 합 P1=%s", keyword_total)
        if bind.exposed_volume_idx >= 0:
            self._write_if_changed("Q", 1, _sheet_text(exposed_total), table)
            self.logger.info("노출된 검색량 합 Q1=%s", exposed_total)

    def _write_if_changed(
        self,
        column: str,
        row_number: int,
        value: str,
        table: list[list[str]],
    ) -> None:
        current = csv_sheet_cell(
            table, row_number, column_index_from_letter(column)
        )
        if sheet_cell_values_match(current, value):
            return
        self.writer.write_cell(self.sheet_url, column, row_number, value)
        self._remember_write(row_number, column, value)

    def _remember_write(self, row_number: int, column: str, value: str) -> None:
        self._written[(row_number, column_index_from_letter(column))] = value

    def _bind_headers(self, headers: list[str]) -> _SheetBind:
        keyword_col, keyword_idx = _find_header(headers, KEYWORD_HEADERS)
        status_col, status_idx = _find_header(headers, STATUS_HEADERS)
        if not keyword_col or not status_col:
            raise SheetError(
                "구글 시트에서 키워드·노출상태 열을 찾지 못했습니다. "
                "노출 현황 탭 주소를 그대로 넣었는지 확인하세요"
            )
        search_url_col, search_url_idx = _find_header(headers, SEARCH_URL_HEADERS)
        post_url_col, post_url_idx = _find_header(headers, POST_URL_HEADERS)
        cafe_col, cafe_idx = _find_header(headers, CAFE_HEADERS)
        volume_col, volume_idx = _find_header(headers, VOLUME_HEADERS)
        exposed_volume_col, exposed_volume_idx = _find_header(
            headers, EXPOSED_VOLUME_HEADERS
        )
        edited_col, edited_idx = _find_header(headers, EDITED_HEADERS)
        return _SheetBind(
            keyword_col=keyword_col,
            keyword_idx=keyword_idx,
            status_col=status_col,
            status_idx=status_idx,
            search_url_col=search_url_col,
            search_url_idx=search_url_idx,
            post_url_col=post_url_col,
            post_url_idx=post_url_idx,
            cafe_col=cafe_col,
            cafe_idx=cafe_idx,
            volume_col=volume_col,
            volume_idx=volume_idx,
            exposed_volume_col=exposed_volume_col,
            exposed_volume_idx=exposed_volume_idx,
            edited_col=edited_col,
            edited_idx=edited_idx,
        )

    def _read_csv(self) -> str:
        export = f"{sheet_export_url(self.sheet_id, self.gid)}&cache={time.time_ns()}"
        request = Request(
            export,
            headers={"User-Agent": "Mozilla/5.0 (compatible; V2R-Exposure-Checker)"},
        )
        try:
            with self.opener(request, timeout=30) as response:
                raw = response.read()
            text = (
                raw.decode("utf-8-sig")
                if isinstance(raw, (bytes, bytearray))
                else str(raw)
            )
            if looks_like_html(text):
                return self._download_via_browser()
            return text
        except SheetError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            if self.browser is not None:
                self.logger.info("공개 내려받기가 안 되어 크롬으로 시트를 받습니다")
                return self._download_via_browser()
            raise SheetError(f"구글 시트를 읽지 못했습니다: {exc}") from exc

    def _download_via_browser(self) -> str:
        if self.browser is None:
            raise SheetError(
                "구글 시트를 읽지 못했습니다. 링크를 누구나 볼 수 있게 공유하거나, "
                "크롬에서 구글 로그인하세요"
            )
        path = self.browser.download_sheet(self.sheet_url)
        text = Path(path).read_text(encoding="utf-8-sig")
        if looks_like_html(text):
            raise SheetError(
                "구글 시트를 읽지 못했습니다. 공유 권한 또는 구글 로그인을 확인하세요"
            )
        return text
