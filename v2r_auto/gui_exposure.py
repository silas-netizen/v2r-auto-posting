from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .exposure import (
    DEFAULT_BRAND_MARKERS,
    DEFAULT_CAFE_NAMES,
    ExposureChecker,
    match_selected_rows,
    parse_brands,
    parse_cafes,
    parse_keyword_lines,
)
from .exposure_naver import SeleniumNaverSearch
from .exposure_notion import NotionError, NotionExposureStore
from .exposure_sheet import (
    GoogleSheetExposureStore,
    SeleniumSheetWriter,
    SheetError,
)
from .gui import AutomationApp
from .state import AnotherInstanceRunningError, InstanceLock


class ExposureApp(AutomationApp):
    app_name = "키워드 노출 관리"
    data_folder_name = "V2RExposureChecker"

    def __init__(self):
        super().__init__()
        self.instance_lock = InstanceLock(self.data_dir / "worker.lock")
        try:
            self.instance_lock.__enter__()
        except AnotherInstanceRunningError as exc:
            messagebox.showerror("중복 실행", str(exc))
            self.destroy()
            raise SystemExit(1) from exc
        self.geometry("960x880")
        self.minsize(880, 760)
        self._load_settings()
        self._apply_source()

    def _settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def _load_settings(self) -> None:
        path = self._settings_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.notion_token.set(str(data.get("notion_token") or ""))
        self.database_url.set(str(data.get("database_url") or ""))
        self.sheet_url.set(str(data.get("sheet_url") or ""))
        source = str(data.get("source") or "notion").strip()
        if source in {"notion", "sheet"}:
            self.source.set(source)
        brands = str(data.get("brands") or "").strip()
        if brands:
            self.brands.set(brands)
        cafes = str(data.get("cafes") or "").strip()
        if cafes:
            self.cafes.set(cafes)

    def _save_settings(self) -> None:
        payload = {
            "source": self.source.get().strip() or "notion",
            "notion_token": self.notion_token.get().strip(),
            "database_url": self.database_url.get().strip(),
            "sheet_url": self.sheet_url.get().strip(),
            "brands": self.brands.get().strip(),
            "cafes": self.cafes.get().strip(),
        }
        self._settings_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _create_variables(self) -> None:
        self.source = tk.StringVar(value="notion")
        self.notion_token = tk.StringVar()
        self.database_url = tk.StringVar()
        self.sheet_url = tk.StringVar()
        self.cafes = tk.StringVar(value=", ".join(DEFAULT_CAFE_NAMES))
        self.brands = tk.StringVar(value=", ".join(DEFAULT_BRAND_MARKERS))
        self.dry_run = tk.BooleanVar(value=True)
        self.progress_text = tk.StringVar(value="대기 중")
        self.selected_count = tk.StringVar(value="0개 키워드")
        self.pause_event = threading.Event()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(11, weight=1)

        ttk.Label(outer, text=self.app_name, font=("", 18, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            outer,
            text="네이버 검색은 한 번만 로그인하면 됩니다. 노출완은 통합검색 결과칸에 우리 카페 글이 보이고 그 글 제목·본문·댓글에 브랜드 식별어가 있을 때만입니다. 검색량은 광고주센터 왼쪽 메뉴 도구 → 키워드 도구에서 읽습니다. 크롬 창은 닫지 마세요.",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        source_row = ttk.Frame(outer)
        source_row.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ttk.Label(source_row, text="데이터", width=17).pack(side=tk.LEFT)
        ttk.Radiobutton(
            source_row,
            text="노션",
            variable=self.source,
            value="notion",
            command=self._apply_source,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            source_row,
            text="구글 시트",
            variable=self.source,
            value="sheet",
            command=self._apply_source,
        ).pack(side=tk.LEFT, padx=(12, 0))

        self.notion_token_widgets = self._field_row(
            outer, 3, "노션 연결키", self.notion_token, show="*"
        )
        self.database_widgets = self._field_row(outer, 4, "노션 DB 주소", self.database_url)
        self.sheet_widgets = self._field_row(outer, 4, "구글 시트 주소", self.sheet_url)
        self._entry_row(outer, 5, "우리 카페", self.cafes)
        self._entry_row(outer, 6, "브랜드 식별어", self.brands)

        self.source_hint = ttk.Label(outer, text="")
        self.source_hint.grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 8))

        select_frame = ttk.LabelFrame(outer, text="선택 조회", padding=8)
        select_frame.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(0, 8))
        select_frame.columnconfigure(0, weight=1)
        self.select_hint = ttk.Label(select_frame, text="")
        self.select_hint.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.keyword_text = tk.Text(select_frame, height=7, wrap="word")
        self.keyword_text.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.keyword_text.bind("<KeyRelease>", self._refresh_selected_count)
        hint_row = ttk.Frame(select_frame)
        hint_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        ttk.Label(hint_row, text="빈 줄은 자동으로 무시됩니다.").pack(side=tk.LEFT)
        ttk.Label(hint_row, textvariable=self.selected_count).pack(side=tk.RIGHT)
        select_actions = ttk.Frame(select_frame)
        select_actions.grid(row=3, column=0, columnspan=2, sticky="w")
        self.selected_button = ttk.Button(
            select_actions,
            text="선택 키워드 검사",
            command=self._start_selected,
        )
        self.selected_button.pack(side=tk.LEFT)
        ttk.Button(select_actions, text="지우기", command=self._clear_selected).pack(
            side=tk.LEFT, padx=6
        )

        actions = ttk.Frame(outer)
        actions.grid(row=9, column=0, columnspan=3, sticky="ew", pady=8)
        self.dry_run_check = ttk.Checkbutton(
            actions,
            text="검증 모드(노션에 쓰지 않음)",
            variable=self.dry_run,
        )
        self.dry_run_check.pack(side=tk.LEFT)
        ttk.Button(actions, text="1. 크롬 준비", command=self._open_login).pack(
            side=tk.LEFT, padx=(16, 0)
        )
        ttk.Button(actions, text="2. 키워드 확인", command=self._check_data).pack(
            side=tk.LEFT, padx=6
        )
        self.start_button = ttk.Button(
            actions, text="3. 전체 조회 시작", command=self._start
        )
        self.start_button.pack(side=tk.LEFT)
        self.pause_button = ttk.Button(
            actions, text="일시 중지", command=self._pause, state=tk.DISABLED
        )
        self.pause_button.pack(side=tk.LEFT, padx=(6, 0))
        self.resume_button = ttk.Button(
            actions, text="다시 시작", command=self._resume, state=tk.DISABLED
        )
        self.resume_button.pack(side=tk.LEFT, padx=6)
        self.stop_button = ttk.Button(
            actions, text="중지", command=self._stop, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="저장 폴더",
            command=lambda: self._open_folder(self.data_dir),
        ).pack(side=tk.RIGHT)

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=48).grid(
            row=0, column=1, padx=(10, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="실시간 로그", padding=8)
        log_frame.grid(row=11, column=0, columnspan=3, sticky="nsew")
        self._apply_source()
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _field_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        show: str | None = None,
    ) -> tuple[ttk.Label, ttk.Entry]:
        caption = ttk.Label(parent, text=label, width=17)
        caption.grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(parent, textvariable=variable, show=show)
        entry.grid(row=row, column=1, sticky="ew", pady=3)
        return caption, entry

    def _is_sheet_source(self) -> bool:
        return self.source.get().strip() == "sheet"

    def _source_label(self) -> str:
        return "구글 시트" if self._is_sheet_source() else "노션"

    def _apply_source(self) -> None:
        if not hasattr(self, "source_hint"):
            return
        sheet = self._is_sheet_source()
        if sheet:
            for widget in self.notion_token_widgets + self.database_widgets:
                widget.grid_remove()
            for widget in self.sheet_widgets:
                widget.grid()
            self.source_hint.configure(
                text="시트 주소는 해당 탭이 열린 주소를 그대로 넣으세요. 누구나 수정 가능하면 로그인 없이 읽고 씁니다. 막혀 있으면 크롬에서 구글 로그인하세요."
            )
            self.select_hint.configure(
                text="키워드 여러 개, 줄바꿈으로 붙여 넣기. 괄호 안은 빼고, 시트에 있는 키워드만 검사합니다. 검색량도 함께 반영합니다."
            )
            self.dry_run_check.configure(text="검증 모드(시트에 쓰지 않음)")
        else:
            for widget in self.sheet_widgets:
                widget.grid_remove()
            for widget in self.notion_token_widgets + self.database_widgets:
                widget.grid()
            self.source_hint.configure(
                text="연결키는 노션 설정 → 연결에 만든 암호입니다. 데이터베이스에 그 연결을 초대해 주세요."
            )
            self.select_hint.configure(
                text="키워드 여러 개, 줄바꿈으로 붙여 넣기. 괄호 안은 빼고, 노션에 있는 키워드만 검사합니다. 검색량도 함께 반영합니다."
            )
            self.dry_run_check.configure(text="검증 모드(노션에 쓰지 않음)")

    def _open_login(self) -> None:
        self._save_settings()
        sheet_url = self.sheet_url.get().strip() if self._is_sheet_source() else ""

        def work() -> None:
            self._naver().prepare_login(sheet_url=sheet_url)

        self._run_background(work)

    def _store(self):
        if self._is_sheet_source():
            sheet_url = self.sheet_url.get().strip()
            if not sheet_url:
                raise ValueError("구글 시트 주소를 입력하세요")
            return GoogleSheetExposureStore(
                sheet_url,
                self.logger,
                writer=SeleniumSheetWriter(self.browser, self.logger),
                browser=self.browser,
            )
        token = self.notion_token.get().strip()
        database_url = self.database_url.get().strip()
        if not token:
            raise ValueError("노션 연결키를 입력하세요")
        if not database_url:
            raise ValueError("노션 데이터베이스 주소를 입력하세요")
        return NotionExposureStore(token, database_url, self.logger)

    def _check_data(self) -> None:
        try:
            store = self._store()
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return
        self._save_settings()

        def work() -> None:
            try:
                rows = store.load_rows()
            except (NotionError, SheetError) as exc:
                self.ui_queue.put(("error", (f"{store.label} 확인 실패", str(exc))))
                return
            self.logger.info("키워드 확인 완료: %s건", len(rows))
            self.ui_queue.put(
                (
                    "info",
                    ("키워드 확인", f"{store.label}에서 키워드 {len(rows)}건을 읽었습니다"),
                )
            )

        self._run_background(work)

    def _refresh_selected_count(self, _event=None) -> None:
        count = len(parse_keyword_lines(self.keyword_text.get("1.0", tk.END)))
        self.selected_count.set(f"{count}개 키워드")

    def _clear_selected(self) -> None:
        self.keyword_text.delete("1.0", tk.END)
        self._refresh_selected_count()

    def _start(self) -> None:
        self._begin_check(selected_only=False)

    def _start_selected(self) -> None:
        selected = parse_keyword_lines(self.keyword_text.get("1.0", tk.END))
        if not selected:
            messagebox.showerror("입력 오류", "검사할 키워드를 한 줄에 하나씩 넣어 주세요")
            return
        self._begin_check(selected_only=True, selected=selected)

    def _begin_check(self, *, selected_only: bool, selected: list[str] | None = None) -> None:
        if self.worker and not self.worker.done():
            return
        try:
            store = self._store()
            brands = parse_brands(self.brands.get())
            cafes = parse_cafes(self.cafes.get())
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return
        dry_run = self.dry_run.get()
        label = getattr(store, "label", self._source_label())
        if dry_run:
            if not messagebox.askyesno(
                "검증 모드",
                f"검증 모드입니다. 네이버만 확인하고 {label}에는 쓰지 않습니다. 계속할까요?",
            ):
                return
        else:
            cafe_field = "카페명" if self._is_sheet_source() else "카페/ID"
            edited = (
                " 최종 편집 일시는 지금 시각을 넣습니다. "
                "끝나거나 중지하면 그때 검색량 합을 P1, 노출된 검색량 합을 Q1에 덮어씁니다."
                if self._is_sheet_source()
                else ""
            )
            if not messagebox.askyesno(
                "실제 반영",
                (
                    f"검증 모드가 꺼져 있습니다. 검색 결과에 따라 {label} 노출상태와 검색량을 바꿉니다. "
                    "노출완은 통검 결과칸의 우리 카페 글 제목·본문·댓글에 식별어가 있을 때만입니다. "
                    f"{cafe_field}는 노출완이고 카페가 다를 때만 바꿉니다.{edited} 계속할까요?"
                ),
            ):
                return
        self._save_settings()
        self.stop_event.clear()
        self.pause_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.selected_button.configure(state=tk.DISABLED)
        self.pause_button.configure(state=tk.NORMAL)
        self.resume_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0)
        self.progress_text.set(f"{label} 키워드를 읽는 중")

        def work() -> None:
            try:
                rows = store.load_rows()
                if selected_only:
                    rows, missing = match_selected_rows(rows, selected or [])
                    for keyword in missing:
                        self.logger.warning(
                            "%s에 없는 키워드라 건너뜁니다: %s", label, keyword
                        )
                    if not rows:
                        self.ui_queue.put(
                            (
                                "error",
                                ("선택 조회", f"{label}에서 맞는 키워드를 찾지 못했습니다"),
                            )
                        )
                        return
                    self.logger.info("선택 조회 %s건을 검사합니다", len(rows))
                else:
                    self.logger.info("전체 조회 %s건을 검사합니다", len(rows))
                naver = self._naver()
                naver.require_login()
                checker = ExposureChecker(
                    store,
                    naver,
                    self.logger,
                    brands=brands,
                    cafe_names=cafes,
                )
                checker.run(
                    rows,
                    dry_run=dry_run,
                    stop_event=self.stop_event,
                    pause_event=self.pause_event,
                    progress=self._set_progress,
                )
                if self.stop_event.is_set():
                    self.ui_queue.put(
                        (
                            "info",
                            ("검사 중지", "중지했습니다. 이어서 보려면 다시 시작을 누르세요"),
                        )
                    )
                else:
                    label = "선택 조회" if selected_only else "전체 조회"
                    self.ui_queue.put(
                        (
                            "info",
                            ("검사 종료", f"{label} {len(rows)}건 검사를 마쳤습니다"),
                        )
                    )
            except Exception as exc:
                self.logger.exception("노출 검사 실행 실패")
                self.ui_queue.put(("error", ("실행 실패", str(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _pause(self) -> None:
        if not self.worker or self.worker.done():
            return
        self.pause_event.set()
        self.pause_button.configure(state=tk.DISABLED)
        self.resume_button.configure(state=tk.NORMAL)
        self.progress_text.set("일시 중지")
        self.logger.info("일시 중지를 눌렀습니다. 지금 보는 키워드가 끝나면 멈춥니다")

    def _resume(self) -> None:
        if not self.worker or self.worker.done():
            return
        self.pause_event.clear()
        self.pause_button.configure(state=tk.NORMAL)
        self.resume_button.configure(state=tk.DISABLED)
        self.progress_text.set("다시 시작")
        self.logger.info("다시 시작을 눌렀습니다")

    def _stop(self) -> None:
        self.pause_event.clear()
        self.pause_button.configure(state=tk.DISABLED)
        self.resume_button.configure(state=tk.DISABLED)
        super()._stop()

    def _worker_finished(self) -> None:
        super()._worker_finished()
        self.selected_button.configure(state=tk.NORMAL)
        self.pause_button.configure(state=tk.DISABLED)
        self.resume_button.configure(state=tk.DISABLED)
        self.pause_event.clear()

    def _on_close(self) -> None:
        try:
            self._save_settings()
        except OSError:
            pass
        if hasattr(self, "instance_lock"):
            self.instance_lock.__exit__(None, None, None)
        super()._on_close()

    def _naver(self) -> SeleniumNaverSearch:
        existing = getattr(self, "naver_search", None)
        if existing is None:
            self.naver_search = SeleniumNaverSearch(self.browser, self.logger)
        return self.naver_search


def main() -> None:
    app = ExposureApp()
    app.mainloop()
