from pathlib import Path

import pytest

from v2r_auto.browser import V2RBrowser
from v2r_auto.join_marker import (
    MARK_VALUE,
    JoinMarkerError,
    build_plan,
    clean_cell,
    column_letter,
    extract_joined_ids,
    format_cafe_formula_error,
    format_join_write_failure,
    format_locked_sheet_error,
    load_account_rows,
    mismatches_look_like_missed_paste,
    paste_chunk_end_row,
    plan_matches_sheet,
    plan_mismatch_cells,
    require_membership,
    should_split_failed_chunk,
    user_facing_join_error,
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
    assert "기대" in errors[0]
    assert "실제" in errors[0]


def test_paste_chunks_keep_sheet_row_numbers() -> None:
    rows = [
        {"__row": str(index), "ID": f"id{index}", "씨씨앙": "", "양평맘": ""}
        for index in range(2, 8)
    ]
    plan = build_plan(
        ["ID", "씨씨앙", "양평맘"],
        rows,
        {"씨씨앙": {"id4", "id7"}, "양평맘": set()},
    )

    chunks = plan.paste_chunks(["씨씨앙", "양평맘"], chunk_size=3)
    assert [start for start, _ in chunks] == [2, 5]
    assert chunks[0][1] == "\t\n\t\n가입\t\n"
    assert chunks[1][1] == "\t\n\t\n가입\t\n"


def test_sheet_range_url_starts_at_requested_cell() -> None:
    url = V2RBrowser._sheet_range_url(
        "https://docs.google.com/spreadsheets/d/abc_123/edit?gid=9#gid=9",
        "L",
        2,
    )
    assert url.endswith("#gid=9&range=L2")
    assert "abc_123" in url


def test_sheet_range_url_can_force_a_real_reload() -> None:
    url = V2RBrowser._sheet_range_url(
        "https://docs.google.com/spreadsheets/d/abc_123/edit?gid=9#gid=9",
        "Q",
        5,
        reload_token="99",
    )
    assert "join_nav=99" in url
    assert url.endswith("#gid=9&range=Q5")


def test_clean_cell_treats_bom_as_empty() -> None:
    assert clean_cell("\ufeff") == ""
    assert clean_cell("\ufeff가입") == "가입"
    assert clean_cell("  가입  ") == "가입"
    assert clean_cell(None) == ""


def test_load_account_rows_strips_cell_bom(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "번호,ID,김천kb보험,씨씨앙,양평맘\n"
        "1,dmmgit530,노출,\ufeff,\n",
    )
    _headers, rows = load_account_rows(path)
    assert rows[0]["씨씨앙"] == ""
    assert rows[0]["ID"] == "dmmgit530"


def test_plan_matches_sheet_ignores_bom_in_empty_cells() -> None:
    plan = build_plan(
        ["ID", "씨씨앙", "양평맘"],
        [{"__row": "2", "ID": "dmmgit530", "씨씨앙": "", "양평맘": ""}],
        {"씨씨앙": set(), "양평맘": set()},
    )
    errors = plan_matches_sheet(
        ["ID", "씨씨앙", "양평맘"],
        [{"ID": "dmmgit530", "씨씨앙": "\ufeff", "양평맘": "\ufeff"}],
        plan,
    )

    assert errors == []


def test_plan_matches_sheet_accepts_bom_prefix_on_mark() -> None:
    plan = build_plan(
        ["ID", "씨씨앙", "양평맘"],
        [{"__row": "2", "ID": "quilliant", "씨씨앙": "", "양평맘": ""}],
        {"씨씨앙": {"quilliant"}, "양평맘": set()},
    )
    errors = plan_matches_sheet(
        ["ID", "씨씨앙", "양평맘"],
        [{"ID": "quilliant", "씨씨앙": "\ufeff가입", "양평맘": ""}],
        plan,
    )

    assert errors == []


def test_windows_clipboard_bytes_have_no_bom() -> None:
    text = "가입\t\n\t\n"
    payload = V2RBrowser._clipboard_windows_bytes(text)
    assert not payload.startswith(b"\xff\xfe")
    assert payload == text.encode("utf-16le")
    assert text.encode("utf-16").startswith(b"\xff\xfe")
    assert "가입".encode("utf-16le") in payload


def test_paste_chunk_end_row_covers_tsv_lines() -> None:
    assert paste_chunk_end_row(402, "가입\t\n\t\n가입\t\n") == 404
    assert paste_chunk_end_row(2, "") == 2


def test_paste_chunks_can_resplit_a_failed_range() -> None:
    rows = [
        {"__row": str(index), "ID": f"id{index}", "씨씨앙": "", "양평맘": ""}
        for index in range(2, 12)
    ]
    plan = build_plan(
        ["ID", "씨씨앙", "양평맘"],
        rows,
        {"씨씨앙": {"id6", "id9"}, "양평맘": set()},
    )

    chunks = plan.paste_chunks(
        ["씨씨앙", "양평맘"],
        chunk_size=2,
        start_row=6,
        end_row=9,
    )
    assert [start for start, _ in chunks] == [6, 8]
    assert chunks[0][1] == "가입\t\n\t\n"
    assert chunks[1][1] == "\t\n가입\t\n"
    assert should_split_failed_chunk("\n" * 51)
    assert not should_split_failed_chunk("\n" * 50)


def test_plan_matches_sheet_can_check_one_paste_chunk() -> None:
    rows = [
        {"__row": str(index), "ID": f"id{index}", "씨씨앙": "", "양평맘": ""}
        for index in range(2, 8)
    ]
    plan = build_plan(
        ["ID", "씨씨앙", "양평맘"],
        rows,
        {"씨씨앙": {"id4", "id7"}, "양평맘": set()},
    )
    actual = [
        {"ID": row["ID"], "씨씨앙": "", "양평맘": ""}
        for row in rows
    ]
    actual[2]["씨씨앙"] = MARK_VALUE

    first_chunk = plan_matches_sheet(
        ["ID", "씨씨앙", "양평맘"],
        actual,
        plan,
        start_row=2,
        end_row=4,
    )
    second_chunk = plan_matches_sheet(
        ["ID", "씨씨앙", "양평맘"],
        actual,
        plan,
        start_row=5,
        end_row=7,
    )

    assert first_chunk == []
    assert second_chunk
    assert "7행" in second_chunk[0]
    assert "기대 '가입'" in second_chunk[0]
    assert "실제 ''" in second_chunk[0]


def test_plan_mismatch_cells_lists_empty_가입_cells() -> None:
    rows = [
        {"__row": "2", "ID": "a", "씨씨앙": "", "양평맘": ""},
        {"__row": "3", "ID": "b", "씨씨앙": "", "양평맘": ""},
        {"__row": "4", "ID": "c", "씨씨앙": "", "양평맘": ""},
    ]
    plan = build_plan(
        ["ID", "씨씨앙", "양평맘"],
        rows,
        {"씨씨앙": {"b", "c"}, "양평맘": {"c"}},
    )
    actual = [
        {"ID": "a", "씨씨앙": "", "양평맘": ""},
        {"ID": "b", "씨씨앙": "", "양평맘": ""},
        {"ID": "c", "씨씨앙": "가입", "양평맘": ""},
    ]

    cells = plan_mismatch_cells(["ID", "씨씨앙", "양평맘"], actual, plan)
    assert [(cell.row_number, cell.header, cell.value) for cell in cells] == [
        (3, "씨씨앙", "가입"),
        (4, "양평맘", "가입"),
    ]
    assert cells[0].column == "B"
    only_third = plan_mismatch_cells(
        ["ID", "씨씨앙", "양평맘"],
        actual,
        plan,
        start_row=4,
        end_row=4,
    )
    assert len(only_third) == 1
    assert only_third[0].header == "양평맘"


def test_locked_sheet_error_is_plain_korean() -> None:
    message = format_locked_sheet_error(["468행 씨씨앙: 기대 '가입' / 실제 ''"])
    assert "잠겨" in message
    assert "468행" in message


def test_build_plan_records_cafe_formulas() -> None:
    plan = build_plan(
        ["ID", "씨씨앙", "양평맘"],
        [
            {
                "__row": "2",
                "ID": "quilliant",
                "씨씨앙": "=ARRAYFORMULA(D2:D)",
                "양평맘": "",
            }
        ],
        {"씨씨앙": {"quilliant"}, "양평맘": set()},
    )

    assert plan.rows[0]["씨씨앙"] == MARK_VALUE
    assert plan.formula_cells
    assert "2행 씨씨앙" in plan.formula_cells[0]
    assert "수식" in format_cafe_formula_error(plan.formula_cells)
    assert "수식" in plan.summary()


def test_require_membership_blocks_empty_v2r_result() -> None:
    with pytest.raises(JoinMarkerError, match="하나도 읽지 못했습니다"):
        require_membership({"씨씨앙": set(), "양평맘": set()})
    require_membership({"씨씨앙": {"quilliant"}, "양평맘": set()})


def test_missed_paste_error_explains_empty_cells() -> None:
    errors = [
        "468행 씨씨앙: 기대 '가입' / 실제 ''",
        "468행 양평맘: 기대 '가입' / 실제 ''",
        "504행 씨씨앙: 기대 '가입' / 실제 ''",
    ]
    assert mismatches_look_like_missed_paste(errors)
    message = format_join_write_failure(
        errors,
        start_cell="Q402",
        start_row=402,
        end_row=801,
    )
    assert "Q402부터 801행" in message
    assert "이름 상자" in message
    assert "필터" in message


def test_user_facing_join_error_hides_chrome_crash() -> None:
    assert "Chrome 창" in user_facing_join_error(
        RuntimeError("invalid session id")
    )
    assert "Chrome 창" in user_facing_join_error(
        RuntimeError("no such window: target window already closed")
    )
    known = JoinMarkerError("시트에서 열을 찾지 못했습니다: 씨씨앙")
    assert user_facing_join_error(known) == str(known)


def test_name_box_matches_selected_cell_or_range() -> None:
    assert V2RBrowser.name_box_matches_cell("Q402", "Q", 402)
    assert V2RBrowser.name_box_matches_cell("$Q$402", "q", 402)
    assert V2RBrowser.name_box_matches_cell("Q402:R801", "Q", 402)
    assert not V2RBrowser.name_box_matches_cell("Q2", "Q", 402)
    assert not V2RBrowser.name_box_matches_cell("", "Q", 402)
