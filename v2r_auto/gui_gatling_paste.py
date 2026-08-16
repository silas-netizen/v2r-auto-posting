from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from .daily_posts import DailyPostSheetError
from .gatling_paste import (
    DAILY_POST_SHEET_URL,
    GatlingPasteError,
    build_and_write_master,
    build_gatling_master,
    is_affiliate_cafe,
)
from .gui import AutomationApp
from .state import AnotherInstanceRunningError, InstanceLock


class GatlingPasteApp(AutomationApp):
    """Copy brand-sheet manuscripts into a 기관총 마스터 Excel file."""

    app_name = "V2R 기관총 붙여넣기"
    data_folder_name = "V2RGatlingPaste"

    def __init__(self):
        super().__init__()
        self.instance_lock = InstanceLock(self.data_dir / "data" / "worker.lock")
        try:
            self.instance_lock.__enter__()
        except AnotherInstanceRunningError as exc:
            messagebox.showerror("중복 실행", str(exc))
            self.destroy()
            raise SystemExit(1) from exc
        self.geometry("860x640")
        self.minsize(800, 580)

    def _create_variables(self) -> None:
        self.sheet_url = tk.StringVar()
        self.daily_sheet_url = tk.StringVar(value=DAILY_POST_SHEET_URL)
        self.local_csv = tk.StringVar()
        self.local_daily_csv = tk.StringVar()
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
            text=(
                "브랜드 시트 원고를 기관총 마스터 엑셀로 옮깁니다. "
                "제휴 카페(씨씨앙·양평맘)는 일상 글을 새글에, 실제 원고를 글수정에 넣습니다. "
                "기관총에서 등록할 때는 새글만 먼저 등록하면 됩니다. "
                "대댓글 A열은 대상 번호(2.1=대대댓글2, 2.2=대대대댓글2)이고, "
                "글 주소는 결과링크에 넣습니다."
            ),
            wraplength=800,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self._entry_row(outer, 2, "브랜드 시트 URL", self.sheet_url)
        self._entry_row(outer, 3, "일상 글 시트 URL", self.daily_sheet_url)
        self._entry_row(
            outer,
            4,
            "브랜드 CSV",
            self.local_csv,
            button=("파일", self._choose_csv),
        )
        self._entry_row(
            outer,
            5,
            "일상 글 CSV",
            self.local_daily_csv,
            button=("파일", self._choose_daily_csv),
        )

        actions = ttk.Frame(outer)
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 10))
        ttk.Button(actions, text="로그인 준비", command=self._open_login).pack(
            side=tk.LEFT
        )
        ttk.Button(actions, text="대상 확인", command=self._check_data).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self.start_button = ttk.Button(
            actions, text="엑셀 만들기", command=self._start
        )
        self.start_button.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(
            actions, text="중지", command=self._stop, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            actions, text="결과 폴더", command=lambda: self._open_folder(self.report_dir)
        ).pack(side=tk.RIGHT)

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=22).grid(
            row=0, column=1, padx=(10, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="진행 기록", padding=8)
        log_frame.grid(row=8, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _choose_daily_csv(self) -> None:
        selected = filedialog.askopenfilename(
            title="일상 글 CSV 파일 선택",
            filetypes=[("CSV 파일", "*.csv"), ("모든 파일", "*.*")],
        )
        if selected:
            self.local_daily_csv.set(selected)

    def _open_login(self) -> None:
        sheet_url = self.sheet_url.get().strip() or self.daily_sheet_url.get().strip()
        self._run_background(lambda: self.browser.open_login_window(sheet_url))

    def _source_paths(self) -> tuple[Path, Path | None]:
        brand_csv = self.local_csv.get().strip()
        daily_csv = self.local_daily_csv.get().strip()
        sheet_url = self.sheet_url.get().strip()
        daily_url = self.daily_sheet_url.get().strip()
        brand_path = Path(brand_csv) if brand_csv else None
        daily_path = Path(daily_csv) if daily_csv else None
        if brand_path is None:
            if not sheet_url:
                raise GatlingPasteError("브랜드 시트 주소 또는 CSV 파일을 넣어 주세요")
            brand_path = self.browser.download_sheet(sheet_url)
            if daily_path is None and daily_url:
                daily_path = self.browser.download_sheet(daily_url)
        return brand_path, daily_path

    def _check_data(self) -> None:
        def work() -> None:
            brand_path, daily_path = self._source_paths()
            result = build_gatling_master(brand_path, daily_path)
            counts = result.type_counts()
            affiliate = sum(1 for job in result.jobs if is_affiliate_cafe(job.cafe))
            self.logger.info(
                "대상 확인: 원고 %s건 / 제휴 %s건 / 새글 %s / 글수정 %s / 댓글 %s / 대댓글 %s",
                len(result.jobs),
                affiliate,
                counts.get("새글", 0),
                counts.get("글수정", 0),
                counts.get("댓글", 0),
                counts.get("대댓글", 0),
            )
            for item in result.skipped:
                self.logger.info("건너뜀 %s", item)
            self.ui_queue.put(
                (
                    "info",
                    (
                        "대상 확인",
                        (
                            f"원고 {len(result.jobs)}건 (제휴 {affiliate}건)\n"
                            f"새글 {counts.get('새글', 0)} / "
                            f"글수정 {counts.get('글수정', 0)} / "
                            f"댓글 {counts.get('댓글', 0)} / "
                            f"대댓글 {counts.get('대댓글', 0)}\n"
                            f"건너뜀 {len(result.skipped)}건"
                        ),
                    ),
                )
            )

        self._run_background(work)

    def _start(self) -> None:
        if self.worker and not self.worker.done():
            return
        output = self.report_dir / (
            f"기관총_붙여넣기_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        )
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0)
        self.progress_text.set("엑셀 준비 중")

        def work() -> None:
            try:
                self._set_progress(1, 3)
                brand_path, daily_path = self._source_paths()
                self._set_progress(2, 3)
                result = build_and_write_master(brand_path, output, daily_path)
                counts = result.type_counts()
                self.logger.info("기관총 엑셀 저장: %s", output)
                self._set_progress(3, 3)
                self.ui_queue.put(
                    (
                        "info",
                        (
                            "엑셀 만들기 완료",
                            (
                                f"새글 {counts.get('새글', 0)} / "
                                f"글수정 {counts.get('글수정', 0)} / "
                                f"댓글 {counts.get('댓글', 0)} / "
                                f"대댓글 {counts.get('대댓글', 0)}\n"
                                f"건너뜀 {len(result.skipped)}건\n"
                                f"파일: {output}"
                            ),
                        ),
                    )
                )
            except (GatlingPasteError, DailyPostSheetError) as exc:
                self.logger.exception("기관총 붙여넣기 실패")
                self.ui_queue.put(("error", ("붙여넣기 실패", str(exc))))
            except Exception as exc:
                self.logger.exception("기관총 붙여넣기 실행 실패")
                self.ui_queue.put(("error", ("실행 실패", str(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _on_close(self) -> None:
        if hasattr(self, "instance_lock"):
            self.instance_lock.__exit__(None, None, None)
        super()._on_close()


def main() -> None:
    app = GatlingPasteApp()
    app.mainloop()
