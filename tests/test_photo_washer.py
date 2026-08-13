from __future__ import annotations

import copy
import logging
from pathlib import Path

from PIL import Image

from v2r_auto.content import ParsedArticle
from v2r_auto.images import ResolvedImage
from v2r_auto.models import ImmediateJob, JobStatus
from v2r_auto.photo_washer import (
    camera_metadata,
    camera_metadata_changed,
    photo_job_key,
    prepare_photo_wash_plan,
)


def write_jpeg(path: Path, make: str, model: str) -> None:
    image = Image.new("RGB", (20, 20), color="white")
    exif = Image.Exif()
    exif[271] = make
    exif[272] = model
    exif[36867] = "2026:08:13 12:00:00"
    image.save(path, "JPEG", exif=exif)


def make_job(row_number: int) -> ImmediateJob:
    return ImmediateJob(
        row_number=row_number,
        article=ParsedArticle(
            title=f"제목 {row_number}",
            body="본문\n{B/A}",
            keyword="키워드",
            tag="키워드",
            comments=[],
        ),
        cafe="러브인썸",
        board="뷰티미용",
        brand="팥순이",
        source_kind="brand",
        use_comment_ai=False,
    )


class FakeResolver:
    def __init__(self, images: dict[int, list[ResolvedImage]]):
        self.images = images
        self.exclusions: list[set[str]] = []

    def resolve(self, job, *, excluded_file_ids=None):
        excluded = set(excluded_file_ids or set())
        self.exclusions.append(excluded)
        return [
            item
            for item in self.images[job.row_number]
            if item.file_id not in excluded
        ]


class WashingController:
    def __init__(self):
        self.calls = 0
        self.paths: list[Path] = []

    def wash(self, batch_dir: Path, image_paths: list[Path]) -> None:
        self.calls += 1
        self.paths = list(image_paths)
        assert all(path.parent == batch_dir for path in image_paths)
        for index, path in enumerate(image_paths, start=1):
            write_jpeg(path, f"WashedMake{index}", f"WashedModel{index}")


def test_camera_metadata_uses_windows_details_camera_fields(tmp_path: Path) -> None:
    path = tmp_path / "photo.jpg"
    write_jpeg(path, "BeforeMake", "BeforeModel")
    before = camera_metadata(path)
    write_jpeg(path, "AfterMake", "AfterModel")
    after = camera_metadata(path)

    assert before["Make"] == "BeforeMake"
    assert before["Model"] == "BeforeModel"
    assert after["Make"] == "AfterMake"
    assert camera_metadata_changed(before, after)
    assert not camera_metadata_changed(after, dict(after))


def test_batch_selects_unique_images_and_washes_once(tmp_path: Path) -> None:
    first_path = tmp_path / "first.jpg"
    second_path = tmp_path / "second.jpg"
    write_jpeg(first_path, "Original", "One")
    write_jpeg(second_path, "Original", "Two")
    jobs = [make_job(2), make_job(3)]
    resolver = FakeResolver(
        {
            2: [ResolvedImage(0, "B/A", "first-id", "first.jpg", first_path)],
            3: [ResolvedImage(0, "B/A", "second-id", "second.jpg", second_path)],
        }
    )
    controller = WashingController()

    plan = prepare_photo_wash_plan(
        jobs,
        download_dir=tmp_path / "downloads",
        executable=tmp_path / "main.exe",
        logger=logging.getLogger("test"),
        resolver=resolver,
        controller=controller,
    )

    assert plan.selected_count == 2
    assert plan.washed_count == 2
    assert not plan.failures
    assert controller.calls == 1
    assert len({path.parent for path in controller.paths}) == 1
    assert resolver.exclusions == [set(), {"first-id"}]

    reloaded = [copy.deepcopy(job) for job in jobs]
    plan.apply(reloaded)
    assert all(job.photo_wash_prepared for job in reloaded)
    assert all(len(job.prepared_images) == 1 for job in reloaded)
    assert {
        job.prepared_images[0].file_id for job in reloaded
    } == {"first-id", "second-id"}


def test_duplicate_photo_blocks_only_later_job(tmp_path: Path) -> None:
    path = tmp_path / "only.jpg"
    write_jpeg(path, "Original", "Only")
    shared = ResolvedImage(0, "B/A", "same-id", "only.jpg", path)
    jobs = [make_job(2), make_job(3)]
    resolver = FakeResolver({2: [shared], 3: [shared]})
    controller = WashingController()

    plan = prepare_photo_wash_plan(
        jobs,
        download_dir=tmp_path / "downloads",
        executable=tmp_path / "main.exe",
        logger=logging.getLogger("test"),
        resolver=resolver,
        controller=controller,
    )

    assert photo_job_key(jobs[2 - 2]) not in plan.failures
    assert photo_job_key(jobs[1]) in plan.failures
    reloaded = [copy.deepcopy(job) for job in jobs]
    plan.apply(reloaded)
    assert reloaded[0].status == JobStatus.PENDING
    assert reloaded[1].status == JobStatus.FAILED


def test_unchanged_camera_metadata_blocks_article(tmp_path: Path) -> None:
    path = tmp_path / "unchanged.jpg"
    write_jpeg(path, "Original", "Unchanged")
    job = make_job(2)
    resolver = FakeResolver(
        {
            2: [
                ResolvedImage(
                    0,
                    "B/A",
                    "unchanged-id",
                    path.name,
                    path,
                )
            ]
        }
    )

    class NoChangeController:
        def wash(self, batch_dir: Path, image_paths: list[Path]) -> None:
            return None

    plan = prepare_photo_wash_plan(
        [job],
        download_dir=tmp_path / "downloads",
        executable=tmp_path / "main.exe",
        logger=logging.getLogger("test"),
        resolver=resolver,
        controller=NoChangeController(),
    )

    assert photo_job_key(job) in plan.failures
    reloaded = copy.deepcopy(job)
    plan.apply([reloaded])
    assert reloaded.status == JobStatus.FAILED
    assert "카메라 정보가 변경되지 않아" in reloaded.message
