from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, Dataset, DatasetVersion, ImportBatch, Job, UploadSession
from dataset_core.parsers import archive_suffix


def audit(
    db: Session,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    after: dict | None = None,
    request_id: str | None = None,
) -> None:
    db.add(AuditLog(
        organization_id=organization_id,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        after_json=after,
        request_id=request_id,
    ))


def get_dataset_for_org(db: Session, dataset_id: uuid.UUID, organization_id: uuid.UUID) -> Dataset | None:
    return db.scalar(select(Dataset).where(
        Dataset.id == dataset_id,
        Dataset.organization_id == organization_id,
        Dataset.deleted_at.is_(None),
    ))


def create_upload_session(
    db: Session,
    *,
    dataset: Dataset,
    user_id: uuid.UUID,
    original_name: str,
    checksum: str | None,
    batch_name: str,
    bucket: str,
    idempotency_key: str | None = None,
) -> tuple[UploadSession, Job]:
    highest_version = db.scalar(
        select(func.max(DatasetVersion.version_number)).where(DatasetVersion.dataset_id == dataset.id)
    ) or 0
    version = DatasetVersion(
        dataset_id=dataset.id,
        version_number=highest_version + 1,
        status="draft",
        created_by=user_id,
    )
    db.add(version)
    db.flush()
    batch = ImportBatch(
        dataset_version_id=version.id,
        batch_number=1,
        batch_name=batch_name,
        status="uploading",
        created_by=user_id,
    )
    db.add(batch)
    db.flush()
    session_id = uuid.uuid4()
    object_key = f"org/{dataset.organization_id}/datasets/{dataset.id}/uploads/{session_id}{archive_suffix(original_name)}"
    upload = UploadSession(
        id=session_id,
        organization_id=dataset.organization_id,
        dataset_id=dataset.id,
        dataset_version_id=version.id,
        import_batch_id=batch.id,
        bucket=bucket,
        object_key=object_key,
        original_name=original_name,
        idempotency_key=idempotency_key,
        checksum=checksum,
        status="created",
        created_by=user_id,
    )
    job = Job(
        organization_id=dataset.organization_id,
        job_type="scan_upload",
        resource_type="upload_session",
        resource_id=session_id,
        idempotency_key=f"scan-upload:{session_id}",
        status="pending",
        requested_by=user_id,
    )
    db.add_all([upload, job])
    audit(
        db,
        organization_id=dataset.organization_id,
        user_id=user_id,
        action="upload_session.create",
        resource_type="upload_session",
        resource_id=session_id,
        after={"object_key": object_key, "dataset_id": str(dataset.id)},
    )
    return upload, job

def transition_job(
    job: Job,
    *,
    status: str,
    stage: str | None = None,
    current: int | None = None,
    total: int | None = None,
    result: dict | None = None,
    error_code: str | None = None,
    error_detail: dict | None = None,
) -> None:
    job.status = status
    job.stage = stage
    if current is not None:
        job.current = current
    if total is not None:
        job.total = total
        job.progress = int((job.current / total) * 100) if total else 0
    if result is not None:
        job.result_json = result
    if error_code is not None:
        job.error_code = error_code
        job.error_detail = error_detail or {}
    if status == "running" and job.started_at is None:
        job.started_at = datetime.now(timezone.utc)
    if status in {"succeeded", "failed", "cancelled"}:
        job.finished_at = datetime.now(timezone.utc)





def job_is_cancelled(db: Session, job: Job) -> bool:
    """Refresh durable Job state so independent API cancellation is observed by Workers."""
    db.refresh(job)
    return job.status == "cancelled"
