from pathlib import Path

import pytest

from v2r_auto.comment_watch import (
    DEFAULT_SHEET_URL,
    CafeCommentView,
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
    user_facing_watch_error,
    v2r_article_is_gone,
)
from v2r_auto.sheets_write import (
    batch_update_url,
    build_comment_watch_batch_update,
    csv_cell_value,
    post_sheets_batch_update,
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


def test_author_and_comment_box_are_not_extra_comments() -> None:
    html = """
    <button class="nickname">바다그림자도</button>
    <div class="comment_inbox">
      <a class="comment_nickname">구리스틴</a>
    </div>
    <h3>댓글 <em>0</em></h3>
    <script>{"article":{"commentCount":0},"commentCount":0}</script>
    """
    assert other_member_comment_count(html) == 0

    html_one = """
    <button class="nickname">바다그림자도</button>
    <div class="comment_inbox">
      <a class="comment_nickname">구리스틴</a>
    </div>
    <h3>댓글 <em>1</em></h3>
    <li class="CommentItem">
      <a class="comment_nickname">쇼비쇼비2</a>
    </li>
    <script>{"article":{"commentCount":1},"commentCount":1}</script>
    """
    assert other_member_comment_count(html_one) == 1


def test_cafe_html_ignores_v2r_written_comments() -> None:
    html = """
    <h3>댓글 <em>1</em></h3>
    <li class="comment_item">
      <a class="comment_nickname">나는퀼보고(quilliant)</a>
      <span>V2R</span>
    </li>
    <script>{"commentCount": 1}</script>
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


def test_published_revision_still_opens_the_same_cafe_post() -> None:
    revision = parse_article_view(revision_payload(status="SUCCESS"))
    assert decide_row(revision).action == "need_parent"
    parent = parse_article_view(daily_payload())
    decision = decide_row(revision, parent)
    assert decision.action == "open_cafe"
    assert decision.published is True
    assert decision.cafe_url == cafe_article_url(22788814, 730069)
    assert (
        apply_cafe_result(decision, CafeCommentView(0, 12, 12)).action == "clear"
    )
    marked = apply_cafe_result(decision, CafeCommentView(1, 13, 12))
    assert marked.action == "mark"
    assert marked.cafe_url == decision.cafe_url
    assert "13" in marked.reason
    nickname = apply_cafe_result(decision, CafeCommentView(1, 12, 11))
    assert nickname.action == "mark"
    assert "다른 회원" in nickname.reason
    # 우리 댓글이 화면에 안 보이면 12개만으로는 링크를 남기지 않는다.
    assert (
        apply_cafe_result(decision, CafeCommentView(12, 12, 0)).action == "clear"
    )


def test_published_ssiang_row_uses_the_same_rule() -> None:
    revision = parse_article_view(
        revision_payload(status="DONE", cafe_id=25016228, history={"article_id": 88})
    )
    parent = parse_article_view(
        daily_payload(article_id=88, cafe_id=25016228)
    )
    decision = decide_row(revision, parent)
    assert decision.published is True
    assert decision.cafe_url == cafe_article_url(25016228, 88)
    assert apply_cafe_result(decision, CafeCommentView(0, 13, 12)).action == "mark"


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


def test_plan_marks_published_row_when_comments_reach_thirteen(
    tmp_path: Path,
) -> None:
    path = write_sheet(tmp_path)
    headers, rows = load_watch_rows(path)
    payloads = {
        "REV1": revision_payload(status="SUCCESS"),
        "DAILY1": daily_payload(),
        "REV2": revision_payload(source_id="REV2", parent_source_id=None),
    }
    opened: list[tuple[int, int]] = []

    def check_cafe(cafe_id: int, article_id: int) -> CafeCommentView:
        opened.append((cafe_id, article_id))
        return CafeCommentView(1, 13, 12)

    plan = build_plan(headers, rows, payloads.__getitem__, check_cafe)
    assert opened == [(22788814, 730069)]
    assert plan.rows[0]["일상 글에 댓글"] == cafe_article_url(22788814, 730069)
    assert plan.rows[0]["__reason"] == "발행 후 댓글 13개"
    assert plan.rows[0]["__action"] == "mark"


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


def test_leftover_empty_cells_are_cleared_in_batch_update(tmp_path: Path) -> None:
    path = write_sheet(tmp_path)
    headers, rows = load_watch_rows(path)
    payloads = {
        "REV1": revision_payload(status="RESERVED"),
        "DAILY1": daily_payload(),
        "REV2": revision_payload(source_id="REV2", parent_source_id=None),
    }
    plan = build_plan(headers, rows, payloads.__getitem__, lambda cafe_id, article_id: 0)
    stale = [
        {"일상 글에 댓글": cafe_article_url(22788814, 731466)},
        {"일상 글에 댓글": ""},
        {"일상 글에 댓글": cafe_article_url(22788814, 731473)},
    ]
    assert plan_matches_sheet(headers, stale, plan) == [
        f"2행 K: 기대 '' / 실제 '{cafe_article_url(22788814, 731466)}'",
        f"4행 K: 기대 '' / 실제 '{cafe_article_url(22788814, 731473)}'",
    ]
    assert plan.leftover_mark_cells(stale) == [
        (2, ""),
        (4, ""),
    ]
    payload = build_comment_watch_batch_update(plan, 0)
    request = payload["requests"][0]["updateCells"]
    assert request["range"] == {
        "sheetId": 0,
        "startRowIndex": 1,
        "endRowIndex": 4,
        "startColumnIndex": 10,
        "endColumnIndex": 11,
    }
    assert [row["values"][0]["userEnteredValue"]["stringValue"] for row in request["rows"]] == [
        "",
        "",
        "",
    ]
    assert "비워야" in user_facing_watch_error(
        RuntimeError("시트 표시를 확인하지 못했습니다: 13행 K")
    )
    assert csv_cell_value([["키워드", "본문"], ["홍현희", "본문"]], 2, 10) == ""


def test_post_sheets_batch_update_sends_bearer() -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def read(self) -> bytes:
            return b'{"replies":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

    def opener(request, timeout=45):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        return FakeResponse()

    result = post_sheets_batch_update(
        "abc123",
        {"requests": []},
        bearer="ya29.token",
        opener=opener,
    )
    assert result == {"replies": []}
    assert captured["url"] == batch_update_url("abc123")
    assert captured["auth"] == "Bearer ya29.token"


DELETED_SOURCE_ERROR = (
    "V2R 요청 실패 (400): /naver_cafe_articles/article - "
    '{"error":{"code":36,"reason":"DELETED_NAVER_CAFE_ARTICLE_SOURCE"}}'
)


def test_deleted_reason_is_recognized() -> None:
    assert v2r_article_is_gone(RuntimeError(DELETED_SOURCE_ERROR))
    assert not v2r_article_is_gone(RuntimeError("TOKEN_ERROR"))
    assert "건너" in user_facing_watch_error(RuntimeError(DELETED_SOURCE_ERROR))
    assert "1OwR_LSjO1ofOojldtSIqoxv0gieNSMx_t35_5G1VTCc" in DEFAULT_SHEET_URL


def test_deleted_completion_link_skips_and_later_rows_still_run(tmp_path: Path) -> None:
    extra = (
        "키워드D,본문,양평맘,writer,질문형,"
        "https://v2r.daboja.im/nc/articleDetail/REV3,,,,,\n"
    )
    path = write_sheet(tmp_path, extra_row=extra)
    headers, rows = load_watch_rows(path)
    payloads = {
        "REV3": revision_payload(source_id="REV3", parent_source_id="DAILY3"),
        "DAILY3": daily_payload(source_id="DAILY3", article_id=731500),
        "REV2": revision_payload(source_id="REV2", parent_source_id=None),
    }

    def fetch(source_id: str) -> dict:
        if source_id == "REV1":
            raise RuntimeError(DELETED_SOURCE_ERROR)
        return payloads[source_id]

    opened: list[tuple[int, int]] = []

    def check_cafe(cafe_id: int, article_id: int) -> int:
        opened.append((cafe_id, article_id))
        return 1

    plan = build_plan(headers, rows, fetch, check_cafe)
    assert plan.rows[0]["일상 글에 댓글"] == ""
    assert plan.rows[0]["__action"] == "skip"
    assert plan.rows[0]["__reason"] == "V2R에서 글이 삭제됨"
    assert opened == [(22788814, 731500)]
    assert plan.rows[3]["일상 글에 댓글"] == cafe_article_url(22788814, 731500)


def test_deleted_parent_skips_and_later_rows_still_run(tmp_path: Path) -> None:
    extra = (
        "키워드D,본문,양평맘,writer,질문형,"
        "https://v2r.daboja.im/nc/articleDetail/REV3,,,,,\n"
    )
    path = write_sheet(tmp_path, extra_row=extra)
    headers, rows = load_watch_rows(path)
    payloads = {
        "REV1": revision_payload(source_id="REV1", status="RESERVED"),
        "REV3": revision_payload(source_id="REV3", parent_source_id="DAILY3"),
        "DAILY3": daily_payload(source_id="DAILY3", article_id=731500),
        "REV2": revision_payload(source_id="REV2", parent_source_id=None),
    }
    parent_fetches = {"DAILY1": 0}

    def fetch(source_id: str) -> dict:
        if source_id == "DAILY1":
            parent_fetches["DAILY1"] += 1
            raise RuntimeError(DELETED_SOURCE_ERROR)
        return payloads[source_id]

    opened: list[tuple[int, int]] = []

    def check_cafe(cafe_id: int, article_id: int) -> int:
        opened.append((cafe_id, article_id))
        return 1

    plan = build_plan(headers, rows, fetch, check_cafe)
    assert parent_fetches["DAILY1"] == 1
    assert plan.rows[0]["일상 글에 댓글"] == ""
    assert plan.rows[0]["__action"] == "skip"
    assert plan.rows[0]["__reason"] == "V2R에서 원글이 삭제됨"
    assert opened == [(22788814, 731500)]
    assert plan.rows[3]["일상 글에 댓글"] == cafe_article_url(22788814, 731500)


def test_token_error_still_stops_the_run(tmp_path: Path) -> None:
    path = write_sheet(tmp_path)
    headers, rows = load_watch_rows(path)

    def fetch(source_id: str) -> dict:
        raise RuntimeError("V2R 요청 실패 (403): TOKEN_ERROR")

    with pytest.raises(RuntimeError, match="TOKEN_ERROR"):
        build_plan(headers, rows, fetch, lambda cafe_id, article_id: 0)


def test_cafe_open_failure_skips_unless_login(tmp_path: Path) -> None:
    extra = (
        "키워드D,본문,양평맘,writer,질문형,"
        "https://v2r.daboja.im/nc/articleDetail/REV3,,,,,\n"
    )
    path = write_sheet(tmp_path, extra_row=extra)
    headers, rows = load_watch_rows(path)
    payloads = {
        "REV1": revision_payload(status="RESERVED"),
        "DAILY1": daily_payload(),
        "REV2": revision_payload(source_id="REV2", parent_source_id=None),
        "REV3": revision_payload(source_id="REV3", parent_source_id="DAILY3"),
        "DAILY3": daily_payload(source_id="DAILY3", article_id=731500),
    }

    def check_cafe(cafe_id: int, article_id: int) -> int:
        if article_id == 730069:
            raise CommentWatchError("카페 글 730069을 열지 못했습니다. 네이버 로그인과 카페 가입을 확인해 주세요")
        return 1

    plan = build_plan(headers, rows, payloads.__getitem__, check_cafe)
    assert plan.rows[0]["일상 글에 댓글"] == ""
    assert plan.rows[0]["__action"] == "skip"
    assert plan.rows[0]["__reason"] == "카페 글을 열 수 없음"
    assert plan.rows[3]["일상 글에 댓글"] == cafe_article_url(22788814, 731500)


def test_cafe_login_error_still_stops_the_run(tmp_path: Path) -> None:
    path = write_sheet(tmp_path)
    headers, rows = load_watch_rows(path)
    payloads = {
        "REV1": revision_payload(status="RESERVED"),
        "DAILY1": daily_payload(),
        "REV2": revision_payload(source_id="REV2", parent_source_id=None),
    }

    def check_cafe(cafe_id: int, article_id: int) -> int:
        raise CommentWatchError("네이버 로그인 화면이 열렸습니다. 다시 로그인해 주세요")

    with pytest.raises(CommentWatchError, match="로그인"):
        build_plan(headers, rows, payloads.__getitem__, check_cafe)
