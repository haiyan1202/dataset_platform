from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes import platform
from app.auth import hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import (
    Asset,
    Dataset,
    DatasetVersion,
    ImportBatch,
    LabelDefinition,
    Membership,
    Organization,
    Sample,
    UploadSession,
    User,
)


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.sizes: dict[tuple[str, str], int] = {}

    def ensure_bucket(self, bucket: str) -> None:
        return None

    def create_upload_url(self, bucket: str, object_key: str, expires: timedelta) -> str:
        return f"https://storage.invalid/{bucket}/{object_key}"

    def object_exists(self, bucket: str, object_key: str) -> bool:
        return (bucket, object_key) in self.objects

    def stat(self, bucket: str, object_key: str):
        from app.storage.service import ObjectMetadata

        return ObjectMetadata(
            bucket,
            object_key,
            self.sizes.get((bucket, object_key), len(self.objects[(bucket, object_key)])),
        )


@pytest.fixture
def api_client(
    tmp_path, monkeypatch
) -> Generator[tuple[TestClient, Session, FakeStorage], None, None]:
    engine = create_engine(f"sqlite:///{tmp_path / 'platform.db'}")
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    db = factory()
    organization = Organization(name="Test Org")
    user = User(email="owner@example.test", password_hash=hash_password("safe-password"))
    db.add_all([organization, user])
    db.flush()
    db.add(Membership(organization_id=organization.id, user_id=user.id, role="owner"))
    db.commit()
    storage = FakeStorage()

    def override_db() -> Generator[Session, None, None]:
        request_db = factory()
        try:
            yield request_db
        finally:
            request_db.close()

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(platform, "get_storage", lambda: storage)
    monkeypatch.setattr(platform.scan_upload, "delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(platform.confirm_import, "delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(platform.create_export, "delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(platform.run_quality_check, "delay", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(platform.purge_dataset, "delay", lambda *_args, **_kwargs: None)
    with TestClient(app) as client:
        yield client, db, storage
    app.dependency_overrides.clear()
    db.close()
    Base.metadata.drop_all(engine)


def login(client: TestClient) -> str:
    response = client.post(
        "/api/auth/login", json={"email": "owner@example.test", "password": "safe-password"}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def test_dataset_and_direct_upload_session_are_organization_scoped(api_client) -> None:
    client, db, _storage = api_client
    token = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    organization = db.query(Organization).one()
    response = client.post(
        "/api/datasets",
        headers=headers,
        json={"organization_id": str(organization.id), "name": "Cars"},
    )
    assert response.status_code == 201
    dataset = response.json()

    request_headers = {**headers, "Idempotency-Key": "upload-key-1"}
    upload = client.post(
        f"/api/datasets/{dataset['id']}/upload-sessions",
        headers=request_headers,
        json={"original_name": "cars.zip", "batch_name": "batch-a"},
    )
    assert upload.status_code == 201
    body = upload.json()
    replay = client.post(
        f"/api/datasets/{dataset['id']}/upload-sessions",
        headers=request_headers,
        json={"original_name": "cars.zip", "batch_name": "batch-a"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == body["id"]
    assert body["object_key"].startswith(f"org/{organization.id}/datasets/{dataset['id']}/uploads/")
    assert "C:" not in body["object_key"]
    assert body["upload_url"].startswith("https://storage.invalid/")


def test_dataset_delete_hides_the_dataset_from_the_workspace(api_client) -> None:
    client, db, _storage = api_client
    token = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    organization = db.query(Organization).one()
    created = client.post(
        "/api/datasets",
        headers=headers,
        json={"organization_id": str(organization.id), "name": "Temporary dataset"},
    )
    assert created.status_code == 201
    dataset_id = created.json()["id"]

    deleted = client.delete(
        f"/api/datasets/{dataset_id}?organization_id={organization.id}",
        headers=headers,
    )
    assert deleted.status_code == 204

    visible = client.get(f"/api/datasets?organization_id={organization.id}", headers=headers)
    assert visible.status_code == 200
    assert visible.json()["total"] == 0
    assert db.get(Dataset, uuid.UUID(dataset_id)).deleted_at is not None

    removed = client.get(
        f"/api/datasets/removed?organization_id={organization.id}", headers=headers
    )
    assert removed.status_code == 200
    assert removed.json()["total"] == 1
    assert removed.json()["items"][0]["id"] == dataset_id
    assert removed.json()["items"][0]["estimated_bytes"] == 0

    restored = client.post(
        f"/api/datasets/{dataset_id}/restore?organization_id={organization.id}",
        headers=headers,
    )
    assert restored.status_code == 200
    assert restored.json()["id"] == dataset_id
    assert db.get(Dataset, uuid.UUID(dataset_id)).deleted_at is None


def test_statistics_match_the_same_filtered_samples_as_the_browser(api_client) -> None:
    client, db, _storage = api_client
    token = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    organization = db.query(Organization).one()
    user = db.query(User).one()
    dataset = client.post(
        "/api/datasets",
        headers=headers,
        json={"organization_id": str(organization.id), "name": "Statistics filters"},
    ).json()
    version = DatasetVersion(
        dataset_id=uuid.UUID(dataset["id"]), version_number=1, created_by=user.id
    )
    db.add(version)
    db.flush()
    first_batch = ImportBatch(
        dataset_version_id=version.id,
        batch_number=1,
        batch_name="first",
        created_by=user.id,
    )
    second_batch = ImportBatch(
        dataset_version_id=version.id,
        batch_number=2,
        batch_name="second",
        created_by=user.id,
    )
    db.add_all([first_batch, second_batch])
    db.flush()

    image_assets = [
        Asset(
            organization_id=organization.id,
            bucket="dataset-platform",
            object_key=f"stats/image-{index}.jpg",
            original_name=f"image-{index}.jpg",
            relative_path=f"image-{index}.jpg",
            asset_type="image",
            size_bytes=1,
        )
        for index in range(3)
    ]
    annotation_assets = [
        Asset(
            organization_id=organization.id,
            bucket="dataset-platform",
            object_key=f"stats/annotation-{index}.txt",
            original_name=f"annotation-{index}.txt",
            relative_path=f"annotation-{index}.txt",
            asset_type="annotation",
            size_bytes=1,
        )
        for index in range(2)
    ]
    db.add_all([*image_assets, *annotation_assets])
    db.flush()
    db.add_all(
        [
            Sample(
                dataset_version_id=version.id,
                import_batch_id=first_batch.id,
                image_asset_id=image_assets[0].id,
                annotation_asset_id=annotation_assets[0].id,
                file_name="first-annotated.jpg",
                file_stem="first-annotated",
                relative_path="first-annotated.jpg",
                subset="train",
                annotation_type="yolo",
            ),
            Sample(
                dataset_version_id=version.id,
                import_batch_id=first_batch.id,
                image_asset_id=image_assets[1].id,
                file_name="first-missing.jpg",
                file_stem="first-missing",
                relative_path="first-missing.jpg",
                subset="val",
                annotation_type="yolo",
            ),
            Sample(
                dataset_version_id=version.id,
                import_batch_id=second_batch.id,
                image_asset_id=image_assets[2].id,
                annotation_asset_id=annotation_assets[1].id,
                file_name="second-annotated.jpg",
                file_stem="second-annotated",
                relative_path="second-annotated.jpg",
                subset="train",
                annotation_type="coco",
            ),
        ]
    )
    db.commit()

    query = f"organization_id={organization.id}&import_batch_id={first_batch.id}"
    samples = client.get(f"/api/datasets/{dataset['id']}/samples?{query}", headers=headers)
    statistics = client.get(f"/api/datasets/{dataset['id']}/statistics?{query}", headers=headers)
    assert samples.status_code == 200
    assert statistics.status_code == 200
    assert samples.json()["total"] == 2
    assert statistics.json()["sample_count"] == 2
    assert statistics.json()["annotated_sample_count"] == 1
    assert statistics.json()["missing_annotation_count"] == 1
    assert statistics.json()["by_subset"] == {"train": 1, "val": 1}
    assert statistics.json()["by_annotation_type"] == {"yolo": 2}

    annotated_query = f"{query}&has_annotation=true"
    annotated_samples = client.get(
        f"/api/datasets/{dataset['id']}/samples?{annotated_query}", headers=headers
    )
    annotated_statistics = client.get(
        f"/api/datasets/{dataset['id']}/statistics?{annotated_query}", headers=headers
    )
    assert annotated_samples.json()["total"] == 1
    assert annotated_statistics.json()["sample_count"] == 1
    assert annotated_statistics.json()["annotated_sample_count"] == 1
    assert annotated_statistics.json()["missing_annotation_count"] == 0


def test_complete_upload_accepts_a_multi_gib_archive(api_client) -> None:
    client, db, storage = api_client
    token = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    organization = db.query(Organization).one()
    dataset = client.post(
        "/api/datasets",
        headers=headers,
        json={"organization_id": str(organization.id), "name": "Large archive"},
    ).json()
    created = client.post(
        f"/api/datasets/{dataset['id']}/upload-sessions",
        headers=headers,
        json={"original_name": "large.7z", "batch_name": "large batch"},
    )
    assert created.status_code == 201
    upload = created.json()
    object_ref = ("dataset-platform", upload["object_key"])
    storage.objects[object_ref] = b"uploaded"
    storage.sizes[object_ref] = 9_479_409_751

    completed = client.post(
        f"/api/upload-sessions/{upload['id']}/complete", headers=headers, json={}
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "queued"
    assert db.get(UploadSession, uuid.UUID(upload["id"])).size_bytes == 9_479_409_751


def test_dataset_purge_preview_requires_the_exact_dataset_name(api_client) -> None:
    client, db, storage = api_client
    token = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    organization = db.query(Organization).one()
    dataset = client.post(
        "/api/datasets",
        headers=headers,
        json={"organization_id": str(organization.id), "name": "Purge me"},
    ).json()
    upload = client.post(
        f"/api/datasets/{dataset['id']}/upload-sessions",
        headers=headers,
        json={"original_name": "purge.zip", "batch_name": "purge batch"},
    ).json()
    storage.objects[("dataset-platform", upload["object_key"])] = b"archive"
    removed = client.delete(
        f"/api/datasets/{dataset['id']}?organization_id={organization.id}",
        headers=headers,
    )
    assert removed.status_code == 204

    preview = client.get(
        f"/api/datasets/{dataset['id']}/purge-preview?organization_id={organization.id}",
        headers=headers,
    )
    assert preview.status_code == 200
    assert preview.json()["object_count"] == 1
    assert preview.json()["source_upload_count"] == 1

    rejected = client.post(
        f"/api/datasets/{dataset['id']}/purge?organization_id={organization.id}",
        headers=headers,
        json={"confirmation_name": "wrong"},
    )
    assert rejected.status_code == 422

    queued = client.post(
        f"/api/datasets/{dataset['id']}/purge?organization_id={organization.id}",
        headers=headers,
        json={"confirmation_name": "Purge me"},
    )
    assert queued.status_code == 202
    assert queued.json()["job"]["job_type"] == "purge_dataset"
    assert db.get(Dataset, uuid.UUID(dataset["id"])).status == "purging"


def test_export_request_validates_supported_format_and_can_be_cancelled(api_client) -> None:
    client, db, _storage = api_client
    token = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    organization = db.query(Organization).one()
    dataset = client.post(
        "/api/datasets",
        headers=headers,
        json={"organization_id": str(organization.id), "name": "Road"},
    ).json()
    invalid = client.post(
        f"/api/datasets/{dataset['id']}/exports?organization_id={organization.id}",
        headers=headers,
        json={"format": "invalid"},
    )
    assert invalid.status_code == 422
    valid = client.post(
        f"/api/datasets/{dataset['id']}/exports?organization_id={organization.id}",
        headers=headers,
        json={"format": "coco"},
    )
    assert valid.status_code == 202
    job = valid.json()["job"]
    assert job["result_json"]["requested_format"] == "coco"
    cancelled = client.post(
        f"/api/jobs/{job['id']}/cancel?organization_id={organization.id}", headers=headers, json={}
    )
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"
    second_cancel = client.post(
        f"/api/jobs/{job['id']}/cancel?organization_id={organization.id}", headers=headers, json={}
    )
    assert second_cancel.status_code == 409


def test_upload_session_accepts_common_archives_and_rejects_unsupported_ones(api_client) -> None:
    client, db, _storage = api_client
    token = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    organization = db.query(Organization).one()
    dataset = client.post(
        "/api/datasets",
        headers=headers,
        json={"organization_id": str(organization.id), "name": "Archive formats"},
    ).json()
    for name, suffix in (
        ("images.7z", ".7z"),
        ("images.tar", ".tar"),
        ("images.tar.gz", ".tar.gz"),
        ("images.tgz", ".tgz"),
    ):
        response = client.post(
            f"/api/datasets/{dataset['id']}/upload-sessions",
            headers=headers,
            json={"original_name": name, "batch_name": name},
        )
        assert response.status_code == 201
        assert response.json()["object_key"].endswith(suffix)
    invalid = client.post(
        f"/api/datasets/{dataset['id']}/upload-sessions",
        headers=headers,
        json={"original_name": "images.rar", "batch_name": "unsupported"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "upload.archive_required"


def test_part_metadata_soft_delete_and_label_management(api_client) -> None:
    client, db, storage = api_client
    token = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    organization = db.query(Organization).one()
    dataset_response = client.post(
        "/api/datasets",
        headers=headers,
        json={"organization_id": str(organization.id), "name": "Managed parts"},
    )
    assert dataset_response.status_code == 201
    dataset_id = dataset_response.json()["id"]
    dataset = db.get(Dataset, uuid.UUID(dataset_id))
    assert dataset is not None
    version = DatasetVersion(
        dataset_id=dataset.id, version_number=1, created_by=db.query(User).one().id, status="ready"
    )
    db.add(version)
    db.flush()
    batch = ImportBatch(
        dataset_version_id=version.id,
        batch_number=1,
        batch_name="Original part",
        created_by=version.created_by,
    )
    label = LabelDefinition(dataset_version_id=version.id, class_id=0, class_name="old name")
    db.add_all([batch, label])
    db.commit()

    updated = client.patch(
        f"/api/datasets/{dataset_id}/import-batches/{batch.id}?organization_id={organization.id}",
        headers=headers,
        json={"batch_name": "Renamed part", "note": "field capture"},
    )
    assert updated.status_code == 200
    assert updated.json()["batch_name"] == "Renamed part"
    assert updated.json()["note"] == "field capture"

    label_response = client.put(
        f"/api/datasets/{dataset_id}/labels/0?organization_id={organization.id}",
        headers=headers,
        json={"class_name": "cat", "color": "#22AA88"},
    )
    assert label_response.status_code == 200
    assert label_response.json()["class_name"] == "cat"
    keypoints = client.put(
        f"/api/datasets/{dataset_id}/labels/0/keypoints?organization_id={organization.id}",
        headers=headers,
        json={"names": ["nose", "tail"]},
    )
    assert keypoints.status_code == 200
    assert keypoints.json()["names"] == ["nose", "tail"]

    batches = client.get(
        f"/api/datasets/{dataset_id}/import-batches?organization_id={organization.id}",
        headers=headers,
    )
    assert batches.status_code == 200
    assert batches.json()["items"][0]["note"] == "field capture"
    storage.objects[("dataset-platform", "uploads/rescan.zip")] = b"archive"
    upload = UploadSession(
        organization_id=organization.id,
        dataset_id=dataset.id,
        dataset_version_id=version.id,
        import_batch_id=batch.id,
        bucket="dataset-platform",
        object_key="uploads/rescan.zip",
        original_name="rescan.zip",
        created_by=version.created_by,
        status="ready",
    )
    db.add(upload)
    db.commit()
    rescan = client.post(
        f"/api/datasets/{dataset_id}/import-batches/{batch.id}/rescan?organization_id={organization.id}",
        headers=headers,
        json={},
    )
    assert rescan.status_code == 202
    assert rescan.json()["job"]["job_type"] == "scan_upload"
    deleted = client.delete(
        f"/api/datasets/{dataset_id}/import-batches/{batch.id}?organization_id={organization.id}",
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    history = client.get(
        f"/api/operation-history?organization_id={organization.id}&dataset_id={dataset_id}",
        headers=headers,
    )
    assert history.status_code == 200
    latest = history.json()["items"][0]
    assert latest["action"] == "import_batch.delete"
    undone = client.post(
        f"/api/operation-history/{latest['id']}/undo?organization_id={organization.id}",
        headers=headers,
    )
    assert undone.status_code == 200
    assert undone.json()["status"] == "undone"
    restored = client.get(
        f"/api/datasets/{dataset_id}/import-batches?organization_id={organization.id}",
        headers=headers,
    )
    assert restored.json()["total"] == 1
    redone = client.post(
        f"/api/operation-history/{latest['id']}/redo?organization_id={organization.id}",
        headers=headers,
    )
    assert redone.status_code == 200
    assert redone.json()["status"] == "applied"
