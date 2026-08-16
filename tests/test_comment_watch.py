from pathlib import Path

import pytest

from v2r_auto.comment_watch import (
    CommentWatchError,
    apply_cafe_result,
    build_plan,
    cafe_article_url,
    decide_row,
    load_watch_rows,
    other_member_comment_count,
    page_requires_cafe_login,
    parse_article_view,
    plan_matches_sheet,
    source_id_from_url,
)


def revision_payload(
    *,
    source_id: str = "REV1",
    parent_source_id: str | None = "DAILY1",
    status: str = "RESERVED",
    cafe_id: int = 22788814,
    history: dict | None = None,
) -> dict:
    return {
        "naver_cafe_article_source": {
            "source_id": source_id,
            "parent_source_id": parent_source_id,
            "title": "수정 글",
        },
        "naver_cafe_article_destination": {
            "status": status,
            "cafe_id": cafe_id,
            "title": "수정 글",
        },
        "naver_cafe_article_history": history,
    }


def daily_payload(
    *,
    source_id: str = "DAILY1",
    article_id: int | None = 730069,
    cafe_id: int = 22788814,
) -> dict:
    history = None
    if article_id is not None:
        history = {
            "article_id": article_id,
            "cafe_id": cafe_id,
        }
    return {
        "naver_cafe_article_source": {
            "source_id": source_id,
            "parent_source_id": None,
            "child_source_id": "REV1",
            "title": "일상 글",
        },
        "naver_cafe_article_destination": {
            "status": "SUCCESS",
            "cafe_id": cafe_id,
            "title": "일상 글",
        },
        "naver_cafe_article_history": history,
    }


def write_sheet(tmp_path: Path, extra_row: str = "") -> Path:
    path = tmp_path / "brand.csv"
    path.write_text(
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,말머리,계정유형,이미지 없음,게시판명,일상 글에 댓글\n"
        "키워드A,본문,양평맘,writer,질문형,https://v2r.daboja.im/nc/articleDetail/REV1,,,,,\n"
        "키워드B,본문,양평맘,writer,질문형,,,,,,\n"
        "키워드C,본문,씨씨앙,writer,질문형,https://v2r.daboja.im/nc/articleDetail/REV2,,,,,\n"
        + extra_row,
        encoding="utf-8-sig",
    )
    return path


def cafe_html_with_member_comment() -> str:
    return """
    <div class="article_container">
      <h3>댓글 <em>1</em></h3>
      <ul class="comment_list">
        <li class="comment_item">
          <a class="comment_nickname">쇼비쇼비2</a>
          <p>인터넷 아님 무인발급기요</p>
        </li>
      </ul>
    </div>
    """


def cafe_html_without_comments() -> str:
    return """
    <div class="article_container">
      <h3>댓글 <em>0</em></h3>
      <ul class="comment_list"></ul>
      <script>window.__ARTICLE__ = {"commentCount": 0}</script>
    </div>
    """


def test_source_id_from_completion_url() -> None:
    assert (
        source_id_from_url("https://v2r.daboja.im/nc/articleDetail/01M008QZYX3PCMHP62HH61TZQM")
        == "01M008QZYX3PCMHP62HH61TZQM"
    )


def test_cafe_html_counts_other_member_not_v2r_number() -> None:
    assert other_member_comment_count(cafe_html_with_member_comment()) == 1
    assert other_member_comment_count(cafe_html_without_comments()) == 0
    assert other_member_comment_count('{"commentCount": 1}') == 1


def test_cafe_html_ignores_v2r_written_comments() -> None:
    html = """
    <li class="comment_item">
      <a class="comment_nickname">나는퀼보고(quilliant)</a>
      <span>V2R</span>
    </li>
    """
    assert other_member_comment_count(html) == 0


def test_login_wall_is_an_error() -> None:
    html = "<html>로그인이 필요합니다</html>"
    assert page_requires_cafe_login(html, "https://nid.naver.com/nidlogin.login") is True
    with pytest.raises(CommentWatchError, match="로그인"):
        other_member_comment_count(html, "https://nid.naver.com/nidlogin.login")


def test_logged_in_cafe_page_is_not_a_login_wall() -> None:
    html = cafe_html_with_member_comment() + (
        '<script src="https://nid.naver.com/login/static/js/login.js"></script>'
        "<a href='https://nid.naver.com/nidlogin.login'>로그인</a>"
    )
    url = "https://cafe.naver.com/f-e/cafes/22788814/articles/730069"
    assert page_requires_cafe_login(html, url) is False
    assert other_member_comment_count(html, url) == 1


