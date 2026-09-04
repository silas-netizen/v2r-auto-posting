from datetime import datetime
from pathlib import Path
import threading

import pytest
from PIL import Image
from selenium.common.exceptions import StaleElementReferenceException

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


def test_downloaded_csv_must_contain_expected_sheet_headers(
    tmp_path: Path,
) -> None:
    daily = tmp_path / "daily.csv"
    daily.write_text(
        "번호,제목,내용,카페\n1,분류,본문,씨씨앙\n",
        encoding="utf-8-sig",
    )
    wrong = tmp_path / "brand.csv"
    wrong.write_text(
        "키워드,본문,카페명\n키워드,본문,씨씨앙\n",
        encoding="utf-8-sig",
    )

    assert V2RBrowser._csv_has_headers(daily, {"내용", "카페"})
    assert not V2RBrowser._csv_has_headers(wrong, {"내용", "카페"})


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


def test_se_one_selection_ignores_stale_dropdown_options() -> None:
    state = {"selected": False}

    class FakeSelection:
        text = ""

        def click(self):
            return None

        def find_elements(self, by, selector):
            return []

    class StaleOption:
        def is_displayed(self):
            raise StaleElementReferenceException("rerendered")

    class LiveOption:
        text = "자유 수다방"

        def is_displayed(self):
            return True

        def click(self):
            state["selected"] = True
            selection.text = self.text

    selection = FakeSelection()

    class FakeDriver:
        def execute_script(self, script, element):
            return None

        def find_elements(self, by, selector):
            return [StaleOption(), LiveOption()]

    class BoardBrowser(V2RBrowser):
        def _visible_se_one_selections(self):
            return [selection]

    browser = object.__new__(BoardBrowser)
    browser.driver = FakeDriver()
    browser.logger = __import__("logging").getLogger("stale-option-test")

    browser._select_se_one_option("게시판", "자유 수다방", 0)

    assert state["selected"] is True


def test_se_one_selection_waits_for_loading_overlay_to_clear() -> None:
    state = {"overlay_checks": 0, "clicked": False}

    class FakeSelection:
        text = ""

        def click(self):
            state["clicked"] = True
            self.text = "자유 수다방"

        def find_elements(self, by, selector):
            return []

    class FakeOption:
        text = "자유 수다방"

        def is_displayed(self):
            return True

        def click(self):
            selection.text = self.text

    selection = FakeSelection()

    class FakeDriver:
        def execute_script(self, script, element):
            if "elementFromPoint" not in script:
                return None
            state["overlay_checks"] += 1
            return state["overlay_checks"] >= 3

        def find_elements(self, by, selector):
            return [FakeOption()]

    class BoardBrowser(V2RBrowser):
        def _visible_se_one_selections(self):
            return [selection]

    browser = object.__new__(BoardBrowser)
    browser.driver = FakeDriver()
    browser.logger = __import__("logging").getLogger("overlay-wait-test")

    browser._select_se_one_option("게시판", "자유 수다방", 0)

    assert state["overlay_checks"] == 3
    assert state["clicked"] is True


def test_known_subscription_notice_is_dismissed_before_seone_work() -> None:
    state = {"clicked": False}

    class FakeButton:
        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

    class FakeDriver:
        page_source = (
            "N 카페 요금제 종료 1일전 안내 "
            "오늘 하루 안보기 닫기"
        )

        def find_elements(self, by, selector):
            return [FakeButton()]

        def execute_script(self, script, element):
            state["clicked"] = True

    browser = object.__new__(V2RBrowser)
    browser.driver = FakeDriver()
    browser.logger = __import__("logging").getLogger("notice-test")

    assert browser._dismiss_known_seone_notice() is True
    assert state["clicked"] is True


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

    calls.clear()
    job.cafe = "씨씨앙"
    affiliate_destination = {
        "cafe_id": 25016228,
        "cafe_name": "국내1위 다이어트 커뮤니티 씨씨앙(식단,운동,후기,헬스,체험단)",
        "naver_login_id": "writer",
        "menu_id": 328,
        "menu_name": "자유 수다방",
    }
    browser._prepare_seone_image_editor(job, affiliate_destination)

    assert calls[1:4] == [
        ("카페", "씨씨앙"),
        ("계정", "writer"),
        ("게시판", "자유 수다방"),
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


def test_photo_focus_uses_last_document_paragraph_even_when_offscreen(
    monkeypatch,
) -> None:
    state = {"target": None, "scrolled": None}

    class FakeParagraph:
        rect = {"width": 800, "height": 24, "x": 454, "y": -900}

        def is_displayed(self) -> bool:
            return True

    paragraph = FakeParagraph()

    class FakeDriver:
        def find_elements(self, by, selector):
            return [paragraph] if selector == "paragraph-last" else []

        def execute_script(self, script, element):
            state["scrolled"] = element

    class FocusBrowser(V2RBrowser):
        def _get_seone_document(self) -> dict:
            return {
                "document": {
                    "components": [
                        {
                            "@ctype": "text",
                            "value": [
                                {"@ctype": "paragraph", "id": "paragraph-first"},
                                {"@ctype": "paragraph", "id": "paragraph-last"},
                            ],
                        }
                    ]
                }
            }

    class FakeActions:
        def __init__(self, driver):
            return None

        def move_to_element(self, element):
            state["target"] = element
            return self

        def click(self):
            return self

        def perform(self):
            return None

    monkeypatch.setattr("v2r_auto.browser.ActionChains", FakeActions)
    browser = object.__new__(FocusBrowser)
    browser.driver = FakeDriver()

    browser._focus_seone_text_paragraph()

    assert state["scrolled"] is paragraph
    assert state["target"] is paragraph


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


def test_image_component_waits_for_final_resource_metadata() -> None:
    incomplete = {
        "@ctype": "image",
        "src": "",
        "path": None,
        "fileName": None,
        "fileSize": 0,
    }
    complete = {
        "@ctype": "image",
        "src": "https://example.test/photo.jpg?type=w1600",
        "path": "/photo.jpg",
        "fileName": "photo.jpg",
        "fileSize": 1024,
    }

    assert V2RBrowser._image_component_ready(incomplete) is False
    assert V2RBrowser._image_component_ready(complete) is True
    assert V2RBrowser._image_component_ready(
        {
            "@ctype": "imageGroup",
            "images": [complete, {**complete, "fileName": "second.jpg"}],
        }
    ) is True


def test_multiple_images_share_one_prepared_se_one_editor(
    tmp_path: Path,
) -> None:
    image_paths = [
        tmp_path / "요즘 그릭요거트.jpg",
        tmp_path / "532357.jpg",
    ]
    for image_path in image_paths:
        Image.new("RGB", (10, 10), "white").save(image_path)
    state = {
        "prepared": 0,
        "clicked": 0,
        "focused": 0,
        "selected": [],
    }

    class FakeInput:
        def __init__(self, input_id: str):
            self.id = input_id
            self.value = ""

        def send_keys(self, value: str) -> None:
            state["selected"].append(value)
            self.value = value

        def get_attribute(self, name: str):
            return self.value if name == "value" else ""

    base_input = FakeInput("base-input")
    generated_inputs = [FakeInput("generated-input-1")]

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
                {
                    "@ctype": "image",
                    "id": f"uploaded-image-{index}",
                    "src": f"https://example.test/image-{index}.jpg",
                    "path": f"/image-{index}.jpg",
                    "fileName": f"image-{index}.jpg",
                    "fileSize": 1024,
                }
                for index, _path in enumerate(state["selected"], start=1)
            ]
            return {"document": {"components": components}}

        def _focus_seone_text_paragraph(self) -> None:
            state["focused"] += 1

        def _wait_for_seone_idle(self) -> None:
            return None

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
    assert state["focused"] == 2
    assert state["selected"] == [str(path.resolve()) for path in image_paths]
    assert [component["id"] for component in uploaded] == [
        "uploaded-image-1",
        "uploaded-image-2",
    ]


