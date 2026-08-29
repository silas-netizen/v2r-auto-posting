from datetime import datetime
from pathlib import Path
from urllib.error import URLError

from v2r_auto.exposure import ExposureChecker, ExposureRow, STATUS_EXPOSED, STATUS_HIDDEN
from v2r_auto.exposure_sheet import (
    GoogleSheetExposureStore,
    SheetError,
    SheetWrite,
    column_letter,
    now_stamp,
    parse_spreadsheet_ref,
    plan_sheet_writes,
    sheet_export_url,
)


PATSOON_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1OwR_LSjO1ofOojldtSIqoxv0gieNSMx_t35_5G1VTCc/"
    "edit?gid=1325327696#gid=1325327696"
)

SAMPLE_CSV = (
    "카페명,url,발행시간,작성자 아이디,작성자 비밀번호,발행 URL,"
    "노출 상태,키워드,통합검색,최종 편집 일시,키워드 검색량,노출된 검색량,"
    "비고,본문 분류,1~5순위 진입\n"
    "씨씨앙/dtsx,,,,,,밀려남,고농축행감환,,,100,50,,,\n"
    ",,,,,,노출완,코숨핏,,,,,,,\n"
    ",,,,,,,,,,,,,,\n"
)


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
        self.writes: list[tuple[str, str, int, str]] = []

    def write_cell(self, sheet_url: str, column: str, row_number: int, value: str) -> None:
        self.writes.append((sheet_url, column, row_number, value))


def _sheet_row(
    keyword: str = "코숨핏",
    status: str = "밀려남",
    cafe: str = "씨씨앙",
    page_id: str = "2",
) -> ExposureRow:
    return ExposureRow(
        page_id,
        keyword,
        "",
        "",
        status,
        "G",
        "select",
        current_cafe=cafe,
        cafe_property="A",
        cafe_type="select",
        volume_property="K",
        volume_type="number",
        exposed_volume_property="L",
        exposed_volume_type="number",
        edited_property="J",
    )


def test_parse_spreadsheet_ref_from_edit_url() -> None:
    sheet_id, gid = parse_spreadsheet_ref(PATSOON_URL)
    assert sheet_id == "1OwR_LSjO1ofOojldtSIqoxv0gieNSMx_t35_5G1VTCc"
    assert gid == "1325327696"


def test_parse_spreadsheet_ref_fragment_gid() -> None:
    sheet_id, gid = parse_spreadsheet_ref(
        "https://docs.google.com/spreadsheets/d/abc_123/edit#gid=5"
    )
    assert sheet_id == "abc_123"
    assert gid == "5"


def test_parse_spreadsheet_ref_ignores_range_in_fragment() -> None:
    sheet_id, gid = parse_spreadsheet_ref(
        "https://docs.google.com/spreadsheets/d/abc_123/edit#gid=5&range=L141"
    )
    assert sheet_id == "abc_123"
    assert gid == "5"


def test_parse_spreadsheet_ref_defaults_gid() -> None:
    sheet_id, gid = parse_spreadsheet_ref(
        "https://docs.google.com/spreadsheets/d/abc_123/edit"
    )
    assert sheet_id == "abc_123"
    assert gid == "0"


def test_parse_spreadsheet_ref_rejects_other_links() -> None:
    try:
        parse_spreadsheet_ref("https://www.notion.so/example")
    except SheetError as exc:
        assert "구글 시트" in str(exc)
    else:
        raise AssertionError("expected SheetError")


def test_sheet_export_url_matches_csv_download() -> None:
    assert sheet_export_url("abc_123", "987") == (
        "https://docs.google.com/spreadsheets/d/abc_123/export?format=csv&gid=987"
    )


def test_column_letter() -> None:
    assert column_letter(0) == "A"
    assert column_letter(9) == "J"
    assert column_letter(10) == "K"
    assert column_letter(11) == "L"
    assert column_letter(25) == "Z"
    assert column_letter(26) == "AA"


def test_now_stamp_uses_local_clock() -> None:
    assert now_stamp(datetime(2026, 8, 27, 16, 32, 5)) == "2026-08-27 16:32:05"


