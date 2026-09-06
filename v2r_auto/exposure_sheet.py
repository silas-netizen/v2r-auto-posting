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
    sheet_keywords_match,
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
    pick_kept_duplicate,
    shift_row_page_ids,
    spacing_keyword_key,
    status_option,
    strip_parenthetical,
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


def keyword_cells_match(actual: str, expected: str) -> bool:
    return sheet_keywords_match(actual, expected)


def find_sheet_row_numbers(
    table: list[list[str]], keyword: str, keyword_idx: int
) -> list[int]:
    found: list[int] = []
    for number in range(2, len(table) + 1):
        if keyword_cells_match(csv_sheet_cell(table, number, keyword_idx), keyword):
            found.append(number)
    return found


def find_spacing_duplicate_rows(
    table: list[list[str]], keyword: str, keyword_idx: int
) -> list[int]:
    wanted = spacing_keyword_key(keyword)
    if not wanted:
        return []
    found: list[int] = []
    for number in range(2, len(table) + 1):
        if spacing_keyword_key(csv_sheet_cell(table, number, keyword_idx)) == wanted:
            found.append(number)
    return found


def resolve_sheet_row_number(
    table: list[list[str]], row: ExposureRow, keyword_idx: int
) -> int:
    """Use the live keyword cell, not the row number remembered at load.

    If a sheet row is deleted mid-run, later page_id values land on the
    next keyword. Confirm H (or the keyword column) before writing.
    """
    current = int(row.page_id)
    if keyword_idx < 0:
        return current
    if keyword_cells_match(
        csv_sheet_cell(table, current, keyword_idx), row.keyword
    ):
        return current
    matches = find_sheet_row_numbers(table, row.keyword, keyword_idx)
    if not matches:
        raise SheetError(
            f"시트에서 키워드를 다시 찾지 못했습니다: {row.keyword} "
            f"(기억한 행 {current})"
        )
    return min(matches, key=lambda number: abs(number - current))


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
        self,
        sheet_url: str,
        column: str,
        row_number: int,
        value: str,
        expect_keyword: str = "",
        keyword_column: str = "",
    ) -> int:
        written = self.browser.update_sheet_cell(
            sheet_url,
            column,
            row_number,
            value,
            expect_keyword=expect_keyword,
            keyword_column=keyword_column,
        )
        return int(written or row_number)

    def delete_row(
        self,
        sheet_url: str,
        row_number: int,
        expect_keyword: str = "",
        keyword_column: str = "",
    ) -> None:
        self.browser.delete_sheet_row(
            sheet_url,
            row_number,
            expect_keyword=expect_keyword,
            keyword_column=keyword_column,
        )


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
        self.last_duplicate_removed = False

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
        errors: list[str] = []
        for write in writes:
            table = apply_sheet_overlay(
                parse_sheet_table(self._read_csv()), self._written
            )
            bind = self._bind
            if bind is None and table:
                bind = self._bind_headers(
                    [sheet_plain_text(header) for header in table[0]]
                )
                self._bind = bind
            if bind is None or not bind.keyword_col:
                raise SheetError("키워드 열을 확인하지 못해 쓰지 않습니다")
            try:
                row_number = self._lock_sheet_row(table, row, bind)
            except SheetError as exc:
                self.logger.error("시트 행 확인 실패 (%s): %s", row.keyword, exc)
                errors.append(str(exc))
                break
            try:
                written_row = self._write_locked_cell(
                    write, row, row_number, bind
                )
                if written_row != row_number:
                    row.page_id = str(written_row)
                    row_number = written_row
                self._remember_write(row_number, write.column, write.value)
            except Exception as exc:
                if self._looks_like_row_mismatch(exc):
                    try:
                        table = apply_sheet_overlay(
                            parse_sheet_table(self._read_csv()), self._written
                        )
                        row_number = self._lock_sheet_row(table, row, bind)
                        written_row = self._write_locked_cell(
                            write, row, row_number, bind
                        )
                        if written_row != row_number:
                            row.page_id = str(written_row)
                            row_number = written_row
                        self._remember_write(
                            row_number, write.column, write.value
                        )
                        continue
                    except Exception as retry_exc:
                        exc = retry_exc
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

    def collapse_spacing_duplicates(
        self,
        row: ExposureRow,
        *,
        canonical_keyword: str,
        first_cafe: str = "",
        queue: list[ExposureRow] | None = None,
    ) -> ExposureRow:
        self.last_duplicate_removed = False
        if self.writer is None:
            raise SheetError("구글 시트에 쓸 브라우저가 없습니다")
        table = apply_sheet_overlay(parse_sheet_table(self._read_csv()), self._written)
        bind = self._bind
        if bind is None and table:
            bind = self._bind_headers(
                [sheet_plain_text(header) for header in table[0]]
            )
            self._bind = bind
        if bind is None or bind.keyword_idx < 0:
            return row
        numbers = find_spacing_duplicate_rows(table, row.keyword, bind.keyword_idx)
        if len(numbers) < 2:
            return row
        candidates: list[tuple[int, str, str]] = []
        for number in numbers:
            keyword = csv_sheet_cell(table, number, bind.keyword_idx)
            cafe = (
                csv_sheet_cell(table, number, bind.cafe_idx)
                if bind.cafe_idx >= 0
                else ""
            )
            candidates.append((number, keyword, cafe))
        keep_number, keep_keyword, _keep_cafe = pick_kept_duplicate(
            candidates,
            canonical_keyword=canonical_keyword,
            first_cafe=first_cafe,
        )
        self.logger.info(
            "같은 키워드 중복 %s행 → %s 남기고 나머지 삭제",
            numbers,
            keep_number,
        )
        live_rows = list(queue or [row])
        if row not in live_rows:
            live_rows.append(row)
        for number, keyword, _cafe in sorted(candidates, key=lambda item: item[0], reverse=True):
            if number == keep_number:
                continue
            self.writer.delete_row(
                self.sheet_url,
                number,
                expect_keyword=keyword,
                keyword_column=bind.keyword_col,
            )
            self.logger.info(
                "중복 행 %s를 삭제했습니다: %s",
                number,
                keyword if str(keyword).strip() else "(비어 있음)",
            )
            if number < keep_number:
                keep_number -= 1
            shift_row_page_ids(live_rows, number)
            self._shift_written_after_delete(number)
            self.last_duplicate_removed = True
        canon = strip_parenthetical(canonical_keyword).strip()
        if canon and strip_parenthetical(keep_keyword).strip() != canon:
            written = self.writer.write_cell(
                self.sheet_url,
                bind.keyword_col,
                keep_number,
                canon,
                expect_keyword=keep_keyword,
                keyword_column=bind.keyword_col,
            )
            keep_number = int(written or keep_number)
            keep_keyword = canon
            self._remember_write(keep_number, bind.keyword_col, canon)
            self.logger.info(
                "남긴 행 %s 키워드를 자동완성 기준으로 맞췄습니다: %s",
                keep_number,
                canon,
            )
        row.page_id = str(keep_number)
        row.keyword = keep_keyword
        for item in live_rows:
            if int(item.page_id) == keep_number:
                item.keyword = keep_keyword
        return row

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
            self._write_total_cell("P", 1, _sheet_text(keyword_total), table)
            self.logger.info("키워드 검색량 합 P1=%s", keyword_total)
        if bind.exposed_volume_idx >= 0:
            self._write_total_cell("Q", 1, _sheet_text(exposed_total), table)
            self.logger.info("노출된 검색량 합 Q1=%s", exposed_total)

    def _lock_sheet_row(
        self, table: list[list[str]], row: ExposureRow, bind: _SheetBind | None
    ) -> int:
        remembered = int(row.page_id)
        keyword_idx = bind.keyword_idx if bind is not None else -1
        matches = (
            find_sheet_row_numbers(table, row.keyword, keyword_idx)
            if keyword_idx >= 0
            else []
        )
        if len(matches) > 1:
            self.logger.warning(
                "시트에 같은 키워드가 여러 행에 있습니다: %s → %s",
                row.keyword,
                matches,
            )
        row_number = resolve_sheet_row_number(table, row, keyword_idx)
        if row_number != remembered:
            remembered_keyword = (
                sheet_plain_text(csv_sheet_cell(table, remembered, keyword_idx))
                if keyword_idx >= 0
                else ""
            )
            self.logger.warning(
                "시트 행이 달라져 %s행 키워드는 %s입니다. %s는 %s행에 씁니다",
                remembered,
                remembered_keyword or "(비어 있음)",
                row.keyword,
                row_number,
            )
            row.page_id = str(row_number)
        else:
            self.logger.info(
                "시트 행 확인: %s / 기억 %s / 실제 %s",
                row.keyword,
                remembered,
                row_number,
            )
        return row_number

    def _write_locked_cell(
        self,
        write: SheetWrite,
        row: ExposureRow,
        row_number: int,
        bind: _SheetBind | None,
    ) -> int:
        expect_keyword = row.keyword if row.keyword else ""
        keyword_column = bind.keyword_col if bind is not None else ""
        written = self.writer.write_cell(
            self.sheet_url,
            write.column,
            row_number,
            write.value,
            expect_keyword=expect_keyword,
            keyword_column=keyword_column,
        )
        if written is None:
            return row_number
        return int(written)

    @staticmethod
    def _looks_like_row_mismatch(exc: Exception) -> bool:
        text = str(exc)
        if "키워드" not in text:
            return False
        return any(
            token in text
            for token in ("찾지", "다릅니다", "확인하지", "쓰지 않습니다")
        )

    def _write_total_cell(
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
            self.logger.info(
                "시트 %s%s는 이미 같아서 건너뜁니다",
                column,
                row_number,
            )
            self._remember_write(row_number, column, value)
            return
        try:
            self.writer.write_cell(self.sheet_url, column, row_number, value)
            self._remember_write(row_number, column, value)
        except Exception as exc:
            self.logger.error(
                "시트 %s%s 저장 실패: %s",
                column,
                row_number,
                exc,
            )

    def _remember_write(self, row_number: int, column: str, value: str) -> None:
        self._written[(row_number, column_index_from_letter(column))] = value

    def _shift_written_after_delete(self, deleted: int) -> None:
        shifted: dict[tuple[int, int], str] = {}
        for (row_number, column_index), value in self._written.items():
            if row_number == deleted:
                continue
            if row_number > deleted:
                shifted[(row_number - 1, column_index)] = value
            else:
                shifted[(row_number, column_index)] = value
        self._written = shifted

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
