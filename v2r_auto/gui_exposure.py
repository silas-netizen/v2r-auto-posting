from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .exposure import (
    DEFAULT_BRAND_MARKERS,
    ExposureChecker,
    parse_brands,
)
from .exposure_naver import fetch_naver_html
from .exposure_notion import NotionError, NotionExposureStore
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
        self._load_settings()

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
        brands = str(data.get("brands") or "").strip()
        if brands:
            self.brands.set(brands)

    def _save_settings(self) -> None:
        payload = {
            "notion_token": self.notion_token.get().strip(),
            "database_url": self.database_url.get().strip(),
            "brands": self.brands.get().strip(),
        }
        self._settings_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _create_variables(self) -> None:
        self.notion_token = tk.StringVar()
        self.database_url = tk.StringVar()
        self.brands = tk.StringVar(value=", ".join(DEFAULT_BRAND_MARKERS))
        self.dry_run = tk.BooleanVar(value=True)
        self.progress_text = tk.StringVar(value="대기 중")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(8, weight=1)

        ttk.Label(outer, text=self.app_name, font=("", 18, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            outer,
            text="노션 키워드를 네이버 통합검색에서 확인하고 노출상태를 바꿉니다",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self._entry_row(outer, 2, "노션 연결키", self.notion_token, show="*")
        self._entry_row(outer, 3, "노션 데이터베이스 주소", self.database_url)
        self._entry_row(outer, 4, "브랜드 식별어", self.brands)

        ttk.Label(
            outer,
            text="연결키는 노션 설정 → 연결에 만든 암호입니다. 데이터베이스에 그 연결을 초대해 주세요.",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))

        actions = ttk.Frame(outer)
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=8)
        ttk.Checkbutton(
            actions,
            text="검증 모드(노션에 쓰지 않음)",
            variable=self.dry_run,
        ).pack(side=tk.LEFT)
        ttk.Button(actions, text="1. 크롬 준비", command=self._open_login).pack(
            side=tk.LEFT, padx=(16, 0)
        )
        ttk.Button(actions, text="2. 키워드 확인", command=self._check_data).pack(
            side=tk.LEFT, padx=6
        )
        self.start_button = ttk.Button(
            actions, text="3. 노출 검사 시작", command=self._start
        )
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(
            actions, text="중지", command=self._stop, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=6)
        ttk.Button(
            actions,
            text="저장 폴더",
            command=lambda: self._open_folder(self.data_dir),
        ).pack(side=tk.RIGHT)

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=48).grid(
            row=0, column=1, padx=(10, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="실시간 로그", padding=8)
        log_frame.grid(row=8, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _open_login(self) -> None:
        self._save_settings()

        def work() -> None:
            self.browser.start()
            if not self.browser.driver:
                raise RuntimeError("Chrome이 시작되지 않았습니다")
            self.browser.driver.get("https://search.naver.com/")
            self.logger.info("네이버 검색 창을 열었습니다. 필요하면 네이버에 로그인하세요")

        self._run_background(work)

    def _store(self) -> NotionExposureStore:
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
            except NotionError as exc:
                self.ui_queue.put(("error", ("노션 확인 실패", str(exc))))
                return
            self.logger.info("키워드 확인 완료: %s건", len(rows))
            self.ui_queue.put(
                (
                    "info",
                    ("키워드 확인", f"노션에서 키워드 {len(rows)}건을 읽었습니다"),
                )
            )

        self._run_background(work)

    def _start(self) -> None:
        if self.worker and not self.worker.done():
            return
        try:
            store = self._store()
            brands = parse_brands(self.brands.get())
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return
        dry_run = self.dry_run.get()
        if dry_run:
            if not messagebox.askyesno(
                "검증 모드",
                "검증 모드입니다. 네이버만 확인하고 노션 노출상태는 바꾸지 않습니다. 계속할까요?",
            ):
                return
        elif not messagebox.askyesno(
            "실제 반영",
            "검증 모드가 꺼져 있습니다. 검색 결과에 따라 노션 노출상태를 바꿀까요?",
        ):
            return
        self._save_settings()
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0)
        self.progress_text.set("노션 키워드를 읽는 중")

        def work() -> None:
            try:
                rows = store.load_rows()
                checker = ExposureChecker(
                    store,
                    lambda url: fetch_naver_html(self.browser, url),
                    self.logger,
                    brands=brands,
                )
                checker.run(
                    rows,
                    dry_run=dry_run,
                    stop_event=self.stop_event,
                    progress=self._set_progress,
                )
                self.ui_queue.put(
                    (
                        "info",
                        ("검사 종료", f"키워드 {len(rows)}건 검사를 마쳤습니다"),
                    )
                )
            except Exception as exc:
                self.logger.exception("노출 검사 실행 실패")
                self.ui_queue.put(("error", ("실행 실패", str(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _on_close(self) -> None:
        try:
            self._save_settings()
        except OSError:
            pass
        if hasattr(self, "instance_lock"):
            self.instance_lock.__exit__(None, None, None)
        super()._on_close()


def main() -> None:
    app = ExposureApp()
    app.mainloop()
