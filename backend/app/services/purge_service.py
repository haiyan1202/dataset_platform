from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Asset, Dataset, DatasetVersion, ImportBatch, Job, Sample, UploadSession
from app.settings import get_settings

TEMP_JOB_MARKER = ".dataset-platform-job.json"


@dataclass(frozen=True)
class StorageObject:
    bucket: str
    object_key: str
    size_bytes: int


@dataclass(frozen=True)
class DatasetPurgeInventory:
    version_ids: tuple[uuid.UUID, ...]
    batch_ids: tuple[uuid.UUID, ...]
    upload_ids: tuple[uuid.UUID, ...]
    asset_ids: tuple[uuid.UUID, ...]
    object_refs: tuple[StorageObject, ...]
    sample_count: int
    export_job_ids: tuple[uuid.UUID, ...]

    @property
    def estimated_bytes(self) -> int:
        return sum(item.size_bytes for item in self.object_refs)


def collect_dataset_purge_inventory(db: Session, dataset: Dataset) -> DatasetPurgeInventory:
    version_ids = tuple(db.scalars(select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset.id)))
    batch_ids = tuple(db.scalars(select(ImportBatch.id).where(ImportBatch.dataset_version_id.in_(version_ids)))) if version_ids else ()
    upload_rows = list(db.scalars(select(UploadSession).where(UploadSession.dataset_id == dataset.id)))
    object_prefix = f"org/{dataset.organization_id}/datasets/{dataset.id}/"
    asset_rows = list(db.scalars(select(Asset).where(Asset.object_key.like(f"{object_prefix}%"))))
    sample_count = db.scalar(select(func.count()).select_from(Sample).where(Sample.dataset_version_id.in_(version_ids))) or 0 if version_ids else 0
    export_rows = list(db.scalars(select(Job).where(Job.organization_id == dataset.organization_id, Job.job_type == "export", Job.resource_id == dataset.id)))

    objects: dict[tuple[str, str], StorageObject] = {}
    for asset in asset_rows:
        objects[(asset.bucket, asset.object_key)] = StorageObject(asset.bucket, asset.object_key, asset.size_bytes)
    for upload in upload_rows:
        objects.setdefault((upload.bucket, upload.object_key), StorageObject(upload.bucket, upload.object_key, upload.size_bytes or 0))
    for export in export_rows:
        result = export.result_json or {}
        bucket = result.get("bucket")
        object_key = result.get("object_key")
        if isinstance(bucket, str) and isinstance(object_key, str):
            objects.setdefault((bucket, object_key), StorageObject(bucket, object_key, 0))

    return DatasetPurgeInventory(
        version_ids=version_ids,
        batch_ids=batch_ids,
        upload_ids=tuple(item.id for item in upload_rows),
        asset_ids=tuple(item.id for item in asset_rows),
        object_refs=tuple(objects.values()),
        sample_count=int(sample_count),
        export_job_ids=tuple(item.id for item in export_rows),
    )


def mark_worker_temp_directory(directory: str | Path, job_id: uuid.UUID | str) -> None:
    path = Path(directory) / TEMP_JOB_MARKER
    path.write_text(json.dumps({"job_id": str(job_id), "created_at": time.time()}), encoding="utf-8")


def _directory_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            candidate = Path(root) / name
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
    return total


def stale_worker_temp_directories(active_job_ids: set[str]) -> list[tuple[Path, int]]:
    settings = get_settings()
    root = Path(os.environ.get("WORKER_TMP_DIR", "/tmp/dataset-worker"))
    if not root.exists():
        return []
    cutoff = time.time() - settings.worker_temp_stale_seconds
    stale: list[tuple[Path, int]] = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or not candidate.name.startswith("tmp"):
            continue
        try:
            if candidate.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        marker = candidate / TEMP_JOB_MARKER
        if marker.exists():
            try:
                marker_job_id = str(json.loads(marker.read_text(encoding="utf-8")).get("job_id", ""))
            except (OSError, ValueError, json.JSONDecodeError):
                marker_job_id = ""
            if marker_job_id in active_job_ids:
                continue
        stale.append((candidate, _directory_size(candidate)))
    return stale


def remove_stale_worker_temp_directories(active_job_ids: set[str]) -> tuple[int, int]:
    removed_directories = 0
    released_bytes = 0
    for directory, size_bytes in stale_worker_temp_directories(active_job_ids):
        try:
            shutil.rmtree(directory)
        except OSError:
            continue
        removed_directories += 1
        released_bytes += size_bytes
    return removed_directories, released_bytes
