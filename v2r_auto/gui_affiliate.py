from __future__ import annotations

import json
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from .daily_posts import load_daily_posts
from .gui import AutomationApp
from .runner import AffiliateRunner
from .sheet import load_affiliate_jobs


DAILY_POST_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1vSON0Rej9anDQXcAOXyBrCr50B4MMqZ79FahF4cDPJw/edit?gid=1842684291#gid=1842684291"
)


class AffiliateAutomationApp(AutomationApp):
    """Small, single-row UI for the affiliate-cafe revision workflow."""

    app_name = "V2R 제휴 카페 수정 발행"
    data_folder_name = "V2RAffiliatePosting"

    def _create_variables(self) -> None:
        self.sheet_url = tk.StringVar()
        self.dry_run = tk.BooleanVar(value=True)
        self.progress_text = tk.StringVar(value="대기 중")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(5, weight=1)

        ttk.Label(outer, text=self.app_name, font=("", 18, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            outer,
            text="일상 글 작성 → 수정 글 예약 → 시트 원고·댓글 입력 → 등록",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self._entry_row(outer, 2, "Google 시트 URL", self.sheet_url)

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 10))
        ttk.Checkbutton(
            actions,
            text="검증 모드(실제 등록하지 않음)",
            variable=self.dry_run,
        ).pack(side=tk.LEFT)
        ttk.Button(actions, text="1. 로그인 준비", command=self._open_login).pack(
            side=tk.LEFT, padx=(16, 0)
        )
        ttk.Button(actions, text="2. 화면 확인", command=self._inspect_form).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(actions, text="3. API 확인 시작", command=self._start_api_capture).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(actions, text="4. API 확인 저장", command=self._finish_api_capture).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(actions, text="5. 대상 확인", command=self._check_data).pack(
            side=tk.LEFT, padx=6
        )
        self.start_button = ttk.Button(actions, text="6. 수정 발행 시작", command=self._start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(actions, text="중지", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=6)

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=18).grid(
            row=0, column=1, padx=(10, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="실시간 로그", padding=8)
        log_frame.grid(row=5, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _open_login(self) -> None:
        sheet_url = self.sheet_url.get().strip()
        if not sheet_url:
            messagebox.showerror("입력 오류", "Google 시트 URL을 입력하세요")
            return
        self._run_background(lambda: self.browser.open_login_window(sheet_url))

    def _load_affiliate_jobs(self, sheet_url: str):
        if not sheet_url:
            raise ValueError("Google 시트 URL을 입력하세요")
        source_csv = self._get_csv_path("", sheet_url)
        daily_csv = self.browser.download_sheet(DAILY_POST_SHEET_URL)
        return load_affiliate_jobs(source_csv), load_daily_posts(daily_csv)

    def _inspect_form(self) -> None:
        def work() -> None:
            form_map = self.browser.inspect_se_one_form()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = self.report_dir / f"SE-ONE_화면확인_{stamp}.json"
            screenshot_path = self.report_dir / f"SE-ONE_화면확인_{stamp}.png"
            report_path.write_text(
                json.dumps(form_map, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.browser.save_screenshot(screenshot_path)
            controls = form_map.get("controls", [])
            self.logger.info(
                "SE-ONE 화면 확인 완료: 입력·버튼 %s개 / 보고서: %s",
                len(controls),
                report_path,
            )
            self.ui_queue.put(
                (
                    "info",
                    (
                        "화면 확인 완료",
                        "글은 등록하지 않았습니다.\n"
                        f"보고서: {report_path}\n"
                        f"화면: {screenshot_path}",
                    ),
                )
            )

        self._run_background(work)

    def _start_api_capture(self) -> None:
        def work() -> None:
            self.browser.start_api_capture()
            self.ui_queue.put(
                (
                    "info",
                    (
                        "API 확인 기록 중",
                        "이제 크롬 V2R 화면에서 일상 글 작성부터 최종 등록까지 진행하세요.\n"
                        "끝나면 프로그램에서 '4. API 확인 저장'을 누르세요.",
                    ),
                )
            )

        self._run_background(work)

    def _finish_api_capture(self) -> None:
        def work() -> None:
            requests = self.browser.finish_api_capture()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = self.report_dir / f"V2R_API_확인_{stamp}.json"
            report_path.write_text(
                json.dumps({"requests": requests}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.ui_queue.put(
                (
                    "info",
                    (
                        "API 확인 저장 완료",
                        f"글 내용·비밀번호는 저장하지 않았습니다.\n결과: {report_path}",
                    ),
                )
            )

        self._run_background(work)

    def _check_data(self) -> None:
        try:
            sheet_url = self.sheet_url.get().strip()
            if not sheet_url:
                raise ValueError("Google 시트 URL을 입력하세요")
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return

        def work() -> None:
            jobs, daily_posts = self._load_affiliate_jobs(sheet_url)
            self.logger.info(
                "처리 대상 확인 완료: 원고 %s건 / 일상 글 %s건",
                len(jobs),
                len(daily_posts),
            )
            self.ui_queue.put(
                (
                    "info",
                    (
                        "처리 대상 확인",
                        f"A~E열이 모두 채워진 원고 {len(jobs)}건\n"
                        f"제휴 일상 글 {len(daily_posts)}건",
                    ),
                )
            )

        self._run_background(work)

    def _start(self) -> None:
        if self.worker and not self.worker.done():
            return
        try:
            sheet_url = self.sheet_url.get().strip()
            if not sheet_url:
                raise ValueError("Google 시트 URL을 입력하세요")
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
                jobs, daily_posts = self._load_affiliate_jobs(sheet_url)
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
                    daily_posts=daily_posts,
                    source_sheet_url=sheet_url,
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
