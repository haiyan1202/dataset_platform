import io
import zipfile

import pytest

from dataset_core.errors import DatasetCoreError
from dataset_core.parsers import ZipScanPolicy, scan_dataset_zip


def make_zip(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return out.getvalue()


def test_scans_yolo_dataset_and_preserves_relative_names() -> None:
    content = make_zip(
        {
            "data.yaml": b"names: [cat]",
            "train/images/cat.jpg": b"image",
            "train/labels/cat.txt": b"0 0.5 0.5 1 1",
        }
    )
    result = scan_dataset_zip(content)
    assert result.parser_name == "yolo"
    assert result.image_count == 1
    assert result.annotation_count == 1
    assert result.pairs == (("train/images/cat.jpg", "train/labels/cat.txt"),)
    assert result.subsets == {"train": 1}


def test_rejects_path_traversal() -> None:
    content = make_zip({"../outside.jpg": b"evil"})
    with pytest.raises(DatasetCoreError, match="import.unsafe_archive_path"):
        scan_dataset_zip(content)


def test_rejects_file_count_limit() -> None:
    content = make_zip({"a.jpg": b"a", "b.jpg": b"b"})
    with pytest.raises(DatasetCoreError, match="import.too_many_files"):
        scan_dataset_zip(content, policy=ZipScanPolicy(max_files=1))


def test_only_exact_train_val_test_path_parts_define_subsets() -> None:
    content = make_zip(
        {
            "train/images/train.jpg": b"image",
            "val/images/val.jpg": b"image",
            "testing/images/not-a-test-split.jpg": b"image",
        }
    )
    result = scan_dataset_zip(content)
    assert result.image_count == 3
    assert result.subsets == {"train": 1, "val": 1}
