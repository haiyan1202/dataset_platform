from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.auth import create_access_token, get_current_user, require_membership, verify_password
from app.db import get_db
from app.jobs.tasks import confirm_import, create_export, run_quality_check, scan_upload
from app.models import AnnotationIndex, Asset, AuditLog, Dataset, DatasetVersion, ImportBatch, Job, KeypointDefinition, LabelDefinition, Membership, OperationHistory, SampleClassIndex, Organization, QualityIssue, Sample, UploadSession, User
from app.schemas import (
    ActionJobOut, DatasetCreate, DatasetOut, ExportRequest, ImportBatchUpdate, JobOut, KeypointNamesUpdate,
    LabelUpdate, LoginRequest, OrganizationOut, Page, SampleBulkDelete, SampleOut, SampleSubsetUpdate,
    TokenResponse, UploadComplete, UploadSessionCreate, UploadSessionOut, UserOut,
)
from app.services import audit, create_upload_session, get_dataset_for_org, transition_job
from app.services.history_service import record_operation, redo_operation, undo_operation
from app.storage import get_storage
from app.settings import get_settings
from dataset_core.parsers import is_supported_archive_name

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def _job_for_org(db: Session, job_id: uuid.UUID, organization_id: uuid.UUID) -> Job:
    job = db.scalar(select(Job).where(Job.id == job_id, Job.organization_id == organization_id))
    if job is None:
        raise HTTPException(status_code=404, detail="job.not_found")
    return job


def _session_out(upload: UploadSession, upload_url: str | None = None) -> UploadSessionOut:
    return UploadSessionOut(
        id=upload.id,
        dataset_id=upload.dataset_id,
        import_batch_id=upload.import_batch_id,
        status=upload.status,
        object_key=upload.object_key,
        upload_url=upload_url,
        preview=upload.preview_json,
    )


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: DB) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth.invalid_credentials")
    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.get("/auth/me", response_model=UserOut)
def me(user: CurrentUser) -> User:
    return user


@router.get("/organizations", response_model=list[OrganizationOut])
def organizations(user: CurrentUser, db: DB) -> list[Organization]:
    return list(db.scalars(
        select(Organization).join(Membership, Membership.organization_id == Organization.id).where(
            Membership.user_id == user.id, Membership.status == "active"
        )
    ))


@router.get("/datasets", response_model=Page)
def list_datasets(organization_id: uuid.UUID, user: CurrentUser, db: DB, limit: int = 50, offset: int = 0) -> Page:
    require_membership(db, organization_id, user.id)
    limit = min(max(limit, 1), 100)
    base = select(Dataset).where(Dataset.organization_id == organization_id, Dataset.deleted_at.is_(None))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = list(db.scalars(base.order_by(Dataset.created_at.desc()).offset(offset).limit(limit)))
    return Page(items=[DatasetOut.model_validate(item).model_dump() for item in items], total=total, limit=limit, offset=offset)


@router.post("/datasets", response_model=DatasetOut, status_code=status.HTTP_201_CREATED)
def create_dataset(payload: DatasetCreate, request: Request, user: CurrentUser, db: DB) -> Dataset:
    require_membership(db, payload.organization_id, user.id, write=True)
    dataset = Dataset(
        organization_id=payload.organization_id,
        name=payload.name.strip(),
        description=payload.description,
        created_by=user.id,
    )
    db.add(dataset)
    db.flush()
    audit(
        db,
        organization_id=payload.organization_id,
        user_id=user.id,
        action="dataset.create",
        resource_type="dataset",
        resource_id=dataset.id,
        after={"name": dataset.name},
        request_id=request.headers.get("X-Request-ID"),
    )
    db.commit()
    db.refresh(dataset)
    return dataset


