import io
import zipfile

import pytest

from dataset_core.errors import DatasetCoreError
from dataset_core.parsers import ZipScanPolicy, inspect_dataset_zip, scan_dataset_zip


def archive(entries: dict[str, str | bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        for name, content in entries.items():
            target.writestr(name, content.encode() if isinstance(content, str) else content)
    return output.getvalue()


def test_rejects_windows_device_names() -> None:
    with pytest.raises(DatasetCoreError, match="import.unsafe_archive_path"):
        scan_dataset_zip(archive({"CON.jpg": b"image"}))


def test_rejects_deep_archive_path() -> None:
    name = "/".join(["nested"] * 4 + ["image.jpg"])
    with pytest.raises(DatasetCoreError, match="import.path_too_deep"):
        scan_dataset_zip(archive({name: b"image"}), policy=ZipScanPolicy(max_directory_depth=3))


def test_rejects_deep_json_before_labelme_parse() -> None:
    nested = "[]"
    for _ in range(8):
        nested = f"[{nested}]"
    payload = f'{{"shapes": [], "extra": {nested}}}'
    manifest = inspect_dataset_zip(archive({"x.jpg": b"image", "x.json": payload}), policy=ZipScanPolicy(max_json_depth=4))
    assert any(issue.code == "import.json_too_deep" for issue in manifest.issues)


def test_defused_xml_is_reported_as_parse_issue() -> None:
    xml = """<!DOCTYPE doc [<!ENTITY boom "blocked">]><annotation><object><name>&boom;</name></object></annotation>"""
    manifest = inspect_dataset_zip(archive({"x.jpg": b"image", "x.xml": xml}))
    assert any(issue.code == "import.invalid_xml" for issue in manifest.issues)
