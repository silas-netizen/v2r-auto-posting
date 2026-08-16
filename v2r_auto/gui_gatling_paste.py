from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from .daily_posts import DailyPostSheetError
from .gatling_paste import (
    GatlingPasteError,
    build_gatling_master,
    paste_manuscripts_into_gatling,
    recognize_gatling_workbook,
)
from .gui import AutomationApp
from .state import AnotherInstanceRunningError, InstanceLock


class GatlingPasteApp(AutomationApp):
    """Copy brand-sheet title, body, and comments into a 기관총 마스터 file."""

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
        self.local_csv = tk.StringVar()
        self.gatling_path = tk.StringVar()
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
            text=(
                "구글 시트 원고의 제목, 본문, 댓글·대댓글을 기관총 마스터에 넣습니다. "
                "기관총 파일은 .xlsm을 선택하세요. "
                "고른 파일에 마스터 시트와 6행 열 이름(링크, 타입, 제목, 내용)이 있으면 "
                "기관총 파일로 보고 마지막 줄 다음에 이어 넣습니다."
            ),
            wraplength=800,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self._entry_row(outer, 2, "브랜드 시트 URL", self.sheet_url)
        self._entry_row(
            outer,
            3,
            "브랜드 CSV",
            self.local_csv,
            button=("파일", self._choose_csv),
        )
        self._entry_row(
            outer,
            4,
            "기관총 엑셀",
            self.gatling_path,
            button=("파일", self._choose_gatling),
        )

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 10))
        ttk.Button(actions, text="로그인 준비", command=self._open_login).pack(
            side=tk.LEFT
        )
        ttk.Button(actions, text="기관총 확인", command=self._check_gatling).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(actions, text="원고 확인", command=self._check_data).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self.start_button = ttk.Button(
            actions, text="제목·본문·댓글 넣기", command=self._start
        )
        self.start_button.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(
            actions, text="중지", command=self._stop, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=22).grid(
            row=0, column=1, padx=(10, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="진행 기록", padding=8)
        log_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _choose_gatling(self) -> None:
        selected = filedialog.askopenfilename(
            title="기관총 엑셀 선택",
            filetypes=[
                ("기관총 Excel", "*.xlsm"),
                ("엑셀 파일", "*.xlsm *.xlsx *.xlsb"),
                ("모든 파일", "*.*"),
            ],
        )
        if selected:
            self.gatling_path.set(selected)

    def _open_login(self) -> None:
        sheet_url = self.sheet_url.get().strip()
        self._run_background(lambda: self.browser.open_login_window(sheet_url))

    def _brand_path(self) -> Path:
        local_csv = self.local_csv.get().strip()
        if local_csv:
            return Path(local_csv)
        sheet_url = self.sheet_url.get().strip()
        if not sheet_url:
            raise GatlingPasteError("브랜드 시트 주소 또는 CSV 파일을 넣어 주세요")
        return self.browser.download_sheet(sheet_url)

    def _check_gatling(self) -> None:
        path = self.gatling_path.get().strip()
        if not path:
            messagebox.showerror("입력 오류", "기관총 엑셀 파일을 선택해 주세요")
            return

        def work() -> None:
            info = recognize_gatling_workbook(path)
            self.logger.info(info.message)
            kind = "info" if info.recognized else "error"
            self.ui_queue.put((kind, ("기관총 확인", info.message)))

        self._run_background(work)

    def _check_data(self) -> None:
        def work() -> None:
            result = build_gatling_master(self._brand_path(), manuscript_only=True)
            counts = result.type_counts()
            self.logger.info(
                "원고 확인: %s건 / 제목·본문 %s / 댓글 %s / 대댓글 %s",
                len(result.jobs),
                counts.get("새글", 0) + counts.get("글수정", 0),
                counts.get("댓글", 0),
                counts.get("대댓글", 0),
            )
            for item in result.skipped:
                self.logger.info("건너뜀 %s", item)
            self.ui_queue.put(
                (
                    "info",
                    (
                        "원고 확인",
                        (
                            f"원고 {len(result.jobs)}건\n"
                            f"제목·본문 {counts.get('새글', 0) + counts.get('글수정', 0)} / "
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
        gatling_path = self.gatling_path.get().strip()
        if not gatling_path:
            messagebox.showerror("입력 오류", "기관총 엑셀 파일을 선택해 주세요")
            return
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0)
        self.progress_text.set("원고 넣는 중")

        def work() -> None:
            try:
                self._set_progress(1, 3)
                brand_path = self._brand_path()
                self._set_progress(2, 3)
                result, start_row = paste_manuscripts_into_gatling(
                    brand_path,
                    gatling_path,
                    manuscript_only=True,
                )
                counts = result.type_counts()
                self.logger.info(
                    "기관총 마스터 %s행부터 %s줄을 넣었습니다",
                    start_row,
                    len(result.rows),
                )
                self._set_progress(3, 3)
                self.ui_queue.put(
                    (
                        "info",
                        (
                            "넣기 완료",
                            (
                                f"마스터 {start_row}행부터 {len(result.rows)}줄을 넣었습니다.\n"
                                f"제목·본문 {counts.get('새글', 0) + counts.get('글수정', 0)} / "
                                f"댓글 {counts.get('댓글', 0)} / "
                                f"대댓글 {counts.get('대댓글', 0)}"
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
