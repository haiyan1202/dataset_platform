from __future__ import annotations

import os
import shutil
import time
import uuid
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import api_router
from app.db import SessionLocal
from app.settings import get_settings
from app.storage import get_storage

settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", docs_url=f"{settings.api_prefix}/docs", openapi_url=f"{settings.api_prefix}/openapi.json")
app.include_router(api_router, prefix=settings.api_prefix)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next: Callable):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-Ms"] = str(round((time.perf_counter() - started) * 1000, 2))
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": {"code": "request.validation_failed", "details": exc.errors()}})


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    # Deliberately do not leak connection strings, object keys or stack traces.
    return JSONResponse(status_code=500, content={"error": {"code": "internal.error"}})


@app.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> dict:
    checks: dict[str, str] = {}
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "failed"
    try:
        from redis import Redis
        Redis.from_url(settings.redis_url, socket_connect_timeout=1).ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "failed"
    try:
        storage = get_storage()
        storage.ensure_bucket(settings.minio_bucket)
        checks["object_storage"] = "ok"
    except Exception:
        checks["object_storage"] = "failed"
    try:
        free_bytes = shutil.disk_usage(os.environ.get("WORKER_TMP_DIR", "/tmp")).free
        checks["disk"] = "ok" if free_bytes >= settings.health_min_free_bytes else "low_space"
    except Exception:
        checks["disk"] = "failed"
    try:
        from app.jobs.celery_app import celery_app
        checks["worker"] = "ok" if celery_app.control.ping(timeout=0.5) else "unavailable"
    except Exception:
        checks["worker"] = "failed"
    response_status = "ok" if all(value == "ok" for value in checks.values()) else "degraded"
    return {"status": response_status, "checks": checks}