@router.get("/datasets/{dataset_id}", response_model=DatasetOut)
def get_dataset(dataset_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> Dataset:
    require_membership(db, organization_id, user.id)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    return dataset


@router.delete("/datasets/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dataset(dataset_id: uuid.UUID, organization_id: uuid.UUID, request: Request, user: CurrentUser, db: DB) -> None:
    require_membership(db, organization_id, user.id, write=True)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    from datetime import datetime, timezone
    dataset.deleted_at = datetime.now(timezone.utc)
    audit(db, organization_id=organization_id, user_id=user.id, action="dataset.soft_delete", resource_type="dataset", resource_id=dataset.id, request_id=request.headers.get("X-Request-ID"))
    db.commit()


@router.post("/datasets/{dataset_id}/upload-sessions", response_model=UploadSessionOut, status_code=status.HTTP_201_CREATED)
def create_dataset_upload(dataset_id: uuid.UUID, payload: UploadSessionCreate, user: CurrentUser, db: DB, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> UploadSessionOut:
    if not is_supported_archive_name(payload.original_name):
        raise HTTPException(status_code=422, detail="upload.archive_required")
    dataset = db.get(Dataset, dataset_id)
    if dataset is None or dataset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    require_membership(db, dataset.organization_id, user.id, write=True)
    settings = get_settings()
    storage = get_storage()
    if idempotency_key:
        existing = db.scalar(select(UploadSession).where(
            UploadSession.organization_id == dataset.organization_id,
            UploadSession.idempotency_key == idempotency_key,
        ))
        if existing:
            return _session_out(existing, storage.create_upload_url(existing.bucket, existing.object_key, timedelta(hours=1)))
    storage.ensure_bucket(settings.minio_bucket)
    upload, _job = create_upload_session(
        db,
        dataset=dataset,
        user_id=user.id,
        original_name=payload.original_name,
        checksum=payload.checksum,
        batch_name=payload.batch_name,
        bucket=settings.minio_bucket,
        idempotency_key=idempotency_key,
    )
    url = storage.create_upload_url(upload.bucket, upload.object_key, timedelta(hours=1))
    db.commit()
    return _session_out(upload, url)

@router.post("/upload-sessions/{upload_session_id}/complete", response_model=JobOut)
def complete_upload(upload_session_id: uuid.UUID, payload: UploadComplete, user: CurrentUser, db: DB) -> Job:
    upload = db.get(UploadSession, upload_session_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="upload.not_found")
    require_membership(db, upload.organization_id, user.id, write=True)
    job = db.scalar(select(Job).where(Job.resource_id == upload.id, Job.job_type == "scan_upload"))
    if job is None:
        raise HTTPException(status_code=500, detail="job.missing")
    if upload.status not in {"created", "uploading"}:
        if upload.status in {"uploaded", "scanning", "waiting_confirmation", "ready"}:
            return job
        raise HTTPException(status_code=409, detail="upload.invalid_state")
    storage = get_storage()
    if not storage.object_exists(upload.bucket, upload.object_key):
        raise HTTPException(status_code=409, detail="upload.object_missing")
    metadata = storage.stat(upload.bucket, upload.object_key)
    if metadata.size_bytes > get_settings().max_upload_bytes:
        raise HTTPException(status_code=413, detail="upload.too_large")
    upload.size_bytes = metadata.size_bytes
    upload.checksum = payload.checksum or upload.checksum
    upload.status = "uploaded"
    job.status = "queued"
    audit(
        db,
        organization_id=upload.organization_id,
        user_id=user.id,
        action="upload_session.complete",
        resource_type="upload_session",
        resource_id=upload.id,
        after={"size_bytes": metadata.size_bytes},
    )
    db.commit()
    scan_upload.delay(str(job.id))
    return job

@router.get("/upload-sessions/{upload_session_id}", response_model=UploadSessionOut)
def get_upload_session(upload_session_id: uuid.UUID, user: CurrentUser, db: DB) -> UploadSessionOut:
    upload = db.get(UploadSession, upload_session_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="upload.not_found")
    require_membership(db, upload.organization_id, user.id)
    return _session_out(upload)


@router.post("/upload-sessions/{upload_session_id}/confirm", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def confirm_upload(upload_session_id: uuid.UUID, user: CurrentUser, db: DB) -> Job:
    upload = db.get(UploadSession, upload_session_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="upload.not_found")
    require_membership(db, upload.organization_id, user.id, write=True)
    existing = db.scalar(select(Job).where(Job.resource_id == upload.id, Job.job_type == "import_upload"))
    if upload.status != "waiting_confirmation":
        if upload.status in {"importing", "ready"} and existing:
            return existing
        raise HTTPException(status_code=409, detail="import.confirmation_not_available")
    upload.status = "importing"
    job = Job(
        organization_id=upload.organization_id,
        job_type="import_upload",
        resource_type="upload_session",
        resource_id=upload.id,
        idempotency_key=f"import-upload:{upload.id}",
        status="queued",
        requested_by=user.id,
    )
    db.add(job)
    db.flush()
    audit(
        db,
        organization_id=upload.organization_id,
        user_id=user.id,
        action="upload_session.confirm_import",
        resource_type="upload_session",
        resource_id=upload.id,
        after={"job_id": str(job.id)},
    )
    db.commit()
    confirm_import.delay(str(job.id))
    return job

@router.get("/datasets/{dataset_id}/samples", response_model=Page)
def list_samples(
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    limit: int = 50,
    offset: int = 0,
    subset: str | None = None,
    annotation_type: str | None = None,
    class_id: int | None = None,
    file_name: str | None = None,
    import_batch_id: uuid.UUID | None = None,
    has_annotation: bool | None = None,
) -> Page:
    require_membership(db, organization_id, user.id)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    limit = min(max(limit, 1), 100)
    versions = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset_id)
    query = select(Sample).where(Sample.dataset_version_id.in_(versions), Sample.deleted_at.is_(None))
    if subset:
        query = query.where(Sample.subset == subset)
    if annotation_type:
        query = query.where(Sample.annotation_type == annotation_type)
    if class_id is not None:
        query = query.join(SampleClassIndex, SampleClassIndex.sample_id == Sample.id).where(SampleClassIndex.class_id == class_id)
    if file_name:
        query = query.where(Sample.file_name.ilike(f"%{file_name[:160]}%"))
    if import_batch_id:
        query = query.where(Sample.import_batch_id == import_batch_id)
    if has_annotation is True:
        query = query.where(Sample.annotation_asset_id.is_not(None))
    elif has_annotation is False:
        query = query.where(Sample.annotation_asset_id.is_(None))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    samples = list(db.scalars(query.order_by(Sample.created_at.desc()).offset(offset).limit(limit)))
    return Page(items=[SampleOut.model_validate(item).model_dump() for item in samples], total=total, limit=limit, offset=offset)

@router.post("/datasets/{dataset_id}/samples/subset")
def update_sample_subsets(
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    payload: SampleSubsetUpdate,
    user: CurrentUser,
    db: DB,
) -> dict:
    require_membership(db, organization_id, user.id, write=True)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    version_ids = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset.id)
    samples = list(db.scalars(select(Sample).where(
        Sample.id.in_(payload.sample_ids),
        Sample.dataset_version_id.in_(version_ids),
        Sample.deleted_at.is_(None),
    )))
    if len(samples) != len(set(payload.sample_ids)):
        raise HTTPException(status_code=404, detail="sample.not_found")
    history_payload = {
        "samples": [
            {"id": str(sample.id), "before": {"subset": sample.subset}, "after": {"subset": payload.subset}}
            for sample in samples
        ],
    }
    for sample in samples:
        sample.subset = payload.subset
    record_operation(
        db,
        organization_id=organization_id,
        user_id=user.id,
        dataset_id=dataset.id,
        action="samples.subset_update",
        summary=f"Updated the subset for {len(samples)} sample(s)",
        payload=history_payload,
    )
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="samples.subset_update",
        resource_type="dataset",
        resource_id=dataset.id,
        after={"sample_ids": [str(item.id) for item in samples], "subset": payload.subset},
    )
    db.commit()
    return {"updated": len(samples), "subset": payload.subset}


@router.post("/datasets/{dataset_id}/samples/delete")
def delete_samples(
    dataset_id: uuid.UUID,
    organization_id: uuid.UUID,
    payload: SampleBulkDelete,
    user: CurrentUser,
    db: DB,
) -> dict:
    require_membership(db, organization_id, user.id, write=True)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    version_ids = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset.id)
    samples = list(db.scalars(select(Sample).where(
        Sample.id.in_(payload.sample_ids),
        Sample.dataset_version_id.in_(version_ids),
        Sample.deleted_at.is_(None),
    )))
    if len(samples) != len(set(payload.sample_ids)):
        raise HTTPException(status_code=404, detail="sample.not_found")
    history_payload = {
        "samples": [{"id": str(sample.id), "before": {"status": sample.status}} for sample in samples],
    }
    deleted_at = datetime.now(timezone.utc)
    for sample in samples:
        sample.deleted_at = deleted_at
        sample.status = "deleted"
    record_operation(
        db,
        organization_id=organization_id,
        user_id=user.id,
        dataset_id=dataset.id,
        action="samples.delete",
        summary=f"Deleted {len(samples)} sample(s)",
        payload=history_payload,
    )
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="samples.delete",
        resource_type="dataset",
        resource_id=dataset.id,
        after={"sample_ids": [str(item.id) for item in samples]},
    )
    db.commit()
    return {"deleted": len(samples)}

