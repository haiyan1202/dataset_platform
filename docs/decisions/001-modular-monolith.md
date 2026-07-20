# ADR-001: Modular monolith with portable infrastructure adapters

- **Status:** Accepted (2026-07-16)
- **Decision:** Implement the first Web release as a FastAPI modular monolith, backed by PostgreSQL, Redis/Celery, and a `StorageService` abstraction with MinIO as the local adapter. The frontend is React + TypeScript and communicates through Nginx.
- **Context:** Development begins on Windows, production-like local operation is on WSL2, and future migration to managed cloud services must not require a rewrite.
- **Consequences:** Persistent resource identifiers contain only `bucket + object_key`; long-running imports, checks and exports use durable Jobs; configuration is entirely environment-driven. Kubernetes and microservices are explicitly deferred.
- **Package naming note:** The implementation uses `backend/app` rather than a top-level Python package named `platform`, because `platform` is a Python standard-library module and shadowing it would harm tooling and WSL deployments.
