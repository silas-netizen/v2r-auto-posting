from pathlib import Path

import pytest

from v2r_auto.models import JobStatus
from v2r_auto.sheet import SheetDefaults, SheetSchemaError, load_jobs


def write_csv(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "sheet.csv"
    path.write_text(content, encoding="utf-8-sig")
    return path


def test_loads_korean_columns_and_comments(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "키워드,제목,본문,카페명,게시판명,태그,댓글1,댓글2\n"
        '여행,제목 A,"첫 줄\n둘째 줄",카페 A,자유게시판,"여행,#서울",좋아요,궁금해요\n',
    )

    jobs = load_jobs(path)

    assert len(jobs) == 1
    assert jobs[0].keyword == "여행"
    assert jobs[0].body == "첫 줄\n둘째 줄"
    assert jobs[0].tags == ["여행", "서울"]
    assert jobs[0].comments == ["좋아요", "궁금해요"]


def test_applies_defaults_and_skips_completed(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "제목,본문,상태\n제목 A,본문 A,완료\n",
    )

    jobs = load_jobs(path, SheetDefaults(cafe="기본 카페", board="게시판"))

    assert jobs[0].cafe == "기본 카페"
    assert jobs[0].board == "게시판"
    assert jobs[0].status == JobStatus.SKIPPED


def test_requires_title_and_body_columns(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "키워드,메모\n테스트,내용\n")

    with pytest.raises(SheetSchemaError, match="필수 열"):
        load_jobs(path)


def test_handles_whitespace_around_headers(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        " 제목 , 본문 , 카페명 , 게시판명 \n제목 A,본문 A,카페 A,게시판 A\n",
    )

    jobs = load_jobs(path)

    assert jobs[0].title == "제목 A"
    assert jobs[0].body == "본문 A"


def test_loads_only_the_requested_google_sheet_row(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "키워드,제목,본문\n첫째,제목 A,본문 A\n둘째,제목 B,본문 B\n",
    )

    jobs = load_jobs(path, selected_row_number=3)

    assert len(jobs) == 1
    assert jobs[0].row_number == 3
    assert jobs[0].keyword == "둘째"
    assert jobs[0].title == "제목 B"


def test_requested_google_sheet_row_must_exist(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "제목,본문\n제목 A,본문 A\n")

    with pytest.raises(SheetSchemaError, match="행 9"):
        load_jobs(path, selected_row_number=9)