@router.get("/assets/{asset_id}/download-url")
def asset_download_url(asset_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    require_membership(db, organization_id, user.id)
    asset = db.scalar(select(Asset).where(Asset.id == asset_id, Asset.organization_id == organization_id, Asset.deleted_at.is_(None)))
    if asset is None:
        raise HTTPException(status_code=404, detail="asset.not_found")
    return {"url": get_storage().create_download_url(asset.bucket, asset.object_key, timedelta(minutes=15)), "expires_in_seconds": 900}


@router.get("/jobs", response_model=Page)
def list_jobs(organization_id: uuid.UUID, user: CurrentUser, db: DB, limit: int = 50, offset: int = 0) -> Page:
    require_membership(db, organization_id, user.id)
    limit = min(max(limit, 1), 100)
    query = select(Job).where(Job.organization_id == organization_id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    jobs = list(db.scalars(query.order_by(Job.created_at.desc()).offset(offset).limit(limit)))
    return Page(items=[JobOut.model_validate(job).model_dump() for job in jobs], total=total, limit=limit, offset=offset)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def cancel_job(job_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> Job:
    require_membership(db, organization_id, user.id, write=True)
    job = _job_for_org(db, job_id, organization_id)
    if job.status in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="job.not_cancellable")
    transition_job(job, status="cancelled", stage="cancelled")
    if job.resource_type == "upload_session":
        upload = db.get(UploadSession, job.resource_id)
        if upload and upload.status not in {"ready", "import_failed", "scan_failed"}:
            upload.status = "cancelled"
            batch = db.get(ImportBatch, upload.import_batch_id)
            if batch:
                batch.status = "cancelled"
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="job.cancel",
        resource_type="job",
        resource_id=job.id,
        after={"job_type": job.job_type, "status": "cancelled"},
    )
    db.commit()
    return job

@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> Job:
    require_membership(db, organization_id, user.id)
    return _job_for_org(db, job_id, organization_id)


@router.post("/datasets/{dataset_id}/quality-checks", response_model=ActionJobOut, status_code=status.HTTP_202_ACCEPTED)
def queue_quality_check(dataset_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> ActionJobOut:
    require_membership(db, organization_id, user.id, write=True)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    version = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id, DatasetVersion.status == "ready").order_by(DatasetVersion.version_number.desc()))
    if version is None:
        raise HTTPException(status_code=409, detail="dataset.no_ready_version")
    job = Job(organization_id=organization_id, job_type="quality_check", resource_type="dataset_version", resource_id=version.id, idempotency_key=f"quality:{version.id}:{uuid.uuid4()}", status="queued", requested_by=user.id)
    db.add(job)
    db.flush()
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="quality_check.queue",
        resource_type="job",
        resource_id=job.id,
        after={"dataset_version_id": str(version.id)},
    )
    db.commit()
    run_quality_check.delay(str(job.id))
    return ActionJobOut(job=JobOut.model_validate(job))


