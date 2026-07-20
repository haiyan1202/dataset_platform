from functools import lru_cache

from app.settings import get_settings
from .service import MinIOStorageAdapter, ObjectMetadata, StorageService


@lru_cache
def get_storage() -> StorageService:
    settings = get_settings()
    return MinIOStorageAdapter(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
        public_endpoint=settings.minio_public_endpoint,
    )


__all__ = ["StorageService", "ObjectMetadata", "MinIOStorageAdapter", "get_storage"]
