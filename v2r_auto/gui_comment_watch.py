from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .comment_watch import (
    DEFAULT_SHEET_URL,
    CommentWatchError,
    build_plan,
    load_watch_rows,
)
from .gui import AutomationApp
from .state import AnotherInstanceRunningError, InstanceLock


class CommentWatchApp(AutomationApp):
    app_name = "V2R 일상 글 댓글 확인"
    data_folder_name = "V2RCommentWatch"

    def __init__(self):
        super().__init__()
        self.instance_lock = InstanceLock(self.data_dir / "worker.lock")
        try:
            self.instance_lock.__enter__()
        except AnotherInstanceRunningError as exc:
            messagebox.showerror("중복 실행", str(exc))
            self.destroy()
            raise SystemExit(1) from exc
        self.geometry("820x620")
        self.minsize(760, 560)

    def _create_variables(self) -> None:
        self.sheet_url = tk.StringVar(value=DEFAULT_SHEET_URL)
        self.preview_only = tk.BooleanVar(value=False)
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
            text=(
                "브랜드 시트에서 완료 링크가 있는 씨씨앙·양평맘 글을 확인합니다. "
                "이전 원본글이 있고 아직 수정 전이며, 일상 글에 다른 사람 댓글이 있으면 "
                "K열에 카페 글 링크를 넣습니다. 없으면 K열을 비웁니다."
            ),
            wraplength=760,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self._entry_row(outer, 2, "Google 시트 URL", self.sheet_url)

        options = ttk.Frame(outer)
        options.grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Checkbutton(
            options,
            text="미리보기만 (시트에 쓰지 않음)",
            variable=self.preview_only,
        ).pack(side=tk.LEFT)

        buttons = ttk.Frame(outer)
        buttons.grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Button(buttons, text="로그인 준비", command=self._open_login).pack(
            side=tk.LEFT
        )
        self.start_button = ttk.Button(buttons, text="확인하기", command=self._start)
        self.start_button.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(
            buttons, text="중지", command=self._stop, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=22).grid(
            row=0, column=1, padx=(10, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="진행 기록", padding=8)
        log_frame.grid(row=6, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _open_login(self) -> None:
        sheet_url = self.sheet_url.get().strip()
        self._run_background(lambda: self.browser.open_login_window(sheet_url))

    def _start(self) -> None:
        if self.worker and not self.worker.done():
            return
        sheet_url = self.sheet_url.get().strip()
        if not sheet_url:
            messagebox.showerror("입력 오류", "Google 시트 주소를 넣어 주세요")
            return
        preview_only = self.preview_only.get()
        if not preview_only:
            confirmed = messagebox.askyesno(
                "시트에 쓰기",
                "K열(일상 글에 댓글)만 바꿉니다.\n"
                "댓글이 있는 행에는 카페 링크를 넣고, 없는 행은 비웁니다.\n계속할까요?",
            )
            if not confirmed:
                return
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self._set_progress(0, 3)

        def work() -> None:
            try:
                self.logger.info("시트를 읽습니다")
                csv_path = self.browser.download_sheet(sheet_url)
                headers, rows = load_watch_rows(csv_path)
                self._set_progress(1, 3)
                if self.stop_event.is_set():
                    return
                self.logger.info("V2R에서 원본글과 댓글을 확인합니다")
                plan = build_plan(headers, rows, self.browser.fetch_v2r_article)
                for row in plan.rows:
                    if not row.get("__source_id"):
                        continue
                    self.logger.info(
                        "%s행 %s · %s",
                        row.get("__row"),
                        row.get("__title") or row.get("__source_id"),
                        row.get("__reason") or row.get("__skip_reason") or "",
                    )
                self.logger.info(plan.summary().replace("\n", " / "))
                self._set_progress(2, 3)
                if preview_only:
                    self.ui_queue.put(("info", ("미리보기", plan.summary())))
                    return
                if self.stop_event.is_set():
                    return
                self.logger.info("K열을 한 번에 붙여넣습니다")
                self.browser.write_comment_marks(sheet_url, plan)
                self._set_progress(3, 3)
                self.ui_queue.put(("info", ("확인 완료", plan.summary())))
            except CommentWatchError as exc:
                self.ui_queue.put(("error", ("시트 열 확인", str(exc))))
            except Exception as exc:
                self.logger.exception("댓글 확인 실패")
                self.ui_queue.put(("error", ("실행 실패", str(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _on_close(self) -> None:
        if hasattr(self, "instance_lock"):
            self.instance_lock.__exit__(None, None, None)
        super()._on_close()


def main() -> None:
    app = CommentWatchApp()
    app.mainloop()
