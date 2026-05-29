#!/bin/sh
# Pick the uvicorn invocation based on WHUMPF_ENV. The dev path watches
# for file changes; the prod path doesn't (--reload spawns a watcher
# process per file, kills multi-worker support, and adds ~50ms request
# overhead).
set -e

if [ "$WHUMPF_ENV" = "prod" ]; then
    exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --forwarded-allow-ips='*'
else
    exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
fi
