from __future__ import annotations

import json
import re
from defusedxml import ElementTree as element_tree
from defusedxml.common import DefusedXmlException
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any

from dataset_core.domain import Annotation, DatasetManifest, LabelSpec, ParsedSample, ScanIssue
from dataset_core.errors import DatasetCoreError

from .zip_scanner import (
    IMAGE_SUFFIXES,
    ArchiveEntry,
    ArchiveReader,
    ZipScanPolicy,
    detect_parser,
    match_key,
    open_archive,
    subset_for,
    validated_entries,
)


def _read_text(archive: ArchiveReader, entry: ArchiveEntry, policy: ZipScanPolicy) -> str:
    if entry.size > policy.max_annotation_bytes:
        raise DatasetCoreError("import.annotation_file_too_large", params={"path": entry.name})
    return archive.read(entry).decode("utf-8-sig", errors="replace")


def _json_depth(value: Any, *, max_depth: int) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            raise DatasetCoreError("import.json_too_deep", params={"max_depth": max_depth})
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)

def _read_json(archive: ArchiveReader, entry: ArchiveEntry, policy: ZipScanPolicy) -> Any:
    try:
        value = json.loads(_read_text(archive, entry, policy))
        _json_depth(value, max_depth=policy.max_json_depth)
        return value
    except json.JSONDecodeError as exc:
        raise DatasetCoreError("import.invalid_json", params={"path": entry.name}) from exc


def _as_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pairs(values: list[float]) -> tuple[tuple[float, float], ...]:
    return tuple((values[index], values[index + 1]) for index in range(0, len(values) - 1, 2))


def _yaml_hints(text: str) -> tuple[dict[int, str], bool]:
    """Read the small YOLO names/kpt_shape subset without coupling core to PyYAML."""
    labels: dict[int, str] = {}
    pose = bool(re.search(r"^\s*kpt_shape\s*:", text, flags=re.MULTILINE))
    inline = re.search(r"^\s*names\s*:\s*\[([^]]*)\]", text, flags=re.MULTILINE)
    if inline:
        for index, name in enumerate(inline.group(1).split(",")):
            labels[index] = name.strip().strip("'\"")
        return labels, pose
    in_names = False
    for raw_line in text.splitlines():
        if re.match(r"^\s*names\s*:\s*$", raw_line):
            in_names = True
            continue
        if in_names:
            match = re.match(r"^\s+(\d+)\s*:\s*(.+?)\s*$", raw_line)
            if match:
                labels[int(match.group(1))] = match.group(2).strip().strip("'\"")
                continue
            if raw_line.strip() and not raw_line.startswith((" ", "\t", "#")):
                break
    return labels, pose


def _parse_yolo(text: str, label_names: dict[int, str], *, pose_hint: bool) -> tuple[Annotation, ...]:
    parsed: list[Annotation] = []
    for row_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            values = [float(value) for value in line.split()]
        except ValueError:
            continue
        if len(values) < 5 or not values[0].is_integer():
            continue
        class_id = int(values[0])
        coords = values[1:]
        name = label_names.get(class_id, f"class_{class_id}")
        bbox = tuple(coords[:4]) if len(coords) >= 4 else None
        if pose_hint and len(coords) >= 7 and (len(coords) - 4) % 3 == 0:
            keypoints = tuple((coords[index], coords[index + 1], coords[index + 2]) for index in range(4, len(coords), 3))
            parsed.append(Annotation(class_id, name, "keypoints", bbox=bbox, keypoints=keypoints, coordinate_space="normalized"))
        elif len(coords) > 4 and len(coords) % 2 == 0:
            parsed.append(Annotation(class_id, name, "polygon", polygon=_pairs(coords), coordinate_space="normalized"))
        else:
            parsed.append(Annotation(class_id, name, "bbox", bbox=bbox, coordinate_space="normalized"))
    return tuple(parsed)


def _parse_labelme(document: dict[str, Any]) -> tuple[tuple[Annotation, ...], int | None, int | None]:
    annotations: list[Annotation] = []
    for shape in document.get("shapes", []):
        if not isinstance(shape, dict) or not str(shape.get("label", "")).strip():
            continue
        name = str(shape["label"]).strip()
        points = tuple((float(point[0]), float(point[1])) for point in shape.get("points", []) if isinstance(point, list) and len(point) >= 2)
        shape_type = str(shape.get("shape_type") or "polygon").lower()
        if shape_type in {"rectangle", "bbox"} and len(points) >= 2:
            x1, y1 = points[0]
            x2, y2 = points[1]
            annotation = Annotation(-1, name, "bbox", bbox=(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)))
        elif shape_type == "point" and points:
            annotation = Annotation(-1, name, "keypoints", keypoints=((points[0][0], points[0][1], 2.0),))
        else:
            annotation = Annotation(-1, name, "polygon", polygon=points)
        annotations.append(annotation)
    return tuple(annotations), document.get("imageWidth"), document.get("imageHeight")


