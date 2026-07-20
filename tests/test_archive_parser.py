import io
import json
import zipfile

from dataset_core.parsers import inspect_dataset_zip


def make_zip(entries: dict[str, bytes | str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content.encode() if isinstance(content, str) else content)
    return output.getvalue()


def test_parses_yolo_detection_and_labels() -> None:
    archive = make_zip({
        "data.yaml": "names:\n  0: cat\n  1: dog\n",
        "train/images/cat.jpg": b"image",
        "train/labels/cat.txt": "0 0.5 0.5 0.2 0.4\n",
    })
    manifest = inspect_dataset_zip(archive)
    assert manifest.parser_name == "yolo"
    assert [(item.class_id, item.class_name) for item in manifest.labels] == [(0, "cat"), (1, "dog")]
    sample = manifest.samples[0]
    assert sample.annotation_path == "train/labels/cat.txt"
    assert sample.annotations[0].bbox == (0.5, 0.5, 0.2, 0.4)
    assert sample.annotations[0].coordinate_space == "normalized"


def test_parses_coco_and_maps_annotation_file_to_image() -> None:
    coco = {
        "images": [{"id": 7, "file_name": "images/a.jpg", "width": 640, "height": 480}],
        "categories": [{"id": 3, "name": "car"}],
        "annotations": [{"id": 1, "image_id": 7, "category_id": 3, "bbox": [10, 20, 30, 40], "segmentation": [[10, 20, 40, 20, 40, 60]]}],
    }
    manifest = inspect_dataset_zip(make_zip({"images/a.jpg": b"image", "annotations.json": json.dumps(coco)}))
    assert manifest.parser_name == "coco"
    assert manifest.labels[0].class_name == "car"
    assert manifest.samples[0].relative_path == "images/a.jpg"
    assert manifest.samples[0].annotations[0].bbox == (10.0, 20.0, 30.0, 40.0)
    assert manifest.samples[0].annotations[0].polygon[1] == (40.0, 20.0)


def test_parses_labelme_polygon_and_assigns_stable_label_id() -> None:
    labelme = {"imageWidth": 100, "imageHeight": 50, "shapes": [{"label": "road", "shape_type": "polygon", "points": [[1, 2], [3, 4], [5, 6]]}]}
    manifest = inspect_dataset_zip(make_zip({"x.png": b"image", "x.json": json.dumps(labelme)}))
    sample = manifest.samples[0]
    assert manifest.parser_name == "labelme"
    assert manifest.labels == (manifest.labels[0],)
    assert manifest.labels[0].class_name == "road"
    assert sample.width == 100
    assert sample.annotations[0].class_id == 0
    assert sample.annotations[0].polygon[-1] == (5.0, 6.0)


def test_parses_voc_box_and_assigns_label() -> None:
    xml = """<annotation><size><width>100</width><height>60</height></size><object><name>person</name><bndbox><xmin>2</xmin><ymin>3</ymin><xmax>42</xmax><ymax>53</ymax></bndbox></object></annotation>"""
    manifest = inspect_dataset_zip(make_zip({"train/a.jpg": b"image", "train/a.xml": xml}))
    sample = manifest.samples[0]
    assert manifest.parser_name == "voc"
    assert sample.annotations[0].class_name == "person"
    assert sample.annotations[0].class_id == 0
    assert sample.annotations[0].bbox == (2.0, 3.0, 40.0, 50.0)
    assert sample.summary.bbox_count == 1
