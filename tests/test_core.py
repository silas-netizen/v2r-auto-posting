from datetime import datetime
from pathlib import Path

from v2r_auto.browser import V2RBrowser
from v2r_auto.history import HistoryStore
from v2r_auto.models import JobStatus, PostJob, RunResult
from v2r_auto.report import write_report


def sample_job() -> PostJob:
    return PostJob(
        row_number=2,
        keyword="키워드",
        title="제목",
        body="본문",
        cafe="카페",
        board="게시판",
    )


def test_google_sheet_export_url() -> None:
    url = V2RBrowser._sheet_export_url(
        "https://docs.google.com/spreadsheets/d/abc_123/edit?gid=987#gid=987"
    )
    assert url == "https://docs.google.com/spreadsheets/d/abc_123/export?format=csv&gid=987"


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
