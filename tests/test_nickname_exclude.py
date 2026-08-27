import pytest

from v2r_auto.nickname_browser import NicknameExcludeSession
from v2r_auto.nickname_exclude import (
    DEFAULT_CAFE_URL,
    DEFAULT_KEYWORDS,
    NicknameExcludeError,
    article_ids_from_json,
    build_sync_result,
    cafe_id_from_page,
    cafe_search_url,
    cafe_search_url_modern,
    cookies_show_naver_login,
    join_nicknames_lines,
    merge_nicknames,
    new_nicknames,
    nicknames_from_html,
    nicknames_from_json,
    page_is_missing,
    parse_cafe_address,
    require_keywords,
    search_api_urls,
    search_page_info,
    should_stop_search,
    split_keywords,
    write_nicknames_file,
)


def test_default_brand_keywords() -> None:
    assert DEFAULT_KEYWORDS == ("팥순", "자연방패", "장으뜸")


def test_keywords_can_be_added_later() -> None:
    assert split_keywords("팥순, 자연방패, 장으뜸, 새브랜드") == [
        "팥순",
        "자연방패",
        "장으뜸",
        "새브랜드",
    ]
    assert require_keywords("팥순 / 자연방패") == ["팥순", "자연방패"]


def test_parse_cafe_address_from_home_and_id() -> None:
    home = parse_cafe_address("https://cafe.naver.com/cantsb")
    assert home.slug == "cantsb"
    assert home.cafe_id == 25016228
    assert home.home_url == "https://cafe.naver.com/cantsb"

    numbered = parse_cafe_address("https://cafe.naver.com/f-e/cafes/22788814/menus/14")
    assert numbered.cafe_id == 22788814
    assert numbered.home_url == "https://cafe.naver.com/f-e/cafes/22788814"

    other = parse_cafe_address("cafe.naver.com/yangpyeongmom")
    assert other.slug == "yangpyeongmom"
    assert other.cafe_id is None
    assert other.home_url == "https://cafe.naver.com/yangpyeongmom"

    club = parse_cafe_address(
        "https://cafe.naver.com/ArticleSearchList.nhn?search.clubid=25016228"
    )
    assert club.cafe_id == 25016228


def test_parse_cafe_address_rejects_empty_and_non_cafe() -> None:
    with pytest.raises(NicknameExcludeError):
        parse_cafe_address("")
    with pytest.raises(NicknameExcludeError):
        parse_cafe_address("https://www.naver.com")


def test_cafe_id_from_page() -> None:
    assert cafe_id_from_page("", "https://cafe.naver.com/f-e/cafes/25016228") == 25016228
    assert cafe_id_from_page('var g_sClubId = "25016228";', "") == 25016228


def test_cookies_show_naver_login() -> None:
    assert cookies_show_naver_login(["NID_AUT", "NNB"])
    assert not cookies_show_naver_login(["NNB"])


def test_cafe_search_url_is_article_search_not_write() -> None:
    cafe = parse_cafe_address(DEFAULT_CAFE_URL)
    url = cafe_search_url("팥순", cafe, page=3)
    assert "f-e/cafes/25016228" in url
    assert "ca-cafes" not in url
    assert "page=3" in url
    assert "q=" in url
    assert "ArticleWrite" not in url
    assert "글쓰기" not in url
    fallback = cafe_search_url_modern("팥순", cafe)
    assert "ArticleSearchList.nhn" in fallback
    assert "ArticleWrite" not in fallback
    other = parse_cafe_address("https://cafe.naver.com/f-e/cafes/22788814")
    api = search_api_urls("팥순", 1, other)[0]
    assert "apis.cafe.naver.com/search/v2/cafes/22788814/search/articles" in api


def test_missing_cafe_page_is_detected() -> None:
    assert page_is_missing(
        "페이지를 찾을 수 없습니다",
        "https://cafe.naver.com/ca-cafes/25016228/menus/0?q=장으뜸",
    )
    assert not page_is_missing(
        "검색 결과",
        "https://cafe.naver.com/f-e/cafes/25016228/menus/0?q=장으뜸",
    )


def test_merge_adds_only_new_nicknames() -> None:
    existing = ["팥순이", "자연방패"]
    incoming = ["팥순이", "팥순ㅇㅣ", "장으뜸"]
    assert new_nicknames(existing, incoming) == ["팥순ㅇㅣ", "장으뜸"]
    assert merge_nicknames(existing, incoming) == ["팥순이", "자연방패", "팥순ㅇㅣ", "장으뜸"]


def test_nicknames_from_search_json() -> None:
    payload = {
        "result": {
            "articleList": [
                {"writerNickname": "팥순이", "subject": "후기"},
                {"nickname": "자연방패샵", "subject": "문의"},
            ]
        }
    }
    assert nicknames_from_json(payload) == ["팥순이", "자연방패샵"]


def test_nicknames_from_search_html() -> None:
    html = '''
    <a class="nickname">팥순이</a>
    <span data-nickname="장으뜸">장으뜸</span>
    <script>{"writerNickname":"자연방패"}</script>
    '''
    found = nicknames_from_html(html)
    assert "팥순이" in found
    assert "장으뜸" in found
    assert "자연방패" in found


def test_sync_result_keeps_existing_and_adds_new() -> None:
    plan = build_sync_result(
        ["팥순"],
        ["팥순이", "새닉"],
        ["팥순이"],
    )
    assert plan.added == ["새닉"]
    assert plan.saved == ["팥순이", "새닉"]
    assert "1개" in plan.summary()


def test_nickname_browser_module_loads() -> None:
    assert NicknameExcludeSession.__name__ == "NicknameExcludeSession"


def test_nicknames_are_written_one_per_line(tmp_path) -> None:
    text = join_nicknames_lines(["팥순이", "자연방패", "장으뜸"])
    assert text == "팥순이\n자연방패\n장으뜸"
    path = write_nicknames_file(tmp_path / "제외닉네임.txt", ["팥순이", "자연방패"])
    assert path.read_text(encoding="utf-8") == "팥순이\n자연방패\n"


def test_search_keeps_going_until_all_pages() -> None:
    payload = {
        "result": {
            "articleList": [{"articleId": 11, "writerNickname": "팥순이"}],
            "pageInfo": {
                "lastNavigationPageNumber": 4,
                "totalArticleCount": 52,
                "visibleNextButton": True,
            },
        }
    }
    last_page, total, has_more = search_page_info(payload)
    assert last_page == 4
    assert total == 52
    assert has_more
    assert article_ids_from_json(payload) == ["11"]
    assert should_stop_search(1, 4, True, 0, 1) is False
    assert should_stop_search(4, 4, False, 0, 1) is False
    assert should_stop_search(5, 4, False, 2, 0) is True
    assert should_stop_search(2, None, False, 2, 0) is True
