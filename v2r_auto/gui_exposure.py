from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .exposure import (
    DEFAULT_BRAND_MARKERS,
    DEFAULT_CAFE_NAMES,
    DEFAULT_REPEAT_MINUTES,
    ExposureChecker,
    filter_exposed_rows,
    format_wait_remaining,
    match_selected_rows,
    open_text_notepad,
    apply_start_row,
    next_cycle_wait_seconds,
    parse_brands,
    parse_cafes,
    parse_daily_time,
    parse_keyword_lines,
    parse_repeat_minutes,
    parse_sheet_urls,
    parse_start_row,
    sheet_urls_from_settings,
    unique_exposed_urls,
    wait_with_events,
    write_exposed_post_urls,
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
        self.geometry("960x1020")
        self.minsize(880, 860)
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
        sheet_text = sheet_urls_from_settings(data)
        parsed_urls = parse_sheet_urls(sheet_text)
        self.sheet_url.set(parsed_urls[0] if parsed_urls else "")
        if hasattr(self, "sheet_urls_box"):
            self.sheet_urls_box.delete("1.0", tk.END)
            if sheet_text:
                self.sheet_urls_box.insert("1.0", sheet_text)
        source = str(data.get("source") or "notion").strip()
        if source in {"notion", "sheet"}:
            self.source.set(source)
        brands = str(data.get("brands") or "").strip()
        if brands:
            self.brands.set(brands)
        cafes = str(data.get("cafes") or "").strip()
        if cafes:
            self.cafes.set(cafes)
        if "collect_exposed_urls" in data:
            self.collect_exposed_urls.set(bool(data.get("collect_exposed_urls")))
        if "repeat_enabled" in data:
            self.repeat_enabled.set(bool(data.get("repeat_enabled")))
        minutes = str(data.get("repeat_minutes") or "").strip()
        if minutes:
            self.repeat_minutes.set(minutes)
        if "daily_enabled" in data:
            self.daily_enabled.set(bool(data.get("daily_enabled")))
        daily_time = str(data.get("daily_time") or "").strip()
        if daily_time:
            self.daily_time.set(daily_time)

    def _save_settings(self) -> None:
        urls = parse_sheet_urls(self._sheet_urls_value())
        if urls:
            self.sheet_url.set(urls[0])
        payload = {
            "source": self.source.get().strip() or "notion",
            "notion_token": self.notion_token.get().strip(),
            "database_url": self.database_url.get().strip(),
            "sheet_url": urls[0] if urls else self.sheet_url.get().strip(),
            "sheet_urls": urls,
            "brands": self.brands.get().strip(),
            "cafes": self.cafes.get().strip(),
            "collect_exposed_urls": self.collect_exposed_urls.get(),
            "repeat_enabled": self.repeat_enabled.get(),
            "repeat_minutes": self.repeat_minutes.get().strip()
            or str(DEFAULT_REPEAT_MINUTES),
            "daily_enabled": self.daily_enabled.get(),
            "daily_time": self.daily_time.get().strip() or "09:00",
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
        self.collect_exposed_urls = tk.BooleanVar(value=False)
        self.start_row = tk.StringVar()
        self.repeat_enabled = tk.BooleanVar(value=False)
        self.repeat_minutes = tk.StringVar(value=str(DEFAULT_REPEAT_MINUTES))
        self.daily_enabled = tk.BooleanVar(value=False)
        self.daily_time = tk.StringVar(value="09:00")
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
        self.sheet_widgets = self._sheet_url_row(outer, 4)
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

        controls = ttk.Frame(outer)
        controls.grid(row=9, column=0, columnspan=3, sticky="ew", pady=8)
        options = ttk.Frame(controls)
        options.pack(fill=tk.X)
        self.dry_run_check = ttk.Checkbutton(
            options,
            text="검증 모드(노션에 쓰지 않음)",
            variable=self.dry_run,
        )
        self.dry_run_check.pack(side=tk.LEFT)
        ttk.Checkbutton(
            options,
            text="노출완 내 글 URL도 모으기",
            variable=self.collect_exposed_urls,
        ).pack(side=tk.LEFT, padx=(16, 0))
        start_row_frame = ttk.Frame(controls)
        start_row_frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(start_row_frame, text="시작 위치").pack(side=tk.LEFT)
        ttk.Entry(start_row_frame, textvariable=self.start_row, width=8).pack(
            side=tk.LEFT, padx=(8, 8)
        )
        ttk.Label(
            start_row_frame,
            text="시트 행 번호. 비우면 맨 위부터. 예: 201. 여러 시트면 각 시트에 같이 적용됩니다.",
        ).pack(side=tk.LEFT)
        schedule = ttk.LabelFrame(controls, text="자동 반복 · 시간 예약", padding=8)
        schedule.pack(fill=tk.X, pady=(8, 0))
        repeat_row = ttk.Frame(schedule)
        repeat_row.pack(fill=tk.X)
        ttk.Checkbutton(
            repeat_row,
            text="끝나면 다시",
            variable=self.repeat_enabled,
        ).pack(side=tk.LEFT)
        ttk.Label(repeat_row, text="간격(분)").pack(side=tk.LEFT, padx=(12, 4))
        ttk.Entry(repeat_row, textvariable=self.repeat_minutes, width=6).pack(
            side=tk.LEFT
        )
        ttk.Label(
            repeat_row,
            text="모든 시트를 한 바퀴 돈 뒤 기다렸다가 또 합니다.",
        ).pack(side=tk.LEFT, padx=(8, 0))
        daily_row = ttk.Frame(schedule)
        daily_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Checkbutton(
            daily_row,
            text="매일 이 시각에 시작",
            variable=self.daily_enabled,
        ).pack(side=tk.LEFT)
        ttk.Entry(daily_row, textvariable=self.daily_time, width=8).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Label(
            daily_row,
            text="예: 09:00  한국 시각. 전체 조회만 해당.",
        ).pack(side=tk.LEFT, padx=(8, 0))
        actions = ttk.Frame(controls)
        actions.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(actions, text="1. 크롬 준비", command=self._open_login).pack(
            side=tk.LEFT
        )
        ttk.Button(actions, text="2. 키워드 확인", command=self._check_data).pack(
            side=tk.LEFT, padx=6
        )
        self.start_button = ttk.Button(
            actions, text="3. 전체 조회 시작", command=self._start
        )
        self.start_button.pack(side=tk.LEFT)
        self.extract_button = ttk.Button(
            actions, text="노출완 URL만 추출", command=self._start_extract
        )
        self.extract_button.pack(side=tk.LEFT, padx=(6, 0))
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
        ttk.Label(progress_frame, textvariable=self.progress_text, width=56).grid(
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

    def _sheet_url_row(self, parent: ttk.Frame, row: int) -> tuple[ttk.Label, ttk.Frame]:
        caption = ttk.Label(parent, text="구글 시트 주소", width=17)
        caption.grid(row=row, column=0, sticky="nw", pady=3)
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=1, columnspan=2, sticky="nsew", pady=3)
        frame.columnconfigure(0, weight=1)
        self.sheet_urls_box = tk.Text(frame, height=4, wrap="none")
        self.sheet_urls_box.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, command=self.sheet_urls_box.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.sheet_urls_box.configure(yscrollcommand=scrollbar.set)
        return caption, frame

    def _sheet_urls_value(self) -> str:
        box = getattr(self, "sheet_urls_box", None)
        if box is not None:
            return box.get("1.0", tk.END)
        return self.sheet_url.get()

    def _parsed_sheet_urls(self) -> list[str]:
        urls = parse_sheet_urls(self._sheet_urls_value())
        if not urls:
            raise ValueError("구글 시트 주소를 한 줄에 하나씩 입력하세요")
        return urls

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
                text="시트 주소는 해당 탭이 열린 주소를 한 줄에 하나씩 넣으세요. 위에서부터 차례로 검사합니다. 누구나 수정 가능하면 로그인 없이 읽고 씁니다. 막혀 있으면 크롬에서 구글 로그인하세요."
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
        sheet_url = ""
        if self._is_sheet_source():
            urls = parse_sheet_urls(self._sheet_urls_value())
            sheet_url = urls[0] if urls else ""

        def work() -> None:
            self._naver().prepare_login(sheet_url=sheet_url)

        self._run_background(work)

    def _store(self, sheet_url: str | None = None):
        if self._is_sheet_source():
            url = (sheet_url or "").strip()
            if not url:
                urls = parse_sheet_urls(self._sheet_urls_value())
                url = urls[0] if urls else ""
            if not url:
                raise ValueError("구글 시트 주소를 한 줄에 하나씩 입력하세요")
            return GoogleSheetExposureStore(
                url,
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
            stores = []
            if self._is_sheet_source():
                for url in self._parsed_sheet_urls():
                    stores.append(self._store(url))
            else:
                stores.append(self._store())
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return
        self._save_settings()

        def work() -> None:
            counts: list[str] = []
            for index, store in enumerate(stores, start=1):
                try:
                    rows = store.load_rows()
                except (NotionError, SheetError) as exc:
                    self.ui_queue.put(("error", (f"{store.label} 확인 실패", str(exc))))
                    return
                prefix = f"{index}/{len(stores)} " if len(stores) > 1 else ""
                self.logger.info("키워드 확인 완료: %s%s %s건", prefix, store.label, len(rows))
                counts.append(f"{len(rows)}건")
            if len(stores) == 1:
                message = f"{stores[0].label}에서 키워드 {counts[0]}을 읽었습니다"
            else:
                message = f"시트 {len(stores)}개에서 키워드 {', '.join(counts)}을 읽었습니다"
            self.ui_queue.put(("info", ("키워드 확인", message)))

        self._run_background(work)

    def _refresh_selected_count(self, _event=None) -> None:
        count = len(parse_keyword_lines(self.keyword_text.get("1.0", tk.END)))
        self.selected_count.set(f"{count}개 키워드")

    def _clear_selected(self) -> None:
        self.keyword_text.delete("1.0", tk.END)
        self._refresh_selected_count()

    def _start(self) -> None:
        try:
            start_row = parse_start_row(self.start_row.get())
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return
        self._begin_check(selected_only=False, start_row=start_row)

    def _start_selected(self) -> None:
        selected = parse_keyword_lines(self.keyword_text.get("1.0", tk.END))
        if not selected:
            messagebox.showerror("입력 오류", "검사할 키워드를 한 줄에 하나씩 넣어 주세요")
            return
        self._begin_check(selected_only=True, selected=selected)

    def _start_extract(self) -> None:
        selected = parse_keyword_lines(self.keyword_text.get("1.0", tk.END))
        self._begin_check(
            selected_only=bool(selected),
            selected=selected or None,
            urls_only=True,
        )

    def _export_exposed_urls(self, urls: list[str]) -> Path | None:
        items = unique_exposed_urls(urls)
        if not items:
            self.logger.info("모을 노출완 글 링크가 없습니다")
            return None
        path = write_exposed_post_urls(self.data_dir / "노출완글링크.txt", items)
        open_text_notepad(path)
        self.logger.info("노출완 글 링크 %s개를 메모장에 띄웠습니다: %s", len(items), path)
        return path

    def _schedule_options(
        self, *, selected_only: bool, urls_only: bool
    ) -> tuple[bool, int, bool, tuple[int, int] | None]:
        looping = not selected_only and not urls_only
        repeat_enabled = looping and self.repeat_enabled.get()
        daily_enabled = looping and self.daily_enabled.get()
        repeat_minutes = DEFAULT_REPEAT_MINUTES
        daily_time = None
        if repeat_enabled:
            repeat_minutes = parse_repeat_minutes(self.repeat_minutes.get())
        if daily_enabled:
            daily_time = parse_daily_time(self.daily_time.get())
        return repeat_enabled, repeat_minutes, daily_enabled, daily_time

    def _set_status(self, text: str) -> None:
        self.ui_queue.put(("status", text))

    def _wait_for_schedule(
        self,
        seconds: float,
        *,
        daily_enabled: bool,
        daily_time: tuple[int, int] | None,
        first_cycle: bool,
    ) -> bool:
        if seconds <= 0:
            return not self.stop_event.is_set()
        if daily_enabled and first_cycle and daily_time is not None:
            clock = f"{daily_time[0]:02d}:{daily_time[1]:02d}"
            self.logger.info("매일 %s까지 기다립니다", clock)
            prefix = f"매일 {clock}까지"
        else:
            self.logger.info("%s 후 다시 시작합니다", format_wait_remaining(seconds))
            prefix = "다음 실행까지"

        def on_tick(remaining: float, paused: bool = False) -> None:
            if paused:
                self._set_status("일시 중지")
                return
            self._set_status(f"{prefix} {format_wait_remaining(remaining)}")

        return wait_with_events(
            seconds,
            stop_event=self.stop_event,
            pause_event=self.pause_event,
            on_tick=on_tick,
        )

    def _run_one_store(
        self,
        store,
        *,
        selected_only: bool,
        selected: list[str] | None,
        urls_only: bool,
        start_row: int | None,
        dry_run: bool,
        brands: list[str],
        cafes: list[str],
        require_rows: bool,
        sheet_note: str = "",
    ) -> tuple[int, list[str], bool]:
        label = getattr(store, "label", self._source_label())
        tagged = f"{sheet_note}{label}" if sheet_note else label
        self._set_status(f"{tagged} 키워드를 읽는 중")
        rows = store.load_rows()
        if selected_only:
            rows, missing = match_selected_rows(rows, selected or [])
            for keyword in missing:
                self.logger.warning("%s에 없는 키워드라 건너뜁니다: %s", tagged, keyword)
            if not rows:
                if require_rows:
                    self.ui_queue.put(
                        (
                            "error",
                            ("선택 조회", f"{tagged}에서 맞는 키워드를 찾지 못했습니다"),
                        )
                    )
                else:
                    self.logger.warning("%s에서 맞는 키워드를 찾지 못했습니다", tagged)
                return 0, [], False
        checked_count = len(rows)
        if urls_only:
            rows = filter_exposed_rows(rows)
            if not rows:
                if require_rows:
                    self.ui_queue.put(
                        (
                            "error",
                            ("노출완 URL 추출", f"{tagged}에서 노출완 키워드를 찾지 못했습니다"),
                        )
                    )
                else:
                    self.logger.warning("%s에서 노출완 키워드를 찾지 못했습니다", tagged)
                return 0, [], False
            checked_count = len(rows)
            self.logger.info("%s 노출완 URL 추출 %s건을 검색합니다", tagged, checked_count)
        elif selected_only:
            self.logger.info("%s 선택 조회 %s건을 검사합니다", tagged, checked_count)
        else:
            if start_row is not None:
                kept, _skipped = apply_start_row(rows, start_row)
                if not kept:
                    if require_rows:
                        self.ui_queue.put(
                            (
                                "error",
                                ("시작 위치", f"{start_row}행 이후 키워드가 없습니다"),
                            )
                        )
                    else:
                        self.logger.warning(
                            "%s %s행 이후 키워드가 없습니다", tagged, start_row
                        )
                    return 0, [], False
                checked_count = len(kept)
            self.logger.info("%s 전체 조회 %s건을 검사합니다", tagged, checked_count)
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
            dry_run=dry_run or urls_only,
            stop_event=self.stop_event,
            pause_event=self.pause_event,
            progress=self._set_progress,
            urls_only=urls_only,
            start_row=None if selected_only or urls_only else start_row,
        )
        return checked_count, list(checker.exposed_urls), self.stop_event.is_set()

    def _begin_check(
        self,
        *,
        selected_only: bool,
        selected: list[str] | None = None,
        urls_only: bool = False,
        start_row: int | None = None,
    ) -> None:
        if self.worker and not self.worker.done():
            return
        try:
            sheet_urls = self._parsed_sheet_urls() if self._is_sheet_source() else []
            store = self._store(sheet_urls[0] if sheet_urls else None)
            brands = parse_brands(self.brands.get())
            cafes = parse_cafes(self.cafes.get())
            repeat_enabled, repeat_minutes, daily_enabled, daily_time = (
                self._schedule_options(
                    selected_only=selected_only,
                    urls_only=urls_only,
                )
            )
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return
        dry_run = self.dry_run.get()
        collect_urls = self.collect_exposed_urls.get() or urls_only
        looping = repeat_enabled or daily_enabled
        sheet_count = len(sheet_urls) if sheet_urls else 1
        label = getattr(store, "label", self._source_label())
        if sheet_count > 1:
            label = f"구글 시트 {sheet_count}개"
        extra_notes = []
        if sheet_count > 1:
            extra_notes.append(f"시트 {sheet_count}개를 위에서부터 차례로 검사합니다.")
        if daily_enabled and daily_time is not None:
            extra_notes.append(
                f"매일 {daily_time[0]:02d}:{daily_time[1]:02d}에 시작합니다."
            )
        if repeat_enabled:
            extra_notes.append(f"한 바퀴가 끝나면 {repeat_minutes}분 후 다시 합니다.")
        note = (" " + " ".join(extra_notes)) if extra_notes else ""
        if urls_only:
            scope = "선택한 키워드 중 노출완" if selected_only else "표의 노출완"
            if not messagebox.askyesno(
                "노출완 URL 추출",
                (
                    f"{scope}만 통검해서 내 글 주소를 모읍니다. "
                    f"{label}은 바꾸지 않습니다.{note} 계속할까요?"
                ),
            ):
                return
        elif dry_run:
            if not messagebox.askyesno(
                "검증 모드",
                f"검증 모드입니다. 네이버만 확인하고 {label}에는 쓰지 않습니다.{note} 계속할까요?",
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
                    f"{cafe_field}는 노출완이고 카페가 다를 때만 바꿉니다.{edited}{note} 계속할까요?"
                ),
            ):
                return
        self._save_settings()
        self.stop_event.clear()
        self.pause_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.selected_button.configure(state=tk.DISABLED)
        self.extract_button.configure(state=tk.DISABLED)
        self.pause_button.configure(state=tk.NORMAL)
        self.resume_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0)
        self.progress_text.set(f"{label} 키워드를 읽는 중")

        def work() -> None:
            exported = None
            cycle = 0
            total_checked = 0
            collected: list[str] = []
            fatal = False
            try:
                while True:
                    wait_for = next_cycle_wait_seconds(
                        repeat_enabled=repeat_enabled,
                        repeat_minutes=repeat_minutes,
                        daily_enabled=daily_enabled,
                        daily_time=daily_time,
                        first_cycle=cycle == 0,
                    )
                    if wait_for is None:
                        break
                    if not self._wait_for_schedule(
                        wait_for,
                        daily_enabled=daily_enabled,
                        daily_time=daily_time,
                        first_cycle=cycle == 0,
                    ):
                        break
                    cycle += 1
                    if looping:
                        self.logger.info("자동 조회 %s바퀴를 시작합니다", cycle)
                    collected = []
                    cycle_checked = 0
                    found_any = False
                    if sheet_urls:
                        for index, url in enumerate(sheet_urls, start=1):
                            if self.stop_event.is_set():
                                break
                            note_prefix = (
                                f"시트 {index}/{len(sheet_urls)} "
                                if len(sheet_urls) > 1
                                else ""
                            )
                            self.logger.info("%s검사를 시작합니다: %s", note_prefix, url)
                            try:
                                current = self._store(url)
                                checked, urls, stopped = self._run_one_store(
                                    current,
                                    selected_only=selected_only,
                                    selected=selected,
                                    urls_only=urls_only,
                                    start_row=start_row,
                                    dry_run=dry_run,
                                    brands=brands,
                                    cafes=cafes,
                                    require_rows=not looping and len(sheet_urls) == 1,
                                    sheet_note=note_prefix,
                                )
                            except Exception as exc:
                                self.logger.exception("%s검사 실패: %s", note_prefix, url)
                                if looping or len(sheet_urls) > 1:
                                    self.logger.error("%s오류: %s", note_prefix, exc)
                                    continue
                                raise
                            cycle_checked += checked
                            collected.extend(urls)
                            if checked:
                                found_any = True
                            if stopped:
                                break
                    else:
                        checked, urls, stopped = self._run_one_store(
                            store,
                            selected_only=selected_only,
                            selected=selected,
                            urls_only=urls_only,
                            start_row=start_row,
                            dry_run=dry_run,
                            brands=brands,
                            cafes=cafes,
                            require_rows=not looping,
                        )
                        cycle_checked = checked
                        collected.extend(urls)
                        found_any = bool(checked)
                    total_checked += cycle_checked
                    if collect_urls and collected:
                        exported = self._export_exposed_urls(collected)
                    if self.stop_event.is_set():
                        break
                    if not found_any and not looping:
                        if selected_only:
                            self.ui_queue.put(
                                (
                                    "error",
                                    ("선택 조회", f"{label}에서 맞는 키워드를 찾지 못했습니다"),
                                )
                            )
                        elif urls_only:
                            self.ui_queue.put(
                                (
                                    "error",
                                    (
                                        "노출완 URL 추출",
                                        f"{label}에서 노출완 키워드를 찾지 못했습니다",
                                    ),
                                )
                            )
                        fatal = True
                        break
                    if looping:
                        self.logger.info(
                            "자동 조회 %s바퀴를 마쳤습니다. 이번 %s건",
                            cycle,
                            cycle_checked,
                        )
                        continue
                    break
                if self.stop_event.is_set():
                    extra = ""
                    if exported is not None:
                        extra = " 지금까지 모은 링크는 메모장에 띄웠습니다."
                    self.ui_queue.put(
                        (
                            "info",
                            (
                                "검사 중지",
                                f"중지했습니다. 이어서 보려면 다시 시작을 누르세요.{extra}",
                            ),
                        )
                    )
                elif fatal:
                    return
                elif urls_only:
                    count = len(unique_exposed_urls(collected))
                    self.ui_queue.put(
                        (
                            "info",
                            (
                                "노출완 URL 추출",
                                (
                                    f"노출완 글 링크 {count}개를 메모장에 띄웠습니다."
                                    if exported is not None
                                    else "모을 노출완 글 링크가 없습니다."
                                ),
                            ),
                        )
                    )
                elif not looping:
                    kind = "선택 조회" if selected_only else "전체 조회"
                    extra = ""
                    if exported is not None:
                        extra = " 노출완 글 링크도 메모장에 띄웠습니다."
                    self.ui_queue.put(
                        (
                            "info",
                            (
                                "검사 종료",
                                f"{kind} {total_checked}건 검사를 마쳤습니다.{extra}",
                            ),
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
        self.extract_button.configure(state=tk.NORMAL)
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
