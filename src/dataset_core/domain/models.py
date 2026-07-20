from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class ScanIssue:
    """A serializable, UI-independent issue returned by a parser or scanner."""

    code: str
    severity: str
    relative_path: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Annotation:
    """Format-neutral annotation suitable for JSON persistence and export adapters."""

    class_id: int
    class_name: str
    kind: str
    bbox: tuple[float, float, float, float] | None = None
    polygon: tuple[tuple[float, float], ...] = ()
    keypoints: tuple[tuple[float, float, float | None], ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    coordinate_space: str = "pixels"

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "class_name": self.class_name,
            "kind": self.kind,
            "bbox": list(self.bbox) if self.bbox else None,
            "polygon": [list(point) for point in self.polygon],
            "keypoints": [list(point) for point in self.keypoints],
            "attributes": self.attributes,
            "coordinate_space": self.coordinate_space,
        }


@dataclass(frozen=True, slots=True)
class LabelSpec:
    class_id: int
    class_name: str
    color: str | None = None


@dataclass(frozen=True, slots=True)
class AnnotationSummary:
    annotation_type: str
    annotation_count: int = 0
    bbox_count: int = 0
    polygon_count: int = 0
    keypoint_count: int = 0
    class_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedSample:
    relative_path: str
    annotation_path: str | None
    subset: str | None
    annotation_type: str
    annotations: tuple[Annotation, ...] = ()
    width: int | None = None
    height: int | None = None

    @property
    def file_name(self) -> str:
        return self.relative_path.rsplit("/", 1)[-1]

    @property
    def summary(self) -> AnnotationSummary:
        return AnnotationSummary(
            annotation_type=self.annotation_type,
            annotation_count=len(self.annotations),
            bbox_count=sum(annotation.bbox is not None for annotation in self.annotations),
            polygon_count=sum(bool(annotation.polygon) for annotation in self.annotations),
            keypoint_count=sum(bool(annotation.keypoints) for annotation in self.annotations),
            class_ids=tuple(sorted({annotation.class_id for annotation in self.annotations if annotation.class_id >= 0})),
        )

    def normalized_annotation(self, *, parser_version: str = "1") -> dict[str, Any]:
        return {
            "schema_version": 1,
            "parser_version": parser_version,
            "annotation_type": self.annotation_type,
            "image": {"relative_path": self.relative_path, "width": self.width, "height": self.height},
            "annotations": [annotation.to_dict() for annotation in self.annotations],
        }


@dataclass(frozen=True, slots=True)
class ScanResult:
    parser_name: str | None
    image_count: int
    annotation_count: int
    pairs: tuple[tuple[str, str | None], ...]
    issues: tuple[ScanIssue, ...] = ()
    subsets: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parser_name": self.parser_name,
            "image_count": self.image_count,
            "annotation_count": self.annotation_count,
            "pairs": [{"image": image, "annotation": annotation} for image, annotation in self.pairs],
            "issues": [
                {"code": issue.code, "severity": issue.severity, "relative_path": issue.relative_path, "params": issue.params}
                for issue in self.issues
            ],
            "subsets": self.subsets,
        }

    def to_preview(self, *, max_issues: int = 100) -> dict[str, Any]:
        """Return bounded metadata needed before an import is confirmed."""
        return {
            "parser_name": self.parser_name,
            "image_count": self.image_count,
            "annotation_count": self.annotation_count,
            "labels": [],
            "issues": [
                {"code": issue.code, "severity": issue.severity, "relative_path": issue.relative_path, "params": issue.params}
                for issue in self.issues[:max_issues]
            ],
            "subsets": self.subsets,
        }


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    parser_name: str | None
    samples: tuple[ParsedSample, ...]
    labels: tuple[LabelSpec, ...] = ()
    issues: tuple[ScanIssue, ...] = ()

    def to_preview(self, *, max_issues: int = 100) -> dict[str, Any]:
        return {
            "parser_name": self.parser_name,
            "image_count": len(self.samples),
            "annotation_count": sum(len(sample.annotations) for sample in self.samples),
            "labels": [{"class_id": item.class_id, "class_name": item.class_name} for item in self.labels],
            "issues": [
                {"code": issue.code, "severity": issue.severity, "relative_path": issue.relative_path, "params": issue.params}
                for issue in self.issues[:max_issues]
            ],
            "subsets": {
                subset: sum(sample.subset == subset for sample in self.samples)
                for subset in sorted({sample.subset for sample in self.samples if sample.subset})
            },
        }

    def with_resolved_labels(self) -> "DatasetManifest":
        label_by_name = {label.class_name: label.class_id for label in self.labels}
        samples = tuple(
            replace(
                sample,
                annotations=tuple(
                    replace(annotation, class_id=label_by_name.get(annotation.class_name, annotation.class_id))
                    for annotation in sample.annotations
                ),
            )
            for sample in self.samples
        )
        return replace(self, samples=samples)


