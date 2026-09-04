import json
import logging
from pathlib import Path
from urllib.error import HTTPError

from datetime import datetime
from zoneinfo import ZoneInfo

from v2r_auto.exposure import ExposureRow, naver_search_url
from v2r_auto.exposure_notion import NotionExposureStore
from v2r_auto.exposure_sheet import (
    GoogleSheetExposureStore,
    SheetWrite,
    now_stamp,
    plan_volume_writes,
    sheet_clipboard_prompt_visible,
    sheet_edit_target,
)
from v2r_auto.search_volume import (
    rows_to_process,
    SearchVolumeFiller,
    collect_cafe_article_previews,
    empty_volume_rows,
    first_visible_cafe_title,
    is_spacing_only_suggestion,
    keep_keyword_notes,
    spacing_from_autocomplete,
    spacing_from_text,
    volume_is_empty,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def read(self):
        if self.status >= 400:
            raise HTTPError(
                "https://api.notion.com",
                self.status,
                "error",
                hdrs=None,
                fp=None,
            )
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeNaver:
    def __init__(
        self,
        suggestion="",
        search_html="",
        visible_urls=None,
        volume=12,
    ):
        self.suggestion = suggestion
        self.search_html = search_html
        self.visible_urls = visible_urls
        self.volume = volume
        self.peeked: list[str] = []
        self.searched: list[str] = []
        self.looked_up: list[str] = []

    def peek_first_autocomplete(self, keyword: str) -> str:
        self.peeked.append(keyword)
        return self.suggestion

    def search_integrated(self, keyword: str) -> str:
        self.searched.append(keyword)
        return self.search_html

    def visible_cafe_article_urls(self):
        return self.visible_urls

    def lookup_search_volume(self, keyword: str) -> int | None:
        self.looked_up.append(keyword)
        return self.volume


class FakeStore:
    def __init__(self):
        self.writes: list[dict] = []

    def update_volume_and_keyword(
        self,
        row,
        *,
        keyword=None,
        search_volume=None,
        volume_found=False,
        search_url=None,
    ):
        self.writes.append(
            {
                "page_id": row.page_id,
                "keyword": keyword,
                "search_volume": search_volume,
                "volume_found": volume_found,
                "search_url": search_url,
            }
        )


def _row(
    keyword: str,
    *,
    page_id="1",
    current_volume="",
    volume_property="키워드 검색량",
    keyword_type="title",
    search_url="",
    search_url_property="I",
) -> ExposureRow:
    return ExposureRow(
        page_id,
        keyword,
        search_url,
        "",
        "밀려남",
        "노출상태",
        "status",
        current_volume=current_volume,
        keyword_property="키워드",
        keyword_type=keyword_type,
        volume_property=volume_property,
        volume_type="number",
        search_url_property=search_url_property,
    )


def test_blank_volume_is_empty_but_zero_is_not() -> None:
    assert volume_is_empty("")
    assert volume_is_empty(None)
    assert volume_is_empty("  ")
    assert not volume_is_empty("0")
    assert not volume_is_empty("12")


def test_rows_to_process_include_empty_volume_and_missing_search_url() -> None:
    rows = [
        _row("장으뜸", current_volume=""),
        _row("팥순", current_volume="5,680", page_id="2"),
        _row("코숨핏", current_volume="12", page_id="3", search_url="https://search.naver.com"),
    ]
    selected = rows_to_process(rows)
    assert [row.keyword for row in selected] == ["장으뜸", "팥순"]


def test_empty_volume_rows_skip_filled_and_missing_column() -> None:
    rows = [
        _row("장으뜸", current_volume=""),
        _row("팥순", current_volume="0", page_id="2"),
        _row("코숨핏", current_volume="321", page_id="3"),
        _row("자연방패", current_volume="", page_id="4", volume_property=""),
    ]
    empty = empty_volume_rows(rows)
    assert [row.keyword for row in empty] == ["장으뜸"]


def test_autocomplete_spacing_only_when_same_letters() -> None:
    assert spacing_from_autocomplete("장으뜸장어즙", "장으뜸 장어즙") == "장으뜸 장어즙"
    assert spacing_from_autocomplete("장 으뜸 장어즙", "장으뜸 장어즙") == "장으뜸 장어즙"
    assert spacing_from_autocomplete("장으뜸장어즙", "장어즙 효능") == ""
    assert spacing_from_autocomplete("장으뜸장어즙", "") == ""
    assert spacing_from_autocomplete("비만", "비만 계산기") == ""
    assert spacing_from_autocomplete("복부비만", "여자 복부비만") == ""
    assert spacing_from_autocomplete("허벅지안쪽살빼기", "허벅지 안쪽 살 빼기 운동") == ""
    assert spacing_from_autocomplete("비만", "비 만") == "비 만"
    assert is_spacing_only_suggestion("부종원인", "부종 원인")
    assert not is_spacing_only_suggestion("비만", "비만 계산기")
    assert not is_spacing_only_suggestion("복부비만", "여자 복부비만")


def test_spacing_from_cafe_title_keeps_title_spaces() -> None:
    title = "[후기] 장으뜸 장어즙 한달 먹었어요"
    assert spacing_from_text("장으뜸장어즙", title) == "장으뜸 장어즙"
    assert spacing_from_text("장 으뜸장어즙", title) == "장으뜸 장어즙"
    assert spacing_from_text("코숨핏", title) == ""


def test_keep_parenthetical_notes_when_fixing_spaces() -> None:
    assert (
        keep_keyword_notes("내치핵(내치핵자연치료 글로 노출됨)", "내 치핵")
        == "내 치핵(내치핵자연치료 글로 노출됨)"
    )
    assert keep_keyword_notes("장으뜸장어즙", "장으뜸 장어즙") == "장으뜸 장어즙"


def test_first_visible_cafe_title_is_any_cafe_not_only_ours() -> None:
    html = """
    <div id="main_pack">
      <a href="https://cafe.naver.com/othercafe">이웃카페</a>
      <a href="https://cafe.naver.com/othercafe/11">장으뜸 장어즙 후기</a>
      <a href="https://cafe.naver.com/cantsb/99">우리 글은 나중</a>
    </div>
    """
    previews = collect_cafe_article_previews(html)
    assert [item.title for item in previews] == ["장으뜸 장어즙 후기", "우리 글은 나중"]
    assert first_visible_cafe_title(html, None) == "장으뜸 장어즙 후기"
    assert (
        first_visible_cafe_title(html, ["https://cafe.naver.com/cantsb/99"])
        == "우리 글은 나중"
    )


def test_clustered_sub_cafe_title_is_skipped() -> None:
    html = """
    <a href="https://cafe.naver.com/cantsb/1" data-heatmap-target=".link">대표 장으뜸 장어즙</a>
    <a href="https://cafe.naver.com/cantsb/2" data-heatmap-target=".series">서브 글</a>
    """
    assert first_visible_cafe_title(html, None) == "대표 장으뜸 장어즙"


def test_filler_uses_first_autocomplete_spacing() -> None:
    naver = FakeNaver(suggestion="장으뜸 장어즙", volume=88)
    store = FakeStore()
    row = _row("장으뜸장어즙")
    SearchVolumeFiller(store, naver, logging.getLogger("test")).run(
        [row], dry_run=False
    )
    assert naver.peeked == ["장으뜸장어즙"]
    assert naver.searched == []
    assert naver.looked_up == ["장으뜸 장어즙"]
    assert store.writes == [
        {
            "page_id": "1",
            "keyword": "장으뜸 장어즙",
            "search_volume": 88,
            "volume_found": True,
            "search_url": naver_search_url("장으뜸 장어즙"),
        }
    ]
    assert row.keyword == "장으뜸 장어즙"
    assert row.current_volume == "88"
    assert row.search_url == naver_search_url("장으뜸 장어즙")


def test_filler_falls_back_to_first_cafe_title() -> None:
    html = """
    <a href="https://cafe.naver.com/someone/77">장으뜸 장어즙 후기입니다</a>
    """
    naver = FakeNaver(
        suggestion="장어즙 효능",
        search_html=html,
        visible_urls=["https://cafe.naver.com/someone/77"],
        volume=10,
    )
    store = FakeStore()
    row = _row("장으뜸장어즙")
    SearchVolumeFiller(store, naver, logging.getLogger("test")).run(
        [row], dry_run=False
    )
    assert naver.searched == ["장으뜸장어즙"]
    assert store.writes[0]["keyword"] == "장으뜸 장어즙"
    assert store.writes[0]["search_volume"] == 10
    assert store.writes[0]["search_url"] == naver_search_url("장으뜸 장어즙")


def test_filler_skips_rows_that_already_have_volume_and_search_url() -> None:
    naver = FakeNaver(suggestion="장으뜸 장어즙", volume=1)
    store = FakeStore()
    filled = _row(
        "장으뜸장어즙",
        current_volume="0",
        search_url="https://search.naver.com/search.naver?query=x",
    )
    SearchVolumeFiller(store, naver, logging.getLogger("test")).run(
        [filled], dry_run=False
    )
    assert naver.peeked == []
    assert store.writes == []


def test_filler_writes_search_url_when_volume_already_filled() -> None:
    naver = FakeNaver(suggestion="장으뜸 장어즙", volume=1)
    store = FakeStore()
    filled = _row("장으뜸장어즙", current_volume="5,680")
    SearchVolumeFiller(store, naver, logging.getLogger("test")).run(
        [filled], dry_run=False
    )
    assert naver.peeked == []
    assert naver.looked_up == []
    assert store.writes == [
        {
            "page_id": "1",
            "keyword": None,
            "search_volume": None,
            "volume_found": False,
            "search_url": naver_search_url("장으뜸장어즙"),
        }
    ]


def test_dry_run_does_not_write_notion() -> None:
    naver = FakeNaver(suggestion="장으뜸 장어즙", volume=44)
    store = FakeStore()
    SearchVolumeFiller(store, naver, logging.getLogger("test")).run(
        [_row("장으뜸장어즙")], dry_run=True
    )
    assert store.writes == []


def test_notion_store_reads_empty_volume_and_patches_keyword() -> None:
    bodies = []

    def opener(request, timeout=30):
        if request.data:
            bodies.append(json.loads(request.data.decode("utf-8")))
        if request.full_url.endswith("/databases/2260ab12-cdff-80c3-a4c0-d2f0ab1e9c44"):
            return FakeResponse(
                {
                    "properties": {
                        "키워드": {"type": "title"},
                        "노출상태": {"type": "status"},
                        "키워드 검색량": {"type": "number"},
                    }
                }
            )
        if request.full_url.endswith("/query"):
            return FakeResponse(
                {
                    "results": [
                        {
                            "id": "page-empty",
                            "properties": {
                                "키워드": {
                                    "type": "title",
                                    "title": [{"plain_text": "장으뜸장어즙"}],
                                },
                                "노출상태": {
                                    "type": "status",
                                    "status": {"name": "밀려남"},
                                },
                                "키워드 검색량": {"type": "number", "number": None},
                            },
                        },
                        {
                            "id": "page-zero",
                            "properties": {
                                "키워드": {
                                    "type": "title",
                                    "title": [{"plain_text": "팥순"}],
                                },
                                "노출상태": {
                                    "type": "status",
                                    "status": {"name": "밀려남"},
                                },
                                "키워드 검색량": {"type": "number", "number": 0},
                            },
                        },
                    ],
                    "has_more": False,
                }
            )
        return FakeResponse({})

    store = NotionExposureStore(
        "secret",
        "https://www.notion.so/2260ab12cdff80c3a4c0d2f0ab1e9c44",
        logging.getLogger("test"),
        opener=opener,
    )
    rows = store.load_rows()
    assert [row.keyword for row in rows] == ["장으뜸장어즙", "팥순"]
    assert [row.current_volume for row in rows] == ["", "0"]
    assert rows[0].keyword_property == "키워드"
    assert rows[0].keyword_type == "title"
    empty = empty_volume_rows(rows)
    assert [row.keyword for row in empty] == ["장으뜸장어즙"]
    store.update_volume_and_keyword(
        empty[0],
        keyword="장으뜸 장어즙",
        search_volume=21000,
        volume_found=True,
    )
    payload = bodies[-1]["properties"]
    assert payload["키워드"]["title"][0]["text"]["content"] == "장으뜸 장어즙"
    assert payload["키워드 검색량"]["number"] == 21000


PATSOON_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1OwR_LSjO1ofOojldtSIqoxv0gieNSMx_t35_5G1VTCc/"
    "edit?gid=1325327696#gid=1325327696"
)


