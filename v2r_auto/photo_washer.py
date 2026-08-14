from __future__ import annotations

import hashlib
import os
import shutil
import string
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from PIL import ExifTags, Image

from .images import GoogleDriveImageResolver, ResolvedImage, placeholders
from .models import JobStatus


class PhotoWashError(RuntimeError):
    pass


CAMERA_EXIF_NAMES = {
    "Make",
    "Model",
    "DateTime",
    "DateTimeOriginal",
    "DateTimeDigitized",
    "ExposureTime",
    "FNumber",
    "ISOSpeedRatings",
    "FocalLength",
    "LensMake",
    "LensModel",
    "BodySerialNumber",
    "LensSerialNumber",
}


def camera_metadata(path: Path) -> dict[str, str]:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            exif = image.getexif()
            result: dict[str, str] = {}
            for tag_id, value in exif.items():
                name = ExifTags.TAGS.get(tag_id, str(tag_id))
                if name in CAMERA_EXIF_NAMES:
                    result[name] = str(value)
            nested = exif.get_ifd(ExifTags.IFD.Exif)
            for tag_id, value in nested.items():
                name = ExifTags.TAGS.get(tag_id, str(tag_id))
                if name in CAMERA_EXIF_NAMES:
                    result[name] = str(value)
            return result
    except Exception as exc:
        raise PhotoWashError(
            f"사진 카메라 정보를 읽지 못했습니다: {path.name} - {exc}"
        ) from exc


def camera_metadata_changed(
    before: dict[str, str],
    after: dict[str, str],
) -> bool:
    return before != after and bool(after)


