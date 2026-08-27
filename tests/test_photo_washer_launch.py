import logging

from v2r_auto.photo_washer_launch import open_photo_washer_and_folder


def test_opens_photo_washer_and_selected_image_folder(
    tmp_path,
    monkeypatch,
) -> None:
    executable = tmp_path / "main.exe"
    executable.write_bytes(b"exe")
    image_folder = tmp_path / "기관총_선택이미지" / "20260826_194900"
    image_folder.mkdir(parents=True)
    calls = []

    monkeypatch.setattr(
        "v2r_auto.photo_washer_launch.subprocess.Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    open_photo_washer_and_folder(
        executable,
        image_folder,
        logging.getLogger("test"),
        platform_name="nt",
    )

    assert calls[0][0] == [str(executable)]
    assert calls[0][1]["cwd"] == str(executable.parent)
    assert calls[1][0] == ["explorer.exe", str(image_folder)]
