# WSL2 / Docker Compose deployment

## Prerequisites

- Windows 10/11 with **Ubuntu 22.04 on WSL2** (this workstation has that distribution available);
- Docker Engine inside WSL2, or Docker Desktop with that distribution enabled under **WSL Integration**;
- use only one Docker daemon. Docker is not currently installed in this workstation's Ubuntu distribution, so install/enable it before executing the commands below;
- at least 4 CPU cores, 8 GB RAM, and enough disk for the uploaded ZIP, extracted assets, MinIO data, and backups.

All browser-facing ports bind to `127.0.0.1`; PostgreSQL and Redis are isolated on the Compose network. Persistent PostgreSQL, MinIO, Redis, and Worker temporary data use named volumes.

## First startup

```bash
cd ~/projects/dataset-platform
cp deploy/wsl2/env.example deploy/wsl2/.env
# Edit deploy/wsl2/.env: replace every password and TOKEN_SECRET.
chmod +x deploy/wsl2/scripts/*.sh
./deploy/wsl2/scripts/start.sh
```

The start script builds services, starts Compose, runs Alembic migrations, and creates the local bootstrap account/workspace. Verify:

```bash
curl http://127.0.0.1:8080/health/ready
docker compose --env-file deploy/wsl2/.env -f deploy/wsl2/compose.yaml ps
```

Use the bootstrap account from `.env`; API docs are exposed through Nginx at `/api/docs`.

## Operations

```bash
# Follow services
docker compose --env-file deploy/wsl2/.env -f deploy/wsl2/compose.yaml logs -f api worker

# Stop without deleting named volumes
./deploy/wsl2/scripts/stop.sh

# Rebuild and migrate after code changes
./deploy/wsl2/scripts/start.sh
```

## Backup and restore

Back up both PostgreSQL **and** object data. A database dump alone cannot restore image or annotation objects.

```bash
./deploy/wsl2/scripts/backup.sh ./backups
./deploy/wsl2/scripts/restore.sh ./backups/DATE_TIME
```

Restore prompts for explicit confirmation. Stop API and Worker first, and use a disposable or intentionally reset environment because PostgreSQL restore can conflict with existing data.

## Cloud replacement points

| Local component | Cloud replacement |
| --- | --- |
| `postgres` | managed PostgreSQL |
| `redis` | managed Redis / compatible queue |
| `minio` | S3, OSS, COS, or hosted MinIO adapter |
| 
ginx` | load balancer/API gateway |
| Docker worker | horizontally scaled worker service |

Keep `bucket + object_key` identifiers, `StorageService`, environment variables, normalized annotation JSON, and the Job state machine. This permits infrastructure replacement without rewriting core parsing or application services.

