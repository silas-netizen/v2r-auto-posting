import logging
import random
import threading
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from v2r_auto.affiliate_api import AffiliateApiPublisher, _content_json
from v2r_auto.content import parse_article
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


def test_missing_account_and_type_marks_affiliate_row_skipped(tmp_path: Path) -> None:
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

    assert len(jobs) == 2
    assert jobs[0].status == JobStatus.SKIPPED
    assert jobs[1].row_number == 3
    assert jobs[1].keyword == "정상 키워드"


def test_blank_account_uses_h_column_account_type(tmp_path: Path) -> None:
    path = tmp_path / "affiliate.csv"
    path.write_text(
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,말머리,계정유형\n"
        '"키워드","제목 : 제목\n본문 : 본문",씨씨앙,,질문형,,,비실명\n',
        encoding="utf-8-sig",
    )

    job = load_affiliate_jobs(path)[0]

    assert job.account == ""
    assert job.account_type == "비실명"
    assert job.status == JobStatus.PENDING


def test_image_disabled_reads_y_from_i_column(tmp_path: Path) -> None:
    path = tmp_path / "affiliate.csv"
    path.write_text(
        "키워드,본문,카페명,작성계정,원고유형,완료 링크,말머리,계정유형,이미지 없음\n"
        '"키워드","제목 : 제목\n본문 : {키워드}",씨씨앙,writer,질문형,,,,Y\n',
        encoding="utf-8-sig",
    )

    assert load_affiliate_jobs(path)[0].image_disabled is True


def test_daily_posts_are_matched_to_cafe_without_reuse(tmp_path: Path) -> None:
    path = tmp_path / "daily.csv"
    path.write_text(
        (
            "번호,제목,내용,카페\n"
            '1,분류,"제목 : 첫 일상\n본문 : 첫 본문",양평맘\n'
            '2,분류,"제목 : 둘 일상\n본문 : 둘 본문",양평맘\n'
            '3,분류,"제목 :\n본문 : 깨진 본문",양평맘\n'
        ),
        encoding="utf-8-sig",
    )
    jobs = [
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
    ]

    assign_daily_posts(jobs, load_daily_posts(path), random.Random(1))

    assert {job.daily_post.title for job in jobs if job.daily_post} == {"첫 일상", "둘 일상"}


def test_api_content_preserves_blank_lines_as_paragraphs() -> None:
    document = json.loads(_content_json("첫 줄\n\n둘째 줄"))
    paragraphs = document["document"]["components"][0]["value"]

    assert [item["nodes"][0]["value"] for item in paragraphs] == [
        "첫 줄",
        "",
        "둘째 줄",
    ]


def test_api_comments_keep_five_roots_and_seven_replies(tmp_path: Path) -> None:
    job = load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0]
    # Expand the small fixture to the production 12-label pattern.
    job.article = parse_article(
        "키워드",
        "제목 : 제목\n본문 : 본문\n"
        + "\n".join(
            [
                "댓글1: c1", "대댓글1: r1",
                "댓글2: c2", "대댓글2: r2", "대대댓글2: rr2", "대대대댓글2: rrr2",
                "댓글3: c3", "대댓글3: r3",
                "댓글4: c4", "대댓글4: r4",
                "댓글5: c5", "대댓글5: r5",
            ]
        ),
    )
    publisher = AffiliateApiPublisher(None, logging.getLogger("test"))
    publisher._member = lambda cafe_id, account: {  # type: ignore[method-assign]
        "member_key": f"key-{account}",
        "naver_login_id": account,
        "nick": account,
    }

    comments = publisher._comments(job, datetime.now(timezone.utc), 1)

    assert len(comments) == 5
    assert sum(len(root["comments"]) for root in comments) == 7
    comment2 = comments[1]
    assert comment2["comments"][1]["naver_login_id"] == comment2["naver_login_id"]
    assert comment2["comments"][2]["naver_login_id"] != job.account


def test_api_assignment_uses_actual_real_name_type(tmp_path: Path) -> None:
    jobs = [
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
        load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0],
    ]
    jobs[0].account = ""
    jobs[0].account_type = "실명"
    jobs[1].account = ""
    jobs[1].account_type = "비실명"

    class FakeAllocator(AffiliateApiPublisher):
        def _capture_authorization(self) -> None:
            return None

        def _request(self, method, path, payload=None, query=None):
            if path == "/naver_cafes/naver_join_cafes":
                return {"cafes": [{"cafe_id": 1, "pc_cafe_name": "양평맘"}]}
            if path == "/navers/accounts":
                return {
                    "accounts": [
                        {
                            "naver_login_id": "real-id",
                            "is_block": False,
                            "is_login_fail": False,
                            "my_info_v2": {"is_real_name": True},
                        },
                        {
                            "naver_login_id": "alias-id",
                            "is_block": False,
                            "is_login_fail": False,
                            "my_info_v2": {"is_real_name": False},
                        },
                    ]
                }
            if path == "/naver_cafes/naver_join_cafe":
                return {
                    "naver_accounts": [
                        {"login_id": "real-id", "force_drop": False},
                        {"login_id": "alias-id", "force_drop": False},
                    ]
                }
            if path == "/naver_cafe_articles/board_histories":
                return {"histories": [], "next_token": None}
            raise AssertionError(path)

    allocator = FakeAllocator(None, logging.getLogger("test"))
    assigned = allocator.assign_accounts(jobs)

    assert [job.account for job in assigned] == ["real-id", "alias-id"]


def test_comment_time_collision_moves_one_minute() -> None:
    publisher = AffiliateApiPublisher(None, logging.getLogger("test"))
    start = datetime(2026, 8, 10, 9, 0, 15, tzinfo=timezone.utc)

    first = publisher._comment("account", "첫 댓글", start)
    second = publisher._comment("account", "둘째 댓글", start)

    first_at = datetime.fromisoformat(first["start_at"].replace("Z", "+00:00"))
    second_at = datetime.fromisoformat(second["start_at"].replace("Z", "+00:00"))
    assert second_at == first_at + timedelta(minutes=1)


class FakeAffiliateBrowser:
    def __init__(self) -> None:
        self.published = []

    def ensure_v2r_login(self, email: str, password: str) -> None:
        return None

    def start_affiliate_api_run(self, jobs=None) -> None:
        return None

    def assign_affiliate_accounts(self, jobs):
        return []

    def publish_affiliate_revision(
        self, job, dry_run: bool, resume=None, checkpoint=None
    ) -> str:
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


def test_affiliate_runner_retries_with_replacement_account(tmp_path: Path) -> None:
    job = load_affiliate_jobs(write_affiliate_csv(tmp_path), selected_row_number=2)[0]
    job.account_type = "실명"

    class RetryBrowser(FakeAffiliateBrowser):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.sheet_updates = []

        def publish_affiliate_revision(
            self, job, dry_run: bool, resume=None, checkpoint=None
        ) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("NAVER_LOGIN_FAIL")
            return ""

        def classify_affiliate_failure(self, error):
            return ("실패: 네이버 로그인 실패", True)

        def replace_failed_affiliate_account(self, job):
            job.account = "replacement"
            return job.account

        def update_sheet_cell(self, url, column, row, value):
            self.sheet_updates.append((column, row, value))

    browser = RetryBrowser()
    runner = AffiliateRunner(
        browser=browser,  # type: ignore[arg-type]
        report_dir=tmp_path,
        logger=logging.getLogger("test"),
    )

    result, _ = runner.run(
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
    assert browser.calls == 2
    assert browser.sheet_updates == [("D", 2, "replacement")]
