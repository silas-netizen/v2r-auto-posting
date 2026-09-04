from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

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


SHEET_CLIPBOARD_PROMPT_MARKERS = (
    "복사, 잘라내기, 붙여넣기를 사용 설정하시겠습니까",
    "복사, 잘라내기, 붙여넣기를 사용 설정",
)


def sheet_clipboard_prompt_visible(html: str) -> bool:
    """True when Sheets asks to install the copy/paste extension."""
    text = html or ""
    return any(marker in text for marker in SHEET_CLIPBOARD_PROMPT_MARKERS)


def sheet_edit_target(html: str) -> str:
    """Where to type so the first-cell clipboard dialog does not eat the value.

    After the name box selects I2, Sheets focuses the in-cell waffle editor.
    Clicking that waffle (or typing there with F2) pops the 설치 dialog and
    leaves I2 empty. Click the formula bar instead.
    """
    text = html or ""
    if 'id="t-formula-bar-input"' in text or "t-formula-bar-input" in text:
        return "formula_bar"
    if 'id="t-name-box"' in text or 'aria-label="이름 상자"' in text:
        return "name_box"
    if sheet_clipboard_prompt_visible(text):
        return "keyboard"
    if "waffle-rich-text-editor" in text:
        return "keyboard"
    return "keyboard"


_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


def parse_spreadsheet_ref(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    match = _SHEET_ID_RE.search(text)
    if not match:
        raise SheetError("구글 시트 주소를 확인하세요")
    parsed = urlparse(text)
    gid = (parse_qs(parsed.query).get("gid") or ["0"])[0]
    if parsed.fragment.startswith("gid="):
        gid = parsed.fragment.split("=", 1)[1].split("&", 1)[0]
    return match.group(1), gid or "0"


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


KOREA_TZ = ZoneInfo("Asia/Seoul")


def now_stamp(now: datetime | None = None) -> str:
    """Sheet edit time is always Korea time, not the PC's UTC clock."""
    if now is None:
        now = datetime.now(KOREA_TZ)
    elif now.tzinfo is not None:
        now = now.astimezone(KOREA_TZ)
    return now.strftime("%Y-%m-%d %H:%M:%S")


def _norm_header(name: str) -> str:
    return (name or "").replace(" ", "").replace("#", "")


def _find_header(headers: list[str], names: tuple[str, ...]) -> tuple[str, int]:
    wanted = {_norm_header(name) for name in names}
    for index, header in enumerate(headers):
        if _norm_header(header) in wanted:
            return column_letter(index), index
    return "", -1


def _cell(row: list[str], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _looks_like_html(text: str) -> bool:
    head = (text or "").lstrip()[:200].casefold()
    return head.startswith("<!doctype") or head.startswith("<html") or "<html" in head[:80]


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


def plan_volume_writes(
    row: ExposureRow,
    *,
    keyword: str | None,
    search_volume: int | None,
    volume_found: bool,
    search_url: str | None,
    edited_at: str,
) -> list[SheetWrite]:
    writes: list[SheetWrite] = []
    if keyword is not None and row.keyword_property:
        writes.append(SheetWrite(row.keyword_property, keyword))
    if volume_found and row.volume_property:
        writes.append(SheetWrite(row.volume_property, _sheet_text(search_volume)))
    if search_url is not None and row.search_url_property:
        writes.append(SheetWrite(row.search_url_property, search_url))
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

    def load_rows(self) -> list[ExposureRow]:
        text = self._read_csv()
        table = list(csv.reader(io.StringIO(text)))
        if not table:
            raise SheetError("구글 시트에서 열 이름을 찾지 못했습니다")
        headers = [str(header or "").strip() for header in table[0]]
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
                    current_volume=_cell(raw, bind.volume_idx),
                    keyword_property=bind.keyword_col,
                    keyword_type="rich_text",
                    edited_property=bind.edited_col,
                    search_url_property=bind.search_url_col,
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
        for write in writes:
            self.writer.write_cell(
                self.sheet_url, write.column, row_number, write.value
            )

    def update_volume_and_keyword(
        self,
        row: ExposureRow,
        *,
        keyword: str | None = None,
        search_volume: int | None = None,
        volume_found: bool = False,
        search_url: str | None = None,
    ) -> int:
        if self.writer is None:
            raise SheetError("구글 시트에 쓸 브라우저가 없습니다")
        edited_at = now_stamp(self._now() if self._now else None)
        writes = plan_volume_writes(
            row,
            keyword=keyword,
            search_volume=search_volume,
            volume_found=volume_found,
            search_url=search_url,
            edited_at=edited_at,
        )
        row_number = int(row.page_id)
        failed = 0
        for write in writes:
            try:
                self.writer.write_cell(
                    self.sheet_url, write.column, row_number, write.value
                )
            except Exception as exc:
                failed += 1
                self.logger.error(
                    "시트 %s%s 저장 실패, 다음 칸으로 이어갑니다: %s",
                    write.column,
                    row_number,
                    exc,
                )
        return failed

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
        export = sheet_export_url(self.sheet_id, self.gid)
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
            if _looks_like_html(text):
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
        if _looks_like_html(text):
            raise SheetError(
                "구글 시트를 읽지 못했습니다. 공유 권한 또는 구글 로그인을 확인하세요"
            )
        return text
