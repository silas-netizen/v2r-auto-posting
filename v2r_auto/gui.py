from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .browser import BrowserConfig, V2RBrowser
from .cafe_catalog import write_catalog_report
from .runner import AutomationRunner, RunOptions
from .sheet import SheetDefaults, load_jobs


APP_NAME = "V2R 자동 발행"


def app_data_dir(product_name: str = "V2RAutoPosting") -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path.home() / ".local" / "share"
    return root / product_name


class TextQueueHandler(logging.Handler):
    def __init__(self, output_queue: queue.Queue[str]):
        super().__init__()
        self.output_queue = output_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.output_queue.put(self.format(record))


class AutomationApp(tk.Tk):
    app_name = APP_NAME
    data_folder_name = "V2RAutoPosting"

    def __init__(self):
        super().__init__()
        self.title(self.app_name)
        self.geometry("920x720")
        self.minsize(820, 650)

        self.data_dir = app_data_dir(self.data_folder_name)
        self.download_dir = self.data_dir / "downloads"
        self.report_dir = self.data_dir / "reports"
        self.log_dir = self.data_dir / "logs"
        for directory in (self.download_dir, self.report_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.logger = self._configure_logging()
        self.browser = V2RBrowser(
            BrowserConfig(
                profile_dir=self.data_dir / "chrome-profile",
                download_dir=self.download_dir,
            ),
            self.logger,
        )
        self.stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v2r-browser")
        self.worker: Future | None = None
        self._create_variables()
        self._build_ui()
        self.after(100, self._drain_logs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_logging(self) -> logging.Logger:
        logger = logging.getLogger("v2r_auto")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")

        gui_handler = TextQueueHandler(self.log_queue)
        gui_handler.setFormatter(formatter)
        logger.addHandler(gui_handler)

        file_handler = logging.FileHandler(
            self.log_dir / "v2r-auto.log",
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        )
        logger.addHandler(file_handler)
        return logger

    def _create_variables(self) -> None:
        self.sheet_url = tk.StringVar()
        self.local_csv = tk.StringVar()
        self.sheet_row = tk.StringVar()
        self.email = tk.StringVar()
        self.password = tk.StringVar()
        self.default_cafe = tk.StringVar()
        self.default_board = tk.StringVar()
        self.default_account = tk.StringVar()
        self.delay_seconds = tk.IntVar(value=30)
        self.dry_run = tk.BooleanVar(value=True)
        self.skip_duplicates = tk.BooleanVar(value=True)
        self.progress_text = tk.StringVar(value="대기 중")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(10, weight=1)

        ttk.Label(outer, text=APP_NAME, font=("", 18, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 14)
        )
        self._entry_row(outer, 1, "Google 시트 URL", self.sheet_url)
        self._entry_row(
            outer,
            2,
            "또는 CSV 파일",
            self.local_csv,
            button=("찾기", self._choose_csv),
        )
        self._entry_row(
            outer,
            3,
            "시트 행 번호 (선택)",
            self.sheet_row,
        )
        self._entry_row(outer, 4, "V2R 아이디", self.email)
        self._entry_row(outer, 5, "V2R 비밀번호", self.password, show="*")

        defaults = ttk.LabelFrame(outer, text="시트에 값이 없을 때 사용할 기본값", padding=10)
        defaults.grid(row=6, column=0, columnspan=3, sticky="ew", pady=10)
        for column in range(6):
            defaults.columnconfigure(column, weight=1 if column % 2 else 0)
        ttk.Label(defaults, text="카페").grid(row=0, column=0, padx=(0, 5))
        ttk.Entry(defaults, textvariable=self.default_cafe).grid(row=0, column=1, sticky="ew")
        ttk.Label(defaults, text="게시판").grid(row=0, column=2, padx=(10, 5))
        ttk.Entry(defaults, textvariable=self.default_board).grid(row=0, column=3, sticky="ew")
        ttk.Label(defaults, text="계정").grid(row=0, column=4, padx=(10, 5))
        ttk.Entry(defaults, textvariable=self.default_account).grid(row=0, column=5, sticky="ew")

        options = ttk.Frame(outer)
        options.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        ttk.Checkbutton(
            options,
            text="검증 모드(실제 발행하지 않음)",
            variable=self.dry_run,
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            options,
            text="이미 발행한 동일 글 건너뛰기",
            variable=self.skip_duplicates,
        ).pack(side=tk.LEFT, padx=16)
        ttk.Label(options, text="글 사이 간격(초)").pack(side=tk.LEFT)
        ttk.Spinbox(
            options,
            from_=10,
            to=3600,
            textvariable=self.delay_seconds,
            width=7,
        ).pack(side=tk.LEFT, padx=5)

        actions = ttk.Frame(outer)
        actions.grid(row=8, column=0, columnspan=3, sticky="ew")
        ttk.Button(actions, text="1. 로그인 준비", command=self._open_login).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="2. 카페·게시판 API 확인",
            command=self._check_cafe_catalog,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="3. 데이터 확인", command=self._check_data).pack(
            side=tk.LEFT, padx=6
        )
        self.start_button = ttk.Button(actions, text="4. 자동화 시작", command=self._start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(actions, text="중지", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="결과 폴더", command=lambda: self._open_folder(self.report_dir)).pack(
            side=tk.RIGHT
        )
        ttk.Button(actions, text="로그 폴더", command=lambda: self._open_folder(self.log_dir)).pack(
            side=tk.RIGHT, padx=6
        )

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=9, column=0, columnspan=3, sticky="ew", pady=10)
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=18).grid(
            row=0, column=1, padx=(10, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="실시간 로그", padding=8)
        log_frame.grid(row=10, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    @staticmethod
    def _entry_row(
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        show: str | None = None,
        button: tuple[str, object] | None = None,
    ) -> None:
        ttk.Label(parent, text=label, width=17).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=variable, show=show).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        if button:
            ttk.Button(parent, text=button[0], command=button[1]).grid(
                row=row, column=2, padx=(6, 0)
            )

    def _choose_csv(self) -> None:
        selected = filedialog.askopenfilename(
            title="CSV 파일 선택",
            filetypes=[("CSV 파일", "*.csv"), ("모든 파일", "*.*")],
        )
        if selected:
            self.local_csv.set(selected)

    def _open_login(self) -> None:
        sheet_url = self.sheet_url.get().strip()
        self._run_background(lambda: self.browser.open_login_window(sheet_url))

    def _check_cafe_catalog(self) -> None:
        def work() -> None:
            catalog = self.browser.load_v2r_cafe_catalog()
            report_path = write_catalog_report(catalog, self.report_dir)
            relevant = [
                cafe
                for cafe in catalog
                if cafe.category in {"자사 카페", "노출 테스트 카페"}
            ]
            for cafe in relevant:
                writable = sum(bool(menu.writable_accounts) for menu in cafe.menus)
                self.logger.info(
                    "%s 확인: %s / 계정 %s개 / 게시판 %s개(작성 가능 %s개)",
                    cafe.category,
                    cafe.name,
                    len(cafe.accounts),
                    len(cafe.menus),
                    writable,
                )
            summary = "\n".join(
                f"- {cafe.name}: 계정 {len(cafe.accounts)}개, 게시판 {len(cafe.menus)}개"
                for cafe in relevant
            )
            self.ui_queue.put(
                (
                    "info",
                    (
                        "카페·게시판 API 확인 완료",
                        (summary or "자사·테스트 카페를 찾지 못했습니다")
                        + f"\n\n상세 결과: {report_path}",
                    ),
                )
            )

        self._run_background(work)

    def _get_csv_path(self, local_csv: str, sheet_url: str) -> Path:
        if local_csv:
            return Path(local_csv)
        if not sheet_url:
            raise ValueError("Google 시트 URL 또는 CSV 파일을 입력하세요")
        return self.browser.download_sheet(sheet_url)

    def _load_jobs(
        self,
        local_csv: str,
        sheet_url: str,
        defaults: SheetDefaults,
        selected_row_number: int | None,
    ):
        return load_jobs(
            self._get_csv_path(local_csv, sheet_url),
            defaults,
            selected_row_number,
        )

    def _selected_row_number(self) -> int | None:
        value = self.sheet_row.get().strip()
        if not value:
            return None
        try:
            row_number = int(value)
        except ValueError as exc:
            raise ValueError("시트 행 번호는 2 이상의 숫자로 입력하세요") from exc
        if row_number < 2:
            raise ValueError("시트 행 번호는 2 이상의 숫자로 입력하세요")
        return row_number

    def _check_data(self) -> None:
        local_csv = self.local_csv.get().strip()
        sheet_url = self.sheet_url.get().strip()
        try:
            selected_row_number = self._selected_row_number()
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return
        defaults = SheetDefaults(
            cafe=self.default_cafe.get(),
            board=self.default_board.get(),
            account=self.default_account.get(),
        )

        def work() -> None:
            jobs = self._load_jobs(
                local_csv,
                sheet_url,
                defaults,
                selected_row_number,
            )
            errors = sum(bool(job.validate()) for job in jobs)
            self.logger.info("데이터 확인 완료: %s건, 필수값 누락 %s건", len(jobs), errors)
            self.ui_queue.put(
                ("info", ("데이터 확인", f"총 {len(jobs)}건\n필수값 누락 {errors}건"))
            )

        self._run_background(work)

    def _start(self) -> None:
        if self.worker and not self.worker.done():
            return
        if not self.dry_run.get():
            confirmed = messagebox.askyesno(
                "실제 발행 확인",
                "검증 모드가 꺼져 있습니다.\n실제로 V2R에 저장·발행하시겠습니까?",
            )
            if not confirmed:
                return
        try:
            delay_seconds = max(10, self.delay_seconds.get())
            selected_row_number = self._selected_row_number()
        except (tk.TclError, ValueError):
            messagebox.showerror(
                "입력 오류",
                "글 사이 간격과 시트 행 번호는 올바른 숫자로 입력하세요",
            )
            return
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0)
        self.progress_text.set("데이터 준비 중")
        local_csv = self.local_csv.get().strip()
        sheet_url = self.sheet_url.get().strip()
        defaults = SheetDefaults(
            cafe=self.default_cafe.get(),
            board=self.default_board.get(),
            account=self.default_account.get(),
        )
        email = self.email.get().strip()
        password = self.password.get()
        options = RunOptions(
            dry_run=self.dry_run.get(),
            delay_seconds=delay_seconds,
            skip_duplicates=self.skip_duplicates.get(),
        )

        def work() -> None:
            try:
                jobs = self._load_jobs(
                    local_csv,
                    sheet_url,
                    defaults,
                    selected_row_number,
                )
                runner = AutomationRunner(
                    browser=self.browser,
                    history_path=self.data_dir / "history.json",
                    report_dir=self.report_dir,
                    logger=self.logger,
                )
                _, report_path = runner.run(
                    jobs=jobs,
                    email=email,
                    password=password,
                    options=options,
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
                self.logger.exception("자동화 실행 실패")
                self.ui_queue.put(("error", ("실행 실패", str(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _stop(self) -> None:
        self.stop_event.set()
        self.progress_text.set("중지 요청됨")
        self.logger.warning("중지를 요청했습니다. 현재 작업 후 안전하게 종료합니다")

    def _set_progress(self, completed: int, total: int) -> None:
        percent = 0 if total == 0 else int(completed / total * 100)
        self.ui_queue.put(("progress", (completed, total, percent)))

    def _worker_finished(self) -> None:
        self.start_button.configure(state=tk.NORMAL)
        self.stop_button.configure(state=tk.DISABLED)

    def _run_background(self, callback) -> None:
        if self.worker and not self.worker.done():
            messagebox.showwarning("작업 중", "현재 작업이 끝난 뒤 다시 시도하세요")
            return

        def work() -> None:
            try:
                callback()
            except Exception as exc:
                self.logger.exception("작업 실패")
                self.ui_queue.put(("error", ("오류", str(exc))))

        self.worker = self.executor.submit(work)

    def _drain_logs(self) -> None:
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                completed, total, percent = payload
                self.progress.configure(value=percent)
                self.progress_text.set(f"{completed}/{total} ({percent}%)")
            elif kind == "status":
                self.progress_text.set(str(payload))
            elif kind == "info":
                title, message = payload
                messagebox.showinfo(title, message)
            elif kind == "error":
                title, message = payload
                messagebox.showerror(title, message)
            elif kind == "finished":
                self._worker_finished()
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        self.after(100, self._drain_logs)

    @staticmethod
    def _open_folder(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')

    def _on_close(self) -> None:
        self.stop_event.set()
        self.executor.submit(self.browser.close)
        self.executor.shutdown(wait=False)
        self.destroy()


def main() -> None:
    app = AutomationApp()
    app.mainloop()