def photo_job_key(job: Any) -> str:
    payload = "\n".join(
        (
            type(job).__name__,
            str(job.row_number),
            str(getattr(job, "keyword", "")),
            str(job.title),
            str(job.body),
            str(job.cafe),
            str(job.board),
            str(job.account),
            str(getattr(job, "brand", "")),
            "image-disabled"
            if getattr(job, "image_disabled", False)
            else "image-enabled",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def needs_photo_wash(job: Any) -> bool:
    return bool(
        job.status == JobStatus.PENDING
        and not getattr(job, "image_disabled", False)
        and getattr(job, "brand", "")
        and placeholders(job.body)
    )


def find_photo_washer_executable() -> Path | None:
    configured = os.environ.get("V2R_PHOTOWASHER_PATH", "").strip()
    if configured and Path(configured).is_file():
        return Path(configured)
    if os.name != "nt":
        return None
    roots: list[Path] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:/")
        if drive.exists():
            roots.append(drive)
    roots.extend(
        [
            Path.home() / "My Drive",
            Path.home() / "내 드라이브",
            Path.home() / "Google Drive",
        ]
    )
    relative_paths = (
        Path("image") / "photowasher2.1" / "main.exe",
        Path("My Drive") / "image" / "photowasher2.1" / "main.exe",
        Path("내 드라이브") / "image" / "photowasher2.1" / "main.exe",
        Path("photowasher2.1") / "main.exe",
    )
    seen: set[Path] = set()
    for root in roots:
        for relative in relative_paths:
            candidate = root / relative
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                return candidate
    return None


def photo_washer_settings_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path.home() / ".local" / "share"
    return root / "V2RPhotoWasher" / "main-exe-path.txt"


def load_saved_photo_washer_executable(
    settings_path: Path | None = None,
) -> Path | None:
    path = settings_path or photo_washer_settings_path()
    try:
        saved = Path(path.read_text(encoding="utf-8").strip())
    except OSError:
        return None
    return saved if saved.is_file() else None


def save_photo_washer_executable(
    executable: Path,
    settings_path: Path | None = None,
) -> None:
    executable = executable.resolve()
    if not executable.is_file() or executable.name.casefold() != "main.exe":
        raise PhotoWashError(
            f"올바른 포토워셔 main.exe가 아닙니다: {executable}"
        )
    path = settings_path or photo_washer_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(str(executable), encoding="utf-8")
    temporary.replace(path)


def preferred_photo_washer_executable(
    settings_path: Path | None = None,
) -> Path | None:
    return (
        load_saved_photo_washer_executable(settings_path)
        or find_photo_washer_executable()
    )


class PhotoWasherController:
    def __init__(self, executable: Path, logger, timeout_seconds: int = 300):
        self.executable = executable
        self.logger = logger
        self.timeout_seconds = timeout_seconds

    def _validate_installation(self) -> None:
        if os.name != "nt":
            raise PhotoWashError("포토워셔 자동화는 Windows에서만 실행할 수 있습니다")
        if not self.executable.is_file():
            raise PhotoWashError(
                f"포토워셔 main.exe를 찾지 못했습니다: {self.executable}"
            )
        for companion in ("manufacturers.txt", "models.txt"):
            if not (self.executable.parent / companion).is_file():
                raise PhotoWashError(
                    f"포토워셔 필수 파일이 없습니다: {companion}"
                )

    @staticmethod
    def _wait_for_window_text(window, text: str, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                texts = [
                    item.window_text()
                    for item in window.descendants()
                    if item.window_text()
                ]
                if any(text in value for value in texts):
                    return
            except Exception:
                pass
            time.sleep(0.25)
        raise PhotoWashError(f"포토워셔 화면 문구를 찾지 못했습니다: {text}")

    def wash(self, batch_dir: Path, image_paths: list[Path]) -> None:
        self._validate_installation()
        if not image_paths:
            return
        try:
            from pywinauto import Desktop, mouse
            from pywinauto.application import Application
        except Exception as exc:
            raise PhotoWashError(
                "Windows 화면 자동화 모듈을 시작하지 못했습니다"
            ) from exc

        app = None
        explorer_process = None
        explorer_window = None
        try:
            app = Application(backend="uia").start(
                f'"{self.executable}"',
                work_dir=str(self.executable.parent),
            )
            window = Desktop(backend="uia").window(
                title_re=r".*포토 워셔 v2\.0.*"
            )
            window.wait("visible enabled ready", timeout=30)

            explorer_process = subprocess.Popen(
                [
                    "explorer.exe",
                    f"/select,{batch_dir}",
                ]
            )
            desktop = Desktop(backend="uia")
            folder_item = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                for candidate in reversed(
                    desktop.windows(class_name="CabinetWClass")
                ):
                    for control_type in (
                        "ListItem",
                        "DataItem",
                        "TreeItem",
                    ):
                        try:
                            items = candidate.descendants(
                                control_type=control_type
                            )
                        except Exception:
                            items = []
                        folder_item = next(
                            (
                                item
                                for item in items
                                if item.window_text() == batch_dir.name
                            ),
                            None,
                        )
                        if folder_item is not None:
                            explorer_window = candidate
                            break
                    if folder_item is None:
                        try:
                            for item in candidate.descendants():
                                if (
                                    item.window_text() == batch_dir.name
                                    and item.rectangle().width() > 0
                                    and item.rectangle().height() > 0
                                ):
                                    explorer_window = candidate
                                    folder_item = item
                                    break
                        except Exception:
                            pass
                    if folder_item is not None:
                        break
                if folder_item is not None:
                    break
                time.sleep(0.25)
            if folder_item is None or explorer_window is None:
                raise PhotoWashError(
                    "포토워셔로 드래그할 배치 폴더를 탐색기에서 찾지 못했습니다"
                )
            folder_item.wait("visible enabled", timeout=10)
            folder_item.click_input()
            drop_text = window.child_window(
                title="파일 또는 폴더를 여기에 드래그하세요",
            )
            drop_text.wait("visible", timeout=30)
            source_rect = folder_item.rectangle()
            target_rect = drop_text.rectangle()
            source = (
                (source_rect.left + source_rect.right) // 2,
                (source_rect.top + source_rect.bottom) // 2,
            )
            target = (
                (target_rect.left + target_rect.right) // 2,
                (target_rect.top + target_rect.bottom) // 2,
            )
            mouse.move(coords=source)
            mouse.press(button="left", coords=source)
            mouse.move(coords=target, duration=1.5)
            mouse.release(button="left", coords=target)

            self._wait_for_window_text(
                window,
                f"{len(image_paths)}개 로딩이 완료되었습니다",
                60,
            )
            window.child_window(
                title="전체 사진 세척",
                control_type="Button",
            ).click_input()
            completed = Desktop(backend="uia").window(title="완료")
            completed.wait("visible ready", timeout=self.timeout_seconds)
            completed.child_window(
                title_re=r"(?i)^ok$",
                control_type="Button",
            ).click_input()
            self.logger.info(
                "포토워셔 전체 사진 세척 완료: %s개",
                len(image_paths),
            )
            try:
                window.close()
            except Exception:
                pass
        except Exception as exc:
            raise PhotoWashError(f"포토워셔 화면 자동화 실패: {exc}") from exc
        finally:
            if app is not None:
                try:
                    app.kill()
                except Exception:
                    pass
            if explorer_process is not None:
                try:
                    if explorer_window is not None:
                        explorer_window.close()
                except Exception:
                    pass


@dataclass(slots=True)
class PhotoWashPlan:
    prepared_images: dict[str, list[ResolvedImage]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    selected_count: int = 0
    washed_count: int = 0

    def apply(self, jobs: Iterable[Any]) -> None:
        missing: list[int] = []
        for job in jobs:
            key = photo_job_key(job)
            if key in self.failures:
                job.status = JobStatus.FAILED
                job.message = self.failures[key]
                job.photo_wash_prepared = True
                job.prepared_images = []
                continue
            if key not in self.prepared_images:
                missing.append(job.row_number)
                continue
            job.prepared_images = list(self.prepared_images[key])
            job.photo_wash_prepared = True
        if missing:
            raise PhotoWashError(
                "데이터 확인 후 원고가 변경됐습니다. 다시 데이터 확인을 실행하세요: "
                + ", ".join(str(row) for row in missing[:20])
            )


def prepare_photo_wash_plan(
    jobs: list[Any],
    *,
    download_dir: Path,
    executable: Path | None,
    logger,
    resolver: GoogleDriveImageResolver | None = None,
    controller: PhotoWasherController | None = None,
) -> PhotoWashPlan:
    plan = PhotoWashPlan()
    resolver = resolver or GoogleDriveImageResolver(download_dir, logger)
    used_file_ids: set[str] = set()
    selected_by_job: dict[str, list[ResolvedImage]] = {}
    owner_by_file_id: dict[str, str] = {}
    for job in jobs:
        key = photo_job_key(job)
        if not needs_photo_wash(job):
            plan.prepared_images[key] = []
            continue
        markers = placeholders(job.body)
        resolved = resolver.resolve(
            job,
            excluded_file_ids=used_file_ids,
        )
        if len(resolved) != len(markers):
            plan.failures[key] = (
                "중복되지 않는 세탁 대상 사진을 모두 찾지 못해 발행하지 않습니다"
            )
            continue
        selected_by_job[key] = resolved
        for item in resolved:
            used_file_ids.add(item.file_id)
            owner_by_file_id[item.file_id] = key

    all_selected = [
        item
        for items in selected_by_job.values()
        for item in items
    ]
    plan.selected_count = len(all_selected)
    if not all_selected:
        for key, items in selected_by_job.items():
            plan.prepared_images[key] = list(items)
        return plan
    if executable is None:
        raise PhotoWashError(
            "포토워셔 main.exe 위치를 입력한 뒤 다시 데이터 확인을 실행하세요"
        )

    batch_dir = (
        download_dir
        / "photo-washer-batches"
        / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )
    batch_dir.mkdir(parents=True, exist_ok=False)
    moved: dict[str, ResolvedImage] = {}
    before: dict[str, dict[str, str]] = {}
    for item in all_selected:
        destination = batch_dir / item.local_path.name
        if destination.exists():
            raise PhotoWashError(
                f"포토워셔 배치 파일명이 중복됩니다: {destination.name}"
            )
        shutil.move(str(item.local_path), destination)
        moved[item.file_id] = ResolvedImage(
            occurrence=item.occurrence,
            marker=item.marker,
            file_id=item.file_id,
            file_name=item.file_name,
            local_path=destination,
        )
        before[item.file_id] = camera_metadata(destination)

    controller = controller or PhotoWasherController(executable, logger)
    try:
        controller.wash(
            batch_dir,
            [moved[item.file_id].local_path for item in all_selected],
        )
    except Exception as exc:
        message = f"포토워셔 세탁 실패로 발행하지 않습니다: {exc}"
        logger.error(message)
        for key in selected_by_job:
            plan.failures[key] = message
        return plan

    successful_ids: set[str] = set()
    for item in all_selected:
        washed = moved[item.file_id]
        try:
            after = camera_metadata(washed.local_path)
        except PhotoWashError as exc:
            plan.failures[owner_by_file_id[item.file_id]] = str(exc)
            continue
        if not camera_metadata_changed(before[item.file_id], after):
            message = (
                f"카메라 정보가 변경되지 않아 세탁 실패로 처리합니다: "
                f"{washed.local_path.name}"
            )
            logger.error(message)
            plan.failures[owner_by_file_id[item.file_id]] = message
            continue
        successful_ids.add(item.file_id)

    for key, items in selected_by_job.items():
        if key in plan.failures:
            continue
        washed_items = [moved[item.file_id] for item in items]
        if not all(item.file_id in successful_ids for item in washed_items):
            plan.failures[key] = "사진 세탁 확인 실패로 발행하지 않습니다"
            continue
        plan.prepared_images[key] = washed_items
        plan.washed_count += len(washed_items)
    return plan
