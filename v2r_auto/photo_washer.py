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

from PIL import ExifTags, Image, ImageChops

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


def file_content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_photo_washer_file(source: Path, destination: Path) -> Path:
    """Move JPEG files and convert other supported images to washer-safe JPEG."""
    if source.suffix.casefold() in {".jpg", ".jpeg"}:
        shutil.move(str(source), destination)
        return destination
    converted = destination.with_suffix(".jpg")
    if converted.exists():
        raise PhotoWashError(
            f"포토워셔 변환 파일명이 중복됩니다: {converted.name}"
        )
    try:
        with Image.open(source) as image:
            image.seek(0)
            if image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                background.alpha_composite(rgba)
                output = background.convert("RGB")
            else:
                output = image.convert("RGB")
            output.save(converted, format="JPEG", quality=95)
    except Exception as exc:
        raise PhotoWashError(
            f"포토워셔용 JPG 변환 실패: {source.name} - {exc}"
        ) from exc
    source.unlink(missing_ok=True)
    return converted


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

    def open_for_manual_wash(
        self,
        batch_dir: Path,
        image_paths: list[Path],
    ) -> None:
        self._validate_installation()
        if not image_paths:
            return
        if not batch_dir.is_dir():
            raise PhotoWashError(
                f"포토워셔 배치 폴더가 없습니다: {batch_dir}"
            )
        try:
            subprocess.Popen(
                [str(self.executable)],
                cwd=str(self.executable.parent),
            )
            time.sleep(1)
            subprocess.Popen(["explorer.exe", str(batch_dir)])
        except OSError as exc:
            raise PhotoWashError(
                f"포토워셔 또는 배치 폴더를 열지 못했습니다: {exc}"
            ) from exc
        self.logger.info(
            "포토워셔와 배치 폴더를 열었습니다: 사진 %s개 / %s",
            len(image_paths),
            batch_dir,
        )
        self.logger.info(
            "사진을 포토워셔로 드래그해 전체 사진 세척 후 "
            "프로그램의 발행 시작 버튼을 누르세요"
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
            from pywinauto.keyboard import send_keys
            from win32api import GetSystemMetrics
            from win32con import HWND_TOP, SWP_SHOWWINDOW
            from win32gui import SetForegroundWindow, SetWindowPos
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

            self.logger.info(
                "포토워셔 배치 폴더 열기: %s",
                batch_dir,
            )
            explorer_process = subprocess.Popen(
                ["explorer.exe", str(batch_dir)]
            )
            desktop = Desktop(backend="uia")
            image_item = None
            image_names = {path.name for path in image_paths}
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                for candidate in reversed(
                    desktop.windows(class_name="CabinetWClass")
                ):
                    matching_items = []
                    for control_type in (
                        "ListItem",
                        "DataItem",
                    ):
                        try:
                            items = candidate.descendants(
                                control_type=control_type
                            )
                        except Exception:
                            items = []
                        matching_items.extend(
                            item
                            for item in items
                            if item.window_text() in image_names
                        )
                    if not matching_items:
                        try:
                            for item in candidate.descendants():
                                if (
                                    item.window_text() in image_names
                                    and item.rectangle().width() > 0
                                    and item.rectangle().height() > 0
                                ):
                                    matching_items.append(item)
                        except Exception:
                            pass
                    if matching_items:
                        explorer_window = candidate
                        image_item = matching_items[0]
                        break
                if image_item is not None:
                    break
                time.sleep(0.25)
            if image_item is None or explorer_window is None:
                raise PhotoWashError(
                    "포토워셔 배치 폴더의 사진을 탐색기에서 찾지 못했습니다"
                )
            main_window = window.wrapper_object()
            screen_width = GetSystemMetrics(0)
            screen_height = GetSystemMetrics(1)
            main_rect = main_window.rectangle()
            main_width = min(
                main_rect.width(),
                max(500, screen_width // 2 - 30),
            )
            main_height = min(
                main_rect.height(),
                max(500, screen_height - 80),
            )
            SetWindowPos(
                main_window.handle,
                HWND_TOP,
                screen_width - main_width - 10,
                10,
                main_width,
                main_height,
                SWP_SHOWWINDOW,
            )
            SetWindowPos(
                explorer_window.handle,
                HWND_TOP,
                10,
                10,
                max(500, screen_width // 2 - 30),
                max(500, screen_height - 80),
                SWP_SHOWWINDOW,
            )
            time.sleep(1)
            if not image_item.is_visible() or not image_item.is_enabled():
                raise PhotoWashError(
                    "포토워셔로 드래그할 사진이 화면에서 활성화되지 않았습니다"
                )
            SetForegroundWindow(explorer_window.handle)
            explorer_window.set_focus()
            send_keys("^a")
            time.sleep(0.5)
            drop_text = window.child_window(
                title="파일 또는 폴더를 여기에 드래그하세요",
            )
            source_rect = image_item.rectangle()
            source = (
                (source_rect.left + source_rect.right) // 2,
                (source_rect.top + source_rect.bottom) // 2,
            )
            if drop_text.exists(timeout=2):
                target_rect = drop_text.rectangle()
                target = (
                    (target_rect.left + target_rect.right) // 2,
                    (target_rect.top + target_rect.bottom) // 2,
                )
            else:
                window_rect = main_window.rectangle()
                target = (
                    (window_rect.left + window_rect.right) // 2,
                    window_rect.top
                    + int(window_rect.height() * 0.38),
                )
                self.logger.info(
                    "포토워셔 드롭 문구 대신 창 내부 상대 위치를 사용합니다"
                )
            self.logger.info(
                "포토워셔 드래그 준비: 사진 %s개 / 탐색기 항목 %s / "
                "시작 %s / 대상 %s",
                len(image_paths),
                type(image_item).__name__,
                source,
                target,
            )
            before_drop = main_window.capture_as_image().convert("RGB")
            mouse.move(coords=source)
            mouse.press(button="left", coords=source)
            for step in range(1, 16):
                point = (
                    source[0]
                    + (target[0] - source[0]) * step // 15,
                    source[1]
                    + (target[1] - source[1]) * step // 15,
                )
                mouse.move(coords=point)
                time.sleep(0.1)
            mouse.release(button="left", coords=target)
            time.sleep(2)
            after_drop = main_window.capture_as_image().convert("RGB")
            debug_dir = os.environ.get("V2R_PHOTOWASHER_DEBUG_DIR", "").strip()
            if debug_dir:
                debug_path = Path(debug_dir)
                debug_path.mkdir(parents=True, exist_ok=True)
                before_drop.save(debug_path / "before-drop.png")
                after_drop.save(debug_path / "after-drop.png")
            if before_drop.size == after_drop.size:
                difference = ImageChops.difference(
                    before_drop,
                    after_drop,
                ).convert("L")
                histogram = difference.histogram()
                changed_pixels = sum(histogram[1:])
                total_pixels = before_drop.width * before_drop.height
                changed_ratio = (
                    changed_pixels / total_pixels
                    if total_pixels
                    else 0.0
                )
                self.logger.info(
                    "포토워셔 드래그 후 화면 변화율: %.4f",
                    changed_ratio,
                )
                if changed_ratio < 0.003:
                    raise PhotoWashError(
                        "배치 폴더가 포토워셔에 드롭되지 않았습니다"
                    )

            try:
                self._wait_for_window_text(
                    window,
                    f"{len(image_paths)}개 로딩이 완료되었습니다",
                    20,
                )
            except PhotoWashError:
                self.logger.warning(
                    "포토워셔 로딩 문구를 읽지 못해 화면 상태를 기준으로 계속합니다"
                )
                time.sleep(2)
            wash_button = window.child_window(
                title="전체 사진 세척",
                control_type="Button",
            )
            before_wash_stats = {
                path: (
                    path.stat().st_mtime_ns,
                    path.stat().st_size,
                )
                for path in image_paths
            }
            if wash_button.exists(timeout=2):
                wash_button.click_input()
            else:
                window_rect = main_window.rectangle()
                SetForegroundWindow(main_window.handle)
                time.sleep(0.5)
                button_point = (
                    window_rect.left
                    + int(window_rect.width() * 0.44),
                    window_rect.top
                    + int(window_rect.height() * 0.88),
                )
                mouse.click(
                    button="left",
                    coords=button_point,
                )
                time.sleep(0.25)
                mouse.click(
                    button="left",
                    coords=button_point,
                )
                self.logger.info(
                    "전체 사진 세척 버튼의 창 내부 상대 위치를 사용합니다: %s",
                    button_point,
                )
            completed = Desktop(backend="uia").window(title="완료")
            deadline = time.monotonic() + self.timeout_seconds
            popup_found = False
            files_changed = False
            while time.monotonic() < deadline:
                popup_found = completed.exists(timeout=0)
                files_changed = all(
                    path.exists()
                    and (
                        path.stat().st_mtime_ns,
                        path.stat().st_size,
                    )
                    != before_wash_stats[path]
                    for path in image_paths
                )
                if popup_found or files_changed:
                    break
                time.sleep(0.25)
            if not popup_found and not files_changed:
                raise PhotoWashError(
                    "전체 사진 세척 실행 후 파일 변경과 완료 팝업을 확인하지 못했습니다"
                )
            if popup_found:
                completed.child_window(
                    title_re=r"(?i)^ok$",
                    control_type="Button",
                ).click_input()
            else:
                self.logger.warning(
                    "완료 팝업을 읽지 못했지만 전체 사진 파일 덮어쓰기를 확인했습니다"
                )
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
    pending_images: dict[str, list[ResolvedImage]] = field(default_factory=dict)
    metadata_before: dict[str, dict[str, str]] = field(default_factory=dict)
    content_hash_before: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    selected_count: int = 0
    washed_count: int = 0

    def apply(self, jobs: Iterable[Any], logger=None) -> None:
        self.washed_count = 0
        for key, images in self.pending_images.items():
            self.failures.pop(key, None)
            self.prepared_images.pop(key, None)
            successful = True
            for image in images:
                try:
                    after = camera_metadata(image.local_path)
                except PhotoWashError as exc:
                    self.failures[key] = str(exc)
                    successful = False
                    break
                before = self.metadata_before.get(image.file_id, {})
                before_hash = self.content_hash_before.get(image.file_id, "")
                after_hash = file_content_hash(image.local_path)
                if (
                    not camera_metadata_changed(before, after)
                    and (not before_hash or before_hash == after_hash)
                ):
                    self.failures[key] = (
                        "카메라 정보와 파일 내용이 변경되지 않아 세탁하지 않은 사진으로 "
                        f"판단합니다: {image.local_path.name}"
                    )
                    successful = False
                    break
            if successful:
                self.prepared_images[key] = list(images)
                self.washed_count += len(images)
                if logger:
                    logger.info(
                        "수동 포토워셔 세탁 확인 성공: %s",
                        ", ".join(image.local_path.name for image in images),
                    )
            elif logger:
                logger.error(self.failures[key])

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
            message = (
                "중복되지 않는 세탁 대상 사진을 모두 찾지 못해 발행하지 않습니다"
            )
            logger.error("행 %s %s", job.row_number, message)
            plan.failures[key] = message
            continue
        selected_by_job[key] = resolved
        logger.info(
            "행 %s 세탁 사진 선택 완료: %s",
            job.row_number,
            ", ".join(item.file_name for item in resolved),
        )
        for item in resolved:
            used_file_ids.add(item.file_id)

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
    content_hash_before: dict[str, str] = {}
    for item in all_selected:
        destination = batch_dir / item.local_path.name
        if destination.exists():
            raise PhotoWashError(
                f"포토워셔 배치 파일명이 중복됩니다: {destination.name}"
            )
        destination = prepare_photo_washer_file(
            item.local_path,
            destination,
        )
        if destination.suffix.casefold() == ".jpg" and (
            item.local_path.suffix.casefold() not in {".jpg", ".jpeg"}
        ):
            logger.info(
                "포토워셔 호환을 위해 JPG로 변환했습니다: %s → %s",
                item.local_path.name,
                destination.name,
            )
        moved[item.file_id] = ResolvedImage(
            occurrence=item.occurrence,
            marker=item.marker,
            file_id=item.file_id,
            file_name=destination.name,
            local_path=destination,
        )
        before[item.file_id] = camera_metadata(destination)
        content_hash_before[item.file_id] = file_content_hash(destination)

    controller = controller or PhotoWasherController(executable, logger)
    logger.info(
        "수동 포토워셔 준비 시작: 원고 %s건 / 사진 %s개",
        len(selected_by_job),
        len(all_selected),
    )
    try:
        controller.open_for_manual_wash(
            batch_dir,
            [moved[item.file_id].local_path for item in all_selected],
        )
    except Exception as exc:
        message = f"포토워셔 또는 배치 폴더 열기 실패: {exc}"
        logger.error(message)
        for key in selected_by_job:
            plan.failures[key] = message
        return plan

    for key, items in selected_by_job.items():
        if key in plan.failures:
            continue
        washed_items = [moved[item.file_id] for item in items]
        plan.pending_images[key] = washed_items
    plan.metadata_before = before
    plan.content_hash_before = content_hash_before
    logger.info(
        "포토워셔와 사진 폴더가 열렸습니다. 수동 세탁 후 발행 시작을 누르세요"
    )
    return plan
