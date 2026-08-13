import json
import threading
import time
from urllib.error import HTTPError

from v2r_auto.exposure import (
    DEFAULT_CAFE_NAMES,
    ExposureChecker,
    ExposureRow,
    brand_found,
    cafe_id_for_check,
    cafe_name_option,
    collect_our_cafe_hits,
    is_cafe_article_url,
    keyword_tool_query,
    keywordstool_volume,
    match_selected_rows,
    matching_cafe_name,
    parse_brands,
    parse_cafes,
    parse_keyword_lines,
    parse_qc_count,
    preserve_cafe_id,
    same_search_query,
    strip_parenthetical,
    volume_from_result_cells,
)
from v2r_auto.exposure_notion import NotionExposureStore, parse_database_id


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
    def __init__(self, search_html, post_texts=None):
        self.search_html = search_html
        self.post_texts = post_texts or {}
        self.searched: list[str] = []
        self.opened: list[str] = []

    def search_integrated(self, keyword: str) -> str:
        self.searched.append(keyword)
        if isinstance(self.search_html, dict):
            return self.search_html[keyword]
        return self.search_html

    def open_post_text(self, url: str) -> str:
        self.opened.append(url)
        return self.post_texts.get(url, "")


def _row(keyword: str, status: str = "밀려남") -> ExposureRow:
    return ExposureRow("1", keyword, "", "", status, "노출상태", "status")


def test_parse_brands_keeps_default_order() -> None:
    assert parse_brands("")[0] == "팥순추출물"
    assert "코숨핏" in parse_brands("코숨핏, 팥순추출물")


def test_parse_cafes_uses_defaults() -> None:
    cafes = parse_cafes("")
    assert "씨씨앙" in cafes
    assert "러브인썸" in cafes


def test_database_id_from_notion_url() -> None:
    url = "https://www.notion.so/earlybirdz/2260ab12cdff80c3a4c0d2f0ab1e9c44?v=abcd"
    assert parse_database_id(url) == "2260ab12-cdff-80c3-a4c0-d2f0ab1e9c44"


def test_strip_parenthetical_keeps_search_keyword() -> None:
    assert (
        strip_parenthetical("모로오렌지 추출물 (모로오렌지 효능으로 노출)")
        == "모로오렌지 추출물"
    )
    assert strip_parenthetical("코숨핏") == "코숨핏"
    assert strip_parenthetical("통밀빵 100% 다이어트（메모）") == "통밀빵 100% 다이어트"


def test_cafe_name_ignores_spaces() -> None:
    assert matching_cafe_name("러브 인썸 카페", list(DEFAULT_CAFE_NAMES)) == "러브인썸"
    assert matching_cafe_name("마이 웨딩 드림", list(DEFAULT_CAFE_NAMES)) == "마이웨딩드림"
    assert matching_cafe_name("다른카페", list(DEFAULT_CAFE_NAMES)) == ""


def test_brand_found_ignores_spaces() -> None:
    assert brand_found("자연방패항문세정제 후기", ["자연방패 항문세정제"]) == "자연방패 항문세정제"
    assert brand_found("일반 글", ["코숨핏"]) == ""


def test_article_url_detection() -> None:
    assert is_cafe_article_url("https://cafe.naver.com/ccang/12345")
    assert is_cafe_article_url(
        "https://cafe.naver.com/ArticleRead.nhn?articleid=99&clubid=1"
    )
    assert not is_cafe_article_url("https://cafe.naver.com/ccang")


def test_collects_only_our_cafe_articles() -> None:
    html = """
    <div id="main_pack">
      <a href="https://cafe.naver.com/othercafe">이웃카페</a>
      <a href="https://cafe.naver.com/othercafe/11">남의 글</a>
      <a href="https://cafe.naver.com/loveinsome">러브 인썸</a>
      <a href="https://cafe.naver.com/loveinsome/99">우리 글</a>
    </div>
    """
    hits = collect_our_cafe_hits(html, list(DEFAULT_CAFE_NAMES))
    assert [hit.cafe_name for hit in hits] == ["러브인썸"]
    assert hits[0].url.endswith("/loveinsome/99")
    assert all("othercafe" not in hit.url for hit in hits)


