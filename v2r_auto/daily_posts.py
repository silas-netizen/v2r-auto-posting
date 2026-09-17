from __future__ import annotations

import csv
import random
from pathlib import Path

from .content import ContentFormatError, parse_article
from .models import AffiliateJob, DailyPost, JobStatus


DAILY_POST_SOURCE_CAFES = {
    "씨씨앙": "씨씨앙",
    "양평맘": "양평맘",
    "쌍둥이맘 모여라": "양평맘",
}


class DailyPostSheetError(ValueError):
    pass


def _clean(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def load_daily_posts(path: str | Path) -> list[DailyPost]:
    """Read daily posts from 번호/제목/내용/카페 columns."""
    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"내용", "카페"}
        headers = set(reader.fieldnames or [])
        if missing := required - headers:
            raise DailyPostSheetError(
                "일상 글 시트 열을 찾지 못했습니다: " + ", ".join(sorted(missing))
            )

        posts: list[DailyPost] = []
        for row_number, row in enumerate(reader, start=2):
            cafe = _clean(row.get("카페"))
            source = _clean(row.get("내용"))
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
    used_posts: set[tuple[int, str, str, str]] = {
        (
            job.daily_post.row_number,
            job.daily_post.cafe,
            job.daily_post.title,
            job.daily_post.body,
        )
        for job in jobs
        if job.daily_post is not None
    }
    for cafe, source_cafe in DAILY_POST_SOURCE_CAFES.items():
        cafe_jobs = [
            job
            for job in jobs
            if job.cafe == cafe
            and not job.completion_url
            and job.status == JobStatus.PENDING
            and job.daily_post is None
        ]
        candidates = [
            post
            for post in daily_posts
            if post.cafe == source_cafe
            and (post.row_number, post.cafe, post.title, post.body) not in used_posts
        ]
        if len(candidates) < len(cafe_jobs):
            raise DailyPostSheetError(
                f"{cafe}용 {source_cafe} 일상 글이 부족합니다: "
                f"필요 {len(cafe_jobs)}개, "
                f"사용 가능 {len(candidates)}개"
            )
        selected = randomizer.sample(candidates, len(cafe_jobs))
        for job, post in zip(cafe_jobs, selected):
            job.daily_post = post
            used_posts.add((post.row_number, post.cafe, post.title, post.body))
