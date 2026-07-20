from __future__ import annotations

import os
import time
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import hash_password
from app.db.base import Base
from app.jobs import tasks
from app.models import AnnotationIndex, Asset, Dataset, DatasetVersion, ImportBatch, Job, Membership, Organization, Sample, SampleClassIndex, UploadSession, User
from app.storage.service import ObjectMetadata


class PurgeStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def object_exists(self, bucket: str, object_key: str) -> bool:
        return (bucket, object_key) in self.objects

    def stat(self, bucket: str, object_key: str) -> ObjectMetadata:
        return ObjectMetadata(bucket, object_key, len(self.objects[(bucket, object_key)]))

    def delete(self, bucket: str, object_key: str) -> None:
        self.objects.pop((bucket, object_key), None)


def test_purge_dataset_removes_objects_rows_and_stale_worker_temp(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'purge.db'}")
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    storage = PurgeStorage()
    monkeypatch.setattr(tasks, 'SessionLocal', factory)
    monkeypatch.setattr(tasks, 'get_storage', lambda: storage)
    worker_root = tmp_path / 'worker-temp'
    monkeypatch.setenv('WORKER_TMP_DIR', str(worker_root))

    with factory() as db:
        organization = Organization(name='Purge Org')
        user = User(email='purge@example.test', password_hash=hash_password('safe-password'))
        db.add_all([organization, user])
        db.flush()
        db.add(Membership(organization_id=organization.id, user_id=user.id, role='owner'))
        dataset = Dataset(organization_id=organization.id, name='Purge Dataset', created_by=user.id)
        db.add(dataset)
        db.flush()
        version = DatasetVersion(dataset_id=dataset.id, version_number=1, created_by=user.id)
        db.add(version)
        db.flush()
        batch = ImportBatch(dataset_version_id=version.id, batch_number=1, batch_name='batch', created_by=user.id)
        db.add(batch)
        db.flush()
        prefix = f"org/{organization.id}/datasets/{dataset.id}/versions/{version.id}/"
        image = Asset(organization_id=organization.id, bucket='dataset-platform', object_key=f'{prefix}raw/image.jpg', original_name='image.jpg', relative_path='image.jpg', asset_type='image', size_bytes=5)
        normalized = Asset(organization_id=organization.id, bucket='dataset-platform', object_key=f'{prefix}normalized/image.jpg.json', original_name='image.json', relative_path='normalized/image.jpg.json', asset_type='normalized_annotation', size_bytes=7)
        db.add_all([image, normalized])
        db.flush()
        sample = Sample(dataset_version_id=version.id, import_batch_id=batch.id, image_asset_id=image.id, file_name='image.jpg', file_stem='image', relative_path='image.jpg')
        db.add(sample)
        db.flush()
        db.add_all([
            SampleClassIndex(sample_id=sample.id, class_id=0),
            AnnotationIndex(sample_id=sample.id, normalized_annotation_asset_id=normalized.id),
        ])
        upload_key = f"org/{organization.id}/datasets/{dataset.id}/uploads/source.zip"
        upload = UploadSession(organization_id=organization.id, dataset_id=dataset.id, dataset_version_id=version.id, import_batch_id=batch.id, bucket='dataset-platform', object_key=upload_key, original_name='source.zip', size_bytes=11, created_by=user.id)
        db.add(upload)
        db.flush()
        job = Job(organization_id=organization.id, job_type='purge_dataset', resource_type='dataset', resource_id=dataset.id, idempotency_key=f'purge-{uuid.uuid4()}', requested_by=user.id)
        db.add(job)
        db.commit()
        job_id, dataset_id = job.id, dataset.id
        storage.objects[('dataset-platform', image.object_key)] = b'image'
        storage.objects[('dataset-platform', normalized.object_key)] = b'normal!'
        storage.objects[('dataset-platform', upload.object_key)] = b'archive-data'

    stale = worker_root / 'tmp-stale'
    stale.mkdir(parents=True)
    (stale / 'orphan.bin').write_bytes(b'orphan')
    old = time.time() - 7200
    os.utime(stale, (old, old))

    result = tasks.purge_dataset.run(str(job_id))
    assert result['deleted_objects'] == 3
    assert result['deleted_samples'] == 1
    assert result['removed_temp_directories'] == 1
    assert not stale.exists()
    assert storage.objects == {}

    with factory() as db:
        assert db.get(Dataset, dataset_id) is None
        assert db.query(Sample).count() == 0
        assert db.query(Asset).count() == 0
        assert db.query(UploadSession).count() == 0
        assert db.get(Job, job_id).status == 'succeeded'

    Base.metadata.drop_all(engine)
