from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

from v2r_auto.exposure_sheet import (
    GoogleSheetExposureStore,
    now_stamp,
    plan_sheet_writes,
    sheet_export_url,
)
from v2r_auto.sheet_values import (
    column_index_from_letter,
    csv_sheet_cell,
    looks_like_html,
    parse_sheet_datetime,
    parse_sheet_int,
    parse_sheet_table,
    sheet_cell_values_match,
    sheet_gid_from_url,
    sheet_write_confirmed,
    spreadsheet_id_from_url,
)


NEUMIS_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1mgqghfeNrSZ1bTSrfTYWojtPGMc0u-K4VXEikxrHLOw/"
    "edit?gid=1782605844#gid=1782605844"
)
FIXTURE = Path(__file__).parent / "fixtures" / "neumis_exposure.csv"
NOW = datetime(2026, 8, 29, 20, 35, 37)
STAMP = now_stamp(NOW)


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
    def write_cell(self, *_args, **_kwargs) -> None:
        return None


def _old_false_rule(actual: str, expected: str) -> bool:
    if sheet_cell_values_match(actual, expected):
        return True
    return (
        parse_sheet_datetime(actual) is not None
        and parse_sheet_datetime(expected) is not None
    )


def _csv_text(source: str) -> str:
    if source == "fixture":
        return FIXTURE.read_text(encoding="utf-8")
    export = (
        f"{sheet_export_url(spreadsheet_id_from_url(NEUMIS_URL), sheet_gid_from_url(NEUMIS_URL))}"
        f"&cache={datetime.now().timestamp()}"
    )
    request = Request(
        export,
        headers={"User-Agent": "Mozilla/5.0 (compatible; V2R-Exposure-Checker)"},
    )
    with urlopen(request, timeout=30) as response:
        raw = response.read()
    text = raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if looks_like_html(text):
        raise AssertionError("뉴더미스 공개 내려받기가 로그인 화면입니다")
    return text


def simulate_every_row(csv_text: str) -> list[dict]:
    store = GoogleSheetExposureStore(
        NEUMIS_URL,
        __import__("logging").getLogger("test"),
        opener=lambda request, timeout=30: FakeResponse(csv_text.encode("utf-8")),
        writer=RecordingWriter(),
        now=lambda: NOW,
    )
    rows = store.load_rows()
    table = parse_sheet_table(csv_text)
    reports: list[dict] = []
    for row in rows:
        number = int(row.page_id)
        volume_text = csv_sheet_cell(table, number, column_index_from_letter("K"))
        writes = plan_sheet_writes(
            row,
            status=row.current_status or "밀려남",
            cafe_name=None,
            search_volume=parse_sheet_int(volume_text),
            volume_found=bool(str(volume_text).strip()),
            edited_at=STAMP,
        )
        cells = []
        errors = []
        for write in writes:
            current = csv_sheet_cell(
                table, number, column_index_from_letter(write.column)
            )
            skip = sheet_cell_values_match(current, write.value)
            unchanged = sheet_write_confirmed(current, write.value)
            with_ui = sheet_write_confirmed(current, write.value, ui_value=write.value)
            false_old = _old_false_rule(current, write.value)
            if skip:
                verdict = "skip"
                if not unchanged:
                    errors.append(f"{write.column}{number} skip인데 현재값 확인이 안 됩니다")
            else:
                if unchanged:
                    errors.append(
                        f"{write.column}{number} 칸이 그대로인데 성공으로 봤습니다 "
                        f"({current!r} → {write.value!r})"
                    )
                    verdict = "false-success"
                elif write.column == "J" and false_old:
                    verdict = "needs-real-write"
                else:
                    verdict = "needs-write"
                if not with_ui:
                    errors.append(
                        f"{write.column}{number} 화면 칸이 새 값과 같아도 성공이 아닙니다"
                    )
            cells.append(
                {
                    "cell": f"{write.column}{number}",
                    "current": current,
                    "wanted": write.value,
                    "skip": skip,
                    "unchanged_success": unchanged,
                    "ui_success": with_ui,
                    "old_false_success": false_old,
                    "verdict": verdict,
                }
            )
        reports.append(
            {
                "row": number,
                "keyword": row.keyword,
                "status": row.current_status,
                "cells": cells,
                "errors": errors,
            }
        )
    return reports


def _assert_reports(
    reports: list[dict], source: str, *, pin_rows: bool = True
) -> None:
    if pin_rows:
        assert len(reports) == 380, f"{source}: 키워드 {len(reports)}건"
    errors = [f"{item['row']} {item['keyword']}: {err}" for item in reports for err in item["errors"]]
    j_needed = 0
    j_false = 0
    l_needed = []
    g_writes = []
    k_writes = []
    for item in reports:
        for cell in item["cells"]:
            letter = cell["cell"][0]
            if letter == "J":
                if cell["skip"]:
                    errors.append(f"{item['row']} {item['keyword']}: J를 건너뛰면 지금 시각이 안 들어갑니다")
                if cell["unchanged_success"]:
                    j_false += 1
                if cell["old_false_success"] and not cell["skip"]:
                    j_false += 0
                    if not cell["unchanged_success"] and cell["old_false_success"]:
                        j_needed += 1
            if letter == "L" and not cell["skip"]:
                l_needed.append((item["row"], item["keyword"], cell["current"], cell["wanted"]))
            if letter == "G" and not cell["skip"]:
                g_writes.append(item["row"])
            if letter == "K" and not cell["skip"]:
                k_writes.append(item["row"])
    assert errors == [], source + "\n" + "\n".join(errors)
    assert j_false == 0
    assert g_writes == []
    assert k_writes == []
    if pin_rows:
        assert j_needed == 380
        assert [item[0] for item in l_needed] == [141, 340, 346, 361, 374, 375], l_needed
        leftover_hidden = [item for item in l_needed if item[0] == 141]
        assert leftover_hidden[0][1] == "항문농양"
        assert leftover_hidden[0][2] in {"5,800", "5800"}
        assert leftover_hidden[0][3] == ""
        j_old_false = sum(
            1
            for item in reports
            for cell in item["cells"]
            if cell["cell"].startswith("J") and cell["old_false_success"]
        )
        assert j_old_false == 380
    else:
        assert j_needed == len(reports)
        j_old_false = sum(
            1
            for item in reports
            for cell in item["cells"]
            if cell["cell"].startswith("J") and cell["old_false_success"]
        )
        assert j_old_false == len(reports)


def test_fixture_every_row_one_by_one() -> None:
    _assert_reports(simulate_every_row(_csv_text("fixture")), "fixture")


def test_live_every_row_one_by_one() -> None:
    _assert_reports(simulate_every_row(_csv_text("live")), "live", pin_rows=False)


def test_logged_j4_j5_are_failures_until_cell_shows_new_time() -> None:
    reports = simulate_every_row(_csv_text("live"))
    by_row = {item["row"]: item for item in reports}
    for number, keyword in ((4, "치질수술 병원"), (5, "치질수술후재발")):
        item = by_row.get(number)
        if item is None or item["keyword"] != keyword:
            continue
        assert item["keyword"] == keyword
        j_cell = next(cell for cell in item["cells"] if cell["cell"].startswith("J"))
        assert j_cell["skip"] is False
        assert j_cell["unchanged_success"] is False
        assert j_cell["old_false_success"] is True
        assert j_cell["ui_success"] is True
        assert item["errors"] == []
