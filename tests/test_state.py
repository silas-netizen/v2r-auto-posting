from pathlib import Path

import pytest

from v2r_auto.content import parse_article
from v2r_auto.models import AffiliateJob
from v2r_auto.state import AnotherInstanceRunningError, InstanceLock, JobStateStore


def sample_job() -> AffiliateJob:
    return AffiliateJob(
        row_number=2,
        keyword="키워드",
        article=parse_article("키워드", "제목 : 제목\n본문 : 본문"),
        cafe="씨씨앙",
        account="writer",
        article_type="질문형",
    )


def test_job_state_resumes_source_ids(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    store = JobStateStore(path)
    record = store.load_or_create("https://sheet.example/edit?gid=0", sample_job())

    store.update(
        record["job_key"],
        stage="DAILY_CREATED",
        daily_source_id="daily-1",
    )
    store.close()

    reopened = JobStateStore(path)
    resumed = reopened.load_or_create(
        "https://sheet.example/edit?gid=0", sample_job()
    )

    assert resumed["stage"] == "DAILY_CREATED"
    assert resumed["daily_source_id"] == "daily-1"
    reopened.close()


def test_changed_content_creates_new_job_identity(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "jobs.db")
    first = store.load_or_create("https://sheet.example", sample_job())
    changed = sample_job()
    changed.article = parse_article("키워드", "제목 : 다른 제목\n본문 : 본문")
    second = store.load_or_create("https://sheet.example", changed)

    assert first["job_key"] != second["job_key"]
    store.close()


def test_instance_lock_rejects_second_worker(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    with InstanceLock(path):
        with pytest.raises(AnotherInstanceRunningError):
            with InstanceLock(path):
                pass
