from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any


V2R_LIST_URL = "https://v2r.daboja.im/nc/board?view=list"
V2R_WRITER_URL = "https://v2r.daboja.im/nc/seone"
SELECTION_INDEX = {"카페": 0, "계정": 1, "게시판": 2, "말머리": 3}
BLOCK_TEXT = "비정상적인 접근이 감지되어 요청이 차단되었습니다"


class V2RBrowserError(RuntimeError):
    pass


class PlaywrightV2RBrowser:
    """Visible V2R UI driver with a persistent execution-PC profile.

    It uses no V2R API and never submits credentials. The operator logs in
    once in the persistent profile. Anti-automation warnings are surfaced
    instead of bypassed.
    """

    def __init__(
        self,
        profile_dir: Path,
        download_dir: Path,
        *,
        headless: bool = False,
        timeout_seconds: int = 20,
        playwright_factory: Any = None,
    ):
        if playwright_factory is None:
            from playwright.sync_api import sync_playwright

            playwright_factory = sync_playwright
        self.profile_dir = profile_dir
        self.download_dir = download_dir
        self.headless = headless
        self.timeout_ms = timeout_seconds * 1000
        self.playwright_factory = playwright_factory
        self.runtime = None
        self.context = None
        self.page = None
        self.last_title = ""

    def start(self) -> None:
        if self.context is not None:
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.runtime = self.playwright_factory().start()
        self.context = self.runtime.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir.resolve()),
            headless=self.headless,
            accept_downloads=True,
            downloads_path=str(self.download_dir.resolve()),
            no_viewport=True,
            args=["--start-maximized", "--disable-notifications"],
        )
        self.context.set_default_timeout(self.timeout_ms)
        self.context.set_default_navigation_timeout(self.timeout_ms)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def close(self) -> None:
        context, runtime = self.context, self.runtime
        self.context = None
        self.runtime = None
        self.page = None
        try:
            if context is not None:
                context.close()
        finally:
            if runtime is not None:
                runtime.stop()

    def open_login(self) -> None:
        self.start()
        self._goto(V2R_LIST_URL)

    def verify_login(self) -> None:
        self.start()
        source = self.page.content()
        if BLOCK_TEXT in source:
            raise V2RBrowserError(
                "V2R이 자동화 브라우저 접근을 차단했습니다. 우회하지 않고 중단합니다."
            )
        if self.page.locator("input[type='password']").count():
            raise V2RBrowserError(
                "V2R 로그인이 필요합니다. 실행 PC의 전용 창에서 직접 로그인하세요."
            )
        if "/nc/board" not in self.page.url:
            raise V2RBrowserError("V2R 게시글 목록 로그인 상태를 확인하지 못했습니다")

    def open_writer(self) -> None:
        self.verify_login()
        self._goto(V2R_WRITER_URL)
        self.page.locator(".n-base-selection").nth(2).wait_for(state="visible")

    def select_destination(self, cafe: str, account: str, board: str) -> None:
        self._select("카페", cafe)
        self._select("계정", account)
        self._select("게시판", board)

    def fill_article(self, title: str, body: str) -> None:
        self.last_title = title
        title_inputs = self.page.locator(
            "input[placeholder*='제목'], textarea[placeholder*='제목']"
        )
        for index in range(title_inputs.count()):
            title_input = title_inputs.nth(index)
            if title_input.is_visible() and title_input.is_enabled():
                title_input.fill(title)
                break
        else:
            raise V2RBrowserError("제목 입력란을 찾지 못했습니다")
        editor = self.page.locator(
            ".se-module-text .se-text-paragraph, "
            "[contenteditable='true']:not([title='Channel chat']), "
            ".ProseMirror, .ql-editor"
        )
        for index in range(editor.count()):
            candidate = editor.nth(index)
            if candidate.is_visible() and candidate.is_enabled():
                candidate.click()
                candidate.press("Control+A")
                candidate.fill(body)
                return
        raise V2RBrowserError("본문 편집기를 찾지 못했습니다")

    def set_schedule(self, scheduled_at: datetime) -> None:
        inputs = self.page.locator("input[type='datetime-local']")
        visible = [inputs.nth(index) for index in range(inputs.count()) if inputs.nth(index).is_visible()]
        if len(visible) != 1:
            raise V2RBrowserError("예약 시간 입력란을 하나만 찾지 못했습니다")
        value = scheduled_at.astimezone().strftime("%Y-%m-%dT%H:%M")
        visible[0].evaluate(
            """(input, value) => {
                const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(input, value);
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            value,
        )

    def register(self) -> str:
        self._click(("등록",), exact=True)
        self.page.wait_for_load_state("domcontentloaded")
        if "/nc/articleDetail/" not in self.page.url:
            link = self.page.get_by_role("link", name=self.last_title, exact=True)
            if not link.count():
                raise V2RBrowserError("등록 후 완료 게시글 링크를 찾지 못했습니다")
            link.first.click()
            self.page.wait_for_url(re.compile(r"/nc/articleDetail/"))
        return self.page.url

    def verify(self, url: str) -> bool:
        if not url or "/nc/articleDetail/" not in url:
            return False
        self._goto(url)
        source = self.page.content()
        return self.last_title in source and BLOCK_TEXT not in source

    def open_article(self, url: str) -> None:
        self._goto(url)

    def reserve_revision(self, scheduled_at: datetime) -> None:
        self._click(("수정 글 예약",), exact=True)
        self.page.wait_for_load_state("domcontentloaded")
        self.set_schedule(scheduled_at)

    def reserve_comment(
        self,
        text: str,
        *,
        parent_text: str | None,
        scheduled_at: datetime,
    ) -> None:
        if parent_text:
            parent = self.page.get_by_text(parent_text, exact=True).first
            ancestor = parent
            for _ in range(6):
                button = ancestor.get_by_text("답글쓰기", exact=True)
                if button.count():
                    button.first.click()
                    break
                ancestor = ancestor.locator("xpath=..")
            else:
                raise V2RBrowserError("대상 댓글의 답글쓰기 버튼을 찾지 못했습니다")
        inputs = self.page.locator("textarea, [contenteditable='true']")
        visible = [
            inputs.nth(index)
            for index in range(inputs.count())
            if inputs.nth(index).is_visible() and inputs.nth(index).is_enabled()
        ]
        if not visible:
            raise V2RBrowserError("댓글 입력란을 찾지 못했습니다")
        visible[-1].fill(text)
        self.set_schedule(scheduled_at)
        self._click(("예약",), exact=True)

    def _goto(self, url: str) -> None:
        self.page.bring_to_front()
        self.page.goto(url, wait_until="domcontentloaded")
        if BLOCK_TEXT in self.page.content():
            raise V2RBrowserError(
                "V2R이 자동화 브라우저 접근을 차단했습니다. 우회하지 않고 중단합니다."
            )

    def _select(self, label: str, value: str) -> None:
        selections = self.page.locator(".n-base-selection")
        index = SELECTION_INDEX[label]
        if selections.count() <= index:
            raise V2RBrowserError(f"{label} 선택란을 찾지 못했습니다")
        selections.nth(index).click()
        options = self.page.locator(".n-base-select-option")
        options.first.wait_for(state="visible")
        wanted = re.sub(r"[^0-9a-z가-힣]+", "", value, flags=re.I).casefold()
        for option_index in range(options.count()):
            option = options.nth(option_index)
            text = re.sub(
                r"[^0-9a-z가-힣]+",
                "",
                option.inner_text(),
                flags=re.I,
            ).casefold()
            if option.is_visible() and option.is_enabled() and wanted in text:
                option.click()
                self.page.wait_for_timeout(500)
                return
        raise V2RBrowserError(f"{label} 목록에서 '{value}'를 찾지 못했습니다")

    def _click(self, texts: tuple[str, ...], *, exact: bool) -> None:
        for text in texts:
            locator = self.page.get_by_text(text, exact=exact)
            for index in range(locator.count()):
                item = locator.nth(index)
                if item.is_visible() and item.is_enabled():
                    item.click()
                    return
        raise V2RBrowserError(f"버튼을 찾지 못했습니다: {' / '.join(texts)}")
