import json
from urllib.error import HTTPError

from v2r_auto.exposure import (
    DEFAULT_CAFE_NAMES,
    ExposureChecker,
    ExposureRow,
    brand_found,
    collect_our_cafe_hits,
    is_cafe_article_url,
    matching_cafe_name,
    parse_brands,
    parse_cafes,
    strip_parenthetical,
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


def test_naver_login_detected_from_cookies() -> None:
    from v2r_auto.exposure_naver import is_naver_logged_in_cookies

    assert is_naver_logged_in_cookies([{"name": "NID_SES", "value": "1"}])
    assert is_naver_logged_in_cookies([{"name": "NID_AUT", "value": "1"}])
    assert not is_naver_logged_in_cookies([{"name": "NNB", "value": "1"}])
    assert not is_naver_logged_in_cookies([])


def test_naver_tab_is_home_or_search_only() -> None:
    from v2r_auto.exposure_naver import SeleniumNaverSearch

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