def test_skip_when_previous_original_is_missing() -> None:
    revision = parse_article_view(revision_payload(parent_source_id=None, status="RESERVED"))
    decision = decide_row(revision)
    assert decision.action == "skip"
    assert decision.cafe_url == ""
    assert "원본글" in decision.reason


def test_skip_when_revision_already_published() -> None:
    revision = parse_article_view(revision_payload(status="SUCCESS"))
    decision = decide_row(revision)
    assert decision.action == "skip"
    assert "이미" in decision.reason


def test_open_cafe_even_when_v2r_comment_count_is_zero() -> None:
    revision = parse_article_view(revision_payload(status="RESERVED"))
    parent = parse_article_view(daily_payload())
    decision = decide_row(revision, parent)
    assert decision.action == "open_cafe"
    assert decision.cafe_url == cafe_article_url(22788814, 730069)
    assert apply_cafe_result(decision, 1).action == "mark"
    assert apply_cafe_result(decision, 0).action == "clear"
    assert apply_cafe_result(decision, 0).cafe_url == ""


def test_skip_when_cafe_article_id_is_missing() -> None:
    revision = parse_article_view(revision_payload(status="RESERVED"))
    parent = parse_article_view(daily_payload(article_id=None))
    decision = decide_row(revision, parent)
    assert decision.action == "skip"
    assert "카페 글 번호" in decision.reason


def test_three_identical_parses_match() -> None:
    payload = revision_payload(status="SUCCESS", history={"article_id": 730069})
    views = [parse_article_view(payload) for _ in range(3)]
    assert views[0] == views[1] == views[2]
    assert views[0].parent_source_id == "DAILY1"
    assert views[0].article_id == 730069


def test_load_and_build_plan_uses_cafe_page_not_v2r_count(tmp_path: Path) -> None:
    path = write_sheet(tmp_path)
    headers, rows = load_watch_rows(path)
    payloads = {
        "REV1": revision_payload(source_id="REV1", status="RESERVED"),
        "DAILY1": daily_payload(),
        "REV2": revision_payload(source_id="REV2", parent_source_id=None, status="RESERVED"),
    }
    opened: list[tuple[int, int]] = []

    def check_cafe(cafe_id: int, article_id: int) -> int:
        opened.append((cafe_id, article_id))
        return 1

    plan = build_plan(headers, rows, payloads.__getitem__, check_cafe)
    assert opened == [(22788814, 730069)]
    assert plan.checked_count() == 2
    assert plan.mark_count() == 1
    assert plan.rows[0]["일상 글에 댓글"] == cafe_article_url(22788814, 730069)
    assert plan.rows[0]["__reason"] == "카페에서 다른 회원 댓글 1개"
    assert plan.rows[1]["일상 글에 댓글"] == ""
    assert plan.rows[2]["일상 글에 댓글"] == ""
    assert plan.start_cell() == ("K", 2)
    chunks = plan.paste_chunks()
    assert chunks[0][0] == 2
    assert chunks[0][1] == cafe_article_url(22788814, 730069) + "\n\n\n"


def test_plan_clears_when_cafe_has_no_other_member(tmp_path: Path) -> None:
    path = write_sheet(tmp_path)
    headers, rows = load_watch_rows(path)
    payloads = {
        "REV1": revision_payload(status="RESERVED"),
        "DAILY1": daily_payload(),
        "REV2": revision_payload(source_id="REV2", parent_source_id=None),
    }
    plan = build_plan(headers, rows, payloads.__getitem__, lambda cafe_id, article_id: 0)
    assert plan.mark_count() == 0
    assert plan.rows[0]["__reason"] == "카페에서 다른 회원 댓글 없음"


def test_plan_matches_sheet_accepts_written_links(tmp_path: Path) -> None:
    path = write_sheet(tmp_path)
    headers, rows = load_watch_rows(path)
    payloads = {
        "REV1": revision_payload(status="RESERVED"),
        "DAILY1": daily_payload(),
        "REV2": revision_payload(source_id="REV2", parent_source_id=None),
    }
    plan = build_plan(headers, rows, payloads.__getitem__, lambda cafe_id, article_id: 1)
    sheet_rows = [
        {"일상 글에 댓글": cafe_article_url(22788814, 730069)},
        {"일상 글에 댓글": ""},
        {"일상 글에 댓글": ""},
    ]
    assert plan_matches_sheet(headers, sheet_rows, plan) == []
