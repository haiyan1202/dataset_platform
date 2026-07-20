from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Dataset Platform"
    app_env: str = "development"
    api_prefix: str = "/api"
    database_url: str = "postgresql+psycopg://dataset:dataset@postgres:5432/dataset_platform"
    redis_url: str = "redis://redis:6379/0"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket: str = "dataset-platform"
    # Use Nginx as a same-origin streaming proxy so browser uploads work from both Windows and WSL.
    minio_public_endpoint: str = "/storage"
    token_secret: str = Field(min_length=32, default="replace-this-development-secret-before-deploy")
    token_ttl_seconds: int = 3600 * 8
    max_upload_bytes: int = 10 * 1024 * 1024 * 1024
    worker_task_soft_time_limit: int = 60 * 60
    worker_task_time_limit: int = 60 * 70
    # Large archive jobs are memory- and I/O-heavy. Keep the worker deliberately
    # small so a single import cannot starve every other task or exhaust RAM.
    worker_concurrency: int = 2
    worker_prefetch_multiplier: int = 1
    worker_max_tasks_per_child: int = 20
    # Recycle a child after memory-heavy archive extraction/materialization. Celery expects KiB.
    worker_max_memory_per_child: int = 1_536_000
    worker_temp_reserve_bytes: int = 1 * 1024 * 1024 * 1024
    import_commit_batch_size: int = 200
    # Only unmarked/idle temporary directories older than this are eligible for cleanup.
    worker_temp_stale_seconds: int = 60 * 60
    health_min_free_bytes: int = 1 * 1024 * 1024 * 1024
    bootstrap_email: str = Field(default="admin@example.local", validation_alias=AliasChoices("APP_BOOTSTRAP_EMAIL", "BOOTSTRAP_EMAIL"))
    bootstrap_password: str = Field(default="change-me-before-deploy", validation_alias=AliasChoices("APP_BOOTSTRAP_PASSWORD", "BOOTSTRAP_PASSWORD"))
    bootstrap_organization: str = Field(default="Default Workspace", validation_alias=AliasChoices("APP_BOOTSTRAP_ORGANIZATION", "BOOTSTRAP_ORGANIZATION"))


@lru_cache
def get_settings() -> Settings:
    return Settings()



