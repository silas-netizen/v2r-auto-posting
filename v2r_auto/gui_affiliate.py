from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .gui import AutomationApp
from .runner import AffiliateRunner
from .sheet import SheetSchemaError, load_affiliate_jobs


PATSUNI_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1OwR_LSjO1ofOojldtSIqoxv0gieNSMx_t35_5G1VTCc/edit?gid=0#gid=0"
)


class AffiliateAutomationApp(AutomationApp):
    """Small, single-row UI for the affiliate-cafe revision workflow."""

    app_name = "V2R 제휴 카페 수정 발행"
    data_folder_name = "V2RAffiliatePosting"

    def _create_variables(self) -> None:
        self.sheet_row = tk.StringVar()
        self.dry_run = tk.BooleanVar(value=True)
        self.progress_text = tk.StringVar(value="대기 중")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(6, weight=1)

        ttk.Label(outer, text=self.app_name, font=("", 18, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            outer,
            text="일상 글 작성 → 수정 글 예약 → 시트 원고·댓글 입력 → 등록",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Label(outer, text="연결 시트", width=17).grid(row=2, column=0, sticky="w", pady=3)
        ttk.Label(outer, text="팥순이 원고 시트").grid(row=2, column=1, sticky="w", pady=3)
        self._entry_row(outer, 3, "시트 행 번호", self.sheet_row)

        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 10))
        ttk.Checkbutton(
            actions,
            text="검증 모드(실제 등록하지 않음)",
            variable=self.dry_run,
        ).pack(side=tk.LEFT)
        ttk.Button(actions, text="1. 로그인 준비", command=self._open_login).pack(
            side=tk.LEFT, padx=(16, 0)
        )
        ttk.Button(actions, text="2. 행 확인", command=self._check_data).pack(
            side=tk.LEFT, padx=6
        )
        self.start_button = ttk.Button(actions, text="3. 수정 발행 시작", command=self._start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(actions, text="중지", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=6)

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=18).grid(
            row=0, column=1, padx=(10, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="실시간 로그", padding=8)
        log_frame.grid(row=6, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _selected_row_number(self) -> int:
        value = self.sheet_row.get().strip()
        try:
            row_number = int(value)
        except ValueError as exc:
            raise ValueError("시트 행 번호는 2 이상의 숫자로 입력하세요") from exc
        if row_number < 2:
            raise ValueError("시트 행 번호는 2 이상의 숫자로 입력하세요")
        return row_number

    def _open_login(self) -> None:
        self._run_background(lambda: self.browser.open_login_window(PATSUNI_SHEET_URL))

    def _load_affiliate_job(self, selected_row_number: int):
        csv_path = self._get_csv_path("", PATSUNI_SHEET_URL)
        return load_affiliate_jobs(csv_path, selected_row_number)

    def _check_data(self) -> None:
        try:
            selected_row_number = self._selected_row_number()
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return

        def work() -> None:
            jobs = self._load_affiliate_job(selected_row_number)
            job = jobs[0]
            errors = job.validate()
            if errors:
                raise SheetSchemaError(", ".join(errors))
            self.logger.info(
                "행 %s 확인 완료: %s / %s / %s",
                job.row_number,
                job.cafe,
                job.account,
                job.article_type,
            )
            self.ui_queue.put(
                (
                    "info",
                    (
                        "행 확인",
                        f"{job.row_number}행\n카페: {job.cafe}\n원고유형: {job.article_type}"
                        f"\n제목: {job.title}",
                    ),
                )
            )

        self._run_background(work)

    def _start(self) -> None:
        if self.worker and not self.worker.done():
            return
        try:
            selected_row_number = self._selected_row_number()
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return
        if not self.dry_run.get():
            confirmed = messagebox.askyesno(
                "실제 수정 발행 확인",
                "검증 모드가 꺼져 있습니다.\n일상 글과 수정 글이 실제로 등록됩니다. 계속할까요?",
            )
            if not confirmed:
                return

        dry_run = self.dry_run.get()
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0)
        self.progress_text.set("행 데이터 준비 중")

        def work() -> None:
            try:
                jobs = self._load_affiliate_job(selected_row_number)
                runner = AffiliateRunner(
                    browser=self.browser,
                    report_dir=self.report_dir,
                    logger=self.logger,
                )
                _, report_path = runner.run(
                    jobs=jobs,
                    email="",
                    password="",
                    dry_run=dry_run,
                    stop_event=self.stop_event,
                    progress=self._set_progress,
                )
                self.ui_queue.put(
                    (
                        "info",
                        ("작업 완료", f"작업이 완료되었습니다.\n결과: {report_path}"),
                    )
                )
            except Exception as exc:
                self.logger.exception("제휴 수정 발행 실행 실패")
                self.ui_queue.put(("error", ("실행 실패", str(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)


def main() -> None:
    app = AffiliateAutomationApp()
    app.mainloop()