class RecordingWriter:
    def __init__(self):
        self.writes: list[tuple[str, str, int, str]] = []

    def write_cell(self, sheet_url: str, column: str, row_number: int, value: str) -> None:
        self.writes.append((sheet_url, column, row_number, value))


class FakeCsvResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_plan_volume_writes_fills_volume_search_and_time() -> None:
    row = ExposureRow(
        "2",
        "장으뜸장어즙",
        "",
        "",
        "밀려남",
        "G",
        "select",
        volume_property="K",
        keyword_property="H",
        keyword_type="rich_text",
        edited_property="J",
        search_url_property="I",
    )
    writes = plan_volume_writes(
        row,
        keyword="장으뜸 장어즙",
        search_volume=21000,
        volume_found=True,
        search_url=naver_search_url("장으뜸 장어즙"),
        edited_at="2026-08-27 16:45:00",
    )
    assert writes == [
        SheetWrite("H", "장으뜸 장어즙"),
        SheetWrite("K", "21000"),
        SheetWrite("I", naver_search_url("장으뜸 장어즙")),
        SheetWrite("J", "2026-08-27 16:45:00"),
    ]


def test_sheet_store_reads_empty_volume_and_writes_search_url() -> None:
    csv_text = (
        "카페,노출 상태,키워드,통합검색,최종 편집 일시,키워드 검색량\n"
        "씨씨앙,밀려남,장으뜸장어즙,,,\n"
        "씨씨앙,밀려남,팥순,,,0\n"
    )

    def opener(request, timeout=30):
        return FakeCsvResponse(csv_text.encode("utf-8"))

    writer = RecordingWriter()
    store = GoogleSheetExposureStore(
        PATSOON_URL,
        logging.getLogger("test"),
        opener=opener,
        writer=writer,
        now=lambda: datetime(2026, 8, 27, 16, 45, 0),
    )
    rows = store.load_rows()
    assert [row.keyword for row in rows] == ["장으뜸장어즙", "팥순"]
    assert [row.current_volume for row in rows] == ["", "0"]
    assert rows[0].keyword_property == "C"
    assert rows[0].search_url_property == "D"
    assert rows[0].volume_property == "F"
    empty = empty_volume_rows(rows)
    assert [row.keyword for row in empty] == ["장으뜸장어즙"]
    store.update_volume_and_keyword(
        empty[0],
        keyword="장으뜸 장어즙",
        search_volume=21000,
        volume_found=True,
        search_url=naver_search_url("장으뜸 장어즙"),
    )
    assert writer.writes == [
        (PATSOON_URL, "C", 2, "장으뜸 장어즙"),
        (PATSOON_URL, "F", 2, "21000"),
        (PATSOON_URL, "D", 2, naver_search_url("장으뜸 장어즙")),
        (PATSOON_URL, "E", 2, "2026-08-27 16:45:00"),
    ]