def test_collects_product_review_module_with_long_cafe_name() -> None:
    html = """
    <h2>상품리뷰 인기글</h2>
    <div data-template-id="ugcItem">
      <a href="https://cafe.naver.com/cantsb">
        <span>국내1위 다이어트 커뮤니티 씨씨앙(식단,운동,후기,헬스,체험단)</span>
      </a>
      <a href="https://cafe.naver.com/cantsb/3453001?art=token">
        다크 초콜릿 감량에 괜찮나요?
      </a>
    </div>
    """
    hits = collect_our_cafe_hits(html, list(DEFAULT_CAFE_NAMES))
    assert [hit.cafe_name for hit in hits] == ["씨씨앙"]
    assert "cantsb/3453001" in hits[0].url


def test_same_search_query_ignores_spaces_only() -> None:
    assert same_search_query("다크 초콜릿", "다크초콜릿")
    assert not same_search_query("다크 초콜릿", "다크 초콜릿 효능")


def test_checker_strips_notes_before_search() -> None:
    naver = FakeNaver("<div id='main_pack'></div>")
    checker = ExposureChecker(
        type("N", (), {"update_status": staticmethod(lambda *_a: None)})(),
        naver,
        __import__("logging").getLogger("test"),
        delay_seconds=0,
    )
    checker.run(
        [_row("모로오렌지 추출물 (모로오렌지 효능으로 노출)")],
        dry_run=True,
    )
    assert naver.searched == ["모로오렌지 추출물"]
    assert naver.opened == []


def test_no_our_cafe_is_hidden_without_opening_posts() -> None:
    html = """
    <div id="main_pack">
      <a href="https://cafe.naver.com/other/1">다른 카페 글</a>
    </div>
    """
    updated = []
    naver = FakeNaver(html, {"https://cafe.naver.com/other/1": "팥순추출물"})

    class FakeNotion:
        def update_status(self, row, status):
            updated.append((row.keyword, status))

    row = _row("김희선 다이어트", "노출완")
    ExposureChecker(
        FakeNotion(),
        naver,
        __import__("logging").getLogger("test"),
        delay_seconds=0,
    ).run([row], dry_run=False)
    assert naver.opened == []
    assert updated == [("김희선 다이어트", "밀려남")]


def test_our_cafe_without_brand_is_hidden() -> None:
    post = "https://cafe.naver.com/ccang/10"
    html = f"""
    <a href="https://cafe.naver.com/ccang">씨씨앙</a>
    <a href="{post}">제목</a>
    """
    updated = []
    naver = FakeNaver(html, {post: "그냥 일상 글입니다"})

    class FakeNotion:
        def update_status(self, row, status):
            updated.append(status)

    ExposureChecker(
        FakeNotion(),
        naver,
        __import__("logging").getLogger("test"),
        delay_seconds=0,
    ).run([_row("키워드", "노출완")], dry_run=False)
    assert naver.opened == [post]
    assert updated == ["밀려남"]


def test_our_cafe_with_brand_is_exposed() -> None:
    first = "https://cafe.naver.com/ccang/1"
    second = "https://cafe.naver.com/yangpyeongmom/2"
    html = f"""
    <a href="https://cafe.naver.com/ccang">씨씨앙</a>
    <a href="{first}">글1</a>
    <a href="https://cafe.naver.com/yangpyeongmom">양평맘</a>
    <a href="{second}">글2</a>
    """
    updated = []
    naver = FakeNaver(
        html,
        {
            first: "일반 글",
            second: "댓글에 코숨핏 후기",
        },
    )

    class FakeNotion:
        def update_status(self, row, status):
            updated.append((row.keyword, status))

    ExposureChecker(
        FakeNotion(),
        naver,
        __import__("logging").getLogger("test"),
        delay_seconds=0,
    ).run([_row("키워드")], dry_run=False)
    assert naver.opened == [first, second]
    assert updated == [("키워드", "노출완")]


