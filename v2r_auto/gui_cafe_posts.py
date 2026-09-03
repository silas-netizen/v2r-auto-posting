from __future__ import annotations

import json
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .cafe_post_browser import CafePostSession
from .cafe_posts import (
    CafePostError,
    apply_intro_cafe_name,
    excel_filename,
    parse_cafe_board_target,
    write_cafe_posts_xlsx,
)
from .gui import AutomationApp
from .state import AnotherInstanceRunningError, InstanceLock


class CafePostApp(AutomationApp):
    app_name = "카페 글 수집"
    data_folder_name = "V2RCafePosts"

    def __init__(self):
        super().__init__()
        self.instance_lock = InstanceLock(self.data_dir / "worker.lock")
        try:
            self.instance_lock.__enter__()
        except AnotherInstanceRunningError as exc:
            messagebox.showerror("중복 실행", "카페 글 수집 프로그램이 이미 실행 중입니다")
            self.destroy()
            raise SystemExit(1) from exc
        self.geometry("880x720")
        self.minsize(800, 640)
        self._load_settings()

    def _settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def _create_variables(self) -> None:
        self.cafe_url = tk.StringVar()
        self.board_url = tk.StringVar()
        self.delete_duplicates = tk.BooleanVar(value=False)
        self.progress_text = tk.StringVar(value="대기 중")
        self.collected_rows = []

    def _load_settings(self) -> None:
        path = self._settings_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.cafe_url.set(str(data.get("cafe_url") or ""))
        self.board_url.set(str(data.get("board_url") or ""))
        self.delete_duplicates.set(bool(data.get("delete_duplicates")))

    def _save_settings(self) -> None:
        self._settings_path().write_text(
            json.dumps(
                {
                    "cafe_url": self.cafe_url.get().strip(),
                    "board_url": self.board_url.get().strip(),
                    "delete_duplicates": bool(self.delete_duplicates.get()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

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
                "네이버 로그인 창을 연 뒤, 카페 전체 글을 엑셀로 모읍니다. "
                "화면의 10페이지에서 멈추지 않고, 글이 있는 마지막 페이지까지 봅니다. "
                "글이 없는 페이지는 더 보지 않습니다. "
                "특정 게시판만 필요하면 게시판 주소를 넣으세요. "
                "카페명은 카페소개의 카페 이름입니다. "
                "중지를 눌러도 지금까지 모은 글로 엑셀을 만듭니다. "
                "제목과 본문이 모두 같은 글은, 관리자 계정일 때 최신 글을 체크해서 삭제할 수 있습니다. "
                "가장 오래된 글은 남깁니다. "
                "엑셀 열은 카페명, 게시판, 작성자 닉네임, 제목, 본문, 댓글입니다. "
                "댓글은 위에서부터 닉네임과 내용이 한 쌍씩 들어갑니다."
            ),
            wraplength=820,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self._entry_row(outer, 2, "카페 주소", self.cafe_url)
        self._entry_row(outer, 3, "게시판 주소(선택)", self.board_url)
        ttk.Checkbutton(
            outer,
            text="수집 후, 제목·본문이 같은 최신 글 삭제 (관리자 계정)",
            variable=self.delete_duplicates,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 4))

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)
        ttk.Button(actions, text="1. 네이버 로그인", command=self._open_login).pack(
            side=tk.LEFT
        )
        self.start_button = ttk.Button(
            actions, text="2. 수집 시작", command=self._start
        )
        self.start_button.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(
            actions, text="중지", command=self._stop, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            actions, text="3. 중복 글 삭제", command=self._delete_duplicates
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            actions,
            text="저장 폴더",
            command=lambda: self._open_folder(self.data_dir),
        ).pack(side=tk.RIGHT)

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=28).grid(
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

    def _read_target(self):
        try:
            return parse_cafe_board_target(self.cafe_url.get(), self.board_url.get())
        except CafePostError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return None

    def _open_login(self) -> None:
        if self.worker and not self.worker.done():
            messagebox.showwarning("작업 중", "현재 작업이 끝난 뒤 다시 시도하세요")
            return
        target = self._read_target()
        if target is None:
            return
        self._save_settings()
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)

        def work() -> None:
            try:
                session = CafePostSession(self.browser, target)
                session.open_login_window(should_stop=self.stop_event.is_set)
            except CafePostError as exc:
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
        target = self._read_target()
        if target is None:
            return
        self._save_settings()
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.progress.configure(value=0)
        self.progress_text.set("로그인 확인")

        def work() -> None:
            session: CafePostSession | None = None
            error: Exception | None = None
            try:
                session = CafePostSession(self.browser, target)
                session.collect(
                    should_stop=self.stop_event.is_set,
                    progress=self._set_progress,
                )
            except CafePostError as exc:
                error = exc
                self.logger.error("%s", exc)
            except Exception as exc:
                error = exc
                self.logger.exception("카페 글 수집 실패")
            rows = list(session.rows) if session is not None else []
            intro_name = session.intro_cafe_name if session is not None else ""
            apply_intro_cafe_name(rows, intro_name)
            self.collected_rows = rows
            if rows:
                self._write_collected_excel(rows, intro_name)
                if (
                    self.delete_duplicates.get()
                    and session is not None
                    and not self.stop_event.is_set()
                ):
                    try:
                        self._run_duplicate_delete(session, rows)
                    except CafePostError as exc:
                        self.logger.error("%s", exc)
                        self.ui_queue.put(("error", ("중복 글 삭제 실패", str(exc))))
                    except Exception as exc:
                        self.logger.exception("중복 글 삭제 실패")
                        self.ui_queue.put(("error", ("실행 실패", str(exc))))
            elif self.stop_event.is_set():
                self.ui_queue.put(
                    (
                        "error",
                        (
                            "수집 중지",
                            "아직 모은 글이 없어 엑셀을 만들지 못했습니다",
                        ),
                    )
                )
            elif error is not None:
                title = "수집 실패" if isinstance(error, CafePostError) else "실행 실패"
                self.ui_queue.put(("error", (title, str(error))))
            else:
                self.ui_queue.put(("error", ("수집 결과", "저장할 글이 없습니다")))
            self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _delete_duplicates(self) -> None:
        if self.worker and not self.worker.done():
            messagebox.showwarning("작업 중", "현재 작업이 끝난 뒤 다시 시도하세요")
            return
        if not messagebox.askyesno(
            "중복 글 삭제",
            "제목과 본문이 모두 같은 글 중 최신 날짜 글을 삭제합니다.\n"
            "가장 오래된 글은 남깁니다.\n"
            "관리자 계정으로 로그인한 상태여야 합니다.",
        ):
            return
        target = self._read_target()
        if target is None:
            return
        self._save_settings()
        self.stop_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        existing = list(self.collected_rows)

        def work() -> None:
            session: CafePostSession | None = None
            try:
                session = CafePostSession(self.browser, target)
                rows = existing
                if not rows:
                    session.collect(
                        should_stop=self.stop_event.is_set,
                        progress=self._set_progress,
                    )
                    rows = list(session.rows)
                    apply_intro_cafe_name(rows, session.intro_cafe_name)
                    self.collected_rows = rows
                    if rows:
                        self._write_collected_excel(rows, session.intro_cafe_name)
                if not rows:
                    self.ui_queue.put(
                        ("error", ("중복 글 삭제", "먼저 글을 모아 주세요"))
                    )
                    return
                if self.stop_event.is_set():
                    return
                self._run_duplicate_delete(session, rows)
            except CafePostError as exc:
                self.logger.error("%s", exc)
                self.ui_queue.put(("error", ("중복 글 삭제 실패", str(exc))))
            except Exception as exc:
                self.logger.exception("중복 글 삭제 실패")
                self.ui_queue.put(("error", ("실행 실패", str(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _run_duplicate_delete(self, session: CafePostSession, rows: list) -> None:
        deleted = session.delete_newer_duplicates(
            rows, should_stop=self.stop_event.is_set
        )
        if deleted:
            self.ui_queue.put(
                (
                    "info",
                    (
                        "중복 글 삭제",
                        f"제목·본문이 같은 최신 글 {len(deleted)}개를 삭제했습니다.",
                    ),
                )
            )
        else:
            self.ui_queue.put(
                ("info", ("중복 글 삭제", "삭제할 최신 중복 글이 없습니다"))
            )

    def _write_collected_excel(
        self, rows: list, intro_name: str = ""
    ) -> None:
        cafe_name = intro_name or next(
            (row.cafe_name for row in rows if row.cafe_name), ""
        )
        path = write_cafe_posts_xlsx(
            self.data_dir / excel_filename(cafe_name),
            rows,
        )
        self._open_excel(path)
        stopped = self.stop_event.is_set()
        title = "수집 중지" if stopped else "수집 완료"
        self.logger.info("엑셀 %s개 글을 저장했습니다: %s", len(rows), path)
        self.ui_queue.put(
            (
                "info",
                (
                    title,
                    f"글 {len(rows)}개를 엑셀로 저장했습니다.\n{path}",
                ),
            )
        )

    @staticmethod
    def _open_excel(path: Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
        except OSError:
            pass

    def _on_close(self) -> None:
        try:
            self._save_settings()
        except OSError:
            pass
        if hasattr(self, "instance_lock"):
            self.instance_lock.__exit__(None, None, None)
        super()._on_close()


def main() -> None:
    app = CafePostApp()
    app.mainloop()
