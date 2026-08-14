from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from v2r_auto.photo_washer import (
    PhotoWasherController,
    camera_metadata,
    camera_metadata_changed,
)


FILES = {
    "photowasher2.1/main.exe": "1FpAyPMBi_MS4BAgwuJDI3AqRi6sWbykj",
    "photowasher2.1/manufacturers.txt": "1EwOQ9Cf7jNVdJGwTi42lbLa5NcNWhvP3",
    "photowasher2.1/models.txt": "1hpeYDNTua5eVNdz7kuYx5_xjddNXwjPY",
    "row18/1Cgc18d4wqe7z9EnYS6efVx0sBpdRg8ta_천국의계단 가정용.jpg": (
        "1Cgc18d4wqe7z9EnYS6efVx0sBpdRg8ta"
    ),
    "row18/1mF9v-k-0wJrlrtSWCC70OC0f_Bl3wR-l_32354.jpg": (
        "1mF9v-k-0wJrlrtSWCC70OC0f_Bl3wR-l"
    ),
}


def download(file_id: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    query = urlencode({"id": file_id, "export": "download", "confirm": "t"})
    request = Request(
        f"https://drive.usercontent.google.com/download?{query}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urlopen(request, timeout=120) as response:
        target.write_bytes(response.read())
    if not target.is_file() or not target.stat().st_size:
        raise RuntimeError(f"Download failed: {target}")


def main() -> None:
    if os.name != "nt":
        raise RuntimeError("This verification must run on Windows")
    logger = logging.getLogger("photowasher-windows-verification")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler())
    root = Path(os.environ["RUNNER_TEMP"]) / "v2r-photowasher-verification"
    shutil.rmtree(root, ignore_errors=True)
    for relative, file_id in FILES.items():
        download(file_id, root / relative)
    batch_dir = root / "row18"
    image_paths = sorted(batch_dir.glob("*.jpg"))
    if len(image_paths) != 2:
        raise RuntimeError(f"Expected 2 images, found {len(image_paths)}")
    before = {path: camera_metadata(path) for path in image_paths}
    controller = PhotoWasherController(
        root / "photowasher2.1" / "main.exe",
        logger,
        timeout_seconds=30,
    )
    debug_dir = Path.cwd() / "dist" / "PhotoWasher-Debug"
    os.environ["V2R_PHOTOWASHER_DEBUG_DIR"] = str(debug_dir)
    try:
        controller.wash(batch_dir, image_paths)
    except Exception:
        raise
    after = {path: camera_metadata(path) for path in image_paths}
    failures = [
        path.name
        for path in image_paths
        if not camera_metadata_changed(before[path], after[path])
    ]
    if failures:
        raise RuntimeError(
            "PhotoWasher camera metadata did not change: "
            + ", ".join(failures)
        )
    output_dir = Path.cwd() / "dist" / "PhotoWasher-Verification"
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in image_paths:
        shutil.copy2(path, output_dir / path.name)
    logger.info("PhotoWasher UI verification succeeded for row 18 images")


if __name__ == "__main__":
    main()
