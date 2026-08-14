from __future__ import annotations

import html
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import AffiliateJob


DRIVE_IMAGE_ROOT_ID = "13ouLpDi-mSJctFuw3iKCg-z4FZLPjKiK"
DRIVE_FOLDER_URL = "https://drive.google.com/drive/folders/{folder_id}"
DRIVE_EMBEDDED_FOLDER_URL = "https://drive.google.com/embeddedfolderview?id={folder_id}"
DRIVE_DOWNLOAD_URL = "https://drive.usercontent.google.com/download"
PLACEHOLDER_PATTERN = re.compile(r"\{([^{}\r\n]+)\}")
SHEET_BRAND_PATTERN = re.compile(r"\(([^()]+)\)")


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
        """Read every public item from Drive's complete embedded folder view."""
        items: list[DriveItem] = []
        seen: set[tuple[str, str, bool]] = set()
        anchors = re.compile(
            r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
            flags=re.IGNORECASE | re.DOTALL,
        )
        for match in anchors.finditer(source):
            href = html.unescape(match.group(1))
            text = re.sub(r"<[^>]+>", "", match.group(2))
            name = re.sub(r"\s+", " ", html.unescape(text)).strip()
            file_match = re.search(r"/file/d/([A-Za-z0-9_-]{20,})/view", href)
            folder_match = re.search(
                r"/drive/folders/([A-Za-z0-9_-]{20,})",
                href,
            )
            docs_match = re.search(
                r"https://docs\.google\.com/[^/]+/d/([A-Za-z0-9_-]{20,})/",
                href,
            )
            if folder_match:
                item = DriveItem(folder_match.group(1), name, True)
            elif file_match:
                item = DriveItem(file_match.group(1), name, False)
            elif docs_match:
                item = DriveItem(docs_match.group(1), name, False)
            else:
                continue
            key = (item.item_id, item.name, item.is_folder)
            if item.name and key not in seen:
                seen.add(key)
                items.append(item)
        return items

    def _fetch_complete_folder(
        self,
        folder_id: str,
        *,
        refresh: bool = False,
    ) -> list[DriveItem]:
        url = DRIVE_EMBEDDED_FOLDER_URL.format(folder_id=folder_id)
        if refresh:
            url += f"&cache={time.time_ns()}"
        request = Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with self.opener(request, timeout=30) as response:
            source = response.read().decode("utf-8", errors="ignore")
        return self.parse_embedded_folder_page(source)

    def _list_folder(self, folder_id: str) -> list[DriveItem]:
        if folder_id in self._folder_cache:
            return self._folder_cache[folder_id]
        try:
            items = self._fetch_complete_folder(folder_id, refresh=True)
        except Exception:
            items = []
        if not items:
            request = Request(
                DRIVE_FOLDER_URL.format(folder_id=folder_id),
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with self.opener(request, timeout=30) as response:
                source = response.read().decode("utf-8", errors="ignore")
            items = self.parse_folder_page(source)
        self._folder_cache[folder_id] = items
        return items

    def _search_file(self, folder_id: str, wanted_name: str) -> DriveItem | None:
        """Retry one exact filename directly against a fresh complete listing."""
        wanted = re.sub(r"\s+", "", wanted_name).casefold()
        try:
            items = self._fetch_complete_folder(folder_id, refresh=True)
        except Exception as exc:
            self.logger.warning("Google Drive 파일명 직접 검색 실패: %s", exc)
            return None
        return next(
            (
                item
                for item in items
                if not item.is_folder and self._file_key(item.name) == wanted
            ),
            None,
        )

    def _folder(self, parent_id: str, name: str) -> DriveItem | None:
        wanted = self._key(name)
        found = next(
            (
                item
                for item in self._list_folder(parent_id)
                if item.is_folder and self._key(item.name) == wanted
            ),
            None,
        )
        if found is not None:
            return found
        try:
            refreshed = self._fetch_complete_folder(
                parent_id,
                refresh=True,
            )
        except Exception:
            return None
        if refreshed:
            self._folder_cache[parent_id] = refreshed
        return next(
            (
                item
                for item in refreshed
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
        data = b""
        content_type = ""
        delays = (2, 5, 10, 20)
        for attempt in range(5):
            try:
                with self.opener(request, timeout=60) as response:
                    data = response.read()
                    content_type = str(
                        response.headers.get("Content-Type") or ""
                    )
                break
            except HTTPError as exc:
                if (
                    exc.code not in {429, 500, 502, 503, 504}
                    or attempt == 4
                ):
                    raise
                delay = delays[attempt]
                self.logger.warning(
                    "Google Drive 사진 다운로드 일시 오류 %s: "
                    "%s초 후 재시도 (%s/5) - %s",
                    exc.code,
                    delay,
                    attempt + 2,
                    item.name,
                )
                time.sleep(delay)
            except (URLError, TimeoutError) as exc:
                if attempt == 4:
                    raise
                delay = delays[attempt]
                self.logger.warning(
                    "Google Drive 사진 네트워크 오류: "
                    "%s초 후 재시도 (%s/5) - %s",
                    delay,
                    attempt + 2,
                    item.name,
                )
                time.sleep(delay)
        if not data or not content_type.casefold().startswith("image/"):
            raise ValueError(f"Google Drive 파일이 이미지가 아닙니다: {item.name}")
        target.write_bytes(data)
        return target

    def resolve(
        self,
        job: AffiliateJob,
        *,
        excluded_file_ids: set[str] | None = None,
    ) -> list[ResolvedImage]:
        markers = placeholders(job.body)
        if job.image_disabled or not markers:
            return []
        excluded_file_ids = excluded_file_ids or set()
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
            normalized_marker = re.sub(r"\s+", "", marker).casefold()
            normalized_keyword = re.sub(
                r"\s+",
                "",
                job.keyword,
            ).casefold()
            keyword_placeholder = marker == "키워드" or (
                bool(normalized_keyword)
                and normalized_marker == normalized_keyword
            )
            shared_keyword_folder = job.brand in {
                "팥순이",
                "장으뜸",
                "뉴더미스",
            } and keyword_placeholder
            keyword_match = job.brand == "팥순이" and (
                marker == "키워드"
                or (
                    bool(normalized_keyword)
                    and normalized_marker == normalized_keyword
                )
            )
            if shared_keyword_folder:
                folder_name = "키워드"
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
            files = [
                item
                for item in self._list_folder(folder.item_id)
                if not item.is_folder and item.item_id not in excluded_file_ids
            ]
            if keyword_match:
                wanted = re.sub(r"\s+", "", job.keyword).casefold()
                candidates = [item for item in files if self._file_key(item.name) == wanted]
                if not candidates:
                    self.logger.info(
                        "행 %s {%s} 전체 목록에 없어 파일명으로 직접 다시 검색합니다",
                        job.row_number,
                        marker,
                    )
                    direct = self._search_file(folder.item_id, job.keyword)
                    candidates = (
                        [direct]
                        if direct and direct.item_id not in excluded_file_ids
                        else []
                    )
            else:
                candidates = files
            if not candidates:
                self.logger.warning(
                    "행 %s {%s}에 사용할 이미지를 찾지 못해 해당 이미지 생략",
                    job.row_number,
                    marker,
                )
                continue
            item = None
            local_path = None
            for candidate in self.rng.sample(candidates, len(candidates)):
                try:
                    local_path = self._download(candidate)
                    item = candidate
                    break
                except Exception as exc:
                    self.logger.warning(
                        "행 %s {%s} 사진 후보 다운로드 실패, "
                        "다른 미사용 사진으로 교체: %s - %s",
                        job.row_number,
                        marker,
                        candidate.name,
                        exc,
                    )
            if item is None or local_path is None:
                self.logger.warning(
                    "행 %s {%s} 모든 사진 후보 다운로드 실패로 생략",
                    job.row_number,
                    marker,
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
