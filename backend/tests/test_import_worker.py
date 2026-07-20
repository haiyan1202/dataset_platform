from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import py7zr
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import hash_password
from app.db.base import Base
from app.jobs import tasks
from app.models import (
    AnnotationIndex,
    Asset,
    Dataset,
    DatasetVersion,
    ImportBatch,
    Job,
    LabelDefinition,
    Membership,
    Organization,
    Sample,
    UploadSession,
    User,
)
from app.storage.service import ObjectMetadata


class WorkerStorage:
    def __init__(self, archive: bytes) -> None:
        self.objects: dict[tuple[str, str], bytes] = {
            ("dataset-platform", "uploads/source"): archive
        }
        self.uploaded_files = 0

    def download_to_file(self, bucket: str, object_key: str, file_path: str) -> None:
        Path(file_path).write_bytes(self.objects[(bucket, object_key)])

    def stat(self, bucket: str, object_key: str) -> ObjectMetadata:
        return ObjectMetadata(bucket, object_key, len(self.objects[(bucket, object_key)]))

    def put_bytes(
        self, bucket: str, object_key: str, content: bytes, _content_type: str | None = None
    ) -> None:
        self.objects[(bucket, object_key)] = content

    def upload_file(
        self, bucket: str, object_key: str, file_path: str, _content_type: str | None = None
    ) -> None:
        self.uploaded_files += 1
        self.objects[(bucket, object_key)] = Path(file_path).read_bytes()

    def read_bytes(self, bucket: str, object_key: str) -> bytes:
        return self.objects[(bucket, object_key)]


ENTRIES = {
    "data.yaml": b"names:\n  0: cat\n",
    "train/images/cat.jpg": b"image-bytes",
    "train/labels/cat.txt": b"0 0.5 0.5 0.2 0.2\n",
}


def make_archive(original_name: str, tmp_path: Path) -> bytes:
    if original_name.endswith(".zip"):
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            for name, value in ENTRIES.items():
                archive.writestr(name, value)
        return content.getvalue()
    if original_name.endswith(".tar.gz"):
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode="w:gz") as archive:
            for name, value in ENTRIES.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(value)
                archive.addfile(entry, io.BytesIO(value))
        return content.getvalue()
    path = tmp_path / original_name
    with py7zr.SevenZipFile(path, "w") as archive:
        for name, value in ENTRIES.items():
            archive.writestr(value, name)
    return path.read_bytes()


@pytest.mark.parametrize("original_name", ["source.zip", "source.tar.gz", "source.7z"])
def test_scan_then_confirm_import_creates_raw_normalized_and_indexed_assets(
    tmp_path, monkeypatch, original_name: str
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    storage = WorkerStorage(make_archive(original_name, tmp_path))
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(tasks, "get_storage", lambda: storage)
    monkeypatch.setenv("WORKER_TMP_DIR", str(tmp_path / "worker-temp"))

    with factory() as db:
        organization = Organization(name="Worker Org")
        user = User(email="worker@example.test", password_hash=hash_password("safe-password"))
        db.add_all([organization, user])
        db.flush()
        db.add(Membership(organization_id=organization.id, user_id=user.id, role="owner"))
        dataset = Dataset(organization_id=organization.id, name="Cats", created_by=user.id)
        db.add(dataset)
        db.flush()
        version = DatasetVersion(dataset_id=dataset.id, version_number=1, created_by=user.id)
        db.add(version)
        db.flush()
        batch = ImportBatch(
            dataset_version_id=version.id, batch_number=1, batch_name="source", created_by=user.id
        )
        db.add(batch)
        db.flush()
        upload = UploadSession(
            organization_id=organization.id,
            dataset_id=dataset.id,
            dataset_version_id=version.id,
            import_batch_id=batch.id,
            bucket="dataset-platform",
            object_key="uploads/source",
            original_name=original_name,
            created_by=user.id,
        )
        db.add(upload)
        db.flush()
        scan_job = Job(
            organization_id=organization.id,
            job_type="scan_upload",
            resource_type="upload_session",
            resource_id=upload.id,
            idempotency_key="scan-worker-test",
            requested_by=user.id,
        )
        db.add(scan_job)
        db.commit()
        scan_job_id = scan_job.id
        upload_id = upload.id
        version_id = version.id

    preview = tasks.scan_upload.run(str(scan_job_id))
    assert preview["parser_name"] == "yolo"
    assert preview["image_count"] == 1

    with factory() as db:
        upload = db.get(UploadSession, upload_id)
        assert upload.status == "waiting_confirmation"
        upload.status = "importing"
        import_job = Job(
            organization_id=upload.organization_id,
            job_type="import_upload",
            resource_type="upload_session",
            resource_id=upload.id,
            idempotency_key="import-worker-test",
            requested_by=upload.created_by,
        )
        db.add(import_job)
        db.commit()
        import_job_id = import_job.id

    result = tasks.confirm_import.run(str(import_job_id))
    assert result["created_samples"] == 1

    # A rescan treats the archive layout as authoritative for samples from that
    # batch, repairing stale subset values left by earlier imports.
    with factory() as db:
        sample = db.query(Sample).one()
        sample.subset = "test"
        batch = db.get(ImportBatch, sample.import_batch_id)
        upload = db.get(UploadSession, upload_id)
        assert batch is not None and upload is not None
        batch.meta_json = {"rescan": True}
        upload.status = "importing"
        rescan_import_job = Job(
            organization_id=upload.organization_id,
            job_type="import_upload",
            resource_type="upload_session",
            resource_id=upload.id,
            idempotency_key=f"rescan-import-worker-test-{original_name}",
            requested_by=upload.created_by,
        )
        db.add(rescan_import_job)
        db.commit()
        rescan_import_job_id = rescan_import_job.id

    rescan_result = tasks.confirm_import.run(str(rescan_import_job_id))
    assert rescan_result["created_samples"] == 0
    assert rescan_result["reconciled_samples"] == 1

    with factory() as db:
        assert db.query(Sample).one().subset == "train"

    with factory() as db:
        sample = db.query(Sample).one()
        index = db.get(AnnotationIndex, sample.id)
        assert index.annotation_count == 1
        assert index.bbox_count == 1
        assert db.query(LabelDefinition).one().class_name == "cat"
        normalized = db.get(Asset, index.normalized_annotation_asset_id)
        assert normalized.object_key.endswith("normalized/train/images/cat.jpg.json")
        assert (normalized.bucket, normalized.object_key) in storage.objects
        assert db.get(DatasetVersion, version_id).status == "ready"
        if original_name.endswith(".7z"):
            assert storage.uploaded_files >= 2
        quality_job = Job(
            organization_id=organization.id,
            job_type="quality_check",
            resource_type="dataset_version",
            resource_id=sample.dataset_version_id,
            idempotency_key=f"quality-{original_name}",
            requested_by=user.id,
        )
        db.add(quality_job)
        db.commit()
        quality_job_id = quality_job.id

    quality_result = tasks.run_quality_check.run(str(quality_job_id))
    assert quality_result["checked_samples"] == 1
    assert quality_result["issue_count"] == 0

    Base.metadata.drop_all(engine)
