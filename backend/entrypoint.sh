#!/bin/sh
# Pick the uvicorn invocation based on WHUMPF_ENV. The dev path watches
# for file changes; the prod path doesn't (--reload spawns a watcher
# process per file, kills multi-worker support, and adds ~50ms request
# overhead).
set -e

# Apply schema migrations once, before any worker starts (running them in the
# app's lifespan would race across the 4 prod workers). A failed migration
# exits here and the API never starts on a half-migrated schema.
uv run alembic upgrade head

if [ "$WHUMPF_ENV" = "prod" ]; then
    exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --forwarded-allow-ips='*'
else
    exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
fi