def test_plan_sheet_writes_exposed_with_volume() -> None:
    writes = plan_sheet_writes(
        _sheet_row(),
        status=STATUS_EXPOSED,
        cafe_name="양평맘",
        search_volume=321,
        volume_found=True,
        edited_at="2026-08-27 16:32:05",
    )
    assert writes == [
        SheetWrite("G", "노출완"),
        SheetWrite("A", "양평맘"),
        SheetWrite("K", "321"),
        SheetWrite("L", "321"),
        SheetWrite("J", "2026-08-27 16:32:05"),
    ]


def test_plan_sheet_writes_hidden_clears_exposed_volume() -> None:
    writes = plan_sheet_writes(
        _sheet_row(status="노출완"),
        status=STATUS_HIDDEN,
        cafe_name=None,
        search_volume=321,
        volume_found=True,
        edited_at="2026-08-27 16:32:05",
    )
    assert writes == [
        SheetWrite("G", "밀려남"),
        SheetWrite("K", "321"),
        SheetWrite("L", ""),
        SheetWrite("J", "2026-08-27 16:32:05"),
    ]


def test_plan_sheet_writes_skips_volume_when_not_found() -> None:
    writes = plan_sheet_writes(
        _sheet_row(),
        status=STATUS_EXPOSED,
        cafe_name=None,
        search_volume=None,
        volume_found=False,
        edited_at="2026-08-27 16:32:05",
    )
    assert writes == [
        SheetWrite("G", "노출완"),
        SheetWrite("J", "2026-08-27 16:32:05"),
    ]


def test_plan_sheet_writes_cafe_name_only() -> None:
    writes = plan_sheet_writes(
        _sheet_row(),
        status=STATUS_EXPOSED,
        cafe_name="씨씨앙/dtsx",
        search_volume=None,
        volume_found=False,
        edited_at="2026-08-27 16:32:05",
        cafe_options=["씨씨앙", "씨씨앙/dtsx", "양평맘"],
    )
    assert SheetWrite("A", "씨씨앙") in writes


def test_store_reads_cafe_header_without_name_suffix() -> None:
    csv_text = (
        "카페,노출 상태,키워드,최종 편집 일시,키워드 검색량,노출된 검색량\n"
        "씨씨앙,밀려남,코숨핏,,100,\n"
    )

    def opener(request, timeout=30):
        return FakeResponse(csv_text.encode("utf-8"))

    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=opener,
    )
    rows = store.load_rows()
    assert rows[0].cafe_property == "A"
    assert rows[0].current_cafe == "씨씨앙"
    assert rows[0].status_property == "B"
    assert rows[0].edited_property == "D"


def test_store_reads_patsoon_headers() -> None:
    calls = []

    def opener(request, timeout=30):
        calls.append(request.full_url)
        return FakeResponse(SAMPLE_CSV.encode("utf-8-sig"))

    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=opener,
    )
    rows = store.load_rows()
    assert "export?format=csv&gid=1325327696" in calls[0]
    assert [row.keyword for row in rows] == ["고농축행감환", "코숨핏"]
    assert rows[0].page_id == "2"
    assert rows[0].current_status == "밀려남"
    assert rows[0].current_cafe == "씨씨앙/dtsx"
    assert rows[0].status_property == "G"
    assert rows[0].cafe_property == "A"
    assert rows[0].volume_property == "K"
    assert rows[0].exposed_volume_property == "L"
    assert rows[0].edited_property == "J"
    assert rows[1].page_id == "3"


def test_store_rejects_missing_required_columns() -> None:
    def opener(request, timeout=30):
        return FakeResponse("이름,메모\n값,값\n".encode("utf-8"))

    store = GoogleSheetExposureStore(
        "https://docs.google.com/spreadsheets/d/abc/edit?gid=0",
        __import__("logging").getLogger("test"),
        opener=opener,
    )
    try:
        store.load_rows()
    except SheetError as exc:
        assert "키워드" in str(exc)
    else:
        raise AssertionError("expected SheetError")


def test_store_falls_back_to_chrome_download(tmp_path: Path) -> None:
    class FakeBrowser:
        def download_sheet(self, sheet_url: str) -> Path:
            path = tmp_path / "sheet.csv"
            path.write_text(SAMPLE_CSV, encoding="utf-8")
            return path

    def opener(request, timeout=30):
        raise URLError("blocked")

    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=opener,
        browser=FakeBrowser(),
    )
    rows = store.load_rows()
    assert len(rows) == 2


