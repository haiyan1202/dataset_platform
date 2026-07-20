from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IdTimestampMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Organization(IdTimestampMixin, Base):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    storage_quota_bytes: Mapped[int] = mapped_column(default=0)


class User(IdTimestampMixin, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="active")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_membership_org_user"),)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), default="owner")
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dataset(IdTimestampMixin, Base):
    __tablename__ = "datasets"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DatasetVersion(IdTimestampMixin, Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version_number", name="uq_dataset_version_number"),)
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ImportBatch(IdTimestampMixin, Base):
    __tablename__ = "import_batches"
    __table_args__ = (UniqueConstraint("dataset_version_id", "batch_number", name="uq_batch_version_number"),)
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    batch_number: Mapped[int] = mapped_column(Integer)
    batch_name: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(32), default="zip")
    status: Mapped[str] = mapped_column(String(32), default="uploading")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UploadSession(IdTimestampMixin, Base):
    __tablename__ = "upload_sessions"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id"), index=True)
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_versions.id"))
    import_batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batches.id"))
    bucket: Mapped[str] = mapped_column(String(128))
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    original_name: Mapped[str] = mapped_column(String(512))
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="created")
    preview_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))


class Asset(IdTimestampMixin, Base):
    __tablename__ = "assets"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    storage_provider: Mapped[str] = mapped_column(String(32), default="minio")
    bucket: Mapped[str] = mapped_column(String(128))
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    original_name: Mapped[str] = mapped_column(String(512))
    relative_path: Mapped[str] = mapped_column(String(1024))
    asset_type: Mapped[str] = mapped_column(String(32))
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column()
    checksum_algorithm: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ready")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Sample(IdTimestampMixin, Base):
    __tablename__ = "samples"
    __table_args__ = (UniqueConstraint("dataset_version_id", "relative_path", name="uq_sample_version_path"),)
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    import_batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batches.id"), index=True)
    image_asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"))
    annotation_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    file_name: Mapped[str] = mapped_column(String(512), index=True)
    file_stem: Mapped[str] = mapped_column(String(512), index=True)
    relative_path: Mapped[str] = mapped_column(String(1024))
    subset: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    annotation_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    width: Mapped[int | None] = mapped_column(nullable=True)
    height: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ready", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AnnotationIndex(Base):
    __tablename__ = "annotation_indexes"
    sample_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("samples.id"), primary_key=True)
    annotation_count: Mapped[int] = mapped_column(default=0)
    bbox_count: Mapped[int] = mapped_column(default=0)
    polygon_count: Mapped[int] = mapped_column(default=0)
    keypoint_count: Mapped[int] = mapped_column(default=0)
    class_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    class_counts_json: Mapped[dict] = mapped_column(JSON, default=dict)
    normalized_annotation_asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    parser_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SampleClassIndex(Base):
    __tablename__ = "sample_class_indexes"
    sample_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("samples.id"), primary_key=True)
    class_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)


class LabelDefinition(IdTimestampMixin, Base):
    __tablename__ = "label_definitions"
    __table_args__ = (UniqueConstraint("dataset_version_id", "class_id", name="uq_label_version_class"),)
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    class_id: Mapped[int] = mapped_column(Integer)
    class_name: Mapped[str] = mapped_column(String(160))
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)


class KeypointDefinition(IdTimestampMixin, Base):
    __tablename__ = "keypoint_definitions"
    __table_args__ = (UniqueConstraint("dataset_version_id", "class_id", "point_index", name="uq_keypoint_version_class_index"),)
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    class_id: Mapped[int] = mapped_column(Integer)
    point_index: Mapped[int] = mapped_column(Integer)
    point_name: Mapped[str] = mapped_column(String(160))


class QualityIssue(IdTimestampMixin, Base):
    __tablename__ = "quality_issues"
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset_versions.id"), index=True)
    sample_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("samples.id"), nullable=True)
    issue_type: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(20))
    detail_code: Mapped[str] = mapped_column(String(160))
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    checker_version: Mapped[str] = mapped_column(String(32))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Job(IdTimestampMixin, Base):
    __tablename__ = "jobs"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    job_type: Mapped[str] = mapped_column(String(80), index=True)
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[uuid.UUID] = mapped_column(index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    stage: Mapped[str | None] = mapped_column(String(80), nullable=True)
    current: Mapped[int] = mapped_column(default=0)
    total: Mapped[int] = mapped_column(default=0)
    progress: Mapped[int] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(160), nullable=True)
    error_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OperationHistory(IdTimestampMixin, Base):
    __tablename__ = "operation_history"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("datasets.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(160), index=True)
    summary: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="applied", index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(160))
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[uuid.UUID] = mapped_column()
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