def test_dry_run_does_not_write_notion() -> None:
    post = "https://cafe.naver.com/ccang/1"
    html = f"""
    <a href="https://cafe.naver.com/ccang">씨씨앙</a>
    <a href="{post}">글</a>
    """
    updated = []

    class FakeNotion:
        def update_status(self, row, status):
            updated.append(status)

    row = _row("키워드")
    ExposureChecker(
        FakeNotion(),
        FakeNaver(html, {post: "팥순추출물"}),
        __import__("logging").getLogger("test"),
        delay_seconds=0,
    ).run([row], dry_run=True)
    assert updated == []
    assert row.current_status == "밀려남"


def test_notion_store_reads_and_patches_status() -> None:
    calls = []

    def opener(request, timeout=30):
        calls.append((request.get_method(), request.full_url))
        if request.full_url.endswith("/databases/2260ab12-cdff-80c3-a4c0-d2f0ab1e9c44"):
            return FakeResponse(
                {
                    "properties": {
                        "키워드": {"type": "title"},
                        "노출상태": {"type": "status"},
                        "통합검색": {"type": "url"},
                        "작성 글": {"type": "url"},
                    }
                }
            )
        if request.full_url.endswith("/query"):
            return FakeResponse(
                {
                    "results": [
                        {
                            "id": "page-1",
                            "properties": {
                                "키워드": {
                                    "type": "title",
                                    "title": [{"plain_text": "김희선 다이어트"}],
                                },
                                "노출상태": {
                                    "type": "status",
                                    "status": {"name": "밀려남"},
                                },
                                "통합검색": {
                                    "type": "url",
                                    "url": "https://search.naver.com/search.naver?query=test",
                                },
                                "작성 글": {"type": "url", "url": ""},
                            },
                        }
                    ],
                    "has_more": False,
                }
            )
        return FakeResponse({})

    store = NotionExposureStore(
        "secret",
        "https://www.notion.so/2260ab12cdff80c3a4c0d2f0ab1e9c44",
        __import__("logging").getLogger("test"),
        opener=opener,
    )
    rows = store.load_rows()
    assert rows[0].keyword == "김희선 다이어트"
    store.update_status(rows[0], "노출완")
    assert any(method == "PATCH" for method, _url in calls)


def test_notion_store_writes_cafe_and_search_volumes() -> None:
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
                        "카페/ID": {"type": "rich_text"},
                        "키워드 검색량": {"type": "number"},
                        "노출된 검색량": {"type": "number"},
                    }
                }
            )
        if request.full_url.endswith("/query"):
            return FakeResponse({"results": [], "has_more": False})
        return FakeResponse({})

    store = NotionExposureStore(
        "secret",
        "https://www.notion.so/2260ab12cdff80c3a4c0d2f0ab1e9c44",
        __import__("logging").getLogger("test"),
        opener=opener,
    )
    store.load_schema()
    row = ExposureRow(
        "page-1",
        "다크 초콜릿",
        "",
        "",
        "밀려남",
        "노출상태",
        "status",
        current_cafe="씨씨앙/dtsx",
        cafe_property="카페/ID",
        cafe_type="rich_text",
        volume_property="키워드 검색량",
        volume_type="number",
        exposed_volume_property="노출된 검색량",
        exposed_volume_type="number",
    )
    store.update_check_result(
        row,
        status="노출완",
        cafe_name="씨씨앙/dtsx",
        search_volume=321,
        volume_found=True,
    )
    payload = bodies[-1]["properties"]
    assert payload["노출상태"]["status"]["name"] == "노출완"
    assert payload["카페/ID"]["rich_text"][0]["text"]["content"] == "씨씨앙/dtsx"
    assert payload["키워드 검색량"]["number"] == 321
    assert payload["노출된 검색량"]["number"] == 321
    store.update_check_result(
        row,
        status="밀려남",
        cafe_name=None,
        search_volume=321,
        volume_found=True,
    )
    hidden = bodies[-1]["properties"]
    assert "카페/ID" not in hidden
    assert hidden["키워드 검색량"]["number"] == 321
    assert hidden["노출된 검색량"]["number"] is None
    store.update_check_result(
        row,
        status="밀려남",
        cafe_name=None,
        search_volume=None,
        volume_found=False,
    )
    no_volume = bodies[-1]["properties"]
    assert "카페/ID" not in no_volume
    assert "키워드 검색량" not in no_volume
    assert no_volume["노출된 검색량"]["number"] is None


