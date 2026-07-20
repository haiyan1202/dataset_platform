from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from celery.utils.log import get_task_logger
from sqlalchemy import delete, select

from dataset_core.errors import DatasetCoreError
from dataset_core.parsers import ZipScanPolicy, archive_suffix, inspect_dataset_reader, scan_dataset_archive
from dataset_core.parsers.zip_scanner import ArchiveEntry, ArchiveReader, open_archive, validated_entries
from app.db import SessionLocal
from app.models import (
    AnnotationIndex,
    Asset,
    DatasetVersion,
    ImportBatch,
    Job,
    KeypointDefinition,
    LabelDefinition,
    QualityIssue,
    Sample,
    SampleClassIndex,
    UploadSession,
)
from app.services import job_is_cancelled, transition_job
from app.settings import get_settings
from app.storage import get_storage
from .celery_app import celery_app
from .export_task import create_export  # noqa: F401

logger = get_task_logger(__name__)


def _set_failed(db, job: Job, code: str, detail: dict | None = None) -> None:
    transition_job(job, status="failed", stage="failed", error_code=code, error_detail=detail)
    db.commit()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_path(temp_dir: str, original_name: str) -> Path:
    return Path(temp_dir) / f"upload{archive_suffix(original_name)}"


def _work_dir() -> Path:
    """Use only the Worker-owned temporary directory, never an input host path."""
    path = Path(os.environ.get("WORKER_TMP_DIR", "/tmp/dataset-worker"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _require_temp_space(directory: Path, required_bytes: int) -> None:
    """Fail visibly before a large archive consumes the Worker filesystem."""
    available = shutil.disk_usage(directory).free
    reserve = get_settings().worker_temp_reserve_bytes
    if available < required_bytes + reserve:
        raise DatasetCoreError(
            "import.insufficient_temp_space",
            params={"required_bytes": required_bytes + reserve, "available_bytes": available},
        )


def _raw_asset(
    db,
    *,
    storage,
    upload: UploadSession,
    version: DatasetVersion,
    archive: ArchiveReader,
    entries: dict[str, ArchiveEntry],
    relative_path: str,
    asset_type: str,
) -> Asset:
    object_key = f"org/{upload.organization_id}/datasets/{upload.dataset_id}/versions/{version.id}/raw/{relative_path}"
    asset = db.scalar(select(Asset).where(Asset.object_key == object_key))
    if asset:
        return asset
    entry = entries[relative_path]
    content = archive.read(entry)
    storage.put_bytes(upload.bucket, object_key, content, mimetypes.guess_type(relative_path)[0])
    asset = Asset(
        organization_id=upload.organization_id,
        bucket=upload.bucket,
        object_key=object_key,
        original_name=PurePosixPath(relative_path).name,
        relative_path=relative_path,
        asset_type=asset_type,
        content_type=mimetypes.guess_type(relative_path)[0],
        size_bytes=len(content),
        checksum_algorithm="sha256",
        checksum=hashlib.sha256(content).hexdigest(),
    )
    db.add(asset)
    db.flush()
    return asset


def _normalized_asset(db, *, storage, upload: UploadSession, version: DatasetVersion, sample) -> Asset:
    normalized_relative_path = f"normalized/{sample.relative_path}.json"
    object_key = f"org/{upload.organization_id}/datasets/{upload.dataset_id}/versions/{version.id}/{normalized_relative_path}"
    asset = db.scalar(select(Asset).where(Asset.object_key == object_key))
    if asset:
        return asset
    content = json.dumps(sample.normalized_annotation(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    storage.put_bytes(upload.bucket, object_key, content, "application/json")
    asset = Asset(
        organization_id=upload.organization_id,
        bucket=upload.bucket,
        object_key=object_key,
        original_name=f"{PurePosixPath(sample.relative_path).stem}.normalized.json",
        relative_path=normalized_relative_path,
        asset_type="normalized_annotation",
        content_type="application/json",
        size_bytes=len(content),
        checksum_algorithm="sha256",
        checksum=hashlib.sha256(content).hexdigest(),
    )
    db.add(asset)
    db.flush()
    return asset


def _upsert_labels(db, *, version: DatasetVersion, labels) -> None:
    existing = {
        label.class_id: label
        for label in db.scalars(select(LabelDefinition).where(LabelDefinition.dataset_version_id == version.id))
    }
    for label in labels:
        current = existing.get(label.class_id)
        if current is None:
            db.add(LabelDefinition(
                dataset_version_id=version.id,
                class_id=label.class_id,
                class_name=label.class_name,
                color=label.color,
            ))
        elif current.class_name != label.class_name or current.color != label.color:
            current.class_name = label.class_name
            current.color = label.color


def _upsert_keypoints(db, *, version: DatasetVersion, samples) -> None:
    existing = {
        (item.class_id, item.point_index)
        for item in db.scalars(select(KeypointDefinition).where(KeypointDefinition.dataset_version_id == version.id))
    }
    required = {
        (annotation.class_id, point_index)
        for sample in samples
        for annotation in sample.annotations
        for point_index, _point in enumerate(annotation.keypoints)
        if annotation.class_id >= 0
    }
    for class_id, point_index in sorted(required):
        if (class_id, point_index) not in existing:
            db.add(KeypointDefinition(
                dataset_version_id=version.id,
                class_id=class_id,
                point_index=point_index,
                point_name=f"keypoint_{point_index}",
            ))

@celery_app.task(bind=True, name="dataset_platform.scan_upload", autoretry_for=(), max_retries=0)
def scan_upload(self, job_id: str) -> dict:
    """Idempotently inspect a supported archive via Worker temporary storage."""
    with SessionLocal() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return {"status": "missing"}
        if job.status in {"succeeded", "cancelled"}:
            return {"status": job.status}
        upload = db.get(UploadSession, job.resource_id)
        if upload is None:
            _set_failed(db, job, "upload.not_found")
            return {"status": "failed"}
        try:
            transition_job(job, status="running", stage="download", current=0, total=3)
            upload.status = "scanning"
            db.commit()
            if job_is_cancelled(db, job):
                return {"status": "cancelled"}
            storage = get_storage()
            metadata = storage.stat(upload.bucket, upload.object_key)
            _require_temp_space(_work_dir(), metadata.size_bytes)
            with tempfile.TemporaryDirectory(dir=_work_dir()) as temp_dir:
                archive_path = _download_path(temp_dir, upload.original_name)
                storage.download_to_file(upload.bucket, upload.object_key, str(archive_path))
                actual_checksum = _sha256_file(archive_path)

                if upload.checksum and upload.checksum.lower() != actual_checksum:
                    raise DatasetCoreError("upload.checksum_mismatch")
                upload.checksum = actual_checksum
                # Pre-confirmation is intentionally metadata-only. Parsing every
                # annotation here made large/solid 7z archives look permanently
                # pending before users could even start the import.
                transition_job(job, status="running", stage="scan", current=1, total=3)
                db.commit()
                preview = scan_dataset_archive(str(archive_path)).to_preview()
            transition_job(job, status="running", stage="finalize", current=2, total=3)
            upload.preview_json = preview
            upload.status = "waiting_confirmation"
            upload.size_bytes = metadata.size_bytes
            batch = db.get(ImportBatch, upload.import_batch_id)
            if batch:
                batch.status = "waiting_confirmation"
                batch.meta_json = {"preview": preview}
            transition_job(job, status="succeeded", stage="waiting_confirmation", current=3, total=3, result=preview)
            db.commit()
            return preview
        except DatasetCoreError as exc:
            upload.status = "scan_failed"
            _set_failed(db, job, exc.code, exc.params)
            db.commit()
            return {"status": "failed", "error_code": exc.code}
        except Exception:
            logger.exception("scan_upload failed job=%s", job_id)
            upload.status = "scan_failed"
            _set_failed(db, job, "import.scan_failed")
            db.commit()
            return {"status": "failed", "error_code": "import.scan_failed"}


@celery_app.task(bind=True, name="dataset_platform.confirm_import", autoretry_for=(), max_retries=0)
def confirm_import(self, job_id: str) -> dict:
    """Materialize parsed raw and normalized assets without storing host paths in the DB."""
    with SessionLocal() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None or job.status in {"succeeded", "cancelled"}:
            return {"status": "missing_or_terminal"}
        upload = db.get(UploadSession, job.resource_id)
        if upload is None or upload.status != "importing":
            _set_failed(db, job, "import.invalid_state")
            return {"status": "failed"}
        try:
            if job_is_cancelled(db, job):
                return {"status": "cancelled"}
            storage = get_storage()
            with tempfile.TemporaryDirectory(dir=_work_dir()) as temp_dir:
                archive_path = _download_path(temp_dir, upload.original_name)
                metadata = storage.stat(upload.bucket, upload.object_key)
                _require_temp_space(_work_dir(), metadata.size_bytes)
                storage.download_to_file(upload.bucket, upload.object_key, str(archive_path))
                archive = open_archive(archive_path)
                entries = validated_entries(archive, ZipScanPolicy())
                # 7z has no efficient random member reads. Extract its already
                # validated members once under this Worker TemporaryDirectory;
                # ZIP/TAR readers keep streaming from the archive unchanged.
                _require_temp_space(Path(temp_dir), archive.materialization_bytes(entries))
                archive.prepare_for_reading(Path(temp_dir) / "archive-members", entries)
                manifest = inspect_dataset_reader(archive, entries)
                version = db.get(DatasetVersion, upload.dataset_version_id)
                batch = db.get(ImportBatch, upload.import_batch_id)
                if version is None or batch is None:
                    _set_failed(db, job, "import.related_resource_missing")
                    return {"status": "failed"}
                _upsert_labels(db, version=version, labels=manifest.labels)

                _upsert_keypoints(db, version=version, samples=manifest.samples)
                transition_job(job, status="running", stage="materialize", current=0, total=max(len(manifest.samples), 1))
                db.commit()
                created = 0
                for index, parsed_sample in enumerate(manifest.samples, 1):
                    if job_is_cancelled(db, job):
                        db.commit()
                        return {"status": "cancelled", "created_samples": created}
                    existing = db.scalar(select(Sample).where(
                        Sample.dataset_version_id == version.id,
                        Sample.relative_path == parsed_sample.relative_path,
                    ))
                    if existing:
                        continue
                    image_asset = _raw_asset(
                        db,
                        storage=storage,
                        upload=upload,
                        version=version,
                        archive=archive,
                        entries=entries,
                        relative_path=parsed_sample.relative_path,
                        asset_type="image",
                    )
                    annotation_asset = None
                    if parsed_sample.annotation_path and parsed_sample.annotation_path in entries:
                        annotation_asset = _raw_asset(
                            db,
                            storage=storage,
                            upload=upload,
                            version=version,
                            archive=archive,
                            entries=entries,
                            relative_path=parsed_sample.annotation_path,
                            asset_type="annotation",
                        )
                    normalized_asset = _normalized_asset(
                        db,
                        storage=storage,
                        upload=upload,
                        version=version,
                        sample=parsed_sample,
                    )
                    path = PurePosixPath(parsed_sample.relative_path)
                    sample = Sample(
                        dataset_version_id=version.id,
                        import_batch_id=batch.id,
                        image_asset_id=image_asset.id,
                        annotation_asset_id=annotation_asset.id if annotation_asset else None,
                        file_name=path.name,
                        file_stem=path.stem,
                        relative_path=parsed_sample.relative_path,
                        subset=parsed_sample.subset,
                        annotation_type=parsed_sample.annotation_type,
                        width=parsed_sample.width,
                        height=parsed_sample.height,
                    )
                    db.add(sample)
                    db.flush()
                    summary = parsed_sample.summary
                    for class_id in summary.class_ids:
                        db.add(SampleClassIndex(sample_id=sample.id, class_id=class_id))
                    db.add(AnnotationIndex(
                        sample_id=sample.id,
                        annotation_count=summary.annotation_count,
                        bbox_count=summary.bbox_count,
                        polygon_count=summary.polygon_count,
                        keypoint_count=summary.keypoint_count,
                        class_ids_json=list(summary.class_ids),
                        class_counts_json={str(class_id): sum(annotation.class_id == class_id for annotation in parsed_sample.annotations) for class_id in summary.class_ids},
                        normalized_annotation_asset_id=normalized_asset.id,
                        parser_name=manifest.parser_name,
                        parser_version="1",
                    ))
                    created += 1
                    transition_job(job, status="running", stage="materialize", current=index, total=max(len(manifest.samples), 1))
                    if index % 100 == 0:
                        db.commit()
                archive.close()
            version.status = "ready"
            batch.status = "ready"
            upload.status = "ready"
            transition_job(job, status="succeeded", stage="ready", current=max(len(manifest.samples), 1), total=max(len(manifest.samples), 1), result={"created_samples": created, "parser_name": manifest.parser_name})
            db.commit()
            return job.result_json or {}
        except DatasetCoreError as exc:
            _set_failed(db, job, exc.code, exc.params)
            return {"status": "failed", "error_code": exc.code}
        except Exception:
            logger.exception("confirm_import failed job=%s", job_id)
            _set_failed(db, job, "import.materialization_failed")
            return {"status": "failed", "error_code": "import.materialization_failed"}


@celery_app.task(name="dataset_platform.run_quality_check")
def run_quality_check(job_id: str) -> dict:
    """Run durable annotation, integrity, and duplicate-content checks for a dataset version."""
    with SessionLocal() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None or job.status == "succeeded":
            return {"status": "missing_or_terminal"}
        samples = list(db.scalars(select(Sample).where(
            Sample.dataset_version_id == job.resource_id,
            Sample.deleted_at.is_(None),
        )))
        db.execute(delete(QualityIssue).where(
            QualityIssue.dataset_version_id == job.resource_id,
            QualityIssue.checker_version.in_(["1", "2"]),
        ))
        transition_job(job, status="running", stage="validate", current=0, total=max(len(samples), 1))
        storage = get_storage()
        issue_count = 0
        checksum_samples: dict[str, list[Sample]] = {}

        def add_issue(sample: Sample, issue_type: str, severity: str, detail_code: str, detail: dict | None = None) -> None:
            nonlocal issue_count
            db.add(QualityIssue(
                dataset_version_id=sample.dataset_version_id,
                sample_id=sample.id,
                issue_type=issue_type,
                severity=severity,
                detail_code=detail_code,
                detail_json=detail or {},
                checker_version="2",
            ))
            issue_count += 1

        for index, sample in enumerate(samples, 1):
            if job_is_cancelled(db, job):
                db.commit()
                return {"status": "cancelled"}
            image = db.get(Asset, sample.image_asset_id)
            if image and image.checksum:
                checksum_samples.setdefault(image.checksum, []).append(sample)
            if sample.annotation_asset_id is None:
                add_issue(sample, "missing_annotation", "warning", "quality.missing_annotation")
                transition_job(job, status="running", stage="validate", current=index, total=max(len(samples), 1))
                continue
            annotation_index = db.get(AnnotationIndex, sample.id)
            if annotation_index is None or annotation_index.annotation_count == 0:
                add_issue(sample, "empty_annotation", "warning", "quality.empty_annotation")
            normalized_asset = db.get(Asset, annotation_index.normalized_annotation_asset_id) if annotation_index else None
            if normalized_asset is None:
                add_issue(sample, "missing_normalized_annotation", "error", "quality.normalized_annotation_missing")
                transition_job(job, status="running", stage="validate", current=index, total=max(len(samples), 1))
                continue
            try:
                normalized = json.loads(storage.read_bytes(normalized_asset.bucket, normalized_asset.object_key))
                annotations = normalized.get("annotations", [])
                if not isinstance(annotations, list):
                    raise ValueError("annotations is not a list")
            except Exception:
                add_issue(sample, "invalid_normalized_annotation", "error", "quality.normalized_annotation_invalid")
                transition_job(job, status="running", stage="validate", current=index, total=max(len(samples), 1))
                continue
            for annotation_number, annotation in enumerate(annotations, 1):
                if not isinstance(annotation, dict):
                    add_issue(sample, "invalid_annotation", "error", "quality.annotation_invalid", {"index": annotation_number})
                    continue
                coordinate_space = annotation.get("coordinate_space")
                points: list[float] = []
                for value in annotation.get("bbox") or []:
                    if isinstance(value, (int, float)):
                        points.append(float(value))
                for point in annotation.get("polygon") or []:
                    if isinstance(point, list):
                        points.extend(float(value) for value in point[:2] if isinstance(value, (int, float)))
                for point in annotation.get("keypoints") or []:
                    if isinstance(point, list):
                        points.extend(float(value) for value in point[:2] if isinstance(value, (int, float)))
                if any(not math.isfinite(value) for value in points):
                    add_issue(sample, "invalid_coordinate", "error", "quality.coordinate_not_finite", {"index": annotation_number})
                    continue
                if coordinate_space == "normalized" and any(value < 0 or value > 1 for value in points):
                    add_issue(sample, "out_of_bounds_annotation", "warning", "quality.coordinate_out_of_bounds", {"index": annotation_number})
            transition_job(job, status="running", stage="validate", current=index, total=max(len(samples), 1))

        for checksum, matching_samples in checksum_samples.items():
            if len(matching_samples) < 2:
                continue
            for sample in matching_samples:
                add_issue(
                    sample,
                    "duplicate_image_checksum",
                    "warning",
                    "quality.duplicate_image_checksum",
                    {"checksum": checksum, "matching_sample_ids": [str(item.id) for item in matching_samples if item.id != sample.id]},
                )
        transition_job(
            job,
            status="succeeded",
            stage="done",
            current=len(samples),
            total=max(len(samples), 1),
            result={"checked_samples": len(samples), "issue_count": issue_count, "checker_version": "2"},
        )
        db.commit()
        return job.result_json or {}