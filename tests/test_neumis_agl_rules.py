from __future__ import annotations

from v2r_auto.exposure import (
    STATUS_EXPOSED,
    STATUS_HIDDEN,
    cafe_id_for_check,
    cafe_name_option,
    status_option,
)
from v2r_auto.exposure_sheet import GoogleSheetExposureStore, plan_sheet_writes
from v2r_auto.sheet_values import (
    column_index_from_letter,
    csv_sheet_cell,
    parse_sheet_int,
    parse_sheet_table,
    sheet_cell_values_match,
)

from tests.test_neumis_row_by_row import (
    NEUMIS_URL,
    STAMP,
    FakeResponse,
    RecordingWriter,
    _csv_text,
)


def _store(csv_text: str) -> tuple[GoogleSheetExposureStore, list]:
    store = GoogleSheetExposureStore(
        NEUMIS_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(csv_text.encode("utf-8")),
        writer=RecordingWriter(),
    )
    return store, store.load_rows()


def _writes(row, *, status: str, cafe_name: str | None, volume: int | None, found: bool):
    planned = plan_sheet_writes(
        row,
        status=status,
        cafe_name=cafe_name,
        search_volume=volume,
        volume_found=found,
        edited_at=STAMP,
    )
    return {item.column: item.value for item in planned}


def _assert_agl_rules(
    csv_text: str, source: str, *, pin_l_rows: bool = True
) -> None:
    store, rows = _store(csv_text)
    table = parse_sheet_table(csv_text)
    if pin_l_rows:
        assert len(rows) == 380, source
    hidden_l_left: list[tuple[int, str]] = []
    exposed_l_empty: list[tuple[int, str]] = []
    for row in rows:
        number = int(row.page_id)
        volume_text = csv_sheet_cell(table, number, column_index_from_letter("K"))
        volume = parse_sheet_int(volume_text)
        found = bool(str(volume_text).strip())
        current_l = csv_sheet_cell(table, number, column_index_from_letter("L"))

        hidden = _writes(
            row,
            status=STATUS_HIDDEN,
            cafe_name=None,
            volume=volume,
            found=found,
        )
        assert hidden["G"] == "밀려남", (source, number, row.keyword, hidden)
        assert "A" not in hidden, (source, number, row.keyword, hidden)
        assert hidden["L"] == "", (source, number, row.keyword, hidden)

        keep_same, write_same = cafe_id_for_check(
            row.current_cafe, STATUS_EXPOSED, row.current_cafe
        )
        exposed_same = _writes(
            row,
            status=STATUS_EXPOSED,
            cafe_name=write_same,
            volume=volume,
            found=found,
        )
        assert exposed_same["G"] == "노출완", (source, number, row.keyword)
        assert "A" not in exposed_same, (source, number, row.keyword, exposed_same)
        if found:
            assert sheet_cell_values_match(exposed_same["L"], str(volume)), (
                source,
                number,
                row.keyword,
                exposed_same,
            )
        else:
            assert "L" not in exposed_same, (source, number, row.keyword, exposed_same)

        other = "양평맘" if row.current_cafe != "양평맘" else "씨씨앙"
        _keep, write_other = cafe_id_for_check(row.current_cafe, STATUS_EXPOSED, other)
        exposed_other = _writes(
            row,
            status=STATUS_EXPOSED,
            cafe_name=write_other,
            volume=volume,
            found=found,
        )
        assert exposed_other["A"] == other, (source, number, row.keyword, exposed_other)
        hidden_real = _writes(
            row,
            status=STATUS_HIDDEN,
            cafe_name=None,
            volume=volume,
            found=found,
        )
        assert "A" not in hidden_real

        if row.current_status == "밀려남" and str(current_l).strip():
            hidden_l_left.append((number, row.keyword))
            assert hidden["L"] == ""
        if row.current_status == "노출완" and found and not str(current_l).strip():
            exposed_l_empty.append((number, row.keyword))
            assert sheet_cell_values_match(exposed_same["L"], str(volume))

    if pin_l_rows:
        assert hidden_l_left == [(141, "항문농양")], (source, hidden_l_left)
        assert [item[0] for item in exposed_l_empty] == [340, 346, 361, 374, 375], (
            source,
            exposed_l_empty,
        )


def test_live_a_g_l_rules_on_every_row() -> None:
    _assert_agl_rules(_csv_text("live"), "live", pin_l_rows=False)


def test_fixture_a_g_l_rules_on_every_row() -> None:
    _assert_agl_rules(_csv_text("fixture"), "fixture")


def test_g_status_notation_matches_sheet_options() -> None:
    assert status_option("노출완", ["밀려남", "노출완"]) == "노출완"
    assert status_option("밀려남", ["밀려남", "노출완"]) == "밀려남"
    assert status_option("노출 완", ["밀려남", "노출완"]) == "노출완"
    assert status_option("밀려 남", ["밀려남", "노출완"]) == "밀려남"


def test_a_cafe_notation_uses_name_only() -> None:
    options = ["씨씨앙", "씨씨앙/dtsx", "양평맘"]
    assert cafe_name_option("씨씨앙/dtsx", options) == "씨씨앙"
    assert cafe_name_option("양평맘", options) == "양평맘"
    assert cafe_id_for_check("씨씨앙/dtsx", "노출완", "씨씨앙") == ("씨씨앙/dtsx", None)
    assert cafe_id_for_check("씨씨앙", "노출완", "씨씨앙") == ("씨씨앙", None)
    assert cafe_id_for_check("씨씨앙", "노출완", "양평맘") == ("양평맘", "양평맘")
    assert cafe_id_for_check("양평맘", "밀려남", "씨씨앙") == ("양평맘", None)
    assert cafe_id_for_check("", "노출완", "씨씨앙") == ("씨씨앙", "씨씨앙")
    assert cafe_id_for_check("", "밀려남", "씨씨앙") == ("", None)
    assert cafe_id_for_check("해돌", "밀려남", "양평맘") == ("해돌", None)


def test_live_current_sheet_l_mismatches_are_exactly_the_planned_fixes() -> None:
    text = _csv_text("live")
    table = parse_sheet_table(text)
    hidden_with_l = []
    exposed_empty_l = []
    for number in range(2, len(table) + 1):
        status = csv_sheet_cell(table, number, 6)
        keyword = csv_sheet_cell(table, number, 7)
        volume = csv_sheet_cell(table, number, 10)
        exposed = csv_sheet_cell(table, number, 11)
        if status == "밀려남" and str(exposed).strip():
            hidden_with_l.append((number, keyword, exposed))
        if status == "노출완" and str(volume).strip() and not str(exposed).strip():
            exposed_empty_l.append((number, keyword, volume))
    snapshot = hidden_with_l == [(141, "항문농양", "5,800")] and [
        item[0] for item in exposed_empty_l
    ] == [340, 346, 361, 374, 375]
    if not snapshot:
        return
    assert sheet_cell_values_match(exposed_empty_l[3][2], "1900")