def test_store_html_export_uses_browser(tmp_path: Path) -> None:
    class FakeBrowser:
        def download_sheet(self, sheet_url: str) -> Path:
            path = tmp_path / "sheet.csv"
            path.write_text(SAMPLE_CSV, encoding="utf-8")
            return path

    def opener(request, timeout=30):
        return FakeResponse(b"<!DOCTYPE html><html><body>Sign in</body></html>")

    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=opener,
        browser=FakeBrowser(),
    )
    assert [row.keyword for row in store.load_rows()] == ["고농축행감환", "코숨핏"]


def test_store_writes_current_time_and_same_fields() -> None:
    writer = RecordingWriter()
    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(SAMPLE_CSV.encode("utf-8-sig")),
        writer=writer,
        now=lambda: datetime(2026, 8, 27, 16, 32, 5),
    )
    store.update_check_result(
        _sheet_row(),
        status="노출완",
        cafe_name="양평맘",
        search_volume=21000,
        volume_found=True,
    )
    assert writer.writes == [
        (PATSOON_URL, "G", 2, "노출완"),
        (PATSOON_URL, "A", 2, "양평맘"),
        (PATSOON_URL, "K", 2, "21000"),
        (PATSOON_URL, "L", 2, "21000"),
        (PATSOON_URL, "J", 2, "2026-08-27 16:32:05"),
    ]


def test_store_hidden_does_not_touch_cafe() -> None:
    csv_text = (
        "카페명,url,발행시간,작성자 아이디,작성자 비밀번호,발행 URL,"
        "노출 상태,키워드,통합검색,최종 편집 일시,키워드 검색량,노출된 검색량\n"
        "씨씨앙/dtsx,,,,,,노출완,코숨핏,,,100,50\n"
    )
    writer = RecordingWriter()
    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(csv_text.encode("utf-8")),
        writer=writer,
        now=lambda: datetime(2026, 8, 27, 16, 32, 5),
    )
    store.update_check_result(
        _sheet_row(status="노출완"),
        status="밀려남",
        cafe_name=None,
        search_volume=None,
        volume_found=False,
    )
    columns = [item[1] for item in writer.writes]
    assert columns == ["G", "L", "J"]
    assert writer.writes[1] == (PATSOON_URL, "L", 2, "")


def test_store_skips_cells_that_already_match_locale() -> None:
    csv_text = (
        "카페명,url,발행시간,작성자 아이디,작성자 비밀번호,발행 URL,"
        "노출 상태,키워드,통합검색,최종 편집 일시,키워드 검색량,노출된 검색량\n"
        "양평맘,,,,,,밀려남,코숨핏,,2026-08-29 7:33:10,\"5,760\",\n"
    )
    writer = RecordingWriter()
    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(csv_text.encode("utf-8")),
        writer=writer,
        now=lambda: datetime(2026, 8, 29, 7, 33, 10),
    )
    store.update_check_result(
        _sheet_row(status="밀려남", cafe="양평맘"),
        status="밀려남",
        cafe_name=None,
        search_volume=5760,
        volume_found=True,
    )
    assert writer.writes == []


def test_store_does_not_fail_row_when_only_j_time_cannot_be_verified() -> None:
    csv_text = (
        "카페명,url,발행시간,작성자 아이디,작성자 비밀번호,발행 URL,"
        "노출 상태,키워드,통합검색,최종 편집 일시,키워드 검색량,노출된 검색량\n"
        "씨씨앙/dtsx,,,,,,밀려남,임산부 항문 가려움,,2026-08-29 7:52:45,210,\n"
    )

    class StaleJWriter(RecordingWriter):
        def write_cell(self, sheet_url: str, column: str, row_number: int, value: str) -> None:
            if column == "J":
                raise RuntimeError(
                    "시트 J3 저장값을 다시 확인하지 못했습니다 "
                    "(기대 2026-08-29 11:39:20 / 실제 2026-08-29 7:52:45)"
                )
            super().write_cell(sheet_url, column, row_number, value)

    writer = StaleJWriter()
    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(csv_text.encode("utf-8")),
        writer=writer,
        now=lambda: datetime(2026, 8, 29, 11, 39, 20),
    )
    store.update_check_result(
        _sheet_row(keyword="임산부 항문 가려움", status="밀려남", page_id="2"),
        status="밀려남",
        cafe_name=None,
        search_volume=210,
        volume_found=True,
    )
    assert writer.writes == []


