import logging
import random
import threading
from pathlib import Path

from v2r_auto.daily_posts import assign_daily_posts, load_daily_posts
from v2r_auto.models import DailyPost, JobStatus
from v2r_auto.runner import AffiliateRunner
from v2r_auto.sheet import load_affiliate_jobs


def write_affiliate_csv(tmp_path: Path, completion_url: str = "") -> Path:
    path = tmp_path / "affiliate.csv"
    path.write_text(
        "키워드,본문,카페명,작성계정,원고유형,완료 링크\n"
        '"갓비움 후기","제목 :\n제목입니다\n본문 :\n본문입니다\n댓글1:\n댓글입니다\n대댓글1:\n답글입니다",양평맘,writer,질문형,'
        f"{completion_url}\n",
        encoding="utf-8-sig",
    )
    return path


def test_loads_compact_affiliate_sheet_row(tmp_path: Path) -> None:
    jobs = load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)

    job = jobs[0]
    assert job.article.tag == "갓비움후기"
    assert job.cafe == "양평맘"
    assert job.article_type == "질문형"
    assert job.comments[0].children[0].label == "대댓글1"


def test_completion_link_skips_affiliate_row(tmp_path: Path) -> None:
    jobs = load_affiliate_jobs(
        write_affiliate_csv(tmp_path, "https://v2r.example/article"),
        selected_row_number=2,
    )

    assert jobs[0].status == JobStatus.SKIPPED


def test_missing_values_in_columns_a_to_e_skip_affiliate_row(tmp_path: Path) -> None:
    path = tmp_path / "affiliate.csv"
    path.write_text(
        (
            "키워드,본문,카페명,작성계정,원고유형,완료 링크\n"
            '"키워드","제목 : 제목\n본문 : 본문",양평맘,,질문형,\n'
            '"정상 키워드","제목 : 정상 제목\n본문 : 정상 본문",씨씨앙,writer,후기형,\n'
        ),
        encoding="utf-8-sig",
    )

    jobs = load_affiliate_jobs(path)

    assert len(jobs) == 1
    assert jobs[0].row_number == 3
    assert jobs[0].keyword == "정상 키워드"


def test_daily_posts_are_matched_to_cafe_without_reuse(tmp_path: Path) -> None:
    path = tmp_path / "daily.csv"
    path.write_text(
        "번호,제목,내용,카페\n"
        '1,분류,"제목 : 첫 일상\n본문 : 첫 본문",양평맘\n'
        '2,분류,"제목 : 둘 일상\n본문 : 둘 본문",양평맘\n',
        encoding="utf-8-sig",
    )
    jobs = [
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
    ]

    assign_daily_posts(jobs, load_daily_posts(path), random.Random(1))

    assert {job.daily_post.title for job in jobs if job.daily_post} == {"첫 일상", "둘 일상"}


class FakeAffiliateBrowser:
    def __init__(self) -> None:
        self.published = []

    def ensure_v2r_login(self, email: str, password: str) -> None:
        return None

    def publish_affiliate_revision(self, job, dry_run: bool) -> str:
        assert dry_run
        self.published.append(job)
        return ""


def test_affiliate_runner_uses_single_revision_flow(tmp_path: Path) -> None:
    job = load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0]
    browser = FakeAffiliateBrowser()
    runner = AffiliateRunner(
        browser=browser,  # type: ignore[arg-type]
        report_dir=tmp_path,
        logger=logging.getLogger("test"),
    )

    result, report = runner.run(
        jobs=[job],
        email="",
        password="",
        dry_run=True,
        stop_event=threading.Event(),
        progress=lambda current, total: None,
        daily_posts=[
            DailyPost(row_number=2, cafe="양평맘", title="일상", body="내용"),
        ],
        source_sheet_url="https://docs.google.com/spreadsheets/d/example/edit?gid=0",
    )

    assert result.succeeded == 1
    assert browser.published == [job]
    assert report.exists()