@router.get("/datasets/{dataset_id}/statistics")
def dataset_statistics(dataset_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    require_membership(db, organization_id, user.id)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    versions = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset.id)
    total = db.scalar(select(func.count()).select_from(Sample).where(Sample.dataset_version_id.in_(versions), Sample.deleted_at.is_(None))) or 0
    by_subset = dict(db.execute(select(Sample.subset, func.count()).where(Sample.dataset_version_id.in_(versions), Sample.deleted_at.is_(None)).group_by(Sample.subset)).all())
    by_annotation = dict(db.execute(select(Sample.annotation_type, func.count()).where(Sample.dataset_version_id.in_(versions), Sample.deleted_at.is_(None)).group_by(Sample.annotation_type)).all())
    by_class = dict(db.execute(
        select(SampleClassIndex.class_id, func.count())
        .join(Sample, Sample.id == SampleClassIndex.sample_id)
        .where(Sample.dataset_version_id.in_(versions), Sample.deleted_at.is_(None))
        .group_by(SampleClassIndex.class_id)
    ).all())
    annotation_totals = db.execute(
        select(
            func.coalesce(func.sum(AnnotationIndex.annotation_count), 0),
            func.coalesce(func.sum(AnnotationIndex.bbox_count), 0),
            func.coalesce(func.sum(AnnotationIndex.polygon_count), 0),
            func.coalesce(func.sum(AnnotationIndex.keypoint_count), 0),
        )
        .join(Sample, Sample.id == AnnotationIndex.sample_id)
        .where(Sample.dataset_version_id.in_(versions), Sample.deleted_at.is_(None))
    ).one()
    missing_annotation_count = db.scalar(
        select(func.count()).select_from(Sample).where(
            Sample.dataset_version_id.in_(versions),
            Sample.deleted_at.is_(None),
            Sample.annotation_asset_id.is_(None),
        )
    ) or 0
    by_batch = [
        {"id": str(batch_id), "name": batch_name, "sample_count": count}
        for batch_id, batch_name, count in db.execute(
            select(ImportBatch.id, ImportBatch.batch_name, func.count(Sample.id))
            .join(Sample, Sample.import_batch_id == ImportBatch.id)
            .where(Sample.dataset_version_id.in_(versions), Sample.deleted_at.is_(None), ImportBatch.deleted_at.is_(None))
            .group_by(ImportBatch.id, ImportBatch.batch_name)
            .order_by(ImportBatch.created_at.desc())
        ).all()
    ]
    label_rows = list(db.scalars(
        select(LabelDefinition)
        .join(DatasetVersion, DatasetVersion.id == LabelDefinition.dataset_version_id)
        .where(DatasetVersion.dataset_id == dataset.id)
        .order_by(DatasetVersion.version_number.desc(), LabelDefinition.class_id)
    ))
    label_names: dict[int, str] = {}
    for label in label_rows:
        label_names.setdefault(label.class_id, label.class_name)
    class_distribution = [
        {"class_id": class_id, "class_name": label_names.get(class_id, f"class_{class_id}"), "sample_count": count}
        for class_id, count in sorted(by_class.items())
    ]
    return {
        "sample_count": total,
        "annotated_sample_count": total - missing_annotation_count,
        "missing_annotation_count": missing_annotation_count,
        "by_subset": by_subset,
        "by_annotation_type": by_annotation,
        "by_class_id": by_class,
        "class_distribution": class_distribution,
        "by_import_batch": by_batch,
        "annotation_totals": {
            "annotations": int(annotation_totals[0]),
            "boxes": int(annotation_totals[1]),
            "polygons": int(annotation_totals[2]),
            "keypoints": int(annotation_totals[3]),
        },
    }

