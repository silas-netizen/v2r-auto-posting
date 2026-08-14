from pathlib import Path

import pytest

from v2r_auto.browser import V2RBrowser
from v2r_auto.join_marker import (
    MARK_VALUE,
    JoinMarkerError,
    build_plan,
    column_letter,
    extract_joined_ids,
    load_account_rows,
    plan_matches_sheet,
)


def write_csv(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "accounts.csv"
    path.write_text(content, encoding="utf-8-sig")
    return path


def test_column_letter() -> None:
    assert column_letter(0) == "A"
    assert column_letter(1) == "B"
    assert column_letter(11) == "L"
    assert column_letter(12) == "M"
    assert column_letter(25) == "Z"
    assert column_letter(26) == "AA"


def test_extract_joined_ids_skips_dropped_accounts() -> None:
    payload = {
        "naver_join_cafe": [
            {"login_id": "quilliant"},
            {"login_id": "gone", "force_drop": True},
            {"naver_login_id": "hunnede", "stop_cafe_member": True},
            {"login_id": "  ColPith  "},
        ]
    }

    assert extract_joined_ids(payload) == {"quilliant", "colpith"}


def test_plan_marks_only_rows_with_matching_ids(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "번호,ID,김천kb보험,씨씨앙,양평맘\n"
        "1,dmmgit530,노출,,\n"
        "2,quilliant,,,\n"
        "3,,,,\n"
        "4,hunnede,,이미값,\n",
    )
    headers, rows = load_account_rows(path)
    plan = build_plan(
        headers,
        rows,
        {"씨씨앙": {"quilliant", "hunnede"}, "양평맘": {"hunnede"}},
    )

    assert plan.id_header == "ID"
    assert plan.marked_count("씨씨앙") == 2
    assert plan.marked_count("양평맘") == 1
    assert plan.both_count() == 1
    assert plan.rows[0]["씨씨앙"] == ""
    assert plan.rows[0]["김천kb보험"] == "노출"
    assert plan.rows[1]["씨씨앙"] == MARK_VALUE
    assert plan.rows[1]["양평맘"] == ""
    assert plan.rows[2]["ID"] == ""
    assert plan.rows[2]["씨씨앙"] == ""
    assert plan.rows[2]["양평맘"] == ""
    assert plan.rows[3]["씨씨앙"] == MARK_VALUE
    assert plan.rows[3]["양평맘"] == MARK_VALUE


def test_empty_id_is_never_marked_even_if_membership_has_blank() -> None:
    plan = build_plan(
        ["ID", "씨씨앙", "양평맘"],
        [{"__row": "2", "ID": "  ", "씨씨앙": "가입", "양평맘": "가입"}],
        {"씨씨앙": {""}, "양평맘": {""}},
    )

    assert plan.rows[0]["씨씨앙"] == ""
    assert plan.rows[0]["양평맘"] == ""


def test_plan_is_case_insensitive() -> None:
    plan = build_plan(
        ["ID", "씨씨앙", "양평맘"],
        [{"__row": "2", "ID": "QuilLiant", "씨씨앙": "", "양평맘": ""}],
        {"씨씨앙": {"quilliant"}, "양평맘": set()},
    )

    assert plan.rows[0]["씨씨앙"] == MARK_VALUE


def test_missing_cafe_column_raises() -> None:
    with pytest.raises(JoinMarkerError, match="씨씨앙"):
        build_plan(
            ["ID", "양평맘"],
            [{"__row": "2", "ID": "a", "양평맘": ""}],
            {"씨씨앙": {"a"}, "양평맘": set()},
        )


def test_tsv_keeps_row_alignment_and_clears_empty_ids() -> None:
    plan = build_plan(
        ["번호", "ID", "김천kb보험", "씨씨앙", "양평맘"],
        [
            {"__row": "2", "번호": "1", "ID": "", "김천kb보험": "", "씨씨앙": "가입", "양평맘": "가입"},
            {"__row": "3", "번호": "2", "ID": "quilliant", "김천kb보험": "노출", "씨씨앙": "", "양평맘": ""},
        ],
        {"씨씨앙": {"quilliant"}, "양평맘": set()},
    )

    tsv = plan.tsv_for_headers(["씨씨앙", "양평맘"])
    assert tsv == "\t\n가입\t\n"
    assert plan.start_cell("씨씨앙") == ("D", 2)
    assert plan.contiguous_cafe_groups() == [["씨씨앙", "양평맘"]]
    assert plan.rows[1]["김천kb보험"] == "노출"


def test_non_adjacent_cafe_columns_are_separate_pastes() -> None:
    plan = build_plan(
        ["ID", "씨씨앙", "메모", "양평맘"],
        [{"__row": "2", "ID": "a", "씨씨앙": "", "메모": "x", "양평맘": ""}],
        {"씨씨앙": {"a"}, "양평맘": {"a"}},
    )

    assert plan.contiguous_cafe_groups() == [["씨씨앙"], ["양평맘"]]
    assert plan.start_cell("양평맘") == ("D", 2)


def test_plan_matches_sheet_reports_wrong_empty_id_mark() -> None:
    plan = build_plan(
        ["ID", "씨씨앙", "양평맘"],
        [{"__row": "2", "ID": "", "씨씨앙": "", "양평맘": ""}],
        {"씨씨앙": {"a"}, "양평맘": set()},
    )
    errors = plan_matches_sheet(
        ["ID", "씨씨앙", "양평맘"],
        [{"ID": "", "씨씨앙": "가입", "양평맘": ""}],
        plan,
    )

    assert errors
    assert "씨씨앙" in errors[0]


def test_sheet_range_url_starts_at_requested_cell() -> None:
    url = V2RBrowser._sheet_range_url(
        "https://docs.google.com/spreadsheets/d/abc_123/edit?gid=9#gid=9",
        "L",
        2,
    )
    assert url.endswith("#gid=9&range=L2")
    assert "abc_123" in url
