from datetime import datetime
from pathlib import Path
import threading

import pytest

from v2r_auto.browser import (
    AFFILIATE_CAFE_SEARCH_TERMS,
    AutomationError,
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
    browser = object.__new__(V2RBrowser)
    assert browser._se_one_option_matches("계정", "doansli", "닉네임 🔥 doansli")
    assert not browser._se_one_option_matches(
        "계정",
        "doansli",
        "닉네임 🔥 doansli-extra",
    )


def test_board_selection_requires_exact_display_name() -> None:
    assert V2RBrowser._option_text_matches("게시판", "자유 수다방", "자유 수다방")
    assert not V2RBrowser._option_text_matches("게시판", "자유 수다방", "자유 게시판")
    assert V2RBrowser._option_text_matches(
        "게시판", "이모저모 이야기", "이모저모 이야기💘"
    )
    assert V2RBrowser._option_text_matches("게시판", "뷰티&미용", "뷰티미용")


def test_se_one_board_waits_without_repeatedly_toggling_dropdown() -> None:
    state = {"opened": 0, "polled": 0, "selected": False}

    class FakeSelection:
        def click(self) -> None:
            state["opened"] += 1

        def find_elements(self, by, selector):
            return []

        @property
        def text(self) -> str:
            return "뷰티&미용" if state["selected"] else ""

    class FakeOption:
        text = "뷰티&미용"

        def is_displayed(self) -> bool:
            return True

        def click(self) -> None:
            state["selected"] = True

    selection = FakeSelection()
    option = FakeOption()

    class FakeDriver:
        def execute_script(self, script, element) -> None:
            return None

        def find_elements(self, by, selector):
            state["polled"] += 1
            return [option] if state["polled"] >= 3 else []

    class BoardBrowser(V2RBrowser):
        def _visible_se_one_selections(self):
            return [object(), object(), selection, object()]

    browser = object.__new__(BoardBrowser)
    browser.driver = FakeDriver()
    browser.logger = __import__("logging").getLogger("test")

    browser._select_se_one_option("게시판", "뷰티&미용", 2)

    assert state["opened"] == 1
    assert state["polled"] >= 3
    assert state["selected"] is True


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
    browser.logger = __import__("logging").getLogger("test")
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
    destination = {
        "cafe_id": 26616683,
        "cafe_name": "러브 인썸 (Love in Some)",
        "naver_login_id": "yun661021",
        "menu_id": 9,
        "menu_name": "뷰티&미용",
    }

    browser._prepare_seone_image_editor(job, destination)

    assert calls[:6] == [
        ("open", ""),
        ("카페", "러브 인썸 (Love in Some)"),
        ("계정", "yun661021"),
        ("게시판", "뷰티&미용"),
        ("제목", "그릭요거트 테스트"),
        ("본문", "첫 문장\n\n둘째 문장\n"),
    ]


def test_image_editor_retries_whole_setup_when_account_is_not_ready(
    monkeypatch,
) -> None:
    state = {"opened": 0}

    class FakeWait:
        def until(self, condition):
            return True

    class FakeDriver:
        def find_elements(self, by, selector):
            return [object()]

    class RetryBrowser(V2RBrowser):
        @property
        def wait(self):
            return FakeWait()

        def open_se_one_writer(self) -> None:
            state["opened"] += 1

        def _select_option(self, label: str, value: str) -> None:
            if label == "계정" and state["opened"] == 1:
                raise AutomationError("SE-ONE 계정 목록이 준비되지 않았습니다")

        def _fill_input(
            self,
            label: str,
            value: str,
            fallback_css: str | None = None,
        ) -> None:
            return None

        def _fill_editor(self, body: str) -> None:
            return None

        def _get_seone_document(self) -> dict:
            return {"document": {"components": []}}

    monkeypatch.setattr("v2r_auto.browser.time.sleep", lambda _seconds: None)
    browser = object.__new__(RetryBrowser)
    browser.driver = FakeDriver()
    browser.logger = __import__("logging").getLogger("test")
    job = AffiliateJob(
        row_number=2,
        keyword="키워드",
        article=ParsedArticle(
            title="제목",
            body="본문",
            keyword="키워드",
            tag="키워드",
            comments=[],
        ),
        cafe="러브인썸",
        account="doansli",
        article_type="후기형",
    )
    destination = {
        "cafe_id": 26616683,
        "cafe_name": "러브 인썸 (Love in Some)",
        "naver_login_id": "doansli",
        "menu_id": 18,
        "menu_name": "뷰티&미용",
    }

    browser._prepare_seone_image_editor(job, destination)

    assert state["opened"] == 2


def test_fill_editor_uses_visible_smarteditor_paragraph(monkeypatch) -> None:
    state = {"target": None, "keys": [], "default_content": False}

    class FakeParagraph:
        tag_name = "p"
        rect = {"width": 800, "height": 24, "x": 454, "y": 562}

        def is_displayed(self) -> bool:
            return True

        def get_attribute(self, name: str):
            return ""

    paragraph = FakeParagraph()

    class FakeSwitchTo:
        def default_content(self) -> None:
            state["default_content"] = True

    class FakeDriver:
        switch_to = FakeSwitchTo()

        def find_elements(self, by, selector):
            if selector == ".se-module-text .se-text-paragraph":
                return [paragraph]
            return []

    class FakeWait:
        def until(self, condition):
            return condition(browser.driver)

    class FakeActions:
        def __init__(self, driver):
            return None

        def move_to_element(self, element):
            state["target"] = element
            return self

        def click(self):
            return self

        def key_down(self, key):
            state["keys"].append(("down", key))
            return self

        def send_keys(self, value):
            state["keys"].append(("text", value))
            return self

        def key_up(self, key):
            state["keys"].append(("up", key))
            return self

        def perform(self):
            return None

    class EditorBrowser(V2RBrowser):
        @property
        def wait(self):
            return FakeWait()

    monkeypatch.setattr("v2r_auto.browser.ActionChains", FakeActions)
    browser = object.__new__(EditorBrowser)
    browser.driver = FakeDriver()

    browser._fill_editor("첫 줄\n둘째 줄")

    assert state["target"] is paragraph
    assert ("text", "첫 줄\n둘째 줄") in state["keys"]
    assert state["default_content"] is True


def test_reads_document_from_public_smarteditor_api() -> None:
    document = {
        "document": {
            "components": [
                {"@ctype": "text", "value": []},
                {"@ctype": "image", "id": "image-1"},
            ]
        },
        "documentId": "",
    }

    class FakeDriver:
        def execute_async_script(self, script):
            assert "getDocumentData" in script
            assert "cafepc001" in script
            return {"ok": True, "value": document}

    browser = object.__new__(V2RBrowser)
    browser.driver = FakeDriver()

    assert browser._get_seone_document() == document


def test_multiple_images_share_one_prepared_se_one_editor(
    tmp_path: Path,
) -> None:
    image_paths = [
        tmp_path / "요즘 그릭요거트.jpg",
        tmp_path / "532357.jpg",
    ]
    for image_path in image_paths:
        image_path.write_bytes(b"image")
    state = {
        "prepared": 0,
        "clicked": 0,
        "selected": [],
    }

    class FakeInput:
        def __init__(self, input_id: str):
            self.id = input_id

        def send_keys(self, value: str) -> None:
            state["selected"].append(value)

    base_input = FakeInput("base-input")
    generated_inputs = [
        FakeInput("generated-input-1"),
        FakeInput("generated-input-2"),
    ]

    class FakeWait:
        def until(self, condition):
            return condition(None)

    class FakeDriver:
        def execute_script(self, script, button) -> None:
            state["clicked"] += 1

    class ImageBrowser(V2RBrowser):
        @property
        def wait(self):
            return FakeWait()

        def _prepare_seone_image_editor(self, job, destination: dict) -> None:
            state["prepared"] += 1
            state["menu_id"] = destination["menu_id"]

        def _get_seone_document(self) -> dict:
            components = [
                {"@ctype": "image", "id": f"uploaded-image-{index}"}
                for index, _path in enumerate(state["selected"], start=1)
            ]
            return {"document": {"components": components}}

        def _seone_photo_button(self):
            return object()

        def _seone_image_inputs(self):
            return [base_input] + generated_inputs[: state["clicked"]]

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
    destination = {
        "cafe_id": 26616683,
        "cafe_name": "러브 인썸 (Love in Some)",
        "naver_login_id": "yun661021",
        "menu_id": 9,
        "menu_name": "뷰티&미용",
    }

    browser.logger = __import__("logging").getLogger("test")
    uploaded = browser.upload_affiliate_images(job, destination, image_paths)

    assert state["prepared"] == 1
    assert state["menu_id"] == 9
    assert state["clicked"] == 2
    assert state["selected"] == [str(path.resolve()) for path in image_paths]
    assert [component["id"] for component in uploaded] == [
        "uploaded-image-1",
        "uploaded-image-2",
    ]


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
