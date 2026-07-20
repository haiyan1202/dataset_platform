from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    bucket: str
    object_key: str
    size_bytes: int
    content_type: str | None = None
    etag: str | None = None


class StorageService(Protocol):
    def ensure_bucket(self, bucket: str) -> None: ...
    def object_exists(self, bucket: str, object_key: str) -> bool: ...
    def stat(self, bucket: str, object_key: str) -> ObjectMetadata: ...
    def create_upload_url(self, bucket: str, object_key: str, expires: timedelta) -> str: ...
    def create_download_url(self, bucket: str, object_key: str, expires: timedelta) -> str: ...
    def read_bytes(self, bucket: str, object_key: str) -> bytes: ...
    def download_to_file(self, bucket: str, object_key: str, file_path: str) -> None: ...

    def put_bytes(self, bucket: str, object_key: str, content: bytes, content_type: str | None = None) -> None: ...
    def upload_file(self, bucket: str, object_key: str, file_path: str, content_type: str | None = None) -> None: ...
    def delete(self, bucket: str, object_key: str) -> None: ...


class MinIOStorageAdapter:
    """MinIO adapter; swap this class for S3/OSS/COS without changing services."""

    def __init__(self, *, endpoint: str, access_key: str, secret_key: str, secure: bool, public_endpoint: str | None = None) -> None:
        from minio import Minio

        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._public_endpoint = public_endpoint.rstrip("/") if public_endpoint else None

    def ensure_bucket(self, bucket: str) -> None:
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)

    def object_exists(self, bucket: str, object_key: str) -> bool:
        try:
            self._client.stat_object(bucket, object_key)
            return True
        except Exception:  # SDK error is intentionally normalized at the boundary.
            return False

    def stat(self, bucket: str, object_key: str) -> ObjectMetadata:
        result = self._client.stat_object(bucket, object_key)
        return ObjectMetadata(bucket, object_key, result.size, result.content_type, result.etag)

    def create_upload_url(self, bucket: str, object_key: str, expires: timedelta) -> str:
        url = self._client.presigned_put_object(bucket, object_key, expires=expires)
        return self._to_public_url(url)

    def create_download_url(self, bucket: str, object_key: str, expires: timedelta) -> str:
        url = self._client.presigned_get_object(bucket, object_key, expires=expires)
        return self._to_public_url(url)

    def read_bytes(self, bucket: str, object_key: str) -> bytes:
        response = self._client.get_object(bucket, object_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()


    def download_to_file(self, bucket: str, object_key: str, file_path: str) -> None:
        self._client.fget_object(bucket, object_key, file_path)

    def put_bytes(self, bucket: str, object_key: str, content: bytes, content_type: str | None = None) -> None:
        self._client.put_object(
            bucket,
            object_key,
            BytesIO(content),
            length=len(content),
            content_type=content_type or "application/octet-stream",
        )

    def upload_file(self, bucket: str, object_key: str, file_path: str, content_type: str | None = None) -> None:
        self._client.fput_object(bucket, object_key, file_path, content_type=content_type or "application/octet-stream")

    def delete(self, bucket: str, object_key: str) -> None:
        self._client.remove_object(bucket, object_key)

    def _to_public_url(self, url: str) -> str:
        if not self._public_endpoint:
            return url
        # The SDK signs path/query; only replace the internal authority.
        scheme_marker = url.find("://")
        path_start = url.find("/", scheme_marker + 3)
        suffix = url[path_start:] if path_start >= 0 else ""
        return f"{self._public_endpoint}{suffix}"