def test_sheet_store_keeps_writing_other_cells_after_one_fails() -> None:
    class FlakyWriter:
        def __init__(self):
            self.writes: list[str] = []

        def write_cell(self, sheet_url, column, row_number, value):
            if column == "I":
                raise RuntimeError("autocomplete stole the URL")
            self.writes.append(column)

    writer = FlakyWriter()
    store = GoogleSheetExposureStore(
        PATSOON_URL,
        logging.getLogger("test"),
        writer=writer,
        now=lambda: datetime(2026, 8, 27, 16, 45, 0),
    )
    row = ExposureRow(
        "13",
        "비만",
        "",
        "",
        "밀려남",
        "G",
        "select",
        volume_property="K",
        keyword_property="H",
        keyword_type="rich_text",
        edited_property="J",
        search_url_property="I",
    )
    failed = store.update_volume_and_keyword(
        row,
        keyword=None,
        search_volume=3590,
        volume_found=True,
        search_url=naver_search_url("비만"),
    )
    assert failed == 1
    assert writer.writes == ["K", "J"]


def test_filler_continues_after_one_row_write_fails() -> None:
    class BoomFirstStore(FakeStore):
        def update_volume_and_keyword(self, row, **kwargs):
            if row.keyword == "비만":
                raise RuntimeError("시트 I13 저장에 3회 실패했습니다")
            return super().update_volume_and_keyword(row, **kwargs)

    naver = FakeNaver(suggestion="부종 원인", volume=12)
    store = BoomFirstStore()
    filler = SearchVolumeFiller(store, naver, logging.getLogger("test"), delay_seconds=0)
    filler.run(
        [_row("비만", page_id="13"), _row("부종원인", page_id="7")],
        dry_run=False,
    )
    assert filler.failed_rows == 1
    assert [item["page_id"] for item in store.writes] == ["7"]
    assert store.writes[0]["keyword"] == "부종 원인"


