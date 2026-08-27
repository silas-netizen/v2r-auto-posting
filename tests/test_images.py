import logging
import random
from pathlib import Path

from v2r_auto.affiliate_api import AffiliateApiPublisher, _content_json
from v2r_auto.images import (
    DriveItem,
    GoogleDriveImageResolver,
    brand_from_sheet_title,
    normalize_image_marker,
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


def test_image_markers_are_normalized() -> None:
    assert normalize_image_marker("키워드") == "키워드"
    assert normalize_image_marker("A열 키워드") == "키워드"
    assert normalize_image_marker("B/A") == "B/A"
    assert normalize_image_marker("BA") == "B/A"


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


def test_patsooni_keyword_matches_filename_without_spaces(tmp_path: Path) -> None:
    job = make_job("{A열 키워드}\n{B/A}")

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