def _parse_voc(text: str) -> tuple[tuple[Annotation, ...], int | None, int | None]:
    try:
        root = element_tree.fromstring(text)
    except (element_tree.ParseError, DefusedXmlException) as exc:
        raise DatasetCoreError("import.invalid_xml") from exc
    width = root.findtext("size/width")
    height = root.findtext("size/height")
    annotations: list[Annotation] = []
    for element in root.findall("object"):
        name = (element.findtext("name") or "unknown").strip()
        bbox_element = element.find("bndbox")
        if bbox_element is None:
            continue
        xmin, ymin = _as_number(bbox_element.findtext("xmin")), _as_number(bbox_element.findtext("ymin"))
        xmax, ymax = _as_number(bbox_element.findtext("xmax")), _as_number(bbox_element.findtext("ymax"))
        annotations.append(Annotation(-1, name, "bbox", bbox=(xmin, ymin, max(0.0, xmax - xmin), max(0.0, ymax - ymin))))
    return tuple(annotations), int(_as_number(width)) if width else None, int(_as_number(height)) if height else None


def _parse_coco(document: dict[str, Any], image_paths: list[str], annotation_path: str, issues: list[ScanIssue]) -> tuple[tuple[ParsedSample, ...], tuple[LabelSpec, ...]]:
    categories = {int(item["id"]): str(item.get("name") or f"class_{item['id']}") for item in document.get("categories", []) if isinstance(item, dict) and "id" in item}
    labels = tuple(LabelSpec(class_id, name) for class_id, name in sorted(categories.items()))
    by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for annotation in document.get("annotations", []):
        if isinstance(annotation, dict) and "image_id" in annotation:
            by_id[int(annotation["image_id"])].append(annotation)
    exact = {path.lower(): path for path in image_paths}
    by_basename: dict[str, list[str]] = defaultdict(list)
    for path in image_paths:
        by_basename[PurePosixPath(path).name.lower()].append(path)

    samples: list[ParsedSample] = []
    for image in document.get("images", []):
        if not isinstance(image, dict) or "id" not in image or not image.get("file_name"):
            continue
        requested = str(image["file_name"]).replace("\\", "/").lstrip("./")
        relative_path = exact.get(requested.lower())
        if relative_path is None:
            candidates = by_basename.get(PurePosixPath(requested).name.lower(), [])
            relative_path = candidates[0] if len(candidates) == 1 else None
        if relative_path is None:
            issues.append(ScanIssue("import.coco_image_missing", "warning", requested))
            continue
        annotations: list[Annotation] = []
        for source in by_id[int(image["id"])]:
            class_id = int(source.get("category_id", -1))
            class_name = categories.get(class_id, f"class_{class_id}")
            bbox_values = source.get("bbox") or []
            bbox = tuple(float(value) for value in bbox_values[:4]) if len(bbox_values) >= 4 else None
            segmentation = source.get("segmentation")
            polygon: tuple[tuple[float, float], ...] = ()
            if isinstance(segmentation, list) and segmentation and isinstance(segmentation[0], list):
                polygon = _pairs([float(value) for value in segmentation[0]])
            raw_keypoints = source.get("keypoints") or []
            keypoints = tuple((float(raw_keypoints[index]), float(raw_keypoints[index + 1]), float(raw_keypoints[index + 2])) for index in range(0, len(raw_keypoints) - 2, 3))
            kind = "keypoints" if keypoints else "polygon" if polygon else "bbox"
            annotations.append(Annotation(class_id, class_name, kind, bbox=bbox, polygon=polygon, keypoints=keypoints, attributes={"iscrowd": source.get("iscrowd", 0)}))
        samples.append(ParsedSample(relative_path, annotation_path, subset_for(PurePosixPath(relative_path)), "coco", tuple(annotations), image.get("width"), image.get("height")))
    return tuple(samples), labels


