# Installation and operations

## Requirements

- Docker Engine or Docker Desktop with Docker Compose v2.
- Python 3.11 or later to create deployment secrets.
- Local ports 8080, 8000, 9000, and 9001 available. Phoenix uses port 6006 when enabled.

## Start a clean deployment

```bash
python3 scripts/init_env.py
docker compose config
docker compose up -d --build
docker compose ps
```

Keep the generated `.env` private. The generated initial admin password is printed once; store it in a password manager. The generator refuses to overwrite an existing `.env`. Change service settings by editing `.env`, then recreate affected services. Compose requires the JWT secret, administrator password, PostgreSQL password, and MinIO password rather than substituting public defaults. If an existing `.env` predates a required variable, copy the missing line from `.env.example` or move the old file aside and regenerate it.

`UNIFICATION_MAX_ATTEMPTS` sets the maximum worker attempts per unification run. The default is 3, and a value below 1 fails startup. The API exposes the attempt count, error message, and last interruption for each run.

The API applies Alembic migrations on startup. Confirm readiness at `http://localhost:8000/health`; use `docker compose logs backend worker` for startup and task errors. The web workbench is at `http://localhost:8080`.

## Data and restart behavior

PostgreSQL, MinIO, and optional Phoenix use named volumes. The MinIO volume mounts at `/bitnami/minio/data` in the Bitnami-published image. `docker compose restart` or `docker compose down` preserves these volumes. `docker compose down -v` removes them and permanently deletes stored workspace data, uploaded objects, and traces. A pre-release volume created with the previous `/data` path must be migrated or recreated before upgrading.

## Optional Phoenix tracing

Set `PHOENIX_ENABLED=true` and keep `PHOENIX_COLLECTOR_ENDPOINT=http://phoenix:6006/v1/traces` in `.env`, then run:

```bash
docker compose --profile observability up -d phoenix
docker compose up -d --force-recreate backend worker
```

Open Phoenix at `http://localhost:6006`. Tracing is best-effort; database audit records remain the durable business history. A trace link requires the optional Phoenix service to be running and retaining the trace.

## Stop or remove

```bash
docker compose down       # stop and keep data
docker compose down -v    # remove services and all named data volumes
```

Do not run `down -v` against a deployment whose data you need.
