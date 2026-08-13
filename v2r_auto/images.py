from __future__ import annotations

import html
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import AffiliateJob


DRIVE_IMAGE_ROOT_ID = "13ouLpDi-mSJctFuw3iKCg-z4FZLPjKiK"
DRIVE_FOLDER_URL = "https://drive.google.com/drive/folders/{folder_id}"
DRIVE_EMBEDDED_FOLDER_URL = "https://drive.google.com/embeddedfolderview?id={folder_id}"
DRIVE_DOWNLOAD_URL = "https://drive.usercontent.google.com/download"
PLACEHOLDER_PATTERN = re.compile(r"\{([^{}\r\n]+)\}")
SHEET_BRAND_PATTERN = re.compile(r"\(([^()]+)\)")
EMBEDDED_ENTRY_PATTERN = re.compile(
    r'<div class="flip-entry"[^>]*\bid="entry-([^"]+)"(.*?)(?=<div class="flip-entry"|\Z)',
    flags=re.DOTALL,
)
EMBEDDED_TITLE_PATTERN = re.compile(r'<div class="flip-entry-title">([^<]+)</div>')


@dataclass(frozen=True, slots=True)
class DriveItem:
    item_id: str
    name: str
    is_folder: bool


@dataclass(frozen=True, slots=True)
class ResolvedImage:
    occurrence: int
    marker: str
    file_id: str
    file_name: str
    local_path: Path


def placeholders(body: str) -> list[str]:
    return [match.group(1).strip() for match in PLACEHOLDER_PATTERN.finditer(body)]


def strip_placeholders(body: str) -> str:
    return PLACEHOLDER_PATTERN.sub("", body)


def brand_from_sheet_title(title: str) -> str:
    matches = SHEET_BRAND_PATTERN.findall(html.unescape(title or ""))
    return matches[-1].strip() if matches else ""


def load_sheet_brand(sheet_url: str, timeout: int = 20) -> str:
    request = Request(sheet_url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=timeout) as response:
        source = response.read().decode("utf-8", errors="ignore")
    match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        source,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(r"<title>([^<]+)</title>", source, flags=re.IGNORECASE)
    return brand_from_sheet_title(match.group(1) if match else "")