def test_notion_cafe_select_uses_name_only_option() -> None:
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
                        "카페/ID": {
                            "type": "select",
                            "select": {
                                "options": [
                                    {"name": "마이웨딩드림"},
                                    {"name": "씨씨앙"},
                                    {"name": "씨씨앙/dtsx"},
                                    {"name": "양평맘"},
                                    {"name": "줌마/cqu"},
                                ]
                            },
                        },
                    }
                }
            )
        if request.full_url.endswith("/query"):
            return FakeResponse({"results": [], "has_more": False})
        return FakeResponse({})

    store = NotionExposureStore(
        "secret",
        "https://www.notion.so/2260ab12cdff80c3a4c0d2f0ab1e9c44",
        __import__("logging").getLogger("test"),
        opener=opener,
    )
    store.load_schema()
    row = ExposureRow(
        "page-1",
        "키워드",
        "",
        "",
        "밀려남",
        "노출상태",
        "status",
        cafe_property="카페/ID",
        cafe_type="select",
    )
    store.update_check_result(row, status="노출완", cafe_name="씨씨앙/dtsx")
    payload = bodies[-1]["properties"]
    assert payload["카페/ID"]["select"]["name"] == "씨씨앙"
    store.update_check_result(row, status="노출완", cafe_name="줌마")
    payload = bodies[-1]["properties"]
    assert payload["카페/ID"]["select"]["name"] == "줌마"


def test_naver_login_detected_from_cookies() -> None:
    from v2r_auto.exposure_naver import is_naver_logged_in_cookies

    assert is_naver_logged_in_cookies([{"name": "NID_SES", "value": "1"}])
    assert is_naver_logged_in_cookies([{"name": "NID_AUT", "value": "1"}])
    assert not is_naver_logged_in_cookies([{"name": "NNB", "value": "1"}])
    assert not is_naver_logged_in_cookies([])


def test_naver_tab_is_home_or_search_only() -> None:
    from v2r_auto.exposure_naver import (
        SeleniumNaverSearch,
        ads_account_id,
        is_ads_center_url,
        is_keyword_tool_url,
    )

    search = SeleniumNaverSearch(
        type("Browser", (), {"driver": None})(),
        __import__("logging").getLogger("test"),
    )
    assert search._is_naver_search_or_home("https://www.naver.com/")
    assert search._is_naver_search_or_home(
        "https://search.naver.com/search.naver?query=test"
    )
    assert not search._is_naver_search_or_home("https://cafe.naver.com/ccang/1")
    assert not search._is_naver_search_or_home("https://nid.naver.com/nidlogin.login")
    assert not search._is_naver_search_or_home(
        "https://ads.naver.com/manage/ad-accounts/685753/dashboard"
    )
    assert search._ads_login_required("https://nid.naver.com/nidlogin.login")
    assert search._ads_login_required("https://searchad.naver.com/login")
    assert not search._ads_login_required(
        "https://ads.naver.com/manage/ad-accounts/685753/dashboard"
    )
    assert is_ads_center_url("https://ads.naver.com/manage/ad-accounts/685753/dashboard")
    assert is_ads_center_url("https://manage.searchad.naver.com/")
    assert not is_ads_center_url("https://www.naver.com/")
    assert ads_account_id(
        "https://ads.naver.com/manage/ad-accounts/685753/dashboard"
    ) == "685753"
    assert is_keyword_tool_url(
        "https://ads.naver.com/manage/ad-accounts/685753/sa/tool/keyword-planner"
    )
    assert not is_keyword_tool_url(
        "https://ads.naver.com/manage/ad-accounts/685753/dashboard"
    )
    assert not is_keyword_tool_url(
        "https://ads.naver.com/manage/ad-accounts/685753/tools/keyword"
    )
    assert search._keyword_tool_urls("685753")[0].endswith(
        "/685753/sa/tool/keyword-planner"
    )