@router.post("/datasets/{dataset_id}/exports", response_model=ActionJobOut, status_code=status.HTTP_202_ACCEPTED)
def queue_export(dataset_id: uuid.UUID, organization_id: uuid.UUID, payload: ExportRequest, user: CurrentUser, db: DB) -> ActionJobOut:
    require_membership(db, organization_id, user.id, write=True)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    job = Job(organization_id=organization_id, job_type="export", resource_type="dataset", resource_id=dataset.id, idempotency_key=f"export:{dataset.id}:{payload.format}:{uuid.uuid4()}", status="queued", requested_by=user.id, result_json={"requested_format": payload.format, "filters": {"import_batch_ids": [str(item) for item in payload.import_batch_ids], "subsets": payload.subsets, "class_ids": payload.class_ids, "include_unannotated": payload.include_unannotated}})
    db.add(job)
    db.flush()
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="export.queue",
        resource_type="job",
        resource_id=job.id,
        after={"dataset_id": str(dataset.id), "format": payload.format},
    )
    db.commit()
    create_export.delay(str(job.id))
    return ActionJobOut(job=JobOut.model_validate(job))


@router.get("/datasets/{dataset_id}/import-batches", response_model=Page)
def list_import_batches(dataset_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB, limit: int = 50, offset: int = 0) -> Page:
    require_membership(db, organization_id, user.id)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    limit = min(max(limit, 1), 100)
    versions = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset.id)
    query = select(ImportBatch).where(ImportBatch.dataset_version_id.in_(versions), ImportBatch.deleted_at.is_(None)).order_by(ImportBatch.created_at.desc())
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    batches = list(db.scalars(query.offset(offset).limit(limit)))
    return Page(items=[{"id": str(batch.id), "dataset_version_id": str(batch.dataset_version_id), "batch_number": batch.batch_number, "batch_name": batch.batch_name, "source_type": batch.source_type, "status": batch.status, "note": batch.note, "meta": batch.meta_json, "created_at": batch.created_at.isoformat()} for batch in batches], total=total, limit=limit, offset=offset)

@router.patch("/datasets/{dataset_id}/import-batches/{batch_id}")
def update_import_batch(
    dataset_id: uuid.UUID,
    batch_id: uuid.UUID,
    organization_id: uuid.UUID,
    payload: ImportBatchUpdate,
    user: CurrentUser,
    db: DB,
) -> dict:
    require_membership(db, organization_id, user.id, write=True)
    batch = db.scalar(
        select(ImportBatch)
        .join(DatasetVersion, DatasetVersion.id == ImportBatch.dataset_version_id)
        .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
        .where(
            ImportBatch.id == batch_id,
            Dataset.id == dataset_id,
            Dataset.organization_id == organization_id,
            ImportBatch.deleted_at.is_(None),
        )
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="import_batch.not_found")
    before = {"batch_name": batch.batch_name, "note": batch.note}
    changes: dict[str, str | None] = {}
    if "batch_name" in payload.model_fields_set:
        batch.batch_name = payload.batch_name or batch.batch_name
        changes["batch_name"] = batch.batch_name
    if "note" in payload.model_fields_set:
        batch.note = payload.note
        changes["note"] = batch.note
    if not changes:
        raise HTTPException(status_code=422, detail="import_batch.no_changes")
    record_operation(
        db,
        organization_id=organization_id,
        user_id=user.id,
        dataset_id=dataset_id,
        action="import_batch.update",
        summary=f"Updated part {batch.batch_name}",
        payload={"batch_id": str(batch.id), "before": before, "after": {"batch_name": batch.batch_name, "note": batch.note}},
    )
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="import_batch.update",
        resource_type="import_batch",
        resource_id=batch.id,
        after=changes,
    )
    db.commit()
    return {"id": str(batch.id), "batch_name": batch.batch_name, "note": batch.note, "status": batch.status}


