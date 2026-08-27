from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox, ttk

from .gui import AutomationApp
from .nickname_browser import NicknameExcludeSession, open_login_windows
from .nickname_exclude import (
    DEFAULT_KEYWORDS,
    LocalSettings,
    NicknameExcludeError,
)
from .state import AnotherInstanceRunningError, InstanceLock


class NicknameExcludeApp(AutomationApp):
    app_name = "씨씨앙 제외 닉네임"
    data_folder_name = "V2RNicknameExclude"

    def __init__(self):
        super().__init__()
        self.instance_lock = InstanceLock(self.data_dir / "worker.lock")
        try:
            self.instance_lock.__enter__()
        except AnotherInstanceRunningError as exc:
            messagebox.showerror("중복 실행", str(exc))
            self.destroy()
            raise SystemExit(1) from exc
        self.geometry("820x640")
        self.minsize(760, 580)

    def _settings_path(self):
        return self.data_dir / "settings.json"

    def _create_variables(self) -> None:
        settings = LocalSettings.load(self._settings_path())
        self.keywords = tk.StringVar(value=settings.keywords or ", ".join(DEFAULT_KEYWORDS))
        self.flowmoa_user = tk.StringVar(value=settings.flowmoa_user)
        self.flowmoa_password = tk.StringVar()
        self.watch = tk.BooleanVar(value=settings.watch)
        self.watch_minutes = tk.IntVar(value=settings.watch_minutes)
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
                "씨씨앙 카페 글 검색창에 브랜드 식별 키워드를 넣어 "
                "글을 모두 찾은 뒤, 그 글의 닉네임을 신고기 제외 닉네임에 넣습니다. "
                "글쓰기 버튼은 쓰지 않습니다. "
                "이미 있는 닉네임은 그대로 두고 새로 나온 것만 추가합니다. "
                "식별 키워드는 나중에 추가하거나 바꿀 수 있습니다. "
                "네이버는 이 프로그램이 연 크롬에서 로그인하면 됩니다."
            ),
            wraplength=760,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self._entry_row(outer, 2, "식별 키워드", self.keywords)
        self._entry_row(outer, 3, "신고기 아이디", self.flowmoa_user)
        self._entry_row(outer, 4, "신고기 비밀번호", self.flowmoa_password, show="*")

        options = ttk.Frame(outer)
        options.grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Checkbutton(
            options,
            text="새로 나오는 닉네임을 계속 넣기",
            variable=self.watch,
        ).pack(side=tk.LEFT)
        ttk.Label(options, text="간격(분)").pack(side=tk.LEFT, padx=(12, 4))
        ttk.Spinbox(
            options,
            from_=5,
            to=180,
            textvariable=self.watch_minutes,
            width=6,
        ).pack(side=tk.LEFT)

        buttons = ttk.Frame(outer)
        buttons.grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Button(buttons, text="로그인 준비", command=self._open_login).pack(side=tk.LEFT)
        self.start_button = ttk.Button(buttons, text="제외 닉네임 넣기", command=self._start)
        self.start_button.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(
            buttons, text="중지", command=self._stop, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))

        log_frame = ttk.LabelFrame(outer, text="진행 기록", padding=8)
        log_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=22).grid(
            row=0, column=1, padx=(10, 0)
        )

    def _save_settings(self) -> None:
        LocalSettings(
            flowmoa_user=self.flowmoa_user.get().strip() or "earlybirdz",
            keywords=self.keywords.get().strip(),
            watch=self.watch.get(),
            watch_minutes=max(5, int(self.watch_minutes.get() or 30)),
        ).save(self._settings_path())

    def _open_login(self) -> None:
        self._save_settings()
        self._run_background(lambda: open_login_windows(self.browser))

    def _start(self) -> None:
        if self.worker and not self.worker.done():
            return
        user = self.flowmoa_user.get().strip()
        password = self.flowmoa_password.get()
        keywords = self.keywords.get().strip()
        if not keywords:
            messagebox.showerror("입력 오류", "브랜드 식별 키워드를 넣어 주세요")
            return
        if not user or not password:
            messagebox.showerror("입력 오류", "신고기 아이디와 비밀번호를 넣어 주세요")
            return
        watch = self.watch.get()
        try:
            minutes = max(5, int(self.watch_minutes.get() or 30))
        except (tk.TclError, ValueError):
            messagebox.showerror("입력 오류", "간격은 숫자로 넣어 주세요")
            return
        self._save_settings()
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self._set_progress(0, 2)

        def work() -> None:
            session = NicknameExcludeSession(self.browser)
            try:
                while True:
                    self.logger.info("씨씨앙 글을 검색해 제외 닉네임을 맞춥니다")
                    plan = session.sync(
                        keywords,
                        user,
                        password,
                        should_stop=self.stop_event.is_set,
                    )
                    self.logger.info(plan.summary().replace("\n", " / "))
                    if plan.added:
                        self.logger.info("새로 넣은 닉네임: %s", ", ".join(plan.added))
                    self._set_progress(2, 2)
                    if not watch:
                        self.ui_queue.put(("info", ("완료", plan.summary())))
                        return
                    if self.stop_event.is_set():
                        return
                    self.logger.info("%s분 뒤에 다시 확인합니다", minutes)
                    deadline = time.monotonic() + minutes * 60
                    while time.monotonic() < deadline:
                        if self.stop_event.is_set():
                            return
                        time.sleep(1)
            except NicknameExcludeError as exc:
                self.logger.error("%s", exc)
                self.ui_queue.put(("error", ("확인 실패", str(exc))))
            except Exception as exc:
                self.logger.exception("제외 닉네임 작업 실패")
                self.ui_queue.put(("error", ("실행 실패", str(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _on_close(self) -> None:
        try:
            self._save_settings()
        except Exception:
            pass
        if hasattr(self, "instance_lock"):
            self.instance_lock.__exit__(None, None, None)
        super()._on_close()


def main() -> None:
    app = NicknameExcludeApp()
    app.mainloop()
