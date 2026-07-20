# Implementation status — 2026-07-17

## Assumptions adopted

- Deployment target: local-only Windows browser access through WSL2 Docker Compose.
- Stack: React + TypeScript, FastAPI, SQLAlchemy 2 / Alembic, PostgreSQL, MinIO, Redis / Celery, Nginx.
- Initial ingest: ZIP, 7z, and TAR-family archives uploaded directly to object storage; parsing and materialization are background jobs.
- Existing desktop application is not present in this workspace and is therefore not modified.

## Delivered

| Area | Status |
| --- | --- |
| ZIP, 7z, and TAR-family safety scan; YOLO, COCO, LabelMe, Pascal VOC semantic parsing; normalized annotation manifest | Done |
| PostgreSQL schema and Alembic initial migration | Done |
| Portable MinIO `StorageService` adapter | Done |
| Local account bootstrap and organization authorization boundary | Done |
| Dataset CRUD, direct archive upload session/progress, scan preview, confirmation, ImportBatch and Sample browsing/filtering | Done |
| Async scan/import/quality/export jobs; manifest, YOLO, COCO export artifacts; Worker-owned temporary volume | Done |
| React browser shell for sign-in, dataset creation, archive submission, job polling | Done |
| WSL2 Compose, Nginx, named volumes, backup/restore scripts, deployment docs | Done |

## Validation performed on this Windows workspace

```text
pytest: 17 passed
Job cancellation and audit logging: implemented; API cancellation behavior covered by tests
ruff: passed
Alembic head: 0004_upload_idempotency
Alembic offline PostgreSQL SQL generation: passed
SQLite migration execution through revision 0004: passed
React TypeScript check and production Vite build: passed
Compose YAML structure: parsed and expected eight services found
WSL2 shell syntax for preflight/start/stop/backup/restore scripts: passed
Readiness checks include PostgreSQL, Redis, MinIO, Worker ping, and disk capacity
```

## Remaining validation after Docker is installed/enabled in WSL2

1. Copy `deploy/wsl2/env.example` to `.env`, replace every placeholder secret, and start Compose.
2. Execute `alembic upgrade head` and `python -m app.bootstrap` inside the API container.
3. Complete a real browser archive upload (ZIP, 7z, or TAR family) and verify MinIO direct PUT, Worker scan, normalized annotation overlay, confirmation, asset materialization, and all export formats.
4. Exercise backup then restore against a disposable named-volume environment.
5. Install/enable Docker, then run `./deploy/wsl2/scripts/preflight.sh` (it currently correctly reports Docker as unavailable), followed by Docker-backed PostgreSQL/Redis/MinIO integration tests and backup/restore recovery testing.