@router.post("/datasets/{dataset_id}/import-batches/{batch_id}/rescan", response_model=ActionJobOut, status_code=status.HTTP_202_ACCEPTED)
def rescan_import_batch(dataset_id: uuid.UUID, batch_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> ActionJobOut:
    require_membership(db, organization_id, user.id, write=True)
    batch = db.scalar(
        select(ImportBatch)
        .join(DatasetVersion, DatasetVersion.id == ImportBatch.dataset_version_id)
        .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
        .where(
            ImportBatch.id == batch_id,
            Dataset.id == dataset_id,
            Dataset.organization_id == organization_id,
            ImportBatch.deleted_at.is_(None),
        )
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="import_batch.not_found")
    upload = db.scalar(
        select(UploadSession)
        .where(UploadSession.import_batch_id == batch.id)
        .order_by(UploadSession.created_at.desc())
    )
    if upload is None:
        raise HTTPException(status_code=409, detail="import_batch.source_archive_missing")
    storage = get_storage()
    if not storage.object_exists(upload.bucket, upload.object_key):
        raise HTTPException(status_code=409, detail="import_batch.source_object_missing")
    active = db.scalar(select(Job).where(
        Job.resource_id == upload.id,
        Job.job_type == "scan_upload",
        Job.status.in_(["queued", "pending", "running"]),
    ))
    if active is not None:
        return ActionJobOut(job=JobOut.model_validate(active))
    job = Job(
        organization_id=organization_id,
        job_type="scan_upload",
        resource_type="upload_session",
        resource_id=upload.id,
        idempotency_key=f"rescan-upload:{upload.id}:{uuid.uuid4()}",
        status="queued",
        requested_by=user.id,
    )
    upload.status = "uploaded"
    batch.status = "scanning"
    db.add(job)
    db.flush()
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="import_batch.rescan",
        resource_type="import_batch",
        resource_id=batch.id,
        after={"job_id": str(job.id), "upload_session_id": str(upload.id)},
    )
    db.commit()
    scan_upload.delay(str(job.id))
    return ActionJobOut(job=JobOut.model_validate(job))

@router.delete("/datasets/{dataset_id}/import-batches/{batch_id}")
def delete_import_batch(dataset_id: uuid.UUID, batch_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    require_membership(db, organization_id, user.id, write=True)
    batch = db.scalar(
        select(ImportBatch)
        .join(DatasetVersion, DatasetVersion.id == ImportBatch.dataset_version_id)
        .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
        .where(
            ImportBatch.id == batch_id,
            Dataset.id == dataset_id,
            Dataset.organization_id == organization_id,
            ImportBatch.deleted_at.is_(None),
        )
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="import_batch.not_found")
    batch_samples = list(db.scalars(select(Sample).where(Sample.import_batch_id == batch.id, Sample.deleted_at.is_(None))))
    history_payload = {
        "batch_id": str(batch.id),
        "before": {"status": batch.status},
        "samples": [{"id": str(sample.id), "before": {"status": sample.status}} for sample in batch_samples],
    }
    deleted_at = datetime.now(timezone.utc)
    sample_count = db.execute(
        update(Sample)
        .where(Sample.import_batch_id == batch.id, Sample.deleted_at.is_(None))
        .values(deleted_at=deleted_at, status="deleted")
    ).rowcount or 0
    batch.deleted_at = deleted_at
    batch.status = "deleted"
    record_operation(
        db,
        organization_id=organization_id,
        user_id=user.id,
        dataset_id=dataset_id,
        action="import_batch.delete",
        summary=f"Deleted part {batch.batch_name}",
        payload=history_payload,
    )
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="import_batch.delete",
        resource_type="import_batch",
        resource_id=batch.id,
        after={"deleted_samples": sample_count},
    )
    db.commit()
    return {"deleted": True, "deleted_samples": sample_count}

