from datetime import datetime
from pathlib import Path
import threading

import pytest

from v2r_auto.browser import (
    AFFILIATE_CAFE_SEARCH_TERMS,
    SE_ONE_SELECTION_INDEX,
    V2R_SE_ONE_URL,
    V2RBrowser,
)
from v2r_auto.history import HistoryCorruptedError, HistoryStore
from v2r_auto.content import ParsedArticle
from v2r_auto.models import AffiliateJob, JobStatus, PostJob, RunResult
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
    assert V2RBrowser._option_text_matches("게시판", "뷰티&미용", "뷰티미용")


def test_image_editor_is_prepared_in_visible_form_order() -> None:
    calls: list[tuple[str, str]] = []

    class FakeWait:
        def until(self, condition):
            return True

    class FakeDriver:
        def find_elements(self, by, selector):
            calls.append(("wait", selector))
            return [object()]

    class ImageBrowser(V2RBrowser):
        @property
        def wait(self):
            return FakeWait()

        def open_se_one_writer(self) -> None:
            calls.append(("open", ""))

        def _select_option(self, label: str, value: str) -> None:
            calls.append((label, value))

        def _fill_input(
            self,
            label: str,
            value: str,
            fallback_css: str | None = None,
        ) -> None:
            calls.append((label, value))

        def _fill_editor(self, body: str) -> None:
            calls.append(("본문", body))

        def _get_seone_document(self) -> dict:
            return {"document": {"components": []}}

    browser = object.__new__(ImageBrowser)
    browser.driver = FakeDriver()
    job = AffiliateJob(
        row_number=2,
        keyword="요즘 그릭요거트",
        article=ParsedArticle(
            title="그릭요거트 테스트",
            body="첫 문장\n{키워드}\n둘째 문장\n{B/A}",
            keyword="요즘 그릭요거트",
            tag="요즘그릭요거트",
            comments=[],
        ),
        cafe="러브인썸",
        account="yun661021",
        article_type="후기형",
    )

    browser._prepare_seone_image_editor(job, "뷰티&미용")

    assert calls[:6] == [
        ("open", ""),
        ("카페", "러브인썸"),
        ("계정", "yun661021"),
        ("게시판", "뷰티&미용"),
        ("제목", "그릭요거트 테스트"),
        ("본문", "첫 문장\n\n둘째 문장\n"),
    ]


def test_image_upload_clicks_photo_button_then_selects_local_file(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "요즘 그릭요거트.jpg"
    image_path.write_bytes(b"image")
    state = {"prepared": False, "clicked": False, "selected": ""}
    component = {"@ctype": "image", "id": "uploaded-image"}

    class FakeInput:
        def send_keys(self, value: str) -> None:
            state["selected"] = value

    image_input = FakeInput()

    class FakeWait:
        def until(self, condition):
            return [image_input]

    class FakeDriver:
        def execute_script(self, script, button) -> None:
            state["clicked"] = True

    class ImageBrowser(V2RBrowser):
        @property
        def wait(self):
            return FakeWait()

        def _prepare_seone_image_editor(self, job, menu_name: str) -> None:
            state["prepared"] = True

        def _get_seone_document(self) -> dict:
            components = [component] if state["selected"] else []
            return {"document": {"components": components}}

        def _seone_photo_button(self):
            return object()

        def _seone_image_inputs(self):
            return [image_input]

    browser = object.__new__(ImageBrowser)
    browser.driver = FakeDriver()
    job = AffiliateJob(
        row_number=2,
        keyword="요즘 그릭요거트",
        article=ParsedArticle(
            title="제목",
            body="본문\n{키워드}",
            keyword="요즘 그릭요거트",
            tag="요즘그릭요거트",
            comments=[],
        ),
        cafe="러브인썸",
        account="yun661021",
        article_type="후기형",
    )

    uploaded = browser._upload_one_seone_image(job, "뷰티&미용", image_path)

    assert state["prepared"] is True
    assert state["clicked"] is True
    assert state["selected"] == str(image_path.resolve())
    assert uploaded == component


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
