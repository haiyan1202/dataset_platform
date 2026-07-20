from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class OrganizationOut(APIModel):
    id: uuid.UUID
    name: str
    status: str


class UserOut(APIModel):
    id: uuid.UUID
    email: str
    status: str


class DatasetCreate(BaseModel):
    organization_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)


class DatasetOut(APIModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class RemovedDatasetOut(DatasetOut):
    deleted_at: datetime
    estimated_bytes: int
    sample_count: int
    batch_count: int
    source_upload_count: int


class DatasetPurgePreview(APIModel):
    dataset_id: uuid.UUID
    dataset_name: str
    sample_count: int
    version_count: int
    batch_count: int
    source_upload_count: int
    object_count: int
    estimated_bytes: int
    stale_temp_directory_count: int
    stale_temp_bytes: int


class DatasetPurgeRequest(BaseModel):
    confirmation_name: str = Field(min_length=1, max_length=200)


class Page(BaseModel):
    items: list[Any]
    total: int
    limit: int
    offset: int


class UploadSessionCreate(BaseModel):
    original_name: str = Field(min_length=1, max_length=512)
    checksum: str | None = Field(default=None, max_length=128)
    batch_name: str = Field(default="Upload batch", min_length=1, max_length=200)


class UploadSessionOut(APIModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    import_batch_id: uuid.UUID
    status: str
    object_key: str
    upload_url: str | None = None
    expires_in_seconds: int = 3600
    preview: dict | None = None


class UploadComplete(BaseModel):
    checksum: str | None = Field(default=None, max_length=128)


class JobOut(APIModel):
    id: uuid.UUID
    job_type: str
    resource_type: str
    resource_id: uuid.UUID
    status: str
    stage: str | None
    current: int
    total: int
    progress: int
    error_code: str | None
    error_detail: dict | None
    result_json: dict | None
    created_at: datetime
    updated_at: datetime


class SampleOut(APIModel):
    id: uuid.UUID
    dataset_version_id: uuid.UUID
    import_batch_id: uuid.UUID
    file_name: str
    relative_path: str
    subset: str | None
    annotation_type: str | None
    width: int | None
    height: int | None
    status: str


class ImportBatchUpdate(BaseModel):
    batch_name: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=10_000)


class SampleSubsetUpdate(BaseModel):
    sample_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    subset: Literal["train", "val", "test"] | None = None


class SampleBulkDelete(BaseModel):
    sample_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class LabelUpdate(BaseModel):
    class_name: str = Field(min_length=1, max_length=160)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class KeypointNamesUpdate(BaseModel):
    names: list[str] = Field(max_length=100)


class ExportRequest(BaseModel):
    format: Literal["manifest", "yolo", "coco", "labelme", "voc"] = "manifest"
    import_batch_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    subsets: list[Literal["train", "val", "test"]] = Field(default_factory=list, max_length=3)
    class_ids: list[int] = Field(default_factory=list, max_length=500)
    include_unannotated: bool = True


class ActionJobOut(BaseModel):
    job: JobOut


class ErrorResponse(BaseModel):
    error: dict[str, Any]