class GoogleDriveImageResolver:
    """Resolve public Drive folders without requiring Google API credentials."""

    def __init__(
        self,
        download_dir: Path,
        logger,
        *,
        root_folder_id: str = DRIVE_IMAGE_ROOT_ID,
        rng: random.Random | None = None,
        opener: Callable = urlopen,
    ):
        self.download_dir = download_dir
        self.logger = logger
        self.root_folder_id = root_folder_id
        self.rng = rng or random.SystemRandom()
        self.opener = opener
        self._folder_cache: dict[str, list[DriveItem]] = {}

    @staticmethod
    def _key(value: str) -> str:
        return (value or "").strip().casefold()

    @staticmethod
    def _file_key(value: str) -> str:
        stem = Path(value).stem
        return re.sub(r"\s+", "", stem).casefold()

    @staticmethod
    def parse_folder_page(source: str) -> list[DriveItem]:
        items: list[DriveItem] = []
        seen: set[tuple[str, str, bool]] = set()
        pattern = re.compile(
            r'data-id="([A-Za-z0-9_-]+)"(?:(?!data-id=).){0,5000}?'
            r'aria-label="([^"]+)"',
            flags=re.DOTALL,
        )
        for match in pattern.finditer(source):
            item_id = match.group(1)
            label = html.unescape(match.group(2)).strip()
            is_folder = label.endswith(" Shared folder")
            if is_folder:
                name = label.removesuffix(" Shared folder").strip()
            elif " Image Shared" in label:
                name = label.split(" Image Shared", 1)[0].strip()
            else:
                continue
            key = (item_id, name, is_folder)
            if key not in seen:
                seen.add(key)
                items.append(DriveItem(item_id, name, is_folder))
        return items

    @staticmethod
    def parse_embedded_folder_page(source: str) -> list[DriveItem]:
        """Parse the full public folder list, not just the first Drive screen."""
        items: list[DriveItem] = []
        seen: set[tuple[str, str, bool]] = set()
        for match in EMBEDDED_ENTRY_PATTERN.finditer(source):
            item_id = match.group(1)
            chunk = match.group(2)
            title_match = EMBEDDED_TITLE_PATTERN.search(chunk)
            if not title_match:
                continue
            name = html.unescape(title_match.group(1)).strip()
            if not name:
                continue
            is_folder = "/drive/folders/" in chunk or 'aria-label="Folder"' in chunk
            key = (item_id, name, is_folder)
            if key not in seen:
                seen.add(key)
                items.append(DriveItem(item_id, name, is_folder))
        return items

    def _fetch_folder_page(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with self.opener(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="ignore")

    def _list_folder(self, folder_id: str) -> list[DriveItem]:
        if folder_id in self._folder_cache:
            return self._folder_cache[folder_id]
        items: list[DriveItem] = []
        try:
            items = self.parse_embedded_folder_page(
                self._fetch_folder_page(
                    DRIVE_EMBEDDED_FOLDER_URL.format(folder_id=folder_id)
                )
            )
        except Exception as exc:
            self.logger.warning(
                "Google Drive 전체 목록을 읽지 못해 첫 화면만 사용합니다: %s",
                exc,
            )
        if not items:
            items = self.parse_folder_page(
                self._fetch_folder_page(DRIVE_FOLDER_URL.format(folder_id=folder_id))
            )
        self._folder_cache[folder_id] = items
        return items

    def _folder(self, parent_id: str, name: str) -> DriveItem | None:
        wanted = self._key(name)
        return next(
            (
                item
                for item in self._list_folder(parent_id)
                if item.is_folder and self._key(item.name) == wanted
            ),
            None,
        )

    def _download(self, item: DriveItem) -> Path:
        target_dir = self.download_dir / "affiliate-images"
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", item.name).strip(" .")
        target = target_dir / f"{item.item_id}_{safe_name or 'image'}"
        if target.exists() and target.stat().st_size:
            return target
        query = urlencode({"id": item.item_id, "export": "download", "confirm": "t"})
        request = Request(
            f"{DRIVE_DOWNLOAD_URL}?{query}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with self.opener(request, timeout=60) as response:
            data = response.read()
            content_type = str(response.headers.get("Content-Type") or "")
        if not data or not content_type.casefold().startswith("image/"):
            raise ValueError(f"Google Drive 파일이 이미지가 아닙니다: {item.name}")
        target.write_bytes(data)
        return target

    def resolve(self, job: AffiliateJob) -> list[ResolvedImage]:
        markers = placeholders(job.body)
        if job.image_disabled or not markers:
            return []
        brand_folder = self._folder(self.root_folder_id, job.brand)
        if not brand_folder:
            self.logger.warning(
                "행 %s 이미지 브랜드 폴더를 찾지 못해 이미지 없이 진행: %s",
                job.row_number,
                job.brand or "브랜드 미확인",
            )
            return []

        resolved: list[ResolvedImage] = []
        for occurrence, marker in enumerate(markers):
            folder_name = marker
            keyword_match = job.brand == "팥순이" and marker == "키워드"
            if job.brand == "팥순이" and marker == "B/A":
                folder_name = "BA"
            folder = self._folder(brand_folder.item_id, folder_name)
            if not folder:
                self.logger.warning(
                    "행 %s {%s} 폴더를 찾지 못해 해당 이미지 생략",
                    job.row_number,
                    marker,
                )
                continue
            files = [item for item in self._list_folder(folder.item_id) if not item.is_folder]
            if keyword_match:
                wanted = re.sub(r"\s+", "", job.keyword).casefold()
                candidates = [item for item in files if self._file_key(item.name) == wanted]
            else:
                candidates = files
            if not candidates:
                self.logger.warning(
                    "행 %s {%s}에 사용할 이미지를 찾지 못해 해당 이미지 생략",
                    job.row_number,
                    marker,
                )
                continue
            item = self.rng.choice(candidates)
            try:
                local_path = self._download(item)
            except Exception as exc:
                self.logger.warning(
                    "행 %s {%s} 이미지 다운로드 실패로 생략: %s",
                    job.row_number,
                    marker,
                    exc,
                )
                continue
            resolved.append(
                ResolvedImage(
                    occurrence=occurrence,
                    marker=marker,
                    file_id=item.item_id,
                    file_name=item.name,
                    local_path=local_path,
                )
            )
        return resolved