def test_now_stamp_uses_korea_time() -> None:
    assert now_stamp(datetime(2026, 9, 4, 23, 19, 43)) == "2026-09-04 23:19:43"
    assert (
        now_stamp(datetime(2026, 9, 4, 14, 19, 43, tzinfo=ZoneInfo("UTC")))
        == "2026-09-04 23:19:43"
    )


def test_sheet_store_skips_volume_when_not_found_but_writes_search() -> None:
    writer = RecordingWriter()
    store = GoogleSheetExposureStore(
        PATSOON_URL,
        logging.getLogger("test"),
        writer=writer,
        now=lambda: datetime(2026, 8, 27, 16, 45, 0),
    )
    row = ExposureRow(
        "5",
        "코숨핏",
        "",
        "",
        "밀려남",
        "G",
        "select",
        volume_property="K",
        keyword_property="H",
        keyword_type="rich_text",
        edited_property="J",
        search_url_property="I",
    )
    store.update_volume_and_keyword(
        row,
        keyword=None,
        search_volume=None,
        volume_found=False,
        search_url=naver_search_url("코숨핏"),
    )
    columns = [item[1] for item in writer.writes]
    assert columns == ["I", "J"]


def test_sheet_clipboard_prompt_is_detected() -> None:
    html = (
        "<div>복사, 잘라내기, 붙여넣기를 사용 설정하시겠습니까?</div>"
        "<button>취소</button><button>설치</button>"
    )
    assert sheet_clipboard_prompt_visible(html) is True
    assert sheet_clipboard_prompt_visible("코골이수술 실비") is False


