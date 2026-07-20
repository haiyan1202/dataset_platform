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
from sqlalchemy import delete, select, update

from dataset_core.errors import DatasetCoreError
from dataset_core.parsers import (
    ZipScanPolicy,
    archive_suffix,
    inspect_dataset_reader,
    scan_dataset_archive,
)
from dataset_core.parsers.zip_scanner import (
    ArchiveEntry,
    ArchiveReader,
    open_archive,
    validated_entries,
)
from app.db import SessionLocal
from app.models import (
    AnnotationIndex,
    Asset,
    AuditLog,
    Dataset,
    DatasetVersion,
    ImportBatch,
    Job,
    KeypointDefinition,
    LabelDefinition,
    OperationHistory,
    QualityIssue,
    Sample,
    SampleClassIndex,
    UploadSession,
)
from app.services import job_is_cancelled, transition_job
from app.settings import get_settings
from app.services.purge_service import (
    mark_worker_temp_directory,
    remove_stale_worker_temp_directories,
    collect_dataset_purge_inventory,
)
from app.storage import get_storage
from .celery_app import celery_app
from .export_task import create_export  # noqa: F401

logger = get_task_logger(__name__)


def _set_failed(db, job: Job, code: str, detail: dict | None = None) -> None:
    transition_job(job, status="failed", stage="failed", error_code=code, error_detail=detail)
    db.commit()


def _set_import_failed(db, job_id: uuid.UUID, code: str, detail: dict | None = None) -> None:
    """Recover the session after a transaction failure and expose a retryable import state."""
    db.rollback()
    job = db.get(Job, job_id)
    if job is None:
        return
    upload = db.get(UploadSession, job.resource_id)
    if upload is not None:
        upload.status = "import_failed"
        batch = db.get(ImportBatch, upload.import_batch_id)
        if batch is not None:
            batch.status = "import_failed"
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
    assets_by_key: dict[str, Asset],
) -> Asset:
    object_key = f"org/{upload.organization_id}/datasets/{upload.dataset_id}/versions/{version.id}/raw/{relative_path}"
    cached = assets_by_key.get(object_key)
    if cached is not None:
        return cached
    entry = entries[relative_path]
    content_type = mimetypes.guess_type(relative_path)[0]
    source_path = archive.materialized_path(entry)
    if source_path is not None:
        storage.upload_file(upload.bucket, object_key, str(source_path), content_type)
        size_bytes = source_path.stat().st_size
        checksum = _sha256_file(source_path)
    else:
        content = archive.read(entry)
        storage.put_bytes(upload.bucket, object_key, content, content_type)
        size_bytes = len(content)
        checksum = hashlib.sha256(content).hexdigest()
    asset = Asset(
        id=uuid.uuid4(),
        organization_id=upload.organization_id,
        bucket=upload.bucket,
        object_key=object_key,
        original_name=PurePosixPath(relative_path).name,
        relative_path=relative_path,
        asset_type=asset_type,
        content_type=content_type,
        size_bytes=size_bytes,
        checksum_algorithm="sha256",
        checksum=checksum,
    )
    db.add(asset)
    assets_by_key[object_key] = asset
    return asset


