from __future__ import annotations

import json
import os
import tempfile
import uuid
import zipfile
from xml.etree import ElementTree as element_tree
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AnnotationIndex, Asset, DatasetVersion, Job, LabelDefinition, Sample, SampleClassIndex
from app.services import job_is_cancelled, transition_job
from app.settings import get_settings
from app.storage import get_storage
from .celery_app import celery_app


def _work_dir() -> Path:
    path = Path(os.environ.get("WORKER_TMP_DIR", "/tmp/dataset-worker"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalized(value: float, limit: int | None) -> float:
    return value / limit if limit and limit > 0 else value


def _yolo_line(annotation: dict, class_map: dict[int, int], width: int | None, height: int | None) -> str | None:
    source_id = int(annotation.get("class_id", -1))
    if source_id not in class_map:
        return None
    bbox = annotation.get("bbox")
    coordinate_space = annotation.get("coordinate_space")
    if bbox:
        x, y, w, h = (float(value) for value in bbox)
        if coordinate_space != "normalized":
            x, y, w, h = _normalized(x + w / 2, width), _normalized(y + h / 2, height), _normalized(w, width), _normalized(h, height)
        values = [class_map[source_id], x, y, w, h]
    else:
        values = [class_map[source_id]]
    polygon = annotation.get("polygon") or []
    if polygon:
        values = [class_map[source_id]]
        for point in polygon:
            x, y = float(point[0]), float(point[1])
            if coordinate_space != "normalized":
                x, y = _normalized(x, width), _normalized(y, height)
            values.extend((x, y))
    keypoints = annotation.get("keypoints") or []
    if keypoints:
        if not bbox:
            return None
        for point in keypoints:
            x, y = float(point[0]), float(point[1])
            if coordinate_space != "normalized":
                x, y = _normalized(x, width), _normalized(y, height)
            values.extend((x, y, float(point[2] if len(point) > 2 and point[2] is not None else 2)))
    return " ".join(f"{value:.6f}" if isinstance(value, float) else str(value) for value in values)


def _coco_annotation(annotation: dict, class_map: dict[int, int], image_id: int, annotation_id: int, width: int | None, height: int | None) -> dict | None:
    source_id = int(annotation.get("class_id", -1))
    if source_id not in class_map:
        return None
    coordinate_space = annotation.get("coordinate_space")
    bbox = annotation.get("bbox")
    result: dict = {"id": annotation_id, "image_id": image_id, "category_id": class_map[source_id], "iscrowd": int(annotation.get("attributes", {}).get("iscrowd", 0))}
    if bbox:
        x, y, w, h = (float(value) for value in bbox)
        if coordinate_space == "normalized" and width and height:
            x, y, w, h = (x - w / 2) * width, (y - h / 2) * height, w * width, h * height
        result["bbox"] = [x, y, w, h]
        result["area"] = max(w, 0) * max(h, 0)
    polygon = annotation.get("polygon") or []
    if polygon:
        values: list[float] = []
        for point in polygon:
            x, y = float(point[0]), float(point[1])
            if coordinate_space == "normalized" and width and height:
                x, y = x * width, y * height
            values.extend((x, y))
        result["segmentation"] = [values]
    keypoints = annotation.get("keypoints") or []
    if keypoints:
        values: list[float] = []
        for point in keypoints:
            x, y, visible = float(point[0]), float(point[1]), int(float(point[2] if len(point) > 2 and point[2] is not None else 2))
            if coordinate_space == "normalized" and width and height:
                x, y = x * width, y * height
            values.extend((x, y, visible))
        result["keypoints"] = values
        result["num_keypoints"] = sum(value > 0 for value in values[2::3])
    if "bbox" not in result and "segmentation" not in result and "keypoints" not in result:
        return None
    return result


def _export_manifest(records: list[dict], dataset_id: uuid.UUID) -> tuple[bytes, str, str]:
    payload = json.dumps({"format": "dataset-platform-export-manifest/v1", "dataset_id": str(dataset_id), "samples": records}, ensure_ascii=False, separators=(",", ":")).encode()
    return payload, "manifest.json", "application/json"


def _label_name(annotation: dict, label_names: dict[int, str]) -> str:
    return label_names.get(int(annotation.get("class_id", -1)), str(annotation.get("class_name") or annotation.get("class_id", "unknown")))


def _pixel_point(point: list[float], coordinate_space: str | None, width: int | None, height: int | None) -> tuple[float, float]:
    x, y = float(point[0]), float(point[1])
    if coordinate_space == "normalized" and width and height:
        return x * width, y * height
    return x, y


def _pixel_bbox(annotation: dict, width: int | None, height: int | None) -> tuple[float, float, float, float] | None:
    bbox = annotation.get("bbox")
    if not bbox or len(bbox) < 4:
        return None
    x, y, w, h = (float(value) for value in bbox[:4])
    if annotation.get("coordinate_space") == "normalized" and width and height:
        return (x - w / 2) * width, (y - h / 2) * height, w * width, h * height
    return x, y, w, h


def _write_labelme_json(record: dict, label_names: dict[int, str]) -> bytes:
    width, height = record.get("width") or 0, record.get("height") or 0
    shapes: list[dict] = []
    for annotation in record["normalized"].get("annotations", []):
        coordinate_space = annotation.get("coordinate_space")
        label = _label_name(annotation, label_names)
        polygon = annotation.get("polygon") or []
        bbox = _pixel_bbox(annotation, width, height)
        if polygon:
            shapes.append({
                "label": label,
                "points": [[round(x, 2), round(y, 2)] for x, y in (_pixel_point(point, coordinate_space, width, height) for point in polygon)],
                "group_id": None,
                "description": "",
                "shape_type": "polygon",
                "flags": {},
            })
        elif bbox:
            x, y, box_width, box_height = bbox
            shapes.append({
                "label": label,
                "points": [[round(x, 2), round(y, 2)], [round(x + box_width, 2), round(y + box_height, 2)]],
                "group_id": None,
                "description": "",
                "shape_type": "rectangle",
                "flags": {},
            })
    image_path = PurePosixPath(record["relative_path"]).name
    return json.dumps({
        "version": "5.0.1",
        "flags": {},
        "shapes": shapes,
        "imagePath": image_path,
        "imageData": None,
        "imageHeight": height,
        "imageWidth": width,
    }, ensure_ascii=False, separators=(",", ":")).encode()


def _write_voc_xml(record: dict, label_names: dict[int, str]) -> bytes:
    path = PurePosixPath(record["relative_path"])
    width, height = int(record.get("width") or 0), int(record.get("height") or 0)
    root = element_tree.Element("annotation")
    element_tree.SubElement(root, "folder").text = path.parent.name
    element_tree.SubElement(root, "filename").text = path.name
    size = element_tree.SubElement(root, "size")
    element_tree.SubElement(size, "width").text = str(width)
    element_tree.SubElement(size, "height").text = str(height)
    element_tree.SubElement(size, "depth").text = "3"
    for annotation in record["normalized"].get("annotations", []):
        bbox = _pixel_bbox(annotation, width, height)
        polygon = annotation.get("polygon") or []
        if not bbox and polygon:
            points = [_pixel_point(point, annotation.get("coordinate_space"), width, height) for point in polygon]
            xs, ys = [point[0] for point in points], [point[1] for point in points]
            bbox = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
        if not bbox:
            continue
        x, y, box_width, box_height = bbox
        obj = element_tree.SubElement(root, "object")
        element_tree.SubElement(obj, "name").text = _label_name(annotation, label_names)
        element_tree.SubElement(obj, "pose").text = "Unspecified"
        element_tree.SubElement(obj, "truncated").text = "0"
        element_tree.SubElement(obj, "difficult").text = "0"
        box = element_tree.SubElement(obj, "bndbox")
        element_tree.SubElement(box, "xmin").text = str(max(0, round(x)))
        element_tree.SubElement(box, "ymin").text = str(max(0, round(y)))
        element_tree.SubElement(box, "xmax").text = str(max(0, round(x + box_width)))
        element_tree.SubElement(box, "ymax").text = str(max(0, round(y + box_height)))
    return element_tree.tostring(root, encoding="utf-8", xml_declaration=True)

def _write_export_zip(records: list[dict], labels: list[LabelDefinition], export_format: str, storage, archive_path: Path) -> None:
    ordered_labels = sorted(labels, key=lambda item: item.class_id)
    class_map = {label.class_id: index for index, label in enumerate(ordered_labels)}
    label_names = {label.class_id: label.class_name for label in ordered_labels}
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if export_format == "yolo":
            names = [label.class_name for label in sorted(labels, key=lambda item: item.class_id)]
            archive.writestr("data.yaml", "names:\n" + "".join(f"  {index}: {name}\n" for index, name in enumerate(names)))
            for record in records:
                image_path = record["relative_path"]
                archive.writestr(image_path, storage.read_bytes(record["image"]["bucket"], record["image"]["object_key"]))
                path = PurePosixPath(image_path)
                parts = list(path.parts)
                for index, part in enumerate(parts):
                    if part.lower() == "images":
                        parts[index] = "labels"
                label_path = PurePosixPath(*parts).with_suffix(".txt").as_posix()
                lines = [line for annotation in record["normalized"].get("annotations", []) if (line := _yolo_line(annotation, class_map, record["width"], record["height"]))]
                archive.writestr(label_path, "\n".join(lines) + ("\n" if lines else ""))
        elif export_format == "labelme":
            for record in records:
                image_path = PurePosixPath(record["relative_path"])
                subset = record.get("subset") or next((part for part in image_path.parts if part in {"train", "val", "test"}), "unassigned")
                archive.writestr(f"images/{subset}/{image_path.name}", storage.read_bytes(record["image"]["bucket"], record["image"]["object_key"]))
                archive.writestr(f"annotations/{image_path.with_suffix('.json').as_posix()}", _write_labelme_json(record, label_names))
        elif export_format == "voc":
            subsets: dict[str, list[str]] = {}
            for record in records:
                image_path = PurePosixPath(record["relative_path"])
                stem = image_path.stem
                subset = record.get("subset") or "train"
                subsets.setdefault(subset, []).append(stem)
                archive.writestr(f"JPEGImages/{image_path.name}", storage.read_bytes(record["image"]["bucket"], record["image"]["object_key"]))
                archive.writestr(f"Annotations/{stem}.xml", _write_voc_xml(record, label_names))
            for subset, stems in subsets.items():
                archive.writestr(f"ImageSets/Main/{subset}.txt", "\n".join(stems) + "\n")
        else:
            categories = [{"id": class_map[label.class_id], "name": label.class_name} for label in ordered_labels]
            images: list[dict] = []
            annotations: list[dict] = []
            next_annotation_id = 1
            for image_id, record in enumerate(records, 1):
                image_path = record["relative_path"]
                archive.writestr(image_path, storage.read_bytes(record["image"]["bucket"], record["image"]["object_key"]))
                images.append({"id": image_id, "file_name": image_path, "width": record["width"], "height": record["height"]})
                for annotation in record["normalized"].get("annotations", []):
                    output = _coco_annotation(annotation, class_map, image_id, next_annotation_id, record["width"], record["height"])
                    if output:
                        annotations.append(output)
                        next_annotation_id += 1
            archive.writestr("annotations.json", json.dumps({"images": images, "annotations": annotations, "categories": categories}, ensure_ascii=False, separators=(",", ":")))


@celery_app.task(name="dataset_platform.create_export")
def create_export(job_id: str) -> dict:
    """Build manifest, YOLO, COCO, LabelMe, or Pascal VOC exports in a Worker."""
    with SessionLocal() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return {"status": "missing"}
        if job.status == "succeeded":
            return job.result_json or {}
        if job_is_cancelled(db, job):
            return {"status": "cancelled"}
        requested_format = (job.result_json or {}).get("requested_format", "manifest")
        if requested_format not in {"manifest", "yolo", "coco", "labelme", "voc"}:
            transition_job(job, status="failed", stage="failed", error_code="export.unsupported_format")
            db.commit()
            return {"status": "failed", "error_code": "export.unsupported_format"}
        version_ids = select(DatasetVersion.id).where(DatasetVersion.dataset_id == job.resource_id)
        filters = (job.result_json or {}).get("filters") or {}
        sample_query = select(Sample).where(Sample.dataset_version_id.in_(version_ids), Sample.deleted_at.is_(None))
        batch_ids = [uuid.UUID(value) for value in filters.get("import_batch_ids", [])]
        if batch_ids:
            sample_query = sample_query.where(Sample.import_batch_id.in_(batch_ids))
        subsets = [value for value in filters.get("subsets", []) if value in {"train", "val", "test"}]
        if subsets:
            sample_query = sample_query.where(Sample.subset.in_(subsets))
        if not filters.get("include_unannotated", True):
            sample_query = sample_query.where(Sample.annotation_asset_id.is_not(None))
        class_ids = [int(value) for value in filters.get("class_ids", [])]
        if class_ids:
            sample_query = sample_query.join(SampleClassIndex, SampleClassIndex.sample_id == Sample.id).where(SampleClassIndex.class_id.in_(class_ids)).distinct()
        samples = list(db.scalars(sample_query.order_by(Sample.relative_path)))
        label_candidates = list(db.scalars(
            select(LabelDefinition)
            .join(DatasetVersion, DatasetVersion.id == LabelDefinition.dataset_version_id)
            .where(DatasetVersion.dataset_id == job.resource_id)
            .order_by(DatasetVersion.version_number.desc(), LabelDefinition.class_id)
        ))
        latest_labels: dict[int, LabelDefinition] = {}
        for label in label_candidates:
            latest_labels.setdefault(label.class_id, label)
        labels = list(sorted(latest_labels.values(), key=lambda item: item.class_id))
        transition_job(job, status="running", stage="collect", current=0, total=max(len(samples), 1))
        storage = get_storage()
        records: list[dict] = []
        for index, sample in enumerate(samples, 1):
            if job_is_cancelled(db, job):
                db.commit()
                return {"status": "cancelled"}
            image = db.get(Asset, sample.image_asset_id)
            annotation_index = db.get(AnnotationIndex, sample.id)
            normalized_asset = db.get(Asset, annotation_index.normalized_annotation_asset_id) if annotation_index and annotation_index.normalized_annotation_asset_id else None
            if image is None or normalized_asset is None:
                continue
            normalized = json.loads(storage.read_bytes(normalized_asset.bucket, normalized_asset.object_key))
            records.append({"relative_path": sample.relative_path, "subset": sample.subset, "annotation_type": sample.annotation_type, "width": sample.width, "height": sample.height, "image": {"bucket": image.bucket, "object_key": image.object_key}, "normalized": normalized})
            transition_job(job, status="running", stage="collect", current=index, total=max(len(samples), 1))
        bucket = records[0]["image"]["bucket"] if records else get_settings().minio_bucket
        if requested_format == "manifest":
            payload, file_name, content_type = _export_manifest(records, job.resource_id)
            object_key = f"org/{job.organization_id}/exports/{job.id}/{file_name}"
            storage.put_bytes(bucket, object_key, payload, content_type)
        else:
            file_name = f"dataset.{requested_format}.zip"
            object_key = f"org/{job.organization_id}/exports/{job.id}/{file_name}"
            transition_job(job, status="running", stage="package", current=0, total=max(len(records), 1))
            if job_is_cancelled(db, job):
                return {"status": "cancelled"}
            with tempfile.TemporaryDirectory(dir=_work_dir()) as temp_dir:
                archive_path = Path(temp_dir) / file_name
                _write_export_zip(records, labels, requested_format, storage, archive_path)
                storage.upload_file(bucket, object_key, str(archive_path), "application/zip")
        result = {"bucket": bucket, "object_key": object_key, "file_name": file_name, "sample_count": len(records), "format": requested_format}
        transition_job(job, status="succeeded", stage="ready", current=max(len(records), 1), total=max(len(records), 1), result=result)
        db.commit()
        return result




