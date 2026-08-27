from datetime import datetime
from pathlib import Path
import threading

import pytest

from v2r_auto.browser import (
    AFFILIATE_CAFE_SEARCH_TERMS,
    SE_ONE_SELECTION_INDEX,
    V2R_SE_ONE_URL,
    V2RBrowser,
    sheet_values_match,
)
from v2r_auto.history import HistoryCorruptedError, HistoryStore
from v2r_auto.models import JobStatus, PostJob, RunResult
from v2r_auto.report import write_report
from v2r_auto.runner import AutomationRunner, RunOptions


def sample_job() -> PostJob:
    return PostJob(
        row_number=2,
        keyword="키워드",
        title="제목",
        body="본문",
        cafe="카페",
        board="게시판",
    )


def test_sheet_values_match_treats_thousands_comma_as_same_number() -> None:
    assert sheet_values_match("5680", "5,680")
    assert sheet_values_match("5,680", "5680")
    assert sheet_values_match("5680", "5680")
    assert sheet_values_match("0", "0")
    assert not sheet_values_match("5680", "")
    assert not sheet_values_match("5680", "5681")
    assert not sheet_values_match(
        "https://search.naver.com/search.naver?query=a",
        "https://search.naver.com/search.naver?query=b",
    )


def test_google_sheet_export_url() -> None:
    url = V2RBrowser._sheet_export_url(
        "https://docs.google.com/spreadsheets/d/abc_123/edit?gid=987#gid=987"
    )
    assert url == "https://docs.google.com/spreadsheets/d/abc_123/export?format=csv&gid=987"


def test_se_one_uses_direct_v2r_url() -> None:
    assert V2R_SE_ONE_URL == "https://v2r.daboja.im/nc/seone"


def test_cafe_option_matching_ignores_display_whitespace() -> None:
    assert V2RBrowser._normalize_option_text("양평맘") in V2RBrowser._normalize_option_text(
        "양평 맘's 전원 Story"
    )
    assert AFFILIATE_CAFE_SEARCH_TERMS["양평맘"] == "양평"
    assert SE_ONE_SELECTION_INDEX == {"카페": 0, "계정": 1, "게시판": 2, "말머리": 3}


def test_account_selection_never_uses_partial_id_matches() -> None:
    assert not V2RBrowser._option_text_matches("계정", "prtchht", "prtchhtt")
    assert V2RBrowser._option_text_matches("계정", "prtchht", "prtchht")


def test_board_selection_requires_exact_display_name() -> None:
    assert V2RBrowser._option_text_matches("게시판", "자유 수다방", "자유 수다방")
    assert not V2RBrowser._option_text_matches("게시판", "자유 수다방", "자유 게시판")
    assert V2RBrowser._option_text_matches(
        "게시판", "이모저모 이야기", "이모저모 이야기💘"
    )


def test_api_capture_summarizes_payload_keys_without_values() -> None:
    assert V2RBrowser._request_payload_summary('{"title":"비밀 글","body":"본문"}') == {
        "format": "json",
        "keys": ["body", "title"],
    }


def test_history_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    job = sample_job()
    store = HistoryStore(path)
    assert not store.contains(job)

    store.record(job)

    assert HistoryStore(path).contains(job)


def test_report_is_utf8_bom_text(tmp_path: Path) -> None:
    job = sample_job()
    job.status = JobStatus.SUCCESS
    job.message = "발행 완료"
    now = datetime(2026, 8, 7, 12, 0, 0)
    result = RunResult(started_at=now, finished_at=now, dry_run=False, jobs=[job])

    path = write_report(result, tmp_path)

    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "발행 완료" in path.read_text(encoding="utf-8-sig")


def test_rejects_corrupted_history(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(HistoryCorruptedError):
        HistoryStore(path)


class FakeBrowser:
    def __init__(self):
        self.filled: list[PostJob] = []

    def ensure_v2r_login(self, email: str, password: str) -> None:
        return None

    def fill_post(self, job: PostJob, dry_run: bool) -> None:
        assert dry_run
        self.filled.append(job)


def test_runner_dry_run_never_publishes(tmp_path: Path) -> None:
    browser = FakeBrowser()
    runner = AutomationRunner(
        browser=browser,  # type: ignore[arg-type]
        history_path=tmp_path / "history.json",
        report_dir=tmp_path,
        logger=__import__("logging").getLogger("test"),
    )
    progress: list[tuple[int, int]] = []

    result, report = runner.run(
        jobs=[sample_job()],
        email="",
        password="",
        options=RunOptions(dry_run=True, delay_seconds=0),
        stop_event=threading.Event(),
        progress=lambda current, total: progress.append((current, total)),
    )

    assert result.succeeded == 1
    assert result.jobs[0].message == "입력 검증 완료"
    assert browser.filled
    assert report.exists()
    assert progress[-1] == (1, 1)