def test_store_keeps_writing_other_cells_when_one_column_fails() -> None:
    class BoomWriter(RecordingWriter):
        def write_cell(self, sheet_url: str, column: str, row_number: int, value: str) -> None:
            if column == "L":
                raise RuntimeError("locked")
            super().write_cell(sheet_url, column, row_number, value)

    csv_text = (
        "카페명,url,발행시간,작성자 아이디,작성자 비밀번호,발행 URL,"
        "노출 상태,키워드,통합검색,최종 편집 일시,키워드 검색량,노출된 검색량\n"
        "씨씨앙/dtsx,,,,,,노출완,코숨핏,,,100,50\n"
    )
    writer = BoomWriter()
    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(csv_text.encode("utf-8")),
        writer=writer,
        now=lambda: datetime(2026, 8, 27, 16, 32, 5),
    )
    try:
        store.update_check_result(
            _sheet_row(status="노출완"),
            status="밀려남",
            cafe_name=None,
            search_volume=100,
            volume_found=True,
        )
    except SheetError as exc:
        assert "L2" in str(exc)
    else:
        raise AssertionError("expected SheetError")
    columns = [item[1] for item in writer.writes]
    assert columns == ["G", "J"]


def test_volume_totals_use_written_values_when_csv_is_stale() -> None:
    csv_text = (
        "카페,노출 상태,키워드,최종 편집 일시,키워드 검색량,노출된 검색량\n"
        "씨씨앙,노출완,항문농양,,5760,5800\n"
    )
    writer = RecordingWriter()
    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(csv_text.encode("utf-8")),
        writer=writer,
        now=lambda: datetime(2026, 8, 27, 16, 32, 5),
    )
    store.load_rows()
    store.update_check_result(
        ExposureRow(
            "2",
            "항문농양",
            "",
            "",
            "노출완",
            "B",
            "select",
            current_cafe="씨씨앙",
            cafe_property="A",
            cafe_type="select",
            volume_property="E",
            volume_type="number",
            exposed_volume_property="F",
            exposed_volume_type="number",
            edited_property="D",
        ),
        status="밀려남",
        cafe_name=None,
        search_volume=5760,
        volume_found=True,
    )
    store.write_volume_totals()
    assert (PATSOON_URL, "F", 2, "") in writer.writes
    assert (PATSOON_URL, "P", 1, "5760") in writer.writes
    assert (PATSOON_URL, "Q", 1, "0") in writer.writes


def test_store_skips_volume_totals_when_p1_q1_already_match() -> None:
    header = [
        "카페",
        "노출 상태",
        "키워드",
        "최종 편집 일시",
        "키워드 검색량",
        "노출된 검색량",
        *([""] * 9),
        "1,290",
        "290",
    ]
    csv_text = (
        ",".join(f'"{item}"' if item else "" for item in header)
        + "\n"
        + '씨씨앙,밀려남,코숨핏,,"1,000",0\n'
        + "양평맘,노출완,항문세정제,,290,290\n"
        + "씨씨앙,밀려남,치질,,,\n"
    )
    writer = RecordingWriter()
    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(csv_text.encode("utf-8")),
        writer=writer,
    )
    store.load_rows()
    store.write_volume_totals()
    assert writer.writes == []


def test_store_writes_volume_totals_to_p1_and_q1() -> None:
    csv_text = (
        "카페,노출 상태,키워드,최종 편집 일시,키워드 검색량,노출된 검색량\n"
        '씨씨앙,밀려남,코숨핏,,"1,000",0\n'
        "양평맘,노출완,항문세정제,,290,290\n"
        "씨씨앙,밀려남,치질,,,\n"
    )
    writer = RecordingWriter()

    def opener(request, timeout=30):
        return FakeResponse(csv_text.encode("utf-8"))

    store = GoogleSheetExposureStore(
        PATSOON_URL,
        __import__("logging").getLogger("test"),
        opener=opener,
        writer=writer,
    )
    store.load_rows()
    store.write_volume_totals()
    assert writer.writes == [
        (PATSOON_URL, "P", 1, "1290"),
        (PATSOON_URL, "Q", 1, "290"),
    ]


