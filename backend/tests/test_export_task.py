import json
import zipfile
from types import SimpleNamespace

from app.jobs.export_task import _write_export_zip


class FakeStorage:
    def __init__(self) -> None:
        self.objects = {("bucket", "image/a.jpg"): b"image"}

    def read_bytes(self, bucket: str, object_key: str) -> bytes:
        return self.objects[(bucket, object_key)]


def record() -> dict:
    return {
        "relative_path": "train/images/a.jpg",
        "width": 100,
        "height": 100,
        "image": {"bucket": "bucket", "object_key": "image/a.jpg"},
        "normalized": {
            "annotations": [{
                "class_id": 3,
                "class_name": "car",
                "kind": "bbox",
                "bbox": [10, 20, 30, 40],
                "polygon": [],
                "keypoints": [],
                "coordinate_space": "pixels",
                "attributes": {},
            }],
        },
    }


def test_writes_yolo_zip_from_normalized_annotation(tmp_path) -> None:
    target = tmp_path / "dataset.yolo.zip"
    _write_export_zip([record()], [SimpleNamespace(class_id=3, class_name="car")], "yolo", FakeStorage(), target)
    with zipfile.ZipFile(target) as archive:
        assert archive.read("train/images/a.jpg") == b"image"
        assert archive.read("train/labels/a.txt").decode().strip() == "0 0.250000 0.400000 0.300000 0.400000"
        assert b"0: car" in archive.read("data.yaml")


def test_writes_coco_zip_from_normalized_annotation(tmp_path) -> None:
    target = tmp_path / "dataset.coco.zip"
    _write_export_zip([record()], [SimpleNamespace(class_id=3, class_name="car")], "coco", FakeStorage(), target)
    with zipfile.ZipFile(target) as archive:
        payload = json.loads(archive.read("annotations.json"))
        assert payload["categories"] == [{"id": 0, "name": "car"}]
        assert payload["annotations"][0]["bbox"] == [10.0, 20.0, 30.0, 40.0]
        assert payload["annotations"][0]["category_id"] == 0

def test_writes_labelme_zip_from_normalized_annotation(tmp_path) -> None:
    target = tmp_path / "dataset.labelme.zip"
    _write_export_zip([record()], [SimpleNamespace(class_id=3, class_name="car")], "labelme", FakeStorage(), target)
    with zipfile.ZipFile(target) as archive:
        payload = json.loads(archive.read("annotations/train/images/a.json"))
        assert archive.read("images/train/a.jpg") == b"image"
        assert payload["imagePath"] == "a.jpg"
        assert payload["shapes"][0]["label"] == "car"
        assert payload["shapes"][0]["shape_type"] == "rectangle"


def test_writes_voc_zip_from_normalized_annotation(tmp_path) -> None:
    target = tmp_path / "dataset.voc.zip"
    _write_export_zip([record()], [SimpleNamespace(class_id=3, class_name="car")], "voc", FakeStorage(), target)
    with zipfile.ZipFile(target) as archive:
        xml = archive.read("Annotations/a.xml").decode()
        assert archive.read("JPEGImages/a.jpg") == b"image"
        assert "<name>car</name>" in xml
        assert archive.read("ImageSets/Main/train.txt").decode() == "a\n"