import logging
import random
from pathlib import Path

import pytest

from v2r_auto.affiliate_api import AffiliateApiError, AffiliateApiPublisher, _content_json
from v2r_auto.images import (
    DriveItem,
    GoogleDriveImageResolver,
    ResolvedImage,
    brand_from_sheet_title,
    placeholders,
    strip_placeholders,
)
from v2r_auto.models import AffiliateJob
from v2r_auto.content import parse_article


def make_job(body: str, *, brand: str = "팥순이", keyword: str = "갓 비움") -> AffiliateJob:
    return AffiliateJob(
        row_number=2,
        keyword=keyword,
        article=parse_article(keyword, f"제목 : 제목\n본문 :\n{body}"),
        cafe="양평맘",
        account="writer",
        article_type="질문형",
        brand=brand,
    )


def test_sheet_title_brand_uses_last_parentheses() -> None:
    assert brand_from_sheet_title("카페 원고 작성 시트 (팥순이) - Google Drive") == "팥순이"


def test_all_curly_placeholders_are_removed() -> None:
    body = "첫 줄\n{키워드}\n둘째 {B/A}\n{없는폴더}"

    assert placeholders(body) == ["키워드", "B/A", "없는폴더"]
    assert strip_placeholders(body) == "첫 줄\n\n둘째 \n"


def test_drive_folder_page_parses_folders_and_images() -> None:
    source = """
    <div data-id="folder1"><div aria-label="BA Shared folder"></div></div>
    <div data-id="image1"><div aria-label="갓비움.jpg Image Shared"></div></div>
    <div data-id="image1"><div aria-label="Modified Aug 3"></div></div>
    """

    assert GoogleDriveImageResolver.parse_folder_page(source) == [
        DriveItem("folder1", "BA", True),
        DriveItem("image1", "갓비움.jpg", False),
    ]


def test_embedded_drive_folder_reads_more_than_fifty_files() -> None:
    source = "".join(
        (
            '<a href="https://drive.google.com/file/d/'
            f'file-id-{index:020d}/view"><div>사진 {index}.jpg</div></a>'
        )
        for index in range(60)
    )

    items = GoogleDriveImageResolver.parse_embedded_folder_page(source)

    assert len(items) == 60
    assert items[-1].name == "사진 59.jpg"


def test_patsooni_keyword_matches_filename_without_spaces(tmp_path: Path) -> None:
    job = make_job("{키워드}\n{B/A}")

    class FakeResolver(GoogleDriveImageResolver):
        def __init__(self):
            super().__init__(
                tmp_path,
                logging.getLogger("test"),
                root_folder_id="root",
                rng=random.Random(1),
            )
            self.items = {
                "root": [DriveItem("brand", "팥순이", True)],
                "brand": [
                    DriveItem("keyword", "키워드", True),
                    DriveItem("ba", "BA", True),
                ],
                "keyword": [
                    DriveItem("wrong", "다른키워드.jpg", False),
                    DriveItem("right", "갓비움.PNG", False),
                ],
                "ba": [DriveItem("before-after", "before-after.webp", False)],
            }

        def _list_folder(self, folder_id: str):
            return self.items[folder_id]

        def _download(self, item: DriveItem) -> Path:
            return tmp_path / item.name

    resolved = FakeResolver().resolve(job)

    assert [item.file_id for item in resolved] == ["right", "before-after"]
    assert [item.occurrence for item in resolved] == [0, 1]


def test_patsooni_keyword_retries_direct_filename_search(tmp_path: Path) -> None:
    job = make_job("{키워드}", keyword="요즘 그릭요거트")

    class FakeResolver(GoogleDriveImageResolver):
        def __init__(self):
            super().__init__(tmp_path, logging.getLogger("test"), root_folder_id="root")
            self.direct_searches: list[tuple[str, str]] = []

        def _list_folder(self, folder_id: str):
            return {
                "root": [DriveItem("brand", "팥순이", True)],
                "brand": [DriveItem("keyword", "키워드", True)],
                "keyword": [DriveItem("other", "다른 사진.jpg", False)],
            }[folder_id]

        def _search_file(self, folder_id: str, wanted_name: str):
            self.direct_searches.append((folder_id, wanted_name))
            return DriveItem("greek", "요즘 그릭요거트.jpg", False)

        def _download(self, item: DriveItem) -> Path:
            return tmp_path / item.name

    resolver = FakeResolver()
    resolved = resolver.resolve(job)

    assert resolver.direct_searches == [("keyword", "요즘 그릭요거트")]
    assert [item.file_id for item in resolved] == ["greek"]


def test_content_json_inserts_image_at_placeholder_and_keeps_blank_line() -> None:
    image = {"id": "SE-image", "@ctype": "image", "src": "https://example/image.jpg"}
    document = __import__("json").loads(
        _content_json("첫 줄\n{키워드}\n둘째 줄", {0: image})
    )
    components = document["document"]["components"]

    assert [component["@ctype"] for component in components] == ["text", "image", "text"]
    assert components[0]["value"][0]["nodes"][0]["value"] == "첫 줄"
    assert [paragraph["nodes"][0]["value"] for paragraph in components[2]["value"]] == [
        "",
        "둘째 줄",
    ]


def test_drive_failure_falls_back_to_clean_text() -> None:
    job = make_job("첫 줄\n{키워드}\n둘째 줄")
    publisher = AffiliateApiPublisher(None, logging.getLogger("test"))

    class BrokenResolver:
        def resolve(self, _job):
            raise OSError("Drive unavailable")

    publisher.image_resolver = BrokenResolver()
    content = __import__("json").loads(
        publisher._prepare_revision_content(job, {"menu_name": "게시판"})
    )
    paragraphs = content["document"]["components"][0]["value"]

    assert [item["nodes"][0]["value"] for item in paragraphs] == ["첫 줄", "", "둘째 줄"]


def test_resolved_image_upload_failure_stops_text_only_publication(
    tmp_path: Path,
) -> None:
    job = make_job("첫 줄\n{키워드}\n둘째 줄")
    image_path = tmp_path / "갓비움.jpg"
    image_path.write_bytes(b"image")

    class FakeBrowser:
        def upload_affiliate_images(self, job, destination, image_paths):
            assert destination["menu_id"] == 14
            return [{}]

    class FakeResolver:
        def resolve(self, _job):
            return [
                ResolvedImage(
                    occurrence=0,
                    marker="키워드",
                    file_id="image-id",
                    file_name=image_path.name,
                    local_path=image_path,
                )
            ]

    publisher = AffiliateApiPublisher(FakeBrowser(), logging.getLogger("test"))
    publisher.image_resolver = FakeResolver()

    with pytest.raises(AffiliateApiError, match="사진 첨부에 실패"):
        publisher._prepare_revision_content(
            job,
            {
                "cafe_id": 22788814,
                "cafe_name": "양평 맘`s 전원 Story",
                "naver_login_id": "writer",
                "menu_id": 14,
                "menu_name": "게시판",
            },
        )