def test_image_upload_failure_reopens_editor_and_retries_all_images(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "retry.jpg"
    Image.new("RGB", (10, 10), "white").save(image_path)
    state = {"prepared": 0, "uploaded": 0}

    class RetryImageBrowser(V2RBrowser):
        def _prepare_seone_image_editor(self, job, destination):
            state["prepared"] += 1

        def _upload_one_seone_image(self, image_path, *, timeout_seconds=45):
            state["uploaded"] += 1
            if state["prepared"] == 1:
                raise AutomationError("일시적인 업로드 정지")
            return {
                "@ctype": "image",
                "id": "retried-image",
                "src": "https://example.test/retry.jpg",
                "path": "/retry.jpg",
                "fileName": "retry.jpg",
                "fileSize": 100,
            }

    browser = object.__new__(RetryImageBrowser)
    browser.driver = object()
    browser.logger = __import__("logging").getLogger("image-retry-test")
    job = AffiliateJob(
        row_number=137,
        keyword="키워드",
        article=ParsedArticle(
            title="제목",
            body="본문\n{키워드}",
            keyword="키워드",
            tag="키워드",
            comments=[],
        ),
        cafe="씨씨앙",
        account="writer",
        article_type="질문형",
    )

    uploaded = browser.upload_affiliate_images(
        job,
        {
            "cafe_id": 25016228,
            "cafe_name": "씨씨앙",
            "naver_login_id": "writer",
            "menu_id": 328,
            "menu_name": "자유 수다방",
        },
        [image_path],
    )

    assert state == {"prepared": 2, "uploaded": 2}
    assert uploaded[0]["id"] == "retried-image"


def test_corrupt_image_is_rejected_before_opening_editor(tmp_path: Path) -> None:
    image_path = tmp_path / "corrupt.jpg"
    image_path.write_bytes(b"not-an-image")

    with pytest.raises(AutomationError, match="손상"):
        V2RBrowser._validate_upload_image(image_path)


def test_image_input_detaching_after_send_is_treated_as_normal(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "detached.jpg"
    Image.new("RGB", (10, 10), "white").save(image_path)
    state = {"selected": False}

    class DetachingInput:
        id = "file-input"

        def send_keys(self, value):
            state["selected"] = True

        def get_attribute(self, name):
            if state["selected"]:
                raise StaleElementReferenceException("input replaced")
            return ""

    class ImageBrowser(V2RBrowser):
        def _get_seone_document(self):
            components = []
            if state["selected"]:
                components.append(
                    {
                        "@ctype": "image",
                        "id": "uploaded",
                        "src": "https://example.test/detached.jpg",
                        "path": "/detached.jpg",
                        "fileName": "detached.jpg",
                        "fileSize": 100,
                    }
                )
            return {"document": {"components": components}}

        def _wait_for_seone_idle(self):
            return None

        def _focus_seone_text_paragraph(self):
            return None

        def _seone_photo_button(self):
            return None

        def _seone_image_inputs(self):
            return [DetachingInput()]

        def _seone_upload_error_text(self):
            return ""

    browser = object.__new__(ImageBrowser)
    browser.driver = object()

    uploaded = browser._upload_one_seone_image(image_path)

    assert uploaded["id"] == "uploaded"


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
