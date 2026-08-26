from __future__ import annotations

import os
import subprocess
from pathlib import Path


class PhotoWasherLaunchError(RuntimeError):
    pass


def open_photo_washer_and_folder(
    executable: str | Path,
    image_folder: str | Path,
    logger,
    *,
    platform_name: str | None = None,
) -> None:
    executable = Path(executable)
    image_folder = Path(image_folder)
    if not executable.is_file():
        raise PhotoWasherLaunchError(
            f"포토워셔 실행 파일을 찾지 못했습니다: {executable}"
        )
    if not image_folder.is_dir():
        raise PhotoWasherLaunchError(
            f"선택 이미지 폴더를 찾지 못했습니다: {image_folder}"
        )
    if (platform_name or os.name) != "nt":
        raise PhotoWasherLaunchError("포토워셔는 Windows에서만 열 수 있습니다")
    subprocess.Popen(
        [str(executable)],
        cwd=str(executable.parent),
    )
    subprocess.Popen(["explorer.exe", str(image_folder)])
    logger.info(
        "포토워셔와 선택 이미지 폴더를 열었습니다: %s",
        image_folder,
    )
