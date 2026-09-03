from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from v2r_auto.cafe_posts import (
    EXCEL_HEADERS,
    CafeComment,
    CafePostError,
    CafePostRow,
    article_ids_from_html,
    article_ids_from_payload,
    article_url,
    comments_from_payload,
    excel_filename,
    format_comments,
    html_to_text,
    parse_cafe_board_target,
    post_from_payload,
    write_cafe_posts_xlsx,
)


def test_parse_cafe_home_url() -> None:
    target = parse_cafe_board_target("https://cafe.naver.com/cantsb")
    assert target.slug == "cantsb"
    assert target.menu_id == 0
    assert target.home_url == "https://cafe.naver.com/cantsb"


def test_parse_board_url_keeps_menu() -> None:
    target = parse_cafe_board_target(
        "https://cafe.naver.com/cantsb",
        "https://cafe.naver.com/f-e/cafes/25016228/menus/12",
    )
    assert target.cafe_id == 25016228
    assert target.menu_id == 12
    assert target.slug == "cantsb"


def test_parse_iframe_board_query() -> None:
    target = parse_cafe_board_target(
        "",
        "https://cafe.naver.com/cantsb?iframe_url=/ArticleList.nhn?search.clubid=25016228&search.menuid=88",
    )
    assert target.slug == "cantsb"
    assert target.cafe_id == 25016228
    assert target.menu_id == 88


def test_empty_address_is_error() -> None:
    with pytest.raises(CafePostError):
        parse_cafe_board_target("", "")


def test_article_url_uses_slug() -> None:
    target = parse_cafe_board_target("https://cafe.naver.com/cantsb")
    assert article_url(target, 345) == "https://cafe.naver.com/cantsb/345"


def test_format_comments_is_nickname_then_text() -> None:
    text = format_comments(
        [
            CafeComment("홍길동", "잘 보고 갑니다"),
            CafeComment("김철수", "감사합니다"),
        ]
    )
    assert text == "홍길동\n잘 보고 갑니다\n\n김철수\n감사합니다"


def test_html_body_keeps_line_breaks() -> None:
    assert "첫째" in html_to_text("<p>첫째</p><br>둘째<script>x()</script>")
    assert "둘째" in html_to_text("<p>첫째</p><br>둘째<script>x()</script>")
    assert "x()" not in html_to_text("<p>첫째</p><br>둘째<script>x()</script>")


def test_article_ids_from_list_payload() -> None:
    payload = {
        "result": {
            "articleList": [
                {"articleId": 11, "subject": "글1"},
                {"articleId": 12, "subject": "글2"},
                {"commentId": 99, "content": "댓글"},
            ]
        }
    }
    assert article_ids_from_payload(payload) == [11, 12]
    assert article_ids_from_html('href="/articles/11" articleid=12') == [11, 12]


def test_comments_from_payload_keep_order() -> None:
    payload = {
        "comments": {
            "items": [
                {"commentId": 1, "nickname": "첫째", "content": "안녕"},
                {"commentId": 2, "nickName": "둘째", "content": "<b>반가워</b>"},
            ]
        }
    }
    comments = comments_from_payload(payload)
    assert [(item.nickname, item.text) for item in comments] == [
        ("첫째", "안녕"),
        ("둘째", "반가워"),
    ]


def test_post_from_payload_reads_fields() -> None:
    payload = {
        "result": {
            "article": {
                "subject": "제목입니다",
                "contentHtml": "<p>본문입니다</p>",
                "writer": {"nickname": "작성닉"},
                "menuName": "자유게시판",
                "cafeName": "씨씨앙",
            },
            "comments": {
                "items": [{"commentId": 3, "nickname": "댓닉", "content": "댓글이다"}]
            },
        }
    }
    row = post_from_payload(payload)
    assert row.cafe_name == "씨씨앙"
    assert row.board_name == "자유게시판"
    assert row.author == "작성닉"
    assert row.title == "제목입니다"
    assert row.body == "본문입니다"
    assert row.comment_text() == "댓닉\n댓글이다"


def test_write_excel_has_requested_columns(tmp_path: Path) -> None:
    path = write_cafe_posts_xlsx(
        tmp_path / excel_filename("씨씨앙", datetime(2026, 9, 3, 10, 28)),
        [
            CafePostRow(
                cafe_name="씨씨앙",
                board_name="자유게시판",
                author="작성닉",
                title="제목",
                body="본문",
                comments=[
                    CafeComment("첫째", "댓글1"),
                    CafeComment("둘째", "댓글2"),
                ],
            )
        ],
    )
    assert path.name == "카페글_씨씨앙_20260903_1028.xlsx"
    book = load_workbook(path)
    sheet = book.active
    assert [cell.value for cell in sheet[1]] == list(EXCEL_HEADERS)
    assert [cell.value for cell in sheet[2]] == [
        "씨씨앙",
        "자유게시판",
        "작성닉",
        "제목",
        "본문",
        "첫째\n댓글1\n\n둘째\n댓글2",
    ]


def test_gui_mentions_excel_and_login() -> None:
    source = Path("v2r_auto/gui_cafe_posts.py").read_text(encoding="utf-8")
    assert "네이버 로그인" in source
    assert "게시판 주소(선택)" in source
    assert "엑셀" in source
