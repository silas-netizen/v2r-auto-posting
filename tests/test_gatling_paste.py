from pathlib import Path

import pytest
from openpyxl import load_workbook

from v2r_auto.content import parse_article
from v2r_auto.gatling_paste import (
    AFFILIATE_EXACT_BOARDS,
    MASTER_HEADER_ROW,
    MASTER_HEADERS,
    TYPE_COMMENT,
    TYPE_EDIT_POST,
    TYPE_NEW_POST,
    TYPE_REPLY,
    GatlingBrandJob,
    GatlingPasteError,
    assign_daily_posts,
    build_and_write_master,
    build_gatling_master,
    build_master_rows,
    exact_board_name,
    load_gatling_brand_jobs,
    reply_target_value,
)
from v2r_auto.models import DailyPost


QUESTION_SOURCE = """제목 :
실제 원고 제목

본문 :
실제 원고 본문

댓글1:
첫 댓글
대댓글1:
첫 답글
댓글2:
둘째 댓글
대댓글2:
둘째 답글
대대댓글2:
깊은 답글
대대대댓글2:
더 깊은 답글
"""


def write_brand_csv(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "brand.csv"
    path.write_text(content, encoding="utf-8-sig")
    return path


def write_daily_csv(tmp_path: Path) -> Path:
    path = tmp_path / "daily.csv"
    path.write_text(
        (
            "번호,제목,내용,카페\n"
            '1,분류,"제목 : 씨씨앙 일상\n본문 : 씨씨앙 일상 본문",씨씨앙\n'
            '2,분류,"제목 : 양평맘 일상\n본문 : 양평맘 일상 본문",양평맘\n'
        ),
        encoding="utf-8-sig",
    )
    return path


def make_job(
    *,
    cafe: str,
    board: str,
    cafe_article_url: str = "",
    daily_title: str = "일상 제목",
    daily_body: str = "일상 본문",
    with_daily: bool = True,
) -> GatlingBrandJob:
    job = GatlingBrandJob(
        row_number=2,
        keyword="단식원 가격",
        article=parse_article("단식원 가격", QUESTION_SOURCE),
        cafe=cafe,
        board=board,
        article_type="질문형",
        prefix="자유",
        cafe_article_url=cafe_article_url,
    )
    if with_daily:
        job.daily_post = DailyPost(
            row_number=2,
            cafe=cafe,
            title=daily_title,
            body=daily_body,
        )
    return job


def test_exact_board_name_ignores_spaces_and_keeps_cafe_spelling() -> None:
    assert exact_board_name("자유수다방", "씨씨앙") == "자유 수다방"
    assert exact_board_name("이모저모이야기", "양평맘") == "이모저모 이야기💕"
    assert exact_board_name("", "씨씨앙") == AFFILIATE_EXACT_BOARDS["씨씨앙"]


def test_exact_board_name_rejects_wrong_affiliate_board() -> None:
    with pytest.raises(GatlingPasteError, match="정확한 이름"):
        exact_board_name("다른게시판", "씨씨앙")


def test_self_owned_board_uses_known_exact_name() -> None:
    assert (
        exact_board_name("웨딩홀탐방기", "마이 웨딩 드림") == "웨딩홀탑방기"
    )
    assert (
        exact_board_name("가입인사", "고요한아침", extra_exact_names=("가입 인사",))
        == "가입 인사"
    )


def test_reply_targets_match_manuscript_labels() -> None:
    article = parse_article("단식원 가격", QUESTION_SOURCE)
    first_reply = article.comments[0].children[0]
    second_reply = article.comments[1].children[0]
    deep = second_reply.children[0]
    deeper = deep.children[0]

    assert first_reply.label == "대댓글1"
    assert reply_target_value(first_reply) == 1
    assert reply_target_value(second_reply) == 2
    assert deep.label == "대대댓글2"
    assert reply_target_value(deep) == 2.1
    assert deeper.label == "대대대댓글2"
    assert reply_target_value(deeper) == 2.2


def test_affiliate_daily_goes_to_new_post_and_manuscript_goes_to_edit() -> None:
    rows = build_master_rows(make_job(cafe="씨씨앙", board="자유수다방"))

    assert rows[0].type == TYPE_NEW_POST
    assert rows[0].title == "일상 제목"
    assert rows[0].body == "일상 본문"
    assert rows[0].board_name == "자유 수다방"
    assert rows[0].hashtag == "단식원 가격"
    assert rows[0].prefix == "자유"
    assert rows[0].link is None

    assert rows[1].type == TYPE_EDIT_POST
    assert rows[1].title == "실제 원고 제목"
    assert rows[1].body == "실제 원고 본문"
    assert rows[1].board_name == "자유 수다방"
    assert rows[1].link is None


def test_edit_and_comments_use_cafe_url_in_the_right_columns() -> None:
    url = "https://cafe.naver.com/cantsb/3398059"
    rows = build_master_rows(
        make_job(cafe="양평맘", board="이모저모이야기", cafe_article_url=url)
    )

    assert rows[1].type == TYPE_EDIT_POST
    assert rows[1].link == url
    assert rows[1].board_name == "이모저모 이야기💕"

    comments = [row for row in rows if row.type == TYPE_COMMENT]
    replies = [row for row in rows if row.type == TYPE_REPLY]
    assert [row.link for row in comments] == [url, url]
    assert [row.link for row in replies] == [1, 2, 2.1, 2.2]
    assert [row.result_link for row in replies] == [url, url, url, url]
    assert replies[2].body == "깊은 답글"
    assert replies[3].body == "더 깊은 답글"


def test_self_owned_manuscript_is_new_post_only() -> None:
    rows = build_master_rows(
        make_job(cafe="고요한아침", board="가입인사", with_daily=False)
    )

    types = [row.type for row in rows]
    assert types[0] == TYPE_NEW_POST
    assert TYPE_EDIT_POST not in types
    assert rows[0].title == "실제 원고 제목"
    assert rows[0].board_name == "가입인사"


def test_affiliate_without_daily_post_raises() -> None:
    job = make_job(cafe="씨씨앙", board="자유 수다방", with_daily=False)
    with pytest.raises(GatlingPasteError, match="일상 글"):
        build_master_rows(job)


def test_load_brand_sheet_reads_board_and_skips_completed(tmp_path: Path) -> None:
    path = write_brand_csv(
        tmp_path,
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,말머리,계정유형,이미지 없음,게시판명\n"
        f'"단식원 가격","{QUESTION_SOURCE}",씨씨앙,writer,질문형,,,비실명,,자유수다방\n'
        f'"완료키워드","제목 : 완료\n본문 : 완료본문",씨씨앙,writer,질문형,https://v2r.example/x,,,Y,자유수다방\n',
    )

    jobs, skipped = load_gatling_brand_jobs(path)

    assert len(jobs) == 1
    assert jobs[0].board == "자유수다방"
    assert jobs[0].article.comments[1].children[0].children[0].label == "대대댓글2"
    assert any("완료 링크" in item for item in skipped)


def test_build_assigns_daily_posts_and_writes_master_columns(tmp_path: Path) -> None:
    brand = write_brand_csv(
        tmp_path,
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,말머리,계정유형,이미지 없음,게시판명\n"
        f'"단식원 가격","{QUESTION_SOURCE}",씨씨앙,writer,질문형,,,비실명,,자유수다방\n'
        f'"뱃살 보조제","제목 : 자사 제목\n본문 : 자사 본문",고요한아침,writer,후기형,,,,,가입인사\n',
    )
    daily = write_daily_csv(tmp_path)
    output = tmp_path / "master.xlsx"

    result = build_and_write_master(brand, output, daily, rng=__import__("random").Random(1))

    assert result.type_counts()[TYPE_NEW_POST] == 2
    assert result.type_counts()[TYPE_EDIT_POST] == 1
    affiliate_new = next(
        row
        for row in result.rows
        if row.type == TYPE_NEW_POST and row.board_name == "자유 수다방"
    )
    assert affiliate_new.title == "씨씨앙 일상"
    self_owned_new = next(
        row
        for row in result.rows
        if row.type == TYPE_NEW_POST and row.board_name == "가입인사"
    )
    assert self_owned_new.title == "자사 제목"

    workbook = load_workbook(output)
    sheet = workbook["마스터"]
    headers = [sheet.cell(MASTER_HEADER_ROW, column).value for column in range(1, 20)]
    assert headers == list(MASTER_HEADERS)

    reply_row = None
    for row_number in range(MASTER_HEADER_ROW + 1, sheet.max_row + 1):
        if sheet.cell(row_number, 2).value == TYPE_REPLY and sheet.cell(row_number, 1).value == 2.1:
            reply_row = row_number
            break
    assert reply_row is not None
    assert sheet.cell(reply_row, 4).value == "깊은 답글"
    assert sheet.cell(reply_row, 16).value is None
    assert sheet.cell(reply_row, 7).value is None

    edit_row = next(
        row_number
        for row_number in range(MASTER_HEADER_ROW + 1, sheet.max_row + 1)
        if sheet.cell(row_number, 2).value == TYPE_EDIT_POST
    )
    assert sheet.cell(edit_row, 3).value == "실제 원고 제목"
    assert sheet.cell(edit_row, 10).value == "자유 수다방"


def test_assign_daily_posts_does_not_reuse_same_cafe_template(tmp_path: Path) -> None:
    jobs = [
        make_job(cafe="양평맘", board="이모저모이야기", with_daily=False),
        make_job(cafe="양평맘", board="이모저모이야기", with_daily=False),
    ]
    jobs[1].row_number = 3
    posts = [
        DailyPost(row_number=2, cafe="양평맘", title="일상1", body="본문1"),
        DailyPost(row_number=3, cafe="양평맘", title="일상2", body="본문2"),
    ]

    assign_daily_posts(jobs, posts, rng=__import__("random").Random(3))

    assert {job.daily_post.title for job in jobs if job.daily_post} == {"일상1", "일상2"}


def test_affiliate_sheet_without_daily_file_raises(tmp_path: Path) -> None:
    brand = write_brand_csv(
        tmp_path,
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,말머리,계정유형,이미지 없음,게시판명\n"
        f'"단식원 가격","{QUESTION_SOURCE}",씨씨앙,writer,질문형,,,,,자유수다방\n',
    )

    with pytest.raises(GatlingPasteError, match="일상 글 시트"):
        build_gatling_master(brand)
