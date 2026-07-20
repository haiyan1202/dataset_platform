from __future__ import annotations

import io
import tarfile
import zipfile

import py7zr
import pytest

from dataset_core.errors import DatasetCoreError
from dataset_core.parsers import (
    SUPPORTED_ARCHIVE_SUFFIXES,
    inspect_dataset_archive,
    is_supported_archive_name,
    scan_dataset_archive,
)


ENTRIES = {
    "data.yaml": b"names: [cat]\n",
    "train/images/cat.jpg": b"image-bytes",
    "train/labels/cat.txt": b"0 0.5 0.5 0.2 0.2\n",
}


def make_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in ENTRIES.items():
            archive.writestr(name, content)
    return output.getvalue()


def make_tar_gz() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in ENTRIES.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return output.getvalue()


def make_7z(tmp_path) -> bytes:
    path = tmp_path / "dataset.7z"
    with py7zr.SevenZipFile(path, "w") as archive:
        for name, content in ENTRIES.items():
            archive.writestr(content, name)
    return path.read_bytes()


@pytest.mark.parametrize("archive_factory", [make_zip, make_tar_gz])
def test_scans_and_parses_zip_and_compressed_tar(archive_factory) -> None:
    content = archive_factory()
    scan = scan_dataset_archive(content)
    manifest = inspect_dataset_archive(content)
    assert scan.parser_name == "yolo"
    assert scan.image_count == 1
    assert scan.annotation_count == 1
    assert manifest.parser_name == "yolo"
    assert manifest.samples[0].relative_path == "train/images/cat.jpg"


def test_scans_and_parses_7z(tmp_path) -> None:
    content = make_7z(tmp_path)
    scan = scan_dataset_archive(content)
    manifest = inspect_dataset_archive(content)
    assert scan.parser_name == "yolo"
    assert scan.pairs == (("train/images/cat.jpg", "train/labels/cat.txt"),)
    assert manifest.samples[0].annotations[0].class_name == "cat"


def test_rejects_tar_symlink() -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        entry = tarfile.TarInfo("train/images/link.jpg")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "/etc/passwd"
        archive.addfile(entry)
    with pytest.raises(DatasetCoreError, match="import.unsafe_archive_entry"):
        scan_dataset_archive(output.getvalue())


def test_rejects_unsafe_windows_device_name_in_7z(tmp_path) -> None:
    path = tmp_path / "unsafe.7z"
    with py7zr.SevenZipFile(path, "w") as archive:
        archive.writestr(b"image", "CON.jpg")
    with pytest.raises(DatasetCoreError, match="import.unsafe_archive_path"):
        scan_dataset_archive(path.read_bytes())


def test_supported_archive_suffixes_include_common_linux_and_7z_formats() -> None:
    assert {".zip", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"}.issubset(SUPPORTED_ARCHIVE_SUFFIXES)
    assert all(is_supported_archive_name(name) for name in ("images.zip", "images.7z", "images.tar", "images.tar.gz", "images.tgz", "images.tar.bz2", "images.tar.xz"))
    assert not is_supported_archive_name("images.rar")