def test_pause_then_resume_continues_next_keyword() -> None:
    naver = FakeNaver("<div id='main_pack'></div>")
    pause = threading.Event()
    pause.set()
    checker = ExposureChecker(
        type("N", (), {"update_status": staticmethod(lambda *_a: None)})(),
        naver,
        __import__("logging").getLogger("test"),
        delay_seconds=0,
    )

    def work() -> None:
        checker.run(
            [_row("하나"), _row("둘")],
            dry_run=True,
            pause_event=pause,
        )

    thread = threading.Thread(target=work)
    thread.start()
    time.sleep(0.4)
    assert naver.searched == []
    pause.clear()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert naver.searched == ["하나", "둘"]


def test_pause_after_first_keyword_waits_before_second() -> None:
    pause = threading.Event()
    naver = FakeNaver("<div id='main_pack'></div>")
    original_search = naver.search_integrated

    def search_and_pause(keyword: str) -> str:
        html = original_search(keyword)
        if keyword == "하나":
            pause.set()
        return html

    naver.search_integrated = search_and_pause  # type: ignore[method-assign]
    checker = ExposureChecker(
        type("N", (), {"update_status": staticmethod(lambda *_a: None)})(),
        naver,
        __import__("logging").getLogger("test"),
        delay_seconds=0,
    )

    def work() -> None:
        checker.run(
            [_row("하나"), _row("둘")],
            dry_run=True,
            pause_event=pause,
        )

    thread = threading.Thread(target=work)
    thread.start()
    deadline = time.time() + 2
    while time.time() < deadline and not pause.is_set():
        time.sleep(0.05)
    time.sleep(0.3)
    assert naver.searched == ["하나"]
    pause.clear()
    thread.join(timeout=2)
    assert naver.searched == ["하나", "둘"]


def test_empty_keyword_after_parentheses_is_skipped() -> None:
    naver = FakeNaver("<div id='main_pack'></div>")
    ExposureChecker(
        type("N", (), {"update_status": staticmethod(lambda *_a: None)})(),
        naver,
        __import__("logging").getLogger("test"),
        delay_seconds=0,
    ).run([_row("(메모만 있음)"), _row("코숨핏")], dry_run=True)
    assert naver.searched == ["코숨핏"]


def test_parse_keyword_lines_skips_blank_and_notes() -> None:
    text = """
    다크 초콜릿 (메모)
    
    코숨핏
    다크 초콜릿
    """
    assert parse_keyword_lines(text) == ["다크 초콜릿", "코숨핏"]


def test_match_selected_rows_uses_notion_keyword() -> None:
    rows = [
        _row("다크 초콜릿 (효능으로 노출)"),
        _row("코숨핏"),
        _row("다른 키워드"),
    ]
    matched, missing = match_selected_rows(rows, ["다크 초콜릿", "없는 키워드"])
    assert [row.keyword for row in matched] == ["다크 초콜릿 (효능으로 노출)"]
    assert missing == ["없는 키워드"]


def test_preserve_cafe_id_keeps_existing_suffix() -> None:
    assert preserve_cafe_id("씨씨앙/dtsx", "씨씨앙") == "씨씨앙/dtsx"
    assert preserve_cafe_id("양평맘/aa", "씨씨앙") == "씨씨앙"
    assert preserve_cafe_id("씨씨앙/dtsx", "") == "씨씨앙/dtsx"
    assert cafe_id_for_check("씨씨앙/dtsx", "밀려남", "") == ("씨씨앙/dtsx", None)
    assert cafe_id_for_check("씨씨앙/dtsx", "노출완", "씨씨앙") == ("씨씨앙/dtsx", None)
    assert cafe_id_for_check("양평맘/aa", "노출완", "씨씨앙") == ("씨씨앙", "씨씨앙")
    assert cafe_id_for_check("", "노출완", "씨씨앙") == ("씨씨앙", "씨씨앙")
    assert cafe_name_option(
        "씨씨앙",
        ["마이웨딩드림", "씨씨앙", "씨씨앙/dtsx", "양평맘", "줌마/cqu"],
    ) == "씨씨앙"
    assert cafe_name_option("씨씨앙/dtsx", ["씨씨앙", "씨씨앙/dtsx"]) == "씨씨앙"
    assert cafe_name_option("양평맘/aa", ["양평맘", "양평맘/xx"]) == "양평맘"
    assert cafe_name_option("줌마", ["줌마/cqu", "줌마/ypv"]) == "줌마"


