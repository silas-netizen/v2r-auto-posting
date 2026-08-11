from __future__ import annotations

import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .gui import AutomationApp
from .images import load_sheet_brand
from .immediate_inputs import load_brand_immediate_jobs, load_daily_excel_jobs
from .runner import ImmediateRunner
from .state import AnotherInstanceRunningError, InstanceLock


class ImmediateAutomationApp(AutomationApp):
    app_name = "V2R 자사 카페 예약 발행"
    data_folder_name = "V2RImmediatePosting"

    def __init__(self):
        super().__init__()
        self.instance_lock = InstanceLock(self.data_dir / "worker.lock")
        try:
            self.instance_lock.__enter__()
        except AnotherInstanceRunningError as exc:
            messagebox.showerror("중복 실행", str(exc))
            self.destroy()
            raise SystemExit(1) from exc

    def _create_variables(self) -> None:
        self.input_mode = tk.StringVar(value="brand")
        self.sheet_url = tk.StringVar()
        self.excel_path = tk.StringVar()
        self.dry_run = tk.BooleanVar(value=True)
        self.progress_text = tk.StringVar(value="대기 중")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(7, weight=1)

        ttk.Label(outer, text=self.app_name, font=("", 18, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            outer,
            text="자사 카페는 5~15분 간격 예약 · 테스트 카페 한 줄 글은 즉시 발행",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        modes = ttk.Frame(outer)
        modes.grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(
            modes,
            text="브랜드 원고 (Google Sheet)",
            variable=self.input_mode,
            value="brand",
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            modes,
            text="일상 글 (Excel)",
            variable=self.input_mode,
            value="daily",
        ).pack(side=tk.LEFT, padx=18)

        self._entry_row(outer, 3, "Google 시트 URL", self.sheet_url)
        self._entry_row(
            outer,
            4,
            "일상 글 Excel",
            self.excel_path,
            button=("찾기", self._choose_excel),
        )

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Checkbutton(
            actions,
            text="검증 모드(실제 발행하지 않음)",
            variable=self.dry_run,
        ).pack(side=tk.LEFT)
        ttk.Button(actions, text="1. 로그인 준비", command=self._open_login).pack(
            side=tk.LEFT, padx=(16, 0)
        )
        ttk.Button(actions, text="2. 데이터 확인", command=self._check_data).pack(
            side=tk.LEFT, padx=6
        )
        self.start_button = ttk.Button(
            actions,
            text="3. 예약 발행 시작",
            command=self._start,
        )
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(
            actions,
            text="중지",
            command=self._stop,
            state=tk.DISABLED,
        )
        self.stop_button.pack(side=tk.LEFT, padx=6)
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
        ttk.Label(progress_frame, textvariable=self.progress_text, width=52).grid(
            row=0, column=1, padx=(10, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="실시간 로그", padding=8)
        log_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _choose_excel(self) -> None:
        selected = filedialog.askopenfilename(
            title="일상 글 Excel 선택",
            filetypes=[("Excel 파일", "*.xlsx"), ("모든 파일", "*.*")],
        )
        if selected:
            self.excel_path.set(selected)

    def _open_login(self) -> None:
        sheet_url = self.sheet_url.get().strip() if self.input_mode.get() == "brand" else ""
        self._run_background(lambda: self.browser.open_login_window(sheet_url))

    def _load_immediate_jobs(self):
        if self.input_mode.get() == "daily":
            path = self.excel_path.get().strip()
            if not path:
                raise ValueError("일상 글 Excel 파일을 선택하세요")
            return load_daily_excel_jobs(path), ""
        sheet_url = self.sheet_url.get().strip()
        if not sheet_url:
            raise ValueError("Google 시트 URL을 입력하세요")
        csv_path = self.browser.download_sheet(sheet_url)
        brand = load_sheet_brand(sheet_url)
        if not brand:
            raise ValueError("Google 시트 제목에서 브랜드명을 찾지 못했습니다")
        return load_brand_immediate_jobs(csv_path, brand=brand), sheet_url

    def _check_data(self) -> None:
        def work() -> None:
            jobs, _sheet_url = self._load_immediate_jobs()
            errors = sum(bool(job.validate()) for job in jobs)
            self.logger.info(
                "즉시 발행 데이터 확인: %s건 / 형식 오류 %s건",
                len(jobs),
                errors,
            )
            self.ui_queue.put(
                (
                    "info",
                    ("데이터 확인", f"처리 대상 {len(jobs)}건\n형식 오류 {errors}건"),
                )
            )

        self._run_background(work)

    def _start(self) -> None:
        if self.worker and not self.worker.done():
            return
        dry_run = self.dry_run.get()
        if not dry_run and not messagebox.askyesno(
            "실제 예약 발행",
            "검증 모드가 꺼져 있습니다.\n글을 실제로 예약 등록할까요?",
        ):
            return
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0)
        self.progress_text.set("입력 데이터 준비 중")

        def work() -> None:
            try:
                jobs, sheet_url = self._load_immediate_jobs()
                runner = ImmediateRunner(
                    browser=self.browser,
                    history_path=self.data_dir / "immediate-history.json",
                    report_dir=self.report_dir,
                    logger=self.logger,
                )
                result, report_path = runner.run(
                    jobs,
                    dry_run=dry_run,
                    stop_event=self.stop_event,
                    progress=self._set_progress,
                    source_sheet_url=sheet_url,
                    status=self._set_status,
                )
                self.ui_queue.put(
                    (
                        "info",
                        (
                            "예약 발행 종료",
                            f"성공 {result.succeeded}건\n"
                            f"실패 {result.failed}건\n"
                            f"건너뜀 {result.skipped}건\n"
                            f"결과: {report_path}",
                        ),
                    )
                )
            except Exception as exc:
                self.logger.exception("예약 발행 실행 실패")
                self.ui_queue.put(("error", ("실행 실패", str(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _set_status(self, values: dict[str, int]) -> None:
        self.ui_queue.put(("immediate_status", values))

    def _drain_logs(self) -> None:
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "immediate_status":
                self.progress_text.set(
                    "대기 {pending} | 성공 {success} | 재시도 {retrying} | "
                    "실패 {failed} | 건너뜀 {skipped}".format(**payload)
                )
            else:
                self.ui_queue.put((kind, payload))
                break
        super()._drain_logs()

    def _on_close(self) -> None:
        if hasattr(self, "instance_lock"):
            self.instance_lock.__exit__(None, None, None)
        super()._on_close()


def main() -> None:
    app = ImmediateAutomationApp()
    app.mainloop()
