from v2r_auto.sheet_values import (
    parse_sheet_datetime,
    parse_sheet_int,
    sheet_cell_values_match,
)


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


def test_sheet_cell_values_do_not_match_different_times() -> None:
    assert not sheet_cell_values_match("2026-08-29 7:33:10", "2026-08-29 07:33:11")


def test_parse_sheet_int_blank_is_zero() -> None:
    assert parse_sheet_int("") == 0
    assert parse_sheet_int("1,000") == 1000


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
