from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox, ttk

from .gui import AutomationApp
from .nickname_browser import NicknameExcludeSession
from .nickname_exclude import (
    DEFAULT_CAFE_URL,
    DEFAULT_KEYWORDS,
    LocalSettings,
    NicknameExcludeError,
    open_nicknames_notepad,
    parse_cafe_address,
    write_nicknames_comma_file,
    write_nicknames_file,
)
from .state import AnotherInstanceRunningError, InstanceLock


class NicknameExcludeApp(AutomationApp):
    app_name = "카페 제외 닉네임"
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
        self.geometry("820x680")
        self.minsize(760, 620)

    def _settings_path(self):
        return self.data_dir / "settings.json"

    def _create_variables(self) -> None:
        settings = LocalSettings.load(self._settings_path())
        self.cafe_url = tk.StringVar(value=settings.cafe_url or DEFAULT_CAFE_URL)
        self.keywords = tk.StringVar(value=settings.keywords or ", ".join(DEFAULT_KEYWORDS))
        self.watch = tk.BooleanVar(value=settings.watch)
        self.watch_minutes = tk.IntVar(value=settings.watch_minutes)
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
                "먼저 네이버 로그인 창을 연 뒤, 입력한 카페 주소를 엽니다. "
                "식별 키워드는 글 + 댓글과 댓글내용으로 각각 검색하고, "
                "나온 닉네임을 합쳐 중복을 뺀 다음 "
                "메모장에 줄바꿈과 콤마 목록을 같이 띄웁니다. "
                "복사해서 신고기 제외 닉네임 칸에 넣으면 됩니다. "
                "글쓰기 버튼은 쓰지 않습니다."
            ),
            wraplength=760,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self._entry_row(outer, 2, "카페 주소", self.cafe_url)
        self._entry_row(outer, 3, "식별 키워드", self.keywords)

        options = ttk.Frame(outer)
        options.grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 10))
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
        buttons.grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Button(buttons, text="로그인 준비", command=self._open_login).pack(side=tk.LEFT)
        self.start_button = ttk.Button(buttons, text="닉네임 모으기", command=self._start)
        self.start_button.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(
            buttons, text="중지", command=self._stop, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))

        log_frame = ttk.LabelFrame(outer, text="진행 기록", padding=8)
        log_frame.grid(row=6, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state=tk.DISABLED)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=22).grid(
            row=0, column=1, padx=(10, 0)
        )

    def _save_settings(self) -> None:
        LocalSettings(
            cafe_url=self.cafe_url.get().strip() or DEFAULT_CAFE_URL,
            keywords=self.keywords.get().strip(),
            watch=self.watch.get(),
            watch_minutes=max(5, int(self.watch_minutes.get() or 30)),
        ).save(self._settings_path())

    def _read_cafe(self):
        try:
            return parse_cafe_address(self.cafe_url.get())
        except NicknameExcludeError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return None

    def _open_login(self) -> None:
        if self.worker and not self.worker.done():
            messagebox.showwarning("작업 중", "현재 작업이 끝난 뒤 다시 시도하세요")
            return
        cafe = self._read_cafe()
        if cafe is None:
            return
        self._save_settings()
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)

        def work() -> None:
            try:
                session = NicknameExcludeSession(self.browser, cafe)
                session.open_login_windows(should_stop=self.stop_event.is_set)
            except NicknameExcludeError as exc:
                self.logger.error("%s", exc)
                self.ui_queue.put(("error", ("로그인 준비 실패", str(exc))))
            except Exception as exc:
                self.logger.exception("로그인 준비 실패")
                self.ui_queue.put(("error", ("오류", str(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _start(self) -> None:
        if self.worker and not self.worker.done():
            return
        cafe = self._read_cafe()
        if cafe is None:
            return
        keywords = self.keywords.get().strip()
        cafe_url = self.cafe_url.get().strip()
        if not keywords:
            messagebox.showerror("입력 오류", "브랜드 식별 키워드를 넣어 주세요")
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
            session = NicknameExcludeSession(self.browser, cafe)
            try:
                while True:
                    self.logger.info(
                        "글 + 댓글과 댓글내용으로 검색한 뒤 닉네임을 합칩니다"
                    )
                    plan = session.sync(
                        keywords,
                        should_stop=self.stop_event.is_set,
                        cafe_url=cafe_url,
                    )
                    path = write_nicknames_file(
                        self.data_dir / "제외닉네임.txt",
                        plan.found,
                    )
                    comma_path = write_nicknames_comma_file(
                        self.data_dir / "제외닉네임_콤마.txt",
                        plan.found,
                    )
                    open_nicknames_notepad(path)
                    open_nicknames_notepad(comma_path)
                    self.logger.info(
                        "닉네임 %s개를 줄바꿈과 콤마로 띄웠습니다: %s / %s",
                        len(plan.found),
                        path,
                        comma_path,
                    )
                    self.logger.info(plan.summary().replace("\n", " / "))
                    self._set_progress(2, 2)
                    if not watch:
                        self.ui_queue.put(
                            (
                                "info",
                                (
                                    "닉네임 모음",
                                    f"{plan.summary()}\n\n메모장에 줄바꿈 목록과 콤마 목록을 띄웠습니다.\n"
                                    "복사해서 신고기 제외 닉네임 칸에 넣으면 됩니다.",
                                ),
                            )
                        )
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