def test_keywordstool_adds_pc_and_mobile() -> None:
    payload = {
        "keywordList": [
            {"relKeyword": "다크초콜릿", "monthlyPcQcCnt": 120, "monthlyMobileQcCnt": "< 10"},
        ]
    }
    assert keywordstool_volume(payload, "다크 초콜릿") == 130
    assert parse_qc_count("<10") == 10
    nested = {
        "data": {
            "keywordList": [
                {"relKeyword": "코숨핏", "monthlyPcQcCnt": 20, "monthlyMobileQcCnt": 30},
            ]
        }
    }
    assert keywordstool_volume(nested, "코숨핏") == 50
    assert keyword_tool_query("bnr17 유산균 효과") == "bnr17유산균효과"
    assert volume_from_result_cells(["BNR17유산균효과", "20", "110"], "bnr17 유산균 효과") == 130


def test_checker_writes_cafe_and_volumes() -> None:
    post = "https://cafe.naver.com/ccang/1"
    html = f"""
    <a href="https://cafe.naver.com/ccang">씨씨앙</a>
    <a href="{post}">글</a>
    """
    results = []

    class FakeNotion:
        def update_check_result(self, row, *, status, cafe_name=None, search_volume=None, volume_found=False):
            results.append((status, cafe_name, search_volume, volume_found))

    class VolumeNaver(FakeNaver):
        def lookup_search_volume(self, keyword: str) -> int:
            return 321

    row = _row("키워드")
    row.current_cafe = "씨씨앙/dtsx"
    ExposureChecker(
        FakeNotion(),
        VolumeNaver(html, {post: "코숨핏 후기"}),
        __import__("logging").getLogger("test"),
        delay_seconds=0,
    ).run([row], dry_run=False)
    assert results == [("노출완", None, 321, True)]
    assert row.current_cafe == "씨씨앙/dtsx"


def test_exposed_updates_cafe_only_when_different() -> None:
    post = "https://cafe.naver.com/ccang/1"
    html = f"""
    <a href="https://cafe.naver.com/ccang">씨씨앙</a>
    <a href="{post}">글</a>
    """
    results = []

    class FakeNotion:
        def update_check_result(self, row, *, status, cafe_name=None, search_volume=None, volume_found=False):
            results.append((status, cafe_name, search_volume, volume_found))

    class VolumeNaver(FakeNaver):
        def lookup_search_volume(self, keyword: str) -> int:
            return 10

    row = _row("키워드")
    row.current_cafe = "양평맘/aa"
    ExposureChecker(
        FakeNotion(),
        VolumeNaver(html, {post: "코숨핏 후기"}),
        __import__("logging").getLogger("test"),
        delay_seconds=0,
    ).run([row], dry_run=False)
    assert results == [("노출완", "씨씨앙", 10, True)]
    assert row.current_cafe == "씨씨앙"


def test_hidden_keeps_cafe_and_clears_exposed_volume() -> None:
    results = []

    class FakeNotion:
        def update_check_result(self, row, *, status, cafe_name=None, search_volume=None, volume_found=False):
            results.append((status, cafe_name, search_volume, volume_found))

    class VolumeNaver(FakeNaver):
        def lookup_search_volume(self, keyword: str) -> int:
            return 50

    row = _row("키워드", "노출완")
    row.current_cafe = "씨씨앙/dtsx"
    ExposureChecker(
        FakeNotion(),
        VolumeNaver("<div id='main_pack'></div>"),
        __import__("logging").getLogger("test"),
        delay_seconds=0,
    ).run([row], dry_run=False)
    assert results == [("밀려남", None, 50, True)]
    assert row.current_cafe == "씨씨앙/dtsx"
