from datetime import datetime
from pathlib import Path

from v2r_auto.exposure_sheet import GoogleSheetExposureStore
from v2r_auto.sheet_values import (
    column_index_from_letter,
    csv_sheet_cell,
    parse_sheet_datetime,
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
NEW_STAMP = "2026-08-29 20:35:37"


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class RecordingWriter:
    def __init__(self):
        self.writes: list[tuple[str, int, str]] = []

    def write_cell(
        self,
        sheet_url: str,
        column: str,
        row_number: int,
        value: str,
        **_kwargs,
    ) -> int:
        self.writes.append((column, row_number, value))
        return row_number


def _fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_neumis_old_j_is_not_the_new_time() -> None:
    table = parse_sheet_table(_fixture_text())
    wrongly_accepted: list[tuple[int, str]] = []
    for row_number in range(2, len(table) + 1):
        actual = csv_sheet_cell(table, row_number, column_index_from_letter("J"))
        if sheet_write_confirmed(actual, NEW_STAMP):
            wrongly_accepted.append((row_number, actual))
    assert wrongly_accepted == []
    assert len(table) == 381


def test_neumis_logged_j4_j5_need_the_cell_itself() -> None:
    table = parse_sheet_table(_fixture_text())
    assert csv_sheet_cell(table, 4, 7) == "치질수술 병원"
    assert csv_sheet_cell(table, 5, 7) == "치질수술후재발"
    assert csv_sheet_cell(table, 4, 9) == "2026-08-29 7:52:59"
    assert csv_sheet_cell(table, 5, 9) == "2026-08-29 7:53:12"
    assert not sheet_write_confirmed(csv_sheet_cell(table, 4, 9), NEW_STAMP)
    assert not sheet_write_confirmed(csv_sheet_cell(table, 5, 9), NEW_STAMP)
    assert sheet_write_confirmed(
        csv_sheet_cell(table, 4, 9),
        NEW_STAMP,
        ui_value=NEW_STAMP,
    )


def test_neumis_every_row_writes_j_when_time_changed() -> None:
    csv_text = _fixture_text()
    table = parse_sheet_table(csv_text)
    writer = RecordingWriter()
    store = GoogleSheetExposureStore(
        NEUMIS_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(csv_text.encode("utf-8")),
        writer=writer,
        now=lambda: datetime(2026, 8, 29, 20, 35, 37),
    )
    rows = store.load_rows()
    assert len(rows) == 380
    for row in rows:
        number = int(row.page_id)
        volume_text = csv_sheet_cell(table, number, column_index_from_letter("K"))
        store.update_check_result(
            row,
            status=row.current_status or "밀려남",
            cafe_name=None,
            search_volume=parse_sheet_int(volume_text),
            volume_found=bool(str(volume_text).strip()),
        )
    j_writes = [item for item in writer.writes if item[0] == "J"]
    assert len(j_writes) == 380
    assert all(value == NEW_STAMP for _column, _row, value in j_writes)
    assert ("L", 141, "") in writer.writes


def test_live_neumis_existing_stamp_is_not_future_write() -> None:
    from urllib.request import Request, urlopen

    from v2r_auto.exposure_sheet import sheet_export_url
    from v2r_auto.sheet_values import looks_like_html, sheet_gid_from_url, spreadsheet_id_from_url

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
    found: tuple[str, int] | None = None
    for row_number in range(2, len(table) + 1):
        keyword = csv_sheet_cell(table, row_number, 7)
        stamp = csv_sheet_cell(table, row_number, 9)
        volume = parse_sheet_int(csv_sheet_cell(table, row_number, 10))
        if (
            keyword.strip()
            and parse_sheet_datetime(stamp) is not None
            and volume is not None
            and volume > 0
        ):
            found = (stamp, volume)
            break
    assert found is not None
    actual, volume = found
    assert not sheet_write_confirmed(actual, "2099-01-01 00:00:00")
    assert volume > 0
