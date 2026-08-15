from pathlib import Path

from v2r_auto.comment_watch import (
    build_plan,
    cafe_article_url,
    decide_row,
    extra_comment_count,
    load_watch_rows,
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
    real_comment_count: int = 0,
    write_comment_count: int = 0,
) -> dict:
    history = None
    if article_id is not None:
        history = {
            "article_id": article_id,
            "cafe_id": cafe_id,
            "real_comment_count": real_comment_count,
            "write_comment_count": write_comment_count,
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


def test_source_id_from_completion_url() -> None:
    assert (
        source_id_from_url("https://v2r.daboja.im/nc/articleDetail/01M008QZYX3PCMHP62HH61TZQM")
        == "01M008QZYX3PCMHP62HH61TZQM"
    )


def test_extra_comments_ignore_our_own_writes() -> None:
    assert extra_comment_count({"real_comment_count": 12, "write_comment_count": 12}) == 0
    assert extra_comment_count({"real_comment_count": 3, "write_comment_count": 0}) == 3
    assert extra_comment_count(None) == 0


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


def test_mark_reserved_daily_with_other_comments() -> None:
    revision = parse_article_view(revision_payload(status="RESERVED"))
    parent = parse_article_view(daily_payload(real_comment_count=2, write_comment_count=0))
    decision = decide_row(revision, parent)
    assert decision.action == "mark"
    assert decision.cafe_url == cafe_article_url(22788814, 730069)


def test_clear_reserved_daily_without_other_comments() -> None:
    revision = parse_article_view(revision_payload(status="RESERVED"))
    parent = parse_article_view(daily_payload(real_comment_count=0, write_comment_count=0))
    decision = decide_row(revision, parent)
    assert decision.action == "clear"
    assert decision.cafe_url == ""


def test_three_identical_parses_match() -> None:
    payload = revision_payload(status="SUCCESS", history={"article_id": 730069, "real_comment_count": 12, "write_comment_count": 12})
    views = [parse_article_view(payload) for _ in range(3)]
    assert views[0] == views[1] == views[2]
    assert views[0].parent_source_id == "DAILY1"
    assert extra_comment_count(payload["naver_cafe_article_history"]) == 0


def test_load_and_build_plan(tmp_path: Path) -> None:
    path = write_sheet(tmp_path)
    headers, rows = load_watch_rows(path)
    payloads = {
        "REV1": revision_payload(source_id="REV1", status="RESERVED"),
        "DAILY1": daily_payload(real_comment_count=1),
        "REV2": revision_payload(source_id="REV2", parent_source_id=None, status="RESERVED"),
    }
    plan = build_plan(headers, rows, payloads.__getitem__)
    assert plan.checked_count() == 2
    assert plan.mark_count() == 1
    assert plan.rows[0]["일상 글에 댓글"] == cafe_article_url(22788814, 730069)
    assert plan.rows[1]["일상 글에 댓글"] == ""
    assert plan.rows[2]["일상 글에 댓글"] == ""
    assert plan.start_cell() == ("K", 2)
    chunks = plan.paste_chunks()
    assert chunks[0][0] == 2
    assert chunks[0][1] == cafe_article_url(22788814, 730069) + "\n\n\n"


def test_plan_matches_sheet_accepts_written_links(tmp_path: Path) -> None:
    path = write_sheet(tmp_path)
    headers, rows = load_watch_rows(path)
    payloads = {
        "REV1": revision_payload(status="RESERVED"),
        "DAILY1": daily_payload(real_comment_count=1),
        "REV2": revision_payload(source_id="REV2", parent_source_id=None),
    }
    plan = build_plan(headers, rows, payloads.__getitem__)
    sheet_rows = [
        {"일상 글에 댓글": cafe_article_url(22788814, 730069)},
        {"일상 글에 댓글": ""},
        {"일상 글에 댓글": ""},
    ]
    assert plan_matches_sheet(headers, sheet_rows, plan) == []