def inspect_dataset_reader(
    archive: ArchiveReader,
    entries: dict[str, ArchiveEntry],
    *,
    policy: ZipScanPolicy | None = None,
) -> DatasetManifest:
    """Parse a validated, already-open archive into a normalized manifest."""
    policy = policy or ZipScanPolicy()
    names = list(entries)
    image_paths = [name for name in names if PurePosixPath(name).suffix.lower() in IMAGE_SUFFIXES]
    issues: list[ScanIssue] = []
    parser_name = detect_parser(names)
    if not image_paths:
        issues.append(ScanIssue("import.no_images", "error"))
    json_documents: dict[str, Any] = {}
    for name, entry in entries.items():
        if PurePosixPath(name).suffix.lower() == ".json" and entry.size <= policy.max_annotation_bytes:
            try:
                json_documents[name] = _read_json(archive, entry, policy)
            except DatasetCoreError as exc:
                issues.append(ScanIssue(exc.code, "warning", name, exc.params))

    coco_entry = next((name for name, doc in json_documents.items() if isinstance(doc, dict) and isinstance(doc.get("images"), list) and isinstance(doc.get("annotations"), list) and isinstance(doc.get("categories"), list)), None)
    if coco_entry:
        samples, labels = _parse_coco(json_documents[coco_entry], image_paths, coco_entry, issues)
        return DatasetManifest("coco", samples, labels, tuple(issues))

    yolo_config = next((name for name in names if PurePosixPath(name).name.lower() in {"data.yaml", "data.yml"}), None)
    yolo_names: dict[int, str] = {}
    yolo_pose = False
    if yolo_config:
        try:
            yolo_names, yolo_pose = _yaml_hints(_read_text(archive, entries[yolo_config], policy))
        except DatasetCoreError as exc:
            issues.append(ScanIssue(exc.code, "warning", yolo_config, exc.params))

    annotation_by_key: dict[str, str] = {}
    for name in names:
        suffix = PurePosixPath(name).suffix.lower()
        if suffix in {".txt", ".xml", ".json"} and name not in json_documents:
            annotation_by_key[match_key(PurePosixPath(name))] = name
        elif suffix in {".xml", ".txt"}:
            annotation_by_key[match_key(PurePosixPath(name))] = name
        elif suffix == ".json" and isinstance(json_documents.get(name), dict) and "shapes" in json_documents[name]:
            annotation_by_key[match_key(PurePosixPath(name))] = name

    samples: list[ParsedSample] = []
    discovered_labels: set[str] = set()
    for image_path in sorted(image_paths):
        path = PurePosixPath(image_path)
        annotation_path = annotation_by_key.get(match_key(path))
        annotations: tuple[Annotation, ...] = ()
        width: int | None = None
        height: int | None = None
        annotation_type = parser_name or "unknown"
        if annotation_path:
            try:
                suffix = PurePosixPath(annotation_path).suffix.lower()
                if suffix == ".txt":
                    annotation_type = "yolo"
                    annotations = _parse_yolo(_read_text(archive, entries[annotation_path], policy), yolo_names, pose_hint=yolo_pose)
                elif suffix == ".xml":
                    annotation_type = "voc"
                    annotations, width, height = _parse_voc(_read_text(archive, entries[annotation_path], policy))
                elif suffix == ".json":
                    annotation_type = "labelme"
                    document = json_documents.get(annotation_path)
                    if not isinstance(document, dict):
                        document = _read_json(archive, entries[annotation_path], policy)
                    annotations, width, height = _parse_labelme(document)
                else:
                    issues.append(ScanIssue("import.unsupported_annotation", "warning", annotation_path))
            except DatasetCoreError as exc:
                issues.append(ScanIssue(exc.code, "warning", annotation_path, exc.params))
        else:
            issues.append(ScanIssue("import.missing_annotation", "warning", image_path))
        discovered_labels.update(annotation.class_name for annotation in annotations)
        samples.append(ParsedSample(image_path, annotation_path, subset_for(path), annotation_type, annotations, width, height))

    if parser_name == "yolo" or yolo_names:
        labels = {class_id: LabelSpec(class_id, name) for class_id, name in yolo_names.items()}
        for sample in samples:
            for annotation in sample.annotations:
                labels.setdefault(annotation.class_id, LabelSpec(annotation.class_id, annotation.class_name))
        manifest = DatasetManifest("yolo", tuple(samples), tuple(sorted(labels.values(), key=lambda item: item.class_id)), tuple(issues))
    else:
        labels = tuple(LabelSpec(index, name) for index, name in enumerate(sorted(discovered_labels)))
        manifest = DatasetManifest(parser_name, tuple(samples), labels, tuple(issues))
    return manifest.with_resolved_labels()


def inspect_dataset_archive(content: bytes | str | Any, *, policy: ZipScanPolicy | None = None) -> DatasetManifest:
    """Parse supported archive layouts into a framework-independent normalized manifest."""
    policy = policy or ZipScanPolicy()
    with open_archive(content) as archive:
        entries = validated_entries(archive, policy)
        return inspect_dataset_reader(archive, entries, policy=policy)


def inspect_dataset_zip(content: bytes | str | Any, *, policy: ZipScanPolicy | None = None) -> DatasetManifest:
    """Backward-compatible alias; now accepts every supported archive format."""
    return inspect_dataset_archive(content, policy=policy)