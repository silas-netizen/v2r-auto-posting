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

from .images import GoogleDriveImageResolver, ResolvedImage, job_image_source, placeholders
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
    title, body = _job_title_body(job)
    payload = "\n".join(
        (
            type(job).__name__,
            str(job.row_number),
            str(getattr(job, "keyword", "")),
            str(title),
            str(body),
            str(getattr(job, "cafe", "")),
            str(getattr(job, "brand", "")),
            "image-disabled"
            if getattr(job, "image_disabled", False)
            else "image-enabled",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _job_title_body(job: Any) -> tuple[str, str]:
    source = job_image_source(job)
    title, _, body = source.partition("\n")
    return title, body


def needs_photo_wash(job: Any) -> bool:
    status = getattr(job, "status", None)
    if status is not None and status != JobStatus.PENDING:
        return False
    title, body = _job_title_body(job)
    return bool(
        not getattr(job, "image_disabled", False)
        and getattr(job, "brand", "")
        and placeholders(f"{title}\n{body}")
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
            raise PhotoWashError("포토워셔는 Windows에서만 열 수 있습니다")
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
            "프로그램의 제목·본문·댓글 넣기 버튼을 누르세요"
        )


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
            fields = getattr(type(job), "__dataclass_fields__", {})
            if key in self.failures:
                if "status" in fields:
                    job.status = JobStatus.FAILED
                    job.message = self.failures[key]
                continue
            if (
                key not in self.prepared_images
                and key not in self.failures
                and needs_photo_wash(job)
            ):
                missing.append(job.row_number)
        if missing:
            raise PhotoWashError(
                "원고 확인 후 원고가 변경됐습니다. 다시 원고 확인을 실행하세요: "
                + ", ".join(str(row) for row in missing[:20])
            )

    def require_washed(self) -> dict[str, list[ResolvedImage]]:
        unfinished = [
            key
            for key in self.pending_images
            if key not in self.prepared_images
        ]
        if unfinished or any(
            key in self.failures for key in self.pending_images
        ):
            raise PhotoWashError(
                "사진 세탁을 확인하지 못했습니다. "
                "포토워셔에서 전체 사진 세척 후 다시 넣기를 누르세요"
            )
        return dict(self.prepared_images)


def _resolve_job_photos(
    resolver: Any,
    job: Any,
    excluded_file_ids: set[str],
) -> list[ResolvedImage]:
    resolve = getattr(resolver, "resolve", None)
    if resolve is not None:
        try:
            return list(resolve(job, excluded_file_ids=excluded_file_ids))
        except TypeError:
            return [
                item
                for item in resolve(job)
                if item.file_id not in excluded_file_ids
            ]
    return resolver.resolve_body(
        brand=getattr(job, "brand", ""),
        keyword=getattr(job, "keyword", ""),
        body=job_image_source(job),
        image_disabled=getattr(job, "image_disabled", False),
        row_number=getattr(job, "row_number", 0),
        excluded_file_ids=excluded_file_ids,
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
        markers = placeholders(job_image_source(job))
        resolved = _resolve_job_photos(resolver, job, used_file_ids)
        if len(resolved) != len(markers):
            message = (
                "중복되지 않는 세탁 대상 사진을 모두 찾지 못해 해당 원고를 건너뜁니다"
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
            "포토워셔 main.exe 위치를 입력한 뒤 다시 원고 확인을 실행하세요"
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
        raise PhotoWashError(
            f"포토워셔 또는 배치 폴더 열기 실패: {exc}"
        ) from exc

    for key, items in selected_by_job.items():
        if key in plan.failures:
            continue
        washed_items = [moved[item.file_id] for item in items]
        plan.pending_images[key] = washed_items
    plan.metadata_before = before
    plan.content_hash_before = content_hash_before
    logger.info(
        "포토워셔와 사진 폴더가 열렸습니다. 수동 세탁 후 제목·본문·댓글 넣기를 누르세요"
    )
    return plan
