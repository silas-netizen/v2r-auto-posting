from datetime import datetime
from pathlib import Path

from v2r_auto.exposure import ExposureRow
from v2r_auto.exposure_sheet import GoogleSheetExposureStore
from v2r_auto.sheet_values import (
    column_index_from_letter,
    csv_sheet_cell,
    parse_sheet_int,
    parse_sheet_table,
    sheet_write_confirmed,
)


NEUMIS_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1mgqghfeNrSZ1bTSrfTYWojtPGMc0u-K4VXEikxrHLOw/"
    "edit?gid=1782605844#gid=1782605844"
)
FIXTURE = Path(__file__).parent / "fixtures" / "neumis_exposure.csv"
NEW_STAMP = "2026-08-29 11:39:20"


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class StaleExportWriter:
    def __init__(self, table: list[list[str]], *, typed_ok: bool):
        self.table = table
        self.typed_ok = typed_ok
        self.writes: list[tuple[str, int, str]] = []
        self.failures: list[str] = []

    def write_cell(self, sheet_url: str, column: str, row_number: int, value: str) -> None:
        actual = csv_sheet_cell(
            self.table, row_number, column_index_from_letter(column)
        )
        if not sheet_write_confirmed(actual, value, typed_ok=self.typed_ok):
            message = f"{column}{row_number}: 기대 {value or '(비어 있음)'} / 실제 {actual or '(비어 있음)'}"
            self.failures.append(message)
            raise RuntimeError(message)
        self.writes.append((column, row_number, value))


def _fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_neumis_every_j_cell_confirms_against_new_stamp() -> None:
    table = parse_sheet_table(_fixture_text())
    failed: list[tuple[int, str]] = []
    for row_number in range(2, len(table) + 1):
        actual = csv_sheet_cell(table, row_number, column_index_from_letter("J"))
        if not sheet_write_confirmed(actual, NEW_STAMP):
            failed.append((row_number, actual))
    assert failed == []
    assert len(table) == 381


def test_neumis_logged_j3_and_j4_failures_now_confirm() -> None:
    table = parse_sheet_table(_fixture_text())
    assert csv_sheet_cell(table, 3, 7) == "임산부 항문 가려움"
    assert csv_sheet_cell(table, 4, 7) == "치질수술 병원"
    assert sheet_write_confirmed(
        csv_sheet_cell(table, 3, 9), "2026-08-29 11:39:20"
    )
    assert sheet_write_confirmed(
        csv_sheet_cell(table, 4, 9), "2026-08-29 11:40:27"
    )


def test_neumis_every_row_saves_with_stale_export() -> None:
    csv_text = _fixture_text()
    table = parse_sheet_table(csv_text)
    writer = StaleExportWriter(table, typed_ok=True)
    store = GoogleSheetExposureStore(
        NEUMIS_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(csv_text.encode("utf-8")),
        writer=writer,
        now=lambda: datetime(2026, 8, 29, 11, 39, 20),
    )
    rows = store.load_rows()
    assert len(rows) == 380
    errors: list[str] = []
    for row in rows:
        number = int(row.page_id)
        volume_text = csv_sheet_cell(table, number, column_index_from_letter("K"))
        try:
            store.update_check_result(
                row,
                status=row.current_status or "밀려남",
                cafe_name=None,
                search_volume=parse_sheet_int(volume_text),
                volume_found=bool(str(volume_text).strip()),
            )
        except Exception as exc:
            errors.append(f"{number} {row.keyword}: {exc}")
    assert errors == []
    assert writer.failures == []
    assert any(column == "J" for column, _row, _value in writer.writes)
    assert ("L", 141, "") in writer.writes


def test_neumis_j_stale_export_does_not_need_typed_ok() -> None:
    csv_text = _fixture_text()
    table = parse_sheet_table(csv_text)
    writer = StaleExportWriter(table, typed_ok=False)
    store = GoogleSheetExposureStore(
        NEUMIS_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(csv_text.encode("utf-8")),
        writer=writer,
        now=lambda: datetime(2026, 8, 29, 11, 39, 20),
    )
    rows = store.load_rows()
    j_errors = 0
    for row in rows:
        number = int(row.page_id)
        volume_text = csv_sheet_cell(table, number, column_index_from_letter("K"))
        try:
            store.update_check_result(
                ExposureRow(
                    row.page_id,
                    row.keyword,
                    "",
                    "",
                    row.current_status,
                    row.status_property,
                    "select",
                    current_cafe=row.current_cafe,
                    cafe_property=row.cafe_property,
                    cafe_type="select",
                    volume_property=row.volume_property,
                    volume_type="number",
                    exposed_volume_property="",
                    exposed_volume_type="number",
                    edited_property=row.edited_property,
                ),
                status=row.current_status or "밀려남",
                cafe_name=None,
                search_volume=parse_sheet_int(volume_text),
                volume_found=bool(str(volume_text).strip()),
            )
        except Exception:
            j_errors += 1
    assert j_errors == 0
    assert writer.failures == []


def test_live_neumis_j_column_confirms_if_sheet_is_readable() -> None:
    from urllib.request import Request, urlopen

    from v2r_auto.exposure_sheet import sheet_export_url
    from v2r_auto.sheet_values import looks_like_html, spreadsheet_id_from_url, sheet_gid_from_url

    export = (
        f"{sheet_export_url(spreadsheet_id_from_url(NEUMIS_URL), sheet_gid_from_url(NEUMIS_URL))}"
        "&cache=1"
    )
    request = Request(
        export,
        headers={"User-Agent": "Mozilla/5.0 (compatible; V2R-Exposure-Checker)"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
    except Exception as exc:
        raise AssertionError(f"뉴더미스 시트를 읽지 못했습니다: {exc}") from exc
    text = raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if looks_like_html(text):
        raise AssertionError("뉴더미스 공개 내려받기가 로그인 화면입니다")
    table = parse_sheet_table(text)
    assert len(table) >= 381
    failed: list[int] = []
    for row_number in range(2, len(table) + 1):
        actual = csv_sheet_cell(table, row_number, column_index_from_letter("J"))
        if actual and not sheet_write_confirmed(actual, NEW_STAMP):
            failed.append(row_number)
    assert failed == []