def test_checker_keeps_running_when_one_sheet_write_fails() -> None:
    class FakeSheet:
        label = "구글 시트"

        def __init__(self):
            self.calls = 0

        def update_check_result(self, row, **_kwargs):
            self.calls += 1
            if row.keyword == "항문농양":
                raise RuntimeError("시트 L141 저장에 3회 실패했습니다")

        def write_volume_totals(self) -> None:
            self.calls += 100

    class FakeNaver:
        def search_integrated(self, keyword: str) -> str:
            return "<div id='main_pack'></div>"

        def open_post_text(self, url: str) -> str:
            return ""

    store = FakeSheet()
    ExposureChecker(
        store,
        FakeNaver(),
        __import__("logging").getLogger("v2r_auto.exposure"),
        delay_seconds=0,
    ).run(
        [
            _sheet_row(keyword="항문농양", page_id="141"),
            _sheet_row(keyword="코숨핏", page_id="142"),
        ],
        dry_run=False,
    )
    assert store.calls == 102


def test_checker_writes_volume_totals_after_run() -> None:
    class FakeSheet:
        label = "구글 시트"

        def __init__(self):
            self.totals = 0

        def update_check_result(self, *_args, **_kwargs):
            return None

        def write_volume_totals(self) -> None:
            self.totals += 1

    class FakeNaver:
        def search_integrated(self, keyword: str) -> str:
            return "<div id='main_pack'></div>"

        def open_post_text(self, url: str) -> str:
            return ""

    store = FakeSheet()
    ExposureChecker(
        store,
        FakeNaver(),
        __import__("logging").getLogger("v2r_auto.exposure"),
        delay_seconds=0,
    ).run([_sheet_row()], dry_run=False)
    assert store.totals == 1


def test_checker_finishes_when_volume_totals_fail() -> None:
    class FakeSheet:
        label = "구글 시트"

        def update_check_result(self, *_args, **_kwargs):
            return None

        def write_volume_totals(self) -> None:
            raise RuntimeError("P1 locked")

    class FakeNaver:
        def search_integrated(self, keyword: str) -> str:
            return "<div id='main_pack'></div>"

        def open_post_text(self, url: str) -> str:
            return ""

    ExposureChecker(
        FakeSheet(),
        FakeNaver(),
        __import__("logging").getLogger("v2r_auto.exposure"),
        delay_seconds=0,
    ).run([_sheet_row()], dry_run=False)


def test_checker_skips_volume_totals_in_dry_run() -> None:
    class FakeSheet:
        label = "구글 시트"

        def update_check_result(self, *_args, **_kwargs):
            raise AssertionError("dry-run must not write")

        def write_volume_totals(self) -> None:
            raise AssertionError("dry-run must not write totals")

    class FakeNaver:
        def search_integrated(self, keyword: str) -> str:
            return "<div id='main_pack'></div>"

        def open_post_text(self, url: str) -> str:
            return ""

    ExposureChecker(
        FakeSheet(),
        FakeNaver(),
        __import__("logging").getLogger("v2r_auto.exposure"),
        delay_seconds=0,
    ).run([_sheet_row()], dry_run=True)


def test_checker_uses_sheet_label_in_dry_run(caplog) -> None:
    class FakeSheet:
        label = "구글 시트"

        def update_check_result(self, *_args, **_kwargs):
            raise AssertionError("dry-run must not write")

    class FakeNaver:
        def search_integrated(self, keyword: str) -> str:
            return "<div id='main_pack'></div>"

        def open_post_text(self, url: str) -> str:
            return ""

    with caplog.at_level("INFO"):
        ExposureChecker(
            FakeSheet(),
            FakeNaver(),
            __import__("logging").getLogger("v2r_auto.exposure"),
            delay_seconds=0,
        ).run([_sheet_row()], dry_run=True)
    assert any("구글 시트에 쓰지 않음" in message for message in caplog.messages)
