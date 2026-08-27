from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .exposure_naver import SeleniumNaverSearch
from .exposure_sheet import (
    GoogleSheetExposureStore,
    SeleniumSheetWriter,
    SheetError,
)
from .gui import AutomationApp
from .search_volume import SearchVolumeFiller, empty_volume_rows
from .state import AnotherInstanceRunningError, InstanceLock


class SearchVolumeApp(AutomationApp):
    app_name = "키워드 검색량 채우기"
    data_folder_name = "V2RSearchVolume"

    def __init__(self):
        super().__init__()
        self.instance_lock = InstanceLock(self.data_dir / "worker.lock")
        try:
            self.instance_lock.__enter__()
        except AnotherInstanceRunningError as exc:
            messagebox.showerror("중복 실행", str(exc))
            self.destroy()
            raise SystemExit(1) from exc
        self.geometry("920x760")
        self.minsize(840, 680)
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
        self.sheet_url.set(str(data.get("sheet_url") or ""))

    def _save_settings(self) -> None:
        payload = {
            "sheet_url": self.sheet_url.get().strip(),
        }
        self._settings_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _create_variables(self) -> None:
        self.sheet_url = tk.StringVar()
        self.dry_run = tk.BooleanVar(value=True)
        self.progress_text = tk.StringVar(value="대기 중")
        self.pause_event = threading.Event()

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
            text="구글 시트에서 검색량이 비어 있는 키워드만 채웁니다. 0은 이미 채운 값입니다. 띄어쓰기는 자동완성 첫 항목, 없으면 통합검색 첫 카페 글 제목을 따릅니다. 통합검색 주소도 같이 넣습니다.",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self._entry_row(outer, 2, "구글 시트 주소", self.sheet_url)
        ttk.Label(
            outer,
            text="시트 주소는 해당 탭이 열린 주소를 그대로 넣으세요. 누구나 수정 가능하면 로그인 없이 읽고 씁니다. 막혀 있으면 크롬에서 구글 로그인하세요.",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 8))

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)
        ttk.Checkbutton(
            actions,
            text="검증 모드(시트에 쓰지 않음)",
            variable=self.dry_run,
        ).pack(side=tk.LEFT)
        ttk.Button(actions, text="1. 크롬 준비", command=self._open_login).pack(
            side=tk.LEFT, padx=(16, 0)
        )
        ttk.Button(actions, text="2. 빈 검색량 확인", command=self._check_data).pack(
            side=tk.LEFT, padx=6
        )
        self.start_button = ttk.Button(
            actions, text="3. 채우기 시작", command=self._start
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
        progress_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 10))
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
        sheet_url = self.sheet_url.get().strip()

        def work() -> None:
            self._naver().prepare_login(sheet_url=sheet_url)

        self._run_background(work)

    def _store(self) -> GoogleSheetExposureStore:
        sheet_url = self.sheet_url.get().strip()
        if not sheet_url:
            raise ValueError("구글 시트 주소를 입력하세요")
        return GoogleSheetExposureStore(
            sheet_url,
            self.logger,
            writer=SeleniumSheetWriter(self.browser, self.logger),
            browser=self.browser,
        )

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
            except SheetError as exc:
                self.ui_queue.put(("error", ("구글 시트 확인 실패", str(exc))))
                return
            empty = empty_volume_rows(rows)
            missing_column = sum(1 for row in rows if not row.volume_property)
            if missing_column == len(rows):
                self.ui_queue.put(
                    (
                        "error",
                        (
                            "검색량 열 없음",
                            "시트에서 키워드 검색량 열을 찾지 못했습니다",
                        ),
                    )
                )
                return
            self.logger.info(
                "키워드 %s건 중 검색량이 비어 있는 행 %s건",
                len(rows),
                len(empty),
            )
            self.ui_queue.put(
                (
                    "info",
                    (
                        "빈 검색량 확인",
                        f"시트 키워드 {len(rows)}건 중 검색량이 비어 있는 행은 {len(empty)}건입니다. 0은 이미 채운 값으로 봅니다.",
                    ),
                )
            )

        self._run_background(work)

    def _start(self) -> None:
        if self.worker and not self.worker.done():
            return
        try:
            store = self._store()
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return
        dry_run = self.dry_run.get()
        if dry_run:
            if not messagebox.askyesno(
                "검증 모드",
                "검증 모드입니다. 네이버만 확인하고 시트에는 쓰지 않습니다. 계속할까요?",
            ):
                return
        elif not messagebox.askyesno(
            "실제 반영",
            "검증 모드가 꺼져 있습니다. 검색량이 비어 있는 키워드만 검색량과 통합검색 주소를 넣고, 띄어쓰기가 다르면 시트 키워드도 고칩니다. 최종 편집 일시는 지금 시각을 넣습니다. 계속할까요?",
        ):
            return
        self._save_settings()
        self.stop_event.clear()
        self.pause_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.pause_button.configure(state=tk.NORMAL)
        self.resume_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0)
        self.progress_text.set("구글 시트 키워드를 읽는 중")

        def work() -> None:
            try:
                rows = store.load_rows()
                empty = empty_volume_rows(rows)
                if not empty:
                    self.ui_queue.put(
                        (
                            "info",
                            (
                                "채울 행 없음",
                                "검색량이 비어 있는 키워드가 없습니다. 0은 이미 채운 값입니다.",
                            ),
                        )
                    )
                    return
                self.logger.info("빈 검색량 %s건을 채웁니다", len(empty))
                naver = self._naver()
                naver.require_login()
                filler = SearchVolumeFiller(store, naver, self.logger)
                filler.run(
                    empty,
                    dry_run=dry_run,
                    stop_event=self.stop_event,
                    pause_event=self.pause_event,
                    progress=self._set_progress,
                )
                if self.stop_event.is_set():
                    self.ui_queue.put(
                        (
                            "info",
                            ("작업 중지", "중지했습니다. 이어서 하려면 다시 시작을 누르세요"),
                        )
                    )
                else:
                    self.ui_queue.put(
                        (
                            "info",
                            ("작업 종료", f"빈 검색량 {len(empty)}건 처리를 마쳤습니다"),
                        )
                    )
            except Exception as exc:
                self.logger.exception("검색량 채우기 실행 실패")
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
    app = SearchVolumeApp()
    app.mainloop()
