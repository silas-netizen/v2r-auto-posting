from __future__ import annotations

import csv
import random
from pathlib import Path

from .content import ContentFormatError, parse_article
from .models import AffiliateJob, DailyPost, JobStatus


class DailyPostSheetError(ValueError):
    pass


BODY_HEADERS = ("내용", "본문")
CAFE_HEADERS = ("카페", "카페명")


def _clean(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _header_names(path: str | Path) -> list[str]:
    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return [header.strip() for header in (reader.fieldnames or []) if header]


def _find_header(headers: list[str], candidates: tuple[str, ...]) -> str:
    wanted = set(candidates)
    return next((header for header in headers if header in wanted), "")


def looks_like_brand_sheet(path: str | Path) -> bool:
    headers = set(_header_names(path))
    return "키워드" in headers and ("원고유형" in headers or "본문" in headers)


def looks_like_daily_sheet(path: str | Path) -> bool:
    if looks_like_brand_sheet(path):
        return False
    headers = _header_names(path)
    if "각색본문" in headers or "각색제목" in headers:
        return False
    return bool(_find_header(headers, BODY_HEADERS) and _find_header(headers, CAFE_HEADERS))


def load_daily_posts(path: str | Path) -> list[DailyPost]:
    """Read daily posts from 번호/제목/내용/카페 columns."""
    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        raw_headers = [header for header in (reader.fieldnames or []) if header]
        original = {header.strip(): header for header in raw_headers}
        headers = list(original)
        if "키워드" in original and ("원고유형" in original or "본문" in original):
            raise DailyPostSheetError(
                "일상 글 시트가 아니라 브랜드 원고 시트를 읽었습니다. "
                "원고 확인을 다시 눌러 주세요."
            )
        if "각색본문" in original or "각색제목" in original:
            raise DailyPostSheetError(
                "같은 파일의 다른 탭을 읽었습니다. "
                "씨씨앙·양평맘 일상 글이 있는 탭이 필요합니다."
            )
        body_header = _find_header(headers, BODY_HEADERS)
        cafe_header = _find_header(headers, CAFE_HEADERS)
        if not body_header or not cafe_header:
            found = ", ".join(headers) if headers else "없음"
            raise DailyPostSheetError(
                "일상 글 시트에서 '내용'과 '카페' 열을 찾지 못했습니다. "
                f"지금 파일의 열: {found}"
            )

        posts: list[DailyPost] = []
        for row_number, row in enumerate(reader, start=2):
            cafe = _clean(row.get(original[cafe_header]))
            source = _clean(row.get(original[body_header]))
            if cafe not in {"씨씨앙", "양평맘"} or not source:
                continue
            try:
                article = parse_article("일상", source)
            except ContentFormatError:
                # Broken template rows are ignored; usable templates must not be blocked.
                continue
            posts.append(
                DailyPost(
                    row_number=row_number,
                    cafe=cafe,
                    title=article.title,
                    body=article.body,
                )
            )
    if not posts:
        raise DailyPostSheetError("사용 가능한 씨씨앙·양평맘 일상 글이 없습니다")
    return posts


def assign_daily_posts(
    jobs: list[AffiliateJob],
    daily_posts: list[DailyPost],
    rng: random.Random | None = None,
) -> None:
    """Randomly assign unique same-cafe daily posts for this run."""
    randomizer = rng or random.SystemRandom()
    for cafe in ("씨씨앙", "양평맘"):
        cafe_jobs = [
            job
            for job in jobs
            if job.cafe == cafe
            and not job.completion_url
            and job.status == JobStatus.PENDING
        ]
        candidates = [post for post in daily_posts if post.cafe == cafe]
        if len(candidates) < len(cafe_jobs):
            raise DailyPostSheetError(
                f"{cafe} 일상 글이 부족합니다: 필요 {len(cafe_jobs)}개, "
                f"사용 가능 {len(candidates)}개"
            )
        for job, post in zip(cafe_jobs, randomizer.sample(candidates, len(cafe_jobs))):
            job.daily_post = post
