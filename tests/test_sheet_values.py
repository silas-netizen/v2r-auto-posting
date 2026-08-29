import unicodedata

from v2r_auto.sheet_values import (
    apply_sheet_overlay,
    csv_sheet_cell,
    looks_like_html,
    parse_sheet_datetime,
    parse_sheet_int,
    parse_sheet_number,
    sheet_cell_values_match,
    sheet_csv_export_url,
    sheet_gid_from_url,
    sheet_write_confirmed,
)


def test_csv_sheet_cell_missing_column_is_empty() -> None:
    rows = [["카페", "노출 상태", "키워드", "키워드 검색량"], ["양평맘", "밀려남", "항문농양", "5,760"]]
    assert csv_sheet_cell(rows, 2, 4) == ""
    assert csv_sheet_cell(rows, 2, 3) == "5,760"
    assert sheet_cell_values_match(csv_sheet_cell(rows, 2, 4), "")


def test_empty_does_not_match_old_exposed_volume() -> None:
    assert not sheet_cell_values_match("5,800", "")
    assert not sheet_cell_values_match("0", "")
    assert sheet_cell_values_match("5,760", "5760")
    assert sheet_cell_values_match("5760.0", "5760")


def test_sheet_cell_values_match_j_hour_without_leading_zero() -> None:
    assert sheet_cell_values_match("2026-08-29 7:33:10", "2026-08-29 07:33:10")


def test_sheet_cell_values_match_korean_locale_datetime() -> None:
    assert sheet_cell_values_match(
        "2026. 8. 29 오전 7:33:10",
        "2026-08-29 07:33:10",
    )
    assert sheet_cell_values_match(
        "2026. 8. 29 오후 7:33:10",
        "2026-08-29 19:33:10",
    )


def test_sheet_cell_values_match_thousands_separators() -> None:
    assert sheet_cell_values_match("1,000", "1000")
    assert sheet_cell_values_match("101350", "101350")
    assert sheet_cell_values_match("５，７６０", "5760")


def test_sheet_cell_values_match_nbsp_apostrophe_and_zero_width() -> None:
    assert sheet_cell_values_match("밀려남\u00a0", "밀려남")
    assert sheet_cell_values_match("'노출완", "노출완")
    assert sheet_cell_values_match("밀려\u200b남", "밀려남")


def test_sheet_cell_values_match_compact_status() -> None:
    assert sheet_cell_values_match("노출 완", "노출완")
    assert sheet_cell_values_match("밀려 남", "밀려남")


def test_sheet_cell_values_match_nfd_hangul() -> None:
    assert sheet_cell_values_match(unicodedata.normalize("NFD", "밀려남"), "밀려남")


def test_sheet_cell_values_match_hyperlink_and_quoted_formula() -> None:
    assert sheet_cell_values_match(
        '=HYPERLINK("https://search.naver.com/search.naver?query=코숨핏")',
        "https://search.naver.com/search.naver?query=코숨핏",
    )
    assert sheet_cell_values_match('="5760"', "5,760")


def test_sheet_cell_values_do_not_match_different_times() -> None:
    assert not sheet_cell_values_match("2026-08-29 7:33:10", "2026-08-29 07:33:11")


def test_sheet_write_confirmed_accepts_stale_j_export() -> None:
    assert sheet_write_confirmed("2026-08-29 7:52:45", "2026-08-29 11:39:20")
    assert sheet_write_confirmed("2026-08-29 7:52:59", "2026-08-29 11:40:27")
    assert not sheet_write_confirmed("5,800", "")
    assert sheet_write_confirmed("5,800", "", typed_ok=True)
    assert not sheet_write_confirmed("100", "200")


def test_parse_sheet_int_blank_is_zero() -> None:
    assert parse_sheet_int("") == 0
    assert parse_sheet_int("1,000") == 1000
    assert parse_sheet_int("-") == 0
    assert parse_sheet_number("2026-08-29 7:33:10") is None


def test_parse_sheet_datetime_keeps_seconds() -> None:
    value = parse_sheet_datetime("2026-08-29 7:33:10")
    assert value is not None
    assert (value.year, value.month, value.day, value.hour, value.minute, value.second) == (
        2026,
        8,
        29,
        7,
        33,
        10,
    )


def test_looks_like_html_login_page() -> None:
    assert looks_like_html("<!DOCTYPE html><html><body>Sign in</body></html>")
    assert not looks_like_html("카페,노출 상태\n양평맘,밀려남\n")


def test_sheet_gid_ignores_range_in_fragment() -> None:
    url = (
        "https://docs.google.com/spreadsheets/d/abc_123/edit"
        "?gid=987#gid=987&range=L141"
    )
    assert sheet_gid_from_url(url) == "987"
    assert sheet_csv_export_url(url) == (
        "https://docs.google.com/spreadsheets/d/abc_123/export?format=csv&gid=987"
    )


def test_apply_sheet_overlay_clears_stale_trailing_column() -> None:
    table = [
        ["카페", "노출 상태", "키워드", "키워드 검색량", "노출된 검색량"],
        ["양평맘", "밀려남", "항문농양", "5,760", "5,800"],
    ]
    apply_sheet_overlay(table, {(2, 4): ""})
    assert csv_sheet_cell(table, 2, 4) == ""
    assert sheet_cell_values_match(csv_sheet_cell(table, 2, 3), "5760")
