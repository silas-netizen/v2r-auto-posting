from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from .browser import user_facing_browser_error
from .daily_posts import DailyPostSheetError, looks_like_daily_sheet
from .gatling_accounts import (
    DEFAULT_AUTO_ID_COUNT,
    MAX_AUTO_ID_COUNT,
    MIN_AUTO_ID_COUNT,
    normalize_auto_id_count,
    recognize_proxy_workbook,
    require_proxy_workbook,
)
from .gatling_paste import (
    DAILY_POST_SHEET_URL,
    GatlingPasteError,
    build_gatling_master,
    gatling_image_folder,
    is_affiliate_cafe,
    load_existing_manuscript_keys,
    load_gatling_brand_jobs,
    paste_manuscripts_into_gatling,
    recognize_gatling_workbook,
)
from .images import GoogleDriveImageResolver, brand_from_sheet_title, load_sheet_brand
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
        self.geometry("860x740")
        self.minsize(800, 660)

    def _create_variables(self) -> None:
        self.sheet_url = tk.StringVar()
        self.local_csv = tk.StringVar()
        self.gatling_path = tk.StringVar()
        self.proxy_path = tk.StringVar()
        self.auto_id_count = tk.IntVar(value=DEFAULT_AUTO_ID_COUNT)
        self.progress_text = tk.StringVar(value="대기 중")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(9, weight=1)

        ttk.Label(outer, text=self.app_name, font=("", 18, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            outer,
            text=(
                "구글 시트 원고의 제목, 본문, 댓글·대댓글을 기관총 .xlsm 마스터에 "
                "자동으로 넣는 프로그램입니다. "
                "제휴 카페(씨씨앙·양평맘)는 새글(일상) → 글수정(원고) → 댓글 → 대댓글 "
                "순서로 넣고, 새글 링크 열에는 그 카페 게시판 주소를 넣습니다. "
                "자사 카페는 새글에 원고를 바로 넣습니다. "
                "자사 카페 새글 링크에는 그 카페에 정해 둔 게시판 주소를 넣습니다. "
                "해시태그는 원고 행에만 넣습니다. "
                "시트에 {키워드}·{A열 키워드}·{B/A}가 있으면 사진을 고른 뒤 "
                "{이미지}로 바꾸고, 고른 사진은 기관총 파일 옆 폴더에 모읍니다. "
                "시트에 있는 원고는 모두 넣습니다. "
                "엑셀 타입이 비어 있어도 새글·글수정·댓글·대댓글을 알아서 적습니다. "
                "제목과 본문이 이미 같은 원고만 넣지 않습니다. "
                "프록시 엑셀을 넣으면 양평맘·씨씨앙은 제휴 댓글 아이디를, "
                "자사 카페는 자사 댓글 아이디를 같은 후기형·질문형 순서로 랜덤으로 넣습니다. "
                "아이디를 따로 고르지 않으면 아래 수량만큼 본문 작성 아이디를 자동 배정합니다. "
                "2~10개 중 고를 수 있고, 기본은 6개입니다. "
                "제휴 카페는 프록시 제휴 아이디를, 자사 카페는 자사 아이디를 돌려 가며 넣습니다. "
                "시트에 작성계정이 있으면 그 아이디를 그대로 씁니다. "
                "댓글 아이디는 댓글 넣을 때만 제휴 댓·제휴 댓글 / 자사 댓·자사 댓글에서 6개를 씁니다. "
                "제휴 댓과 자사 댓은 서로 다른 아이디를 씁니다. "
                "작성자 비번·크롬번호는 그 아이디가 프록시에 있을 때만 채웁니다. "
                "구글 시트 주소를 쓰면 '구글 시트 열기'로 시트를 엽니다. "
                "V2R은 쓰지 않습니다. 기관총 파일은 엑셀에서 닫아 둔 .xlsm을 고르세요."
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
        self._entry_row(
            outer,
            5,
            "프록시 엑셀",
            self.proxy_path,
            button=("파일", self._choose_proxy),
        )
        ttk.Label(outer, text="자동 배정 아이디 수량", width=17).grid(
            row=6, column=0, sticky="w", pady=3
        )
        count_row = ttk.Frame(outer)
        count_row.grid(row=6, column=1, columnspan=2, sticky="w", pady=3)
        ttk.Spinbox(
            count_row,
            from_=MIN_AUTO_ID_COUNT,
            to=MAX_AUTO_ID_COUNT,
            textvariable=self.auto_id_count,
            width=6,
            state="readonly",
        ).pack(side=tk.LEFT)
        ttk.Label(
            count_row,
            text="아이디를 고르지 않으면 이 개수만큼 본문 작성 아이디를 자동으로 넣습니다. 제휴 카페는 제휴, 자사 카페는 자사에서 고릅니다",
        ).pack(side=tk.LEFT, padx=(8, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 10))
        ttk.Button(actions, text="구글 시트 열기", command=self._open_sheet).pack(
            side=tk.LEFT
        )
        ttk.Button(actions, text="기관총 확인", command=self._check_gatling).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(actions, text="프록시 확인", command=self._check_proxy).pack(
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
        progress_frame.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(progress_frame, textvariable=self.progress_text, width=22).grid(
            row=0, column=1, padx=(10, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="진행 기록", padding=8)
        log_frame.grid(row=9, column=0, columnspan=3, sticky="nsew")
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

    def _choose_proxy(self) -> None:
        selected = filedialog.askopenfilename(
            title="프록시 엑셀 선택",
            filetypes=[
                ("엑셀 파일", "*.xlsx *.xlsm *.csv"),
                ("모든 파일", "*.*"),
            ],
        )
        if selected:
            self.proxy_path.set(selected)

    def _proxy_book(self):
        path = self.proxy_path.get().strip()
        if not path:
            return None
        return require_proxy_workbook(path)

    def _auto_id_count(self) -> int:
        return normalize_auto_id_count(self.auto_id_count.get())

    def _open_sheet(self) -> None:
        sheet_url = self.sheet_url.get().strip()
        if not sheet_url:
            messagebox.showerror("입력 오류", "브랜드 시트 URL을 넣어 주세요")
            return
        self._run_background(lambda: self.browser.open_google_sheet(sheet_url))

    def _brand_path(self) -> Path:
        local_csv = self.local_csv.get().strip()
        if local_csv:
            return Path(local_csv)
        sheet_url = self.sheet_url.get().strip()
        if not sheet_url:
            raise GatlingPasteError("브랜드 시트 주소 또는 CSV 파일을 넣어 주세요")
        return self.browser.download_sheet(sheet_url)

    def _brand_and_daily_paths(self) -> tuple[Path, Path | None]:
        brand_path = self._brand_path()
        jobs, _ = load_gatling_brand_jobs(brand_path, skip_completed=False)
        if any(is_affiliate_cafe(job.cafe) for job in jobs):
            self.logger.info("제휴 카페용 일상 글 시트를 내려받습니다")
            daily_path = self.browser.download_sheet(
                DAILY_POST_SHEET_URL,
                ignore_paths=[brand_path],
            )
            if not looks_like_daily_sheet(daily_path):
                self.logger.info("일상 글 시트가 아니라 한 번 더 받습니다")
                daily_path = self.browser.download_sheet(
                    DAILY_POST_SHEET_URL,
                    ignore_paths=[brand_path, daily_path],
                )
            return brand_path, daily_path
        return brand_path, None

    def _brand_name(self, brand_path: Path) -> str:
        name = brand_from_sheet_title(brand_path.stem)
        if name:
            return name
        url = self.sheet_url.get().strip()
        if not url:
            return ""
        try:
            return load_sheet_brand(url)
        except Exception:
            return ""

    def _image_resolver(self) -> GoogleDriveImageResolver:
        return GoogleDriveImageResolver(self.data_dir / "data", self.logger)

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

    def _check_proxy(self) -> None:
        path = self.proxy_path.get().strip()
        if not path:
            messagebox.showerror("입력 오류", "프록시 엑셀 파일을 선택해 주세요")
            return

        def work() -> None:
            info = recognize_proxy_workbook(path)
            self.logger.info(info.message)
            kind = "info" if info.recognized else "error"
            self.ui_queue.put((kind, ("프록시 확인", info.message)))

        self._run_background(work)

    def _check_data(self) -> None:
        def work() -> None:
            brand_path, daily_path = self._brand_and_daily_paths()
            gatling_path = self.gatling_path.get().strip()
            existing_keys = (
                load_existing_manuscript_keys(gatling_path) if gatling_path else None
            )
            result = build_gatling_master(
                brand_path,
                daily_path,
                manuscript_only=False,
                skip_completed=False,
                brand=self._brand_name(brand_path),
                image_resolver=self._image_resolver(),
                existing_keys=existing_keys,
                proxy_book=self._proxy_book(),
                author_id_count=self._auto_id_count(),
            )
            counts = result.type_counts()
            self.logger.info(
                "원고 확인: %s건 / 새글 %s / 글수정 %s / 댓글 %s / 대댓글 %s / 이미지 %s / 계정 %s",
                len(result.jobs),
                counts.get("새글", 0),
                counts.get("글수정", 0),
                counts.get("댓글", 0),
                counts.get("대댓글", 0),
                result.image_count,
                result.account_count,
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
                            f"새글 {counts.get('새글', 0)} / "
                            f"글수정 {counts.get('글수정', 0)} / "
                            f"댓글 {counts.get('댓글', 0)} / "
                            f"대댓글 {counts.get('대댓글', 0)} / "
                            f"이미지 {result.image_count}장 / "
                            f"계정 {result.account_count}줄\n"
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
                brand_path, daily_path = self._brand_and_daily_paths()
                self._set_progress(2, 3)
                result, start_row = paste_manuscripts_into_gatling(
                    brand_path,
                    gatling_path,
                    daily_path,
                    manuscript_only=False,
                    skip_completed=False,
                    brand=self._brand_name(brand_path),
                    image_resolver=self._image_resolver(),
                    image_dir=gatling_image_folder(gatling_path),
                    proxy_book=self._proxy_book(),
                    author_id_count=self._auto_id_count(),
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
                                f"기존 글은 그대로 두고, 시트 원고 "
                                f"{len(result.jobs)}건을 {start_row}행부터 넣었습니다. "
                                f"타입이 비어 있던 칸에는 새글·글수정·댓글·대댓글을 "
                                f"알아서 적었습니다.\n"
                                f"전체 {len(result.rows)}줄.\n"
                                f"새글 {counts.get('새글', 0)} / "
                                f"글수정 {counts.get('글수정', 0)} / "
                                f"댓글 {counts.get('댓글', 0)} / "
                                f"대댓글 {counts.get('대댓글', 0)} / "
                                f"이미지 {result.image_count}장 / "
                                f"계정 {result.account_count}줄"
                            ),
                        ),
                    )
                )
            except (GatlingPasteError, DailyPostSheetError) as exc:
                self.logger.exception("기관총 붙여넣기 실패")
                self.ui_queue.put(("error", ("붙여넣기 실패", user_facing_browser_error(exc))))
            except Exception as exc:
                self.logger.exception("기관총 붙여넣기 실행 실패")
                self.ui_queue.put(("error", ("실행 실패", user_facing_browser_error(exc))))
            finally:
                self.ui_queue.put(("finished", None))

        self.worker = self.executor.submit(work)

    def _on_close(self) -> None:
        if hasattr(self, "instance_lock"):
            self.instance_lock.__exit__(None, None, None)
        super()._on_close()

    def _run_background(self, callback) -> None:
        if self.worker and not self.worker.done():
            messagebox.showwarning("작업 중", "현재 작업이 끝난 뒤 다시 시도하세요")
            return

        def work() -> None:
            try:
                callback()
            except Exception as exc:
                self.logger.exception("작업 실패")
                self.ui_queue.put(("error", ("오류", user_facing_browser_error(exc))))

        self.worker = self.executor.submit(work)


def main() -> None:
    app = GatlingPasteApp()
    app.mainloop()
