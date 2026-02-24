# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Commands (verified)
```bash
# Install
uv sync --all-extras

# Test (full suite)
pytest

# Test (single)
pytest tests/api/test_orders.py::test_create_order

# Lint + format
ruff check . --fix && ruff format .

# Type check
ty check .
```

## Repo map (only what's non-obvious)

- `services/api/` — FastAPI app; routes under `routers/`, business logic under `domain/`
- `services/worker/` — Celery worker; task definitions in `tasks/`, shared models imported from `packages/shared`
- `packages/shared/` — internal package shared by api + worker; do not import from `services/` here
- `infra/` — Terraform only; not relevant to application changes
- `scripts/seed_dev_db.py` — populates local Postgres with fixture data; safe to re-run

## Invariants / footguns

- `packages/shared/models.py` is the source of truth for DB schema. Do not define models in `services/`; import them.
- Alembic migrations live in `services/api/migrations/`. After changing a model, run `alembic revision --autogenerate -m "<desc>"` and review the diff before committing — autogenerate misses some constraints.
- Worker tasks must be registered in `services/worker/celery_app.py`; adding a task file without registering it will silently not execute.
- `pytest` runs against a real Postgres instance spun up via Docker. If tests hang, check that `docker compose up db` is running.

## Where changes usually go

- New API endpoint → `services/api/routers/` + matching test in `tests/api/`
- New background job → `services/worker/tasks/` + register in `celery_app.py`
- Shared validation logic → `packages/shared/validators.py`