def _normalized_asset(
    db,
    *,
    storage,
    upload: UploadSession,
    version: DatasetVersion,
    sample,
    assets_by_key: dict[str, Asset],
) -> Asset:
    normalized_relative_path = f"normalized/{sample.relative_path}.json"
    object_key = f"org/{upload.organization_id}/datasets/{upload.dataset_id}/versions/{version.id}/{normalized_relative_path}"
    cached = assets_by_key.get(object_key)
    if cached is not None:
        return cached
    content = json.dumps(
        sample.normalized_annotation(), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    storage.put_bytes(upload.bucket, object_key, content, "application/json")
    asset = Asset(
        id=uuid.uuid4(),
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
    assets_by_key[object_key] = asset
    return asset


def _upsert_labels(db, *, version: DatasetVersion, labels) -> None:
    existing = {
        label.class_id: label
        for label in db.scalars(
            select(LabelDefinition).where(LabelDefinition.dataset_version_id == version.id)
        )
    }
    for label in labels:
        current = existing.get(label.class_id)
        if current is None:
            db.add(
                LabelDefinition(
                    dataset_version_id=version.id,
                    class_id=label.class_id,
                    class_name=label.class_name,
                    color=label.color,
                )
            )
        elif current.class_name != label.class_name or current.color != label.color:
            current.class_name = label.class_name
            current.color = label.color


def _upsert_keypoints(db, *, version: DatasetVersion, samples) -> None:
    existing = {
        (item.class_id, item.point_index)
        for item in db.scalars(
            select(KeypointDefinition).where(KeypointDefinition.dataset_version_id == version.id)
        )
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
            db.add(
                KeypointDefinition(
                    dataset_version_id=version.id,
                    class_id=class_id,
                    point_index=point_index,
                    point_name=f"keypoint_{point_index}",
                )
            )


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
                mark_worker_temp_directory(temp_dir, job.id)
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
                batch.meta_json = {
                    "preview": preview,
                    "rescan": bool((batch.meta_json or {}).get("rescan")),
                }
            transition_job(
                job,
                status="succeeded",
                stage="waiting_confirmation",
                current=3,
                total=3,
                result=preview,
            )
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
            transition_job(job, status="running", stage="download", current=0, total=4)
            db.commit()
            with tempfile.TemporaryDirectory(dir=_work_dir()) as temp_dir:
                mark_worker_temp_directory(temp_dir, job.id)
                archive_path = _download_path(temp_dir, upload.original_name)
                metadata = storage.stat(upload.bucket, upload.object_key)
                _require_temp_space(_work_dir(), metadata.size_bytes)
                storage.download_to_file(upload.bucket, upload.object_key, str(archive_path))
                transition_job(job, status="running", stage="prepare_archive", current=1, total=4)
                db.commit()
                archive = open_archive(archive_path)
                entries = validated_entries(archive, ZipScanPolicy())
                # 7z has no efficient random member reads. Extract its already
                # validated members once under this Worker TemporaryDirectory;
                # ZIP/TAR readers keep streaming from the archive unchanged.
                _require_temp_space(Path(temp_dir), archive.materialization_bytes(entries))
                archive.prepare_for_reading(Path(temp_dir) / "archive-members", entries)
                transition_job(job, status="running", stage="parse_annotations", current=2, total=4)
                db.commit()
                manifest = inspect_dataset_reader(archive, entries)
                version = db.get(DatasetVersion, upload.dataset_version_id)
                batch = db.get(ImportBatch, upload.import_batch_id)
                if version is None or batch is None:
                    _set_failed(db, job, "import.related_resource_missing")
                    return {"status": "failed"}
                _upsert_labels(db, version=version, labels=manifest.labels)
                _upsert_keypoints(db, version=version, samples=manifest.samples)
                total_samples = max(len(manifest.samples), 1)
                batch_size = max(1, min(get_settings().import_commit_batch_size, 1_000))
                existing_samples = {
                    sample.relative_path: sample
                    for sample in db.scalars(
                        select(Sample).where(Sample.dataset_version_id == version.id)
                    )
                }
                # New rescans record this flag. The job lookup also recognizes scans
                # initiated before the flag existed, so a retry repairs their stale
                # subset values instead of leaving train paths marked as test.
                is_rescan = (
                    bool((batch.meta_json or {}).get("rescan"))
                    or db.scalar(
                        select(Job.id)
                        .where(
                            Job.resource_id == upload.id,
                            Job.job_type == "scan_upload",
                            Job.idempotency_key.like(f"rescan-upload:{upload.id}:%"),
                            Job.status == "succeeded",
                        )
                        .order_by(Job.created_at.desc())
                        .limit(1)
                    )
                    is not None
                )
                object_prefix = f"org/{upload.organization_id}/datasets/{upload.dataset_id}/versions/{version.id}/"
                assets_by_key = {
                    asset.object_key: asset
                    for asset in db.scalars(
                        select(Asset).where(Asset.object_key.like(f"{object_prefix}%"))
                    )
                }
                transition_job(
                    job, status="running", stage="materialize", current=0, total=total_samples
                )
                db.commit()
                created = 0
                reconciled = 0
                pending_sample_class_indexes: list[SampleClassIndex] = []
                pending_annotation_indexes: list[AnnotationIndex] = []
                for index, parsed_sample in enumerate(manifest.samples, 1):
                    existing_sample = existing_samples.get(parsed_sample.relative_path)
                    if existing_sample is not None:
                        if is_rescan and existing_sample.import_batch_id == batch.id:
                            existing_sample.subset = parsed_sample.subset
                            existing_sample.annotation_type = parsed_sample.annotation_type
                            existing_sample.width = parsed_sample.width
                            existing_sample.height = parsed_sample.height
                            reconciled += 1
                        continue
                    else:
                        image_asset = _raw_asset(
                            db,
                            storage=storage,
                            upload=upload,
                            version=version,
                            archive=archive,
                            entries=entries,
                            relative_path=parsed_sample.relative_path,
                            asset_type="image",
                            assets_by_key=assets_by_key,
                        )
                        annotation_asset = None
                        if (
                            parsed_sample.annotation_path
                            and parsed_sample.annotation_path in entries
                        ):
                            annotation_asset = _raw_asset(
                                db,
                                storage=storage,
                                upload=upload,
                                version=version,
                                archive=archive,
                                entries=entries,
                                relative_path=parsed_sample.annotation_path,
                                asset_type="annotation",
                                assets_by_key=assets_by_key,
                            )
                        normalized_asset = _normalized_asset(
                            db,
                            storage=storage,
                            upload=upload,
                            version=version,
                            sample=parsed_sample,
                            assets_by_key=assets_by_key,
                        )
                        path = PurePosixPath(parsed_sample.relative_path)
                        sample = Sample(
                            id=uuid.uuid4(),
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
                        summary = parsed_sample.summary
                        for class_id in summary.class_ids:
                            pending_sample_class_indexes.append(
                                SampleClassIndex(sample_id=sample.id, class_id=class_id)
                            )
                        pending_annotation_indexes.append(
                            AnnotationIndex(
                                sample_id=sample.id,
                                annotation_count=summary.annotation_count,
                                bbox_count=summary.bbox_count,
                                polygon_count=summary.polygon_count,
                                keypoint_count=summary.keypoint_count,
                                class_ids_json=list(summary.class_ids),
                                class_counts_json={
                                    str(class_id): sum(
                                        annotation.class_id == class_id
                                        for annotation in parsed_sample.annotations
                                    )
                                    for class_id in summary.class_ids
                                },
                                normalized_annotation_asset_id=normalized_asset.id,
                                parser_name=manifest.parser_name,
                                parser_version="1",
                            )
                        )
                        existing_samples[parsed_sample.relative_path] = sample
                        created += 1
                    if index % batch_size == 0 or index == len(manifest.samples):
                        # PostgreSQL enforces these foreign keys immediately. Flush
                        # assets/samples first, then insert their index rows in the
                        # same transaction as the batched progress update.
                        db.flush()
                        db.add_all(pending_sample_class_indexes)
                        db.add_all(pending_annotation_indexes)
                        pending_sample_class_indexes.clear()
                        pending_annotation_indexes.clear()
                        if job_is_cancelled(db, job):
                            db.commit()
                            return {"status": "cancelled", "created_samples": created}
                        transition_job(
                            job,
                            status="running",
                            stage="materialize",
                            current=index,
                            total=total_samples,
                        )
                        db.commit()
                archive.close()
            version.status = "ready"
            batch.status = "ready"
            batch.meta_json = {**(batch.meta_json or {}), "rescan": False}
            upload.status = "ready"
            transition_job(
                job,
                status="succeeded",
                stage="ready",
                current=max(len(manifest.samples), 1),
                total=max(len(manifest.samples), 1),
                result={
                    "created_samples": created,
                    "reconciled_samples": reconciled,
                    "parser_name": manifest.parser_name,
                },
            )
            db.commit()
            return job.result_json or {}
        except DatasetCoreError as exc:
            _set_import_failed(db, uuid.UUID(job_id), exc.code, exc.params)
            return {"status": "failed", "error_code": exc.code}
        except Exception:
            logger.exception("confirm_import failed job=%s", job_id)
            _set_import_failed(db, uuid.UUID(job_id), "import.materialization_failed")
            return {"status": "failed", "error_code": "import.materialization_failed"}


def _purge_dataset_records(db, *, dataset: Dataset, inventory, keep_job_id: uuid.UUID) -> None:
    version_ids = list(inventory.version_ids)
    upload_ids = list(inventory.upload_ids)
    batch_ids = list(inventory.batch_ids)
    sample_ids = (
        select(Sample.id).where(Sample.dataset_version_id.in_(version_ids))
        if version_ids
        else select(Sample.id).where(False)
    )

    if version_ids:
        db.execute(delete(QualityIssue).where(QualityIssue.dataset_version_id.in_(version_ids)))
        db.execute(delete(SampleClassIndex).where(SampleClassIndex.sample_id.in_(sample_ids)))
        db.execute(delete(AnnotationIndex).where(AnnotationIndex.sample_id.in_(sample_ids)))
        db.execute(delete(Sample).where(Sample.dataset_version_id.in_(version_ids)))
        db.execute(
            delete(KeypointDefinition).where(KeypointDefinition.dataset_version_id.in_(version_ids))
        )
        db.execute(
            delete(LabelDefinition).where(LabelDefinition.dataset_version_id.in_(version_ids))
        )
    if inventory.asset_ids:
        db.execute(delete(Asset).where(Asset.id.in_(inventory.asset_ids)))

    related_resource_ids = [
        dataset.id,
        *version_ids,
        *upload_ids,
        *batch_ids,
        *inventory.export_job_ids,
    ]
    if related_resource_ids:
        db.execute(delete(AuditLog).where(AuditLog.resource_id.in_(related_resource_ids)))
        db.execute(
            delete(Job).where(Job.id != keep_job_id, Job.resource_id.in_(related_resource_ids))
        )
    db.execute(delete(OperationHistory).where(OperationHistory.dataset_id == dataset.id))
    db.execute(delete(UploadSession).where(UploadSession.dataset_id == dataset.id))
    if batch_ids:
        db.execute(delete(ImportBatch).where(ImportBatch.id.in_(batch_ids)))
    if version_ids:
        db.execute(
            update(DatasetVersion)
            .where(DatasetVersion.id.in_(version_ids))
            .values(parent_version_id=None)
        )
        db.execute(delete(DatasetVersion).where(DatasetVersion.id.in_(version_ids)))
    db.delete(dataset)


@celery_app.task(bind=True, name="dataset_platform.purge_dataset", autoretry_for=(), max_retries=0)
def purge_dataset(self, job_id: str) -> dict:
    """Irreversibly remove a dataset's object-store data and relational records."""
    with SessionLocal() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None or job.status in {"succeeded", "cancelled"}:
            return {"status": "missing_or_terminal"}
        dataset = db.get(Dataset, job.resource_id)
        if dataset is None:
            _set_failed(db, job, "dataset.not_found")
            return {"status": "failed", "error_code": "dataset.not_found"}
        try:
            inventory = collect_dataset_purge_inventory(db, dataset)
            storage = get_storage()
            total_steps = max(len(inventory.object_refs) + 2, 1)
            transition_job(
                job, status="running", stage="purge_storage", current=0, total=total_steps
            )
            db.commit()
            released_object_bytes = 0
            deleted_objects = 0
            for index, object_ref in enumerate(inventory.object_refs, 1):
                if storage.object_exists(object_ref.bucket, object_ref.object_key):
                    metadata = storage.stat(object_ref.bucket, object_ref.object_key)
                    storage.delete(object_ref.bucket, object_ref.object_key)
                    released_object_bytes += metadata.size_bytes
                    deleted_objects += 1
                if index % 25 == 0 or index == len(inventory.object_refs):
                    transition_job(
                        job,
                        status="running",
                        stage="purge_storage",
                        current=index,
                        total=total_steps,
                    )
                    db.commit()

            transition_job(
                job,
                status="running",
                stage="purge_database",
                current=len(inventory.object_refs) + 1,
                total=total_steps,
            )
            _purge_dataset_records(db, dataset=dataset, inventory=inventory, keep_job_id=job.id)
            db.commit()

            active_job_ids = {
                str(item)
                for item in db.scalars(
                    select(Job.id).where(Job.status.in_(["queued", "pending", "running"]))
                )
            }
            removed_temp_dirs, released_temp_bytes = remove_stale_worker_temp_directories(
                active_job_ids
            )
            transition_job(
                job,
                status="succeeded",
                stage="purged",
                current=total_steps,
                total=total_steps,
                result={
                    "deleted_objects": deleted_objects,
                    "released_object_bytes": released_object_bytes,
                    "removed_temp_directories": removed_temp_dirs,
                    "released_temp_bytes": released_temp_bytes,
                    "deleted_samples": inventory.sample_count,
                },
            )
            db.commit()
            return job.result_json or {}
        except Exception:
            logger.exception("purge_dataset failed job=%s", job_id)
            db.rollback()
            failed_job = db.get(Job, uuid.UUID(job_id))
            failed_dataset = db.get(Dataset, job.resource_id)
            if failed_dataset is not None:
                failed_dataset.status = "purge_failed"
            if failed_job is not None:
                transition_job(
                    failed_job, status="failed", stage="failed", error_code="dataset.purge_failed"
                )
            db.commit()
            return {"status": "failed", "error_code": "dataset.purge_failed"}


@celery_app.task(name="dataset_platform.run_quality_check")
def run_quality_check(job_id: str) -> dict:
    """Run durable annotation, integrity, and duplicate-content checks for a dataset version."""
    with SessionLocal() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None or job.status == "succeeded":
            return {"status": "missing_or_terminal"}
        samples = list(
            db.scalars(
                select(Sample).where(
                    Sample.dataset_version_id == job.resource_id,
                    Sample.deleted_at.is_(None),
                )
            )
        )
        db.execute(
            delete(QualityIssue).where(
                QualityIssue.dataset_version_id == job.resource_id,
                QualityIssue.checker_version.in_(["1", "2"]),
            )
        )
        transition_job(
            job, status="running", stage="validate", current=0, total=max(len(samples), 1)
        )
        storage = get_storage()
        issue_count = 0
        checksum_samples: dict[str, list[Sample]] = {}

        def add_issue(
            sample: Sample,
            issue_type: str,
            severity: str,
            detail_code: str,
            detail: dict | None = None,
        ) -> None:
            nonlocal issue_count
            db.add(
                QualityIssue(
                    dataset_version_id=sample.dataset_version_id,
                    sample_id=sample.id,
                    issue_type=issue_type,
                    severity=severity,
                    detail_code=detail_code,
                    detail_json=detail or {},
                    checker_version="2",
                )
            )
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
                transition_job(
                    job,
                    status="running",
                    stage="validate",
                    current=index,
                    total=max(len(samples), 1),
                )
                continue
            annotation_index = db.get(AnnotationIndex, sample.id)
            if annotation_index is None or annotation_index.annotation_count == 0:
                add_issue(sample, "empty_annotation", "warning", "quality.empty_annotation")
            normalized_asset = (
                db.get(Asset, annotation_index.normalized_annotation_asset_id)
                if annotation_index
                else None
            )
            if normalized_asset is None:
                add_issue(
                    sample,
                    "missing_normalized_annotation",
                    "error",
                    "quality.normalized_annotation_missing",
                )
                transition_job(
                    job,
                    status="running",
                    stage="validate",
                    current=index,
                    total=max(len(samples), 1),
                )
                continue
            try:
                normalized = json.loads(
                    storage.read_bytes(normalized_asset.bucket, normalized_asset.object_key)
                )
                annotations = normalized.get("annotations", [])
                if not isinstance(annotations, list):
                    raise ValueError("annotations is not a list")
            except Exception:
                add_issue(
                    sample,
                    "invalid_normalized_annotation",
                    "error",
                    "quality.normalized_annotation_invalid",
                )
                transition_job(
                    job,
                    status="running",
                    stage="validate",
                    current=index,
                    total=max(len(samples), 1),
                )
                continue
            for annotation_number, annotation in enumerate(annotations, 1):
                if not isinstance(annotation, dict):
                    add_issue(
                        sample,
                        "invalid_annotation",
                        "error",
                        "quality.annotation_invalid",
                        {"index": annotation_number},
                    )
                    continue
                coordinate_space = annotation.get("coordinate_space")
                points: list[float] = []
                for value in annotation.get("bbox") or []:
                    if isinstance(value, (int, float)):
                        points.append(float(value))
                for point in annotation.get("polygon") or []:
                    if isinstance(point, list):
                        points.extend(
                            float(value) for value in point[:2] if isinstance(value, (int, float))
                        )
                for point in annotation.get("keypoints") or []:
                    if isinstance(point, list):
                        points.extend(
                            float(value) for value in point[:2] if isinstance(value, (int, float))
                        )
                if any(not math.isfinite(value) for value in points):
                    add_issue(
                        sample,
                        "invalid_coordinate",
                        "error",
                        "quality.coordinate_not_finite",
                        {"index": annotation_number},
                    )
                    continue
                if coordinate_space == "normalized" and any(
                    value < 0 or value > 1 for value in points
                ):
                    add_issue(
                        sample,
                        "out_of_bounds_annotation",
                        "warning",
                        "quality.coordinate_out_of_bounds",
                        {"index": annotation_number},
                    )
            transition_job(
                job, status="running", stage="validate", current=index, total=max(len(samples), 1)
            )

        for checksum, matching_samples in checksum_samples.items():
            if len(matching_samples) < 2:
                continue
            for sample in matching_samples:
                add_issue(
                    sample,
                    "duplicate_image_checksum",
                    "warning",
                    "quality.duplicate_image_checksum",
                    {
                        "checksum": checksum,
                        "matching_sample_ids": [
                            str(item.id) for item in matching_samples if item.id != sample.id
                        ],
                    },
                )
        transition_job(
            job,
            status="succeeded",
            stage="done",
            current=len(samples),
            total=max(len(samples), 1),
            result={
                "checked_samples": len(samples),
                "issue_count": issue_count,
                "checker_version": "2",
            },
        )
        db.commit()
        return job.result_json or {}