def test_browser_closes_sheet_clipboard_prompt() -> None:
    source = Path("v2r_auto/browser.py").read_text(encoding="utf-8")
    assert "_dismiss_sheet_clipboard_prompt" in source
    assert "붙여넣기 설정 창을 닫았습니다" in source
    assert "text === '취소'" in source
    gui = Path("v2r_auto/gui_search_volume.py").read_text(encoding="utf-8")
    assert "붙여넣기 설정 창" in gui


def test_sheet_write_skips_same_value_and_does_not_escape_while_editing() -> None:
    source = Path("v2r_auto/browser.py").read_text(encoding="utf-8")
    update = source.split("def update_sheet_cell", 1)[1].split(
        "def _visible_sheet_editor", 1
    )[0]
    enter = source.split("def _enter_sheet_value", 1)[1].split(
        "def _dismiss_sheet_clipboard_prompt", 1
    )[0]
    dismiss = source.split("def _dismiss_sheet_clipboard_prompt", 1)[1].split(
        "def _find_sheet_name_box", 1
    )[0]
    assert "_sheet_cell_matches" in update
    assert "이미 들어 있어 그대로 둡니다" in update
    assert "시트 %s 저장 재시도" not in update
    assert "_dismiss_sheet_clipboard_prompt" not in enter.split(
        "self._begin_sheet_cell_edit()", 1
    )[1]
    assert "send_keys(Keys.ESCAPE)" not in dismiss.split("if closed and result", 1)[0]
    gui = Path("v2r_auto/gui_search_volume.py").read_text(encoding="utf-8")
    assert "이미 같은 값이면 그대로 두고 오류를 내지 않습니다" in gui


def test_first_cell_write_clicks_formula_bar() -> None:
    fixture = Path("tests/fixtures/sheet_first_cell.html").read_text(encoding="utf-8")
    assert sheet_clipboard_prompt_visible(fixture) is True
    assert sheet_edit_target(fixture) == "formula_bar"
    source = Path("v2r_auto/browser.py").read_text(encoding="utf-8")
    enter = source.split("def _enter_sheet_value", 1)[1].split(
        "def _dismiss_sheet_clipboard_prompt", 1
    )[0]
    begin = source.split("def _begin_sheet_cell_edit", 1)[1].split(
        "def _insert_sheet_text", 1
    )[0]
    focus = source.split("def _focus_sheet_formula_bar", 1)[1].split(
        "def _formula_bar_text", 1
    )[0]
    assert "_select_sheet_cell" in source
    assert "_find_sheet_formula_bar" in source
    assert "_focus_sheet_formula_bar" in source
    assert "_formula_bar_matches" in enter
    assert "waffle-rich-text-editor" not in begin
    assert "waffle-rich-text-editor" not in focus
    assert "bar.click()" in focus
    gui = Path("v2r_auto/gui_search_volume.py").read_text(encoding="utf-8")
    assert "수식 입력줄" in gui
    assert "첫 칸 안을 누르면" in gui
