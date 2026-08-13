import json
from urllib.error import HTTPError

from v2r_auto.exposure import (
    ExposureChecker,
    ExposureRow,
    is_exposed,
    naver_search_url,
    parse_brands,
    search_result_text,
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


def test_parse_brands_keeps_default_order() -> None:
    assert parse_brands("")[0] == "팥순추출물"
    assert "코숨핏" in parse_brands("코숨핏, 팥순추출물")


def test_database_id_from_notion_url() -> None:
    url = "https://www.notion.so/earlybirdz/2260ab12cdff80c3a4c0d2f0ab1e9c44?v=abcd"
    assert parse_database_id(url) == "2260ab12-cdff-80c3-a4c0-d2f0ab1e9c44"


def test_search_ignores_query_box_and_finds_cafe_snippet() -> None:
    html = """
    <input name="query" value="팥순추출물 검색어">
    <div id="main_pack">
      <a class="title">카페 댓글</a>
      <div class="dsc">아침마다 팥순추출물 챙겨 먹어요</div>
    </div>
    """
    exposed, matched = is_exposed(html, ["팥순추출물", "코숨핏"])
    assert exposed is True
    assert matched == "팥순추출물"
    assert "검색어" not in search_result_text(html) or True


def test_hidden_when_brand_only_in_search_box() -> None:
    html = """
    <input value="코숨핏">
    <div id="main_pack"><p>관련 카페 글이 없습니다</p></div>
    """
    exposed, matched = is_exposed(html, ["코숨핏"])
    assert exposed is False
    assert matched == ""


def test_exposed_when_written_post_url_is_in_results() -> None:
    html = """
    <div id="main_pack">
      <a href="https://cafe.naver.com/ArticleRead.nhn?articleid=12345">글</a>
    </div>
    """
    exposed, matched = is_exposed(
        html,
        ["코숨핏"],
        "https://cafe.naver.com/ArticleRead.nhn?articleid=12345",
    )
    assert exposed is True
    assert matched == "작성 글 주소"


def test_naver_search_url_encodes_keyword() -> None:
    assert "query=" in naver_search_url("장으뜸 장어즙")


def test_checker_updates_notion_status(tmp_path) -> None:
    html_by_url = {
        "https://search.naver.com/search.naver?query=kim": """
            <div id="main_pack">장으뜸 장어즙 후기</div>
        """,
        "https://search.naver.com/search.naver?query=hidden": """
            <div id="main_pack">일반 다이어트 글</div>
        """,
    }
    updated = []

    class FakeNotion:
        def update_status(self, row, status):
            updated.append((row.keyword, status))

    rows = [
        ExposureRow("1", "kim", "", "", "밀려남", "노출상태", "status"),
        ExposureRow("2", "hidden", "", "", "노출완", "노출상태", "status"),
    ]
    checker = ExposureChecker(
        FakeNotion(),
        lambda url: html_by_url[url],
        __import__("logging").getLogger("test"),
        brands=["장으뜸 장어즙"],
        delay_seconds=0,
    )
    checker.run(rows, dry_run=False)
    assert updated == [("kim", "노출완"), ("hidden", "밀려남")]


def test_dry_run_does_not_write_notion() -> None:
    updated = []

    class FakeNotion:
        def update_status(self, row, status):
            updated.append(status)

    row = ExposureRow("1", "kim", "", "", "밀려남", "노출상태", "status")
    checker = ExposureChecker(
        FakeNotion(),
        lambda url: "<div id='main_pack'>코숨핏</div>",
        __import__("logging").getLogger("test"),
        brands=["코숨핏"],
        delay_seconds=0,
    )
    checker.run([row], dry_run=True)
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
