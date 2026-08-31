import unicodedata

from v2r_auto.sheet_values import (
    apply_sheet_overlay,
    csv_sheet_cell,
    format_sheet_locale_datetime,
    looks_like_html,
    nearby_sheet_row_numbers,
    parse_sheet_datetime,
    parse_sheet_int,
    parse_sheet_number,
    pick_nearby_keyword_row,
    pick_sheet_typing_value,
    sheet_cell_values_match,
    sheet_csv_export_url,
    sheet_gid_from_url,
    sheet_keywords_match,
    sheet_typing_variants,
    sheet_value_needs_keystrokes,
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


def test_sheet_write_confirmed_needs_the_new_time() -> None:
    assert not sheet_write_confirmed("2026-08-29 7:52:45", "2026-08-29 20:35:37")
    assert not sheet_write_confirmed("2026-08-29 7:52:59", "2026-08-29 20:35:49")
    assert sheet_write_confirmed(
        "2026-08-29 7:52:45",
        "2026-08-29 20:35:37",
        ui_value="2026-08-29 20:35:37",
    )
    assert sheet_write_confirmed("2026-08-29 8:35:37", "2026-08-29 08:35:37")
    assert not sheet_write_confirmed("5,800", "")
    assert sheet_write_confirmed("5,800", "", ui_value="")
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


def test_sheet_value_needs_keystrokes_for_dates_and_numbers() -> None:
    assert sheet_value_needs_keystrokes("2026-09-01 01:43:13")
    assert sheet_value_needs_keystrokes("6700")
    assert sheet_value_needs_keystrokes("1,900")
    assert not sheet_value_needs_keystrokes("밀려남")
    assert not sheet_value_needs_keystrokes("")


def test_format_sheet_locale_datetime_matches_korean_sheets() -> None:
    assert format_sheet_locale_datetime("2026-09-01 01:43:13") == "2026. 9. 1 오전 1:43:13"
    assert format_sheet_locale_datetime("2026-08-30 12:16:34") == "2026. 8. 30 오후 12:16:34"
    assert format_sheet_locale_datetime("2026-08-31 23:59:20") == "2026. 8. 31 오후 11:59:20"
    assert format_sheet_locale_datetime("2026-09-01 00:05:00") == "2026. 9. 1 오전 12:05:00"
    assert sheet_cell_values_match("2026. 9. 1 오전 1:43:13", "2026-09-01 01:43:13")
    assert not sheet_cell_values_match("2026. 8. 30 오후 12:16:34", "2026-09-01 01:43:13")
    assert not sheet_cell_values_match("6,760", "6700")


def test_sheet_typing_variants_try_locale_then_text() -> None:
    assert sheet_typing_variants("밀려남") == ["밀려남"]
    assert sheet_typing_variants("6700") == ["6700"]
    assert sheet_typing_variants("2026-09-01 01:43:13") == [
        "2026-09-01 01:43:13",
        "2026. 9. 1 오전 1:43:13",
        "'2026-09-01 01:43:13",
    ]
    assert pick_sheet_typing_value("2026-09-01 01:43:13", 1) == "2026-09-01 01:43:13"
    assert pick_sheet_typing_value("2026-09-01 01:43:13", 2) == "2026. 9. 1 오전 1:43:13"
    assert pick_sheet_typing_value("2026-09-01 01:43:13", 3) == "'2026-09-01 01:43:13"


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


def test_sheet_keywords_match_ignores_spaces() -> None:
    assert sheet_keywords_match("원포 얼리", "원포얼리")
    assert sheet_keywords_match(" 원포 얼리 ", "원포 얼리")
    assert not sheet_keywords_match("원포", "원포 얼리")
    assert not sheet_keywords_match("", "원포 얼리")


def test_nearby_sheet_row_numbers_check_remembered_first() -> None:
    assert nearby_sheet_row_numbers(1386, span=2) == [1386, 1385, 1387, 1384, 1388]
    assert nearby_sheet_row_numbers(2, span=2) == [2, 3, 4]
    assert nearby_sheet_row_numbers(1386)[0] == 1386
    assert nearby_sheet_row_numbers(1386)[-1] == 1391
    assert 1381 in nearby_sheet_row_numbers(1386)


def test_pick_nearby_keyword_row_follows_one_row_shift() -> None:
    assert (
        pick_nearby_keyword_row(
            {1386: "연세사랑모아여성병원", 1385: "원포 얼리", 1387: "일산차병원"},
            "원포 얼리",
            1386,
        )
        == 1385
    )


def test_pick_nearby_keyword_row_keeps_matching_row() -> None:
    assert (
        pick_nearby_keyword_row({2: "고농축행감환", 3: "코숨핏"}, "고농축행감환", 2)
        == 2
    )


def test_pick_nearby_keyword_row_refuses_when_not_nearby() -> None:
    try:
        pick_nearby_keyword_row(
            {1386: "연세사랑모아여성병원", 1385: "일산차병원"},
            "원포 얼리",
            1386,
            span=2,
        )
    except ValueError as exc:
        assert "원포 얼리" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_apply_sheet_overlay_clears_stale_trailing_column() -> None:
    table = [
        ["카페", "노출 상태", "키워드", "키워드 검색량", "노출된 검색량"],
        ["양평맘", "밀려남", "항문농양", "5,760", "5,800"],
    ]
    apply_sheet_overlay(table, {(2, 4): ""})
    assert csv_sheet_cell(table, 2, 4) == ""
    assert sheet_cell_values_match(csv_sheet_cell(table, 2, 3), "5760")
