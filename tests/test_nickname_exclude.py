from v2r_auto.nickname_exclude import (
    DEFAULT_KEYWORDS,
    build_sync_result,
    cafe_search_url,
    merge_nicknames,
    new_nicknames,
    nicknames_from_html,
    nicknames_from_json,
    require_keywords,
    split_keywords,
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


def test_cafe_search_url_is_article_search_not_write() -> None:
    url = cafe_search_url("팥순")
    assert "ArticleSearchList" in url
    assert "clubid=25016228" in url
    assert "query=" in url
    assert "ArticleWrite" not in url
    assert "글쓰기" not in url


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
