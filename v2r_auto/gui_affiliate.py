from __future__ import annotations

import queue
import threading
import tkinter as tk
import time
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .daily_posts import load_daily_posts
from .gui import AutomationApp
from .images import load_sheet_brand
from .photo_washer import (
    PhotoWashPlan,
    needs_photo_wash,
    preferred_photo_washer_executable,
    prepare_photo_wash_plan,
    save_photo_washer_executable,
)
from .runner import AffiliateRunner
from .sheet import load_affiliate_jobs
from .state import AnotherInstanceRunningError, InstanceLock


DAILY_POST_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1vSON0Rej9anDQXcAOXyBrCr50B4MMqZ79FahF4cDPJw/edit?gid=1842684291#gid=1842684291"
)


class AffiliateAutomationApp(AutomationApp):
    """Small, single-row UI for the affiliate-cafe revision workflow."""

    app_name = "V2R 제휴 카페 수정 발행"
    data_folder_name = "V2RAffiliatePosting"

    def __init__(self):
        self.pause_event = threading.Event()
        self.photo_wash_plan: PhotoWashPlan | None = None
        super().__init__()
        self._cleanup_old_files()
        self.instance_lock = InstanceLock(self.data_dir / "data" / "worker.lock")
        try:
            self.instance_lock.__enter__()
        except AnotherInstanceRunningError as exc:
            messagebox.showerror("중복 실행", str(exc))
            self.destroy()
            raise SystemExit(1) from exc

    def _cleanup_old_files(self) -> None:
        now = time.time()
        for directory, days in ((self.log_dir, 30), (self.report_dir, 90)):
            cutoff = now - days * 86400
            for path in directory.iterdir():
                if path.is_file() and path.stat().st_mtime < cutoff:
                    try:
                        path.unlink()
                    except OSError:
                        self.logger.warning("오래된 파일 삭제 실패: %s", path)

    def _create_variables(self) -> None:
        self.sheet_url = tk.StringVar()
        detected = preferred_photo_washer_executable()
        self.photo_washer_path = tk.StringVar(
            value=str(detected) if detected else ""
        )
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
        self._entry_row(outer, 2, "Google 시트 URL", self.sheet_url)
        self._entry_row(
            outer,
            3,
            "포토워셔 main.exe",
            self.photo_washer_path,
            button=("찾기", self._choose_photo_washer),
        )

        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 10))
        ttk.Checkbutton(
            actions,
            text="검증 모드(글을 절대 등록하지 않음)",
            variable=self.dry_run,
        ).pack(side=tk.LEFT)
        ttk.Button(actions, text="1. 로그인 준비", command=self._open_login).pack(
            side=tk.LEFT, padx=(16, 0)
        )
        ttk.Button(actions, text="2. 대상 확인", command=self._check_data).pack(
            side=tk.LEFT, padx=6
        )
        self.start_button = ttk.Button(actions, text="3. 수정 발행 시작", command=self._start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(actions, text="중지", command=self._stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=6)
        self.pause_button = ttk.Button(
            actions,
            text="일시정지",
            command=self._toggle_pause,
            state=tk.DISABLED,
        )
        self.pause_button.pack(side=tk.LEFT)

        progress_frame = ttk.Frame(outer)
        progress_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=48).grid(
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

    def _choose_photo_washer(self) -> None:
        selected = filedialog.askopenfilename(
            title="포토워셔 main.exe 선택",
            filetypes=[("포토워셔", "main.exe"), ("실행 파일", "*.exe")],
        )
        if selected:
            try:
                save_photo_washer_executable(Path(selected))
            except Exception as exc:
                messagebox.showerror("포토워셔 경로 오류", str(exc))
                return
            self.photo_washer_path.set(selected)

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
        jobs = load_affiliate_jobs(source_csv)
        daily_posts = None
        for attempt in range(1, 4):
            try:
                daily_csv = self.browser.download_sheet(
                    DAILY_POST_SHEET_URL,
                    required_headers={"내용", "카페"},
                )
                daily_posts = load_daily_posts(daily_csv)
                break
            except Exception as exc:
                if attempt == 3:
                    raise
                self.logger.warning(
                    "일상 글 시트 다운로드·헤더 확인 재시도 (%s/3): %s",
                    attempt + 1,
                    exc,
                )
                time.sleep(attempt)
        assert daily_posts is not None
        try:
            brand = load_sheet_brand(sheet_url)
        except Exception as exc:
            brand = ""
            self.logger.warning(
                "Google 시트 제목에서 브랜드를 확인하지 못해 이미지 없이 진행합니다: %s",
                exc,
            )
        for job in jobs:
            job.brand = brand
        self.logger.info("이미지 브랜드 확인: %s", brand or "미확인")
        return jobs, daily_posts

    def _check_data(self) -> None:
        try:
            sheet_url = self.sheet_url.get().strip()
            if not sheet_url:
                raise ValueError("Google 시트 URL을 입력하세요")
            photo_washer_path = self.photo_washer_path.get().strip()
            if photo_washer_path:
                save_photo_washer_executable(Path(photo_washer_path))
        except ValueError as exc:
            messagebox.showerror("입력 오류", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("포토워셔 경로 오류", str(exc))
            return
        self.photo_wash_plan = None

        def work() -> None:
            jobs, daily_posts = self._load_affiliate_jobs(sheet_url)
            self.photo_wash_plan = prepare_photo_wash_plan(
                jobs,
                download_dir=self.download_dir,
                executable=(
                    Path(photo_washer_path)
                    if photo_washer_path
                    else None
                ),
                logger=self.logger,
            )
            self.logger.info(
                "처리 대상 확인 완료: 원고 %s건 / 일상 글 %s건 / "
                "사진 선택 %s개 / 수동 세탁 대기",
                len(jobs),
                len(daily_posts),
                self.photo_wash_plan.selected_count,
            )
            self.ui_queue.put(
                (
                    "info",
                    (
                        "처리 대상 확인",
                        f"A~E열이 모두 채워진 원고 {len(jobs)}건\n"
                        f"제휴 일상 글 {len(daily_posts)}건\n"
                        f"사진 선택 {self.photo_wash_plan.selected_count}개\n"
                        f"사진 준비 실패 원고 "
                        f"{len(self.photo_wash_plan.failures)}건\n\n"
                        "열린 폴더의 사진을 포토워셔로 드래그해 "
                        "전체 사진 세척 후 수정 발행 시작을 누르세요",
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
        if self.dry_run.get():
            confirmed = messagebox.askyesno(
                "검증 모드 확인",
                "검증 모드가 켜져 있어 실제 글은 하나도 등록되지 않습니다.\n"
                "데이터 검증만 실행할까요?",
            )
            if not confirmed:
                return
        else:
            confirmed = messagebox.askyesno(
                "실제 수정 발행 확인",
                "검증 모드가 꺼져 있습니다.\n일상 글과 수정 글이 실제로 등록됩니다. 계속할까요?",
            )
            if not confirmed:
                return

        dry_run = self.dry_run.get()
        self.stop_event.clear()
        self.pause_event.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.pause_button.configure(state=tk.NORMAL, text="일시정지")
        self.progress.configure(value=0)
        self.progress_text.set("행 데이터 준비 중")

        def work() -> None:
            try:
                jobs, daily_posts = self._load_affiliate_jobs(sheet_url)
                if self.photo_wash_plan is not None:
                    self.photo_wash_plan.apply(
                        jobs,
                        logger=self.logger,
                    )
                elif any(needs_photo_wash(job) for job in jobs):
                    raise ValueError(
                        "사진 세탁 준비가 없습니다. "
                        "2. 대상 확인을 먼저 실행하세요"
                    )
                runner = AffiliateRunner(
                    browser=self.browser,
                    report_dir=self.report_dir,
                    logger=self.logger,
                    state_path=self.data_dir / "data" / "jobs.db",
                )
                result, report_path = runner.run(
                    jobs=jobs,
                    email="",
                    password="",
                    dry_run=dry_run,
                    stop_event=self.stop_event,
                    progress=self._set_progress,
                    daily_posts=daily_posts,
                    source_sheet_url=sheet_url,
                    status=self._set_status,
                    pause_event=self.pause_event,
                )
                self.ui_queue.put(
                    (
                        "info",
                        (
                            "작업 종료",
                            f"성공 {result.succeeded}건\n"
                            f"실패 {result.failed}건\n"
                            f"건너뜀 {result.skipped}건\n"
                            f"결과: {report_path}",
                        ),
                    )
                )
            except Exception as exc:
                self.logger.exception("제휴 수정 발행 실행 실패")
                self.ui_queue.put(("error", ("실행 실패", str(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _toggle_pause(self) -> None:
        if not self.worker or self.worker.done():
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_button.configure(text="일시정지")
            self.logger.info("다시 시작을 요청했습니다")
        else:
            self.pause_event.set()
            self.pause_button.configure(text="다시 시작")
            self.progress_text.set("현재 작업 후 일시정지")
            self.logger.info("일시정지를 요청했습니다")

    def _worker_finished(self) -> None:
        self.pause_event.clear()
        self.pause_button.configure(state=tk.DISABLED, text="일시정지")
        super()._worker_finished()

    def _set_status(self, values: dict[str, int]) -> None:
        self.ui_queue.put(("affiliate_status", values))

    def _drain_logs(self) -> None:
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "affiliate_status":
                self.progress_text.set(
                    "대기 {pending} | 예약 {reserved} | 성공 {success} | "
                    "재시도 {retrying} | "
                    "실패 {failed} | 건너뜀 {skipped}".format(**payload)
                )
            else:
                self.ui_queue.put((kind, payload))
                break
        super()._drain_logs()

    def _on_close(self) -> None:
        if hasattr(self, "instance_lock"):
            self.instance_lock.__exit__(None, None, None)
        super()._on_close()


def main() -> None:
    app = AffiliateAutomationApp()
    app.mainloop()