@router.get("/datasets/{dataset_id}/upload-sessions", response_model=Page)
def list_dataset_uploads(dataset_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> Page:
    require_membership(db, organization_id, user.id)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    uploads = list(db.scalars(
        select(UploadSession).where(UploadSession.dataset_id == dataset.id).order_by(UploadSession.created_at.desc())
    ))
    return Page(items=[_session_out(upload).model_dump() for upload in uploads], total=len(uploads), limit=len(uploads), offset=0)


@router.get("/samples/{sample_id}")
def get_sample_preview(sample_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    require_membership(db, organization_id, user.id)
    sample = db.scalar(
        select(Sample)
        .join(DatasetVersion, DatasetVersion.id == Sample.dataset_version_id)
        .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
        .where(Sample.id == sample_id, Dataset.organization_id == organization_id, Sample.deleted_at.is_(None))
    )
    if sample is None:
        raise HTTPException(status_code=404, detail="sample.not_found")
    image = db.get(Asset, sample.image_asset_id)
    annotation = db.get(Asset, sample.annotation_asset_id) if sample.annotation_asset_id else None
    annotation_index = db.get(AnnotationIndex, sample.id)
    normalized = db.get(Asset, annotation_index.normalized_annotation_asset_id) if annotation_index and annotation_index.normalized_annotation_asset_id else None
    storage = get_storage()
    return {
        "sample": SampleOut.model_validate(sample).model_dump(),
        "image_url": storage.create_download_url(image.bucket, image.object_key, timedelta(minutes=15)) if image else None,
        "annotation_url": storage.create_download_url(annotation.bucket, annotation.object_key, timedelta(minutes=15)) if annotation else None,
        "normalized_annotation_url": storage.create_download_url(normalized.bucket, normalized.object_key, timedelta(minutes=15)) if normalized else None,
        "summary": {
            "annotation_count": annotation_index.annotation_count,
            "bbox_count": annotation_index.bbox_count,
            "polygon_count": annotation_index.polygon_count,
            "keypoint_count": annotation_index.keypoint_count,
        } if annotation_index else None,
    }

@router.get("/datasets/{dataset_id}/labels")
def list_labels(dataset_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> list[dict]:
    require_membership(db, organization_id, user.id)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    labels = list(db.scalars(
        select(LabelDefinition)
        .join(DatasetVersion, DatasetVersion.id == LabelDefinition.dataset_version_id)
        .where(DatasetVersion.dataset_id == dataset.id)
        .order_by(DatasetVersion.version_number.desc(), LabelDefinition.class_id)
    ))
    latest_by_class: dict[int, LabelDefinition] = {}
    for item in labels:
        latest_by_class.setdefault(item.class_id, item)
    return [{"id": str(item.id), "class_id": item.class_id, "class_name": item.class_name, "color": item.color} for item in sorted(latest_by_class.values(), key=lambda item: item.class_id)]


@router.put("/datasets/{dataset_id}/labels/{class_id}")
def update_label(
    dataset_id: uuid.UUID,
    class_id: int,
    organization_id: uuid.UUID,
    payload: LabelUpdate,
    user: CurrentUser,
    db: DB,
) -> dict:
    require_membership(db, organization_id, user.id, write=True)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    versions = list(db.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id).order_by(DatasetVersion.version_number.desc())))
    if not versions:
        raise HTTPException(status_code=409, detail="dataset.no_version")
    labels = list(db.scalars(select(LabelDefinition).where(
        LabelDefinition.dataset_version_id.in_([item.id for item in versions]),
        LabelDefinition.class_id == class_id,
    )))
    if not labels:
        label = LabelDefinition(dataset_version_id=versions[0].id, class_id=class_id, class_name=payload.class_name, color=payload.color)
        db.add(label)
        labels = [label]
    else:
        for label in labels:
            label.class_name = payload.class_name
            label.color = payload.color
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="label.update",
        resource_type="dataset",
        resource_id=dataset.id,
        after={"class_id": class_id, "class_name": payload.class_name, "color": payload.color},
    )
    db.commit()
    label = labels[0]
    return {"id": str(label.id), "class_id": label.class_id, "class_name": label.class_name, "color": label.color}


@router.delete("/datasets/{dataset_id}/labels/{class_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_label(
    dataset_id: uuid.UUID,
    class_id: int,
    organization_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
) -> None:
    """Remove a class definition from every version of a dataset.

    Sample annotations are intentionally preserved: this only removes the editable
    display mapping, matching the desktop application's label-map management model.
    """
    require_membership(db, organization_id, user.id, write=True)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    version_ids = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset.id)
    result = db.execute(delete(LabelDefinition).where(
        LabelDefinition.dataset_version_id.in_(version_ids),
        LabelDefinition.class_id == class_id,
    ))
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="label.not_found")
    db.execute(delete(KeypointDefinition).where(
        KeypointDefinition.dataset_version_id.in_(version_ids),
        KeypointDefinition.class_id == class_id,
    ))
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="label.delete",
        resource_type="dataset",
        resource_id=dataset.id,
        after={"class_id": class_id},
    )
    db.commit()


@router.put("/datasets/{dataset_id}/labels/{class_id}/keypoints")
def update_keypoint_names(
    dataset_id: uuid.UUID,
    class_id: int,
    organization_id: uuid.UUID,
    payload: KeypointNamesUpdate,
    user: CurrentUser,
    db: DB,
) -> dict:
    require_membership(db, organization_id, user.id, write=True)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    versions = list(db.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id)))
    if not versions:
        raise HTTPException(status_code=409, detail="dataset.no_version")
    version_ids = [item.id for item in versions]
    db.execute(delete(KeypointDefinition).where(
        KeypointDefinition.dataset_version_id.in_(version_ids),
        KeypointDefinition.class_id == class_id,
    ))
    for version in versions:
        for point_index, point_name in enumerate(payload.names):
            db.add(KeypointDefinition(
                dataset_version_id=version.id,
                class_id=class_id,
                point_index=point_index,
                point_name=point_name.strip() or f"keypoint_{point_index}",
            ))
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="keypoints.update",
        resource_type="dataset",
        resource_id=dataset.id,
        after={"class_id": class_id, "names": payload.names},
    )
    db.commit()
    return {"class_id": class_id, "names": payload.names}

