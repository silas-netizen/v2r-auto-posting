from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .affiliate_api import AffiliateApiError
from .browser import AutomationError
from .gui import AutomationApp
from .join_marker import (
    ACCOUNT_TEST_SHEET_URL,
    CAFE_LABELS,
    JoinMarkerError,
    build_plan,
    format_cafe_formula_error,
    load_account_rows,
    require_membership,
    user_facing_join_error,
)
from .state import AnotherInstanceRunningError, InstanceLock


class JoinMarkerApp(AutomationApp):
    app_name = "V2R 카페 가입 표시"
    data_folder_name = "V2RJoinMarker"

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
        self.sheet_url = tk.StringVar(value=ACCOUNT_TEST_SHEET_URL)
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
                "V2R에서 씨씨앙·양평맘 가입 아이디를 읽어 시트에 '가입'을 한 번에 표시합니다. "
                "아이디가 없는 칸은 비웁니다. 김천kb보험 열은 건드리지 않습니다."
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
        self.start_button = ttk.Button(buttons, text="표시하기", command=self._start)
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
                "씨씨앙·양평맘 열만 '가입'으로 맞춥니다.\n"
                "아이디가 없는 행은 비우고, 김천kb보험은 그대로 둡니다.\n계속할까요?",
            )
            if not confirmed:
                return
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self._set_progress(0, 3)

        def work() -> None:
            try:
                self.logger.info("V2R 카페 가입 아이디를 조회합니다")
                membership = self.browser.load_join_membership()
                require_membership(membership)
                for label in CAFE_LABELS:
                    self.logger.info("%s 가입 %s명", label, len(membership.get(label, set())))
                self._set_progress(1, 3)
                if self.stop_event.is_set():
                    return
                self.logger.info("시트를 읽습니다")
                csv_path = self.browser.download_sheet(sheet_url)
                headers, rows = load_account_rows(csv_path)
                plan = build_plan(headers, rows, membership)
                self.logger.info(plan.summary().replace("\n", " / "))
                self._set_progress(2, 3)
                if preview_only:
                    self.ui_queue.put(("info", ("미리보기", plan.summary())))
                    return
                if plan.formula_cells:
                    raise JoinMarkerError(format_cafe_formula_error(plan.formula_cells))
                if self.stop_event.is_set():
                    return
                self.logger.info("씨씨앙·양평맘 열을 시트에 바로 저장합니다")
                self.browser.write_join_marks(sheet_url, plan)
                self._set_progress(3, 3)
                self.ui_queue.put(("info", ("표시 완료", plan.summary())))
            except (JoinMarkerError, AutomationError, AffiliateApiError) as exc:
                message = user_facing_join_error(exc)
                self.logger.error("%s", message)
                self.ui_queue.put(("error", ("확인 필요", message)))
            except Exception as exc:
                self.logger.exception("가입 표시 실패")
                self.ui_queue.put(("error", ("실행 실패", user_facing_join_error(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _on_close(self) -> None:
        if hasattr(self, "instance_lock"):
            self.instance_lock.__exit__(None, None, None)
        super()._on_close()


def main() -> None:
    app = JoinMarkerApp()
    app.mainloop()
