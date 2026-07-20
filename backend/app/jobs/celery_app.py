from celery import Celery

from app.settings import get_settings

settings = get_settings()
celery_app = Celery("dataset_platform", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    broker_connection_retry_on_startup=True,
    # Acknowledge only after the DB-backed job has completed. If systemd or the
    # worker process dies mid-import, Redis can redeliver the task instead of
    # leaving the UI permanently on queued/running.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=settings.worker_prefetch_multiplier,
    worker_concurrency=settings.worker_concurrency,
    worker_max_tasks_per_child=settings.worker_max_tasks_per_child,
    task_soft_time_limit=settings.worker_task_soft_time_limit,
    task_time_limit=settings.worker_task_time_limit,
)
celery_app.autodiscover_tasks(["app.jobs"])