@router.get("/datasets/{dataset_id}/keypoints")
def list_keypoints(dataset_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> list[dict]:
    require_membership(db, organization_id, user.id)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    versions = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset.id)
    items = list(db.scalars(select(KeypointDefinition).where(KeypointDefinition.dataset_version_id.in_(versions)).order_by(KeypointDefinition.class_id, KeypointDefinition.point_index)))
    return [{"id": str(item.id), "class_id": item.class_id, "point_index": item.point_index, "point_name": item.point_name} for item in items]

@router.get("/datasets/{dataset_id}/quality-issues", response_model=Page)
def list_quality_issues(dataset_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB, limit: int = 50, offset: int = 0) -> Page:
    require_membership(db, organization_id, user.id)
    dataset = get_dataset_for_org(db, dataset_id, organization_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset.not_found")
    version_ids = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset.id)
    query = select(QualityIssue).where(QualityIssue.dataset_version_id.in_(version_ids)).order_by(QualityIssue.checked_at.desc())
    limit = min(max(limit, 1), 100)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    issues = list(db.scalars(query.offset(offset).limit(limit)))
    return Page(items=[{"id": str(issue.id), "sample_id": str(issue.sample_id) if issue.sample_id else None, "issue_type": issue.issue_type, "severity": issue.severity, "detail_code": issue.detail_code, "detail": issue.detail_json or {}, "checked_at": issue.checked_at.isoformat()} for issue in issues], total=total, limit=limit, offset=offset)


@router.get("/jobs/{job_id}/download-url")
def get_export_download_url(job_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    require_membership(db, organization_id, user.id)
    job = _job_for_org(db, job_id, organization_id)
    if job.job_type != "export" or job.status != "succeeded" or not job.result_json:
        raise HTTPException(status_code=409, detail="export.not_ready")
    bucket = job.result_json.get("bucket")
    object_key = job.result_json.get("object_key")
    if not bucket or not object_key:
        raise HTTPException(status_code=409, detail="export.artifact_missing")
    return {
        "url": get_storage().create_download_url(bucket, object_key, timedelta(minutes=15)),
        "file_name": job.result_json.get("file_name", "dataset-export.json"),
        "expires_in_seconds": 900,
    }


@router.get("/operation-history", response_model=Page)
def list_operation_history(
    organization_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    dataset_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Page:
    require_membership(db, organization_id, user.id)
    limit = min(max(limit, 1), 100)
    query = select(OperationHistory).where(OperationHistory.organization_id == organization_id).order_by(OperationHistory.created_at.desc())
    if dataset_id:
        query = query.where(OperationHistory.dataset_id == dataset_id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    records = list(db.scalars(query.offset(offset).limit(limit)))
    return Page(
        items=[
            {
                "id": str(record.id),
                "dataset_id": str(record.dataset_id) if record.dataset_id else None,
                "action": record.action,
                "summary": record.summary,
                "status": record.status,
                "created_at": record.created_at.isoformat(),
            }
            for record in records
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/operation-history/{history_id}/undo")
def undo_history(history_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    require_membership(db, organization_id, user.id, write=True)
    record = db.scalar(select(OperationHistory).where(
        OperationHistory.id == history_id,
        OperationHistory.organization_id == organization_id,
    ))
    if record is None:
        raise HTTPException(status_code=404, detail="history.not_found")
    try:
        undo_operation(db, record)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="history.undo",
        resource_type="operation_history",
        resource_id=record.id,
        after={"action": record.action},
    )
    db.commit()
    return {"id": str(record.id), "status": record.status, "action": record.action}


@router.post("/operation-history/{history_id}/redo")
def redo_history(history_id: uuid.UUID, organization_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    require_membership(db, organization_id, user.id, write=True)
    record = db.scalar(select(OperationHistory).where(
        OperationHistory.id == history_id,
        OperationHistory.organization_id == organization_id,
    ))
    if record is None:
        raise HTTPException(status_code=404, detail="history.not_found")
    try:
        redo_operation(db, record)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit(
        db,
        organization_id=organization_id,
        user_id=user.id,
        action="history.redo",
        resource_type="operation_history",
        resource_id=record.id,
        after={"action": record.action},
    )
    db.commit()
    return {"id": str(record.id), "status": record.status, "action": record.action}

@router.get("/audit-logs", response_model=Page)
def list_audit_logs(organization_id: uuid.UUID, user: CurrentUser, db: DB, limit: int = 50, offset: int = 0) -> Page:
    require_membership(db, organization_id, user.id)
    limit = min(max(limit, 1), 100)
    query = select(AuditLog).where(AuditLog.organization_id == organization_id).order_by(AuditLog.created_at.desc())
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    records = list(db.scalars(query.offset(offset).limit(limit)))
    return Page(items=[{"id": str(item.id), "action": item.action, "resource_type": item.resource_type, "resource_id": str(item.resource_id), "request_id": item.request_id, "created_at": item.created_at.isoformat()} for item in records], total=total, limit=limit, offset=offset)
















