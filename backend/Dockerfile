# -----------------------------------------------------------------------------
# Whumpf backend — FastAPI + geospatial Python stack
#
# Using the official astral-sh/uv image gives us fast, reproducible installs.
# We use a slim debian base (not alpine) because the GDAL/rasterio wheels are
# built against glibc.
# -----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# System deps for geospatial Python libs.
# GDAL binaries are pulled in by rasterio's wheels, so we do NOT need the
# libgdal-dev package. We keep the layer small.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        libexpat1 \
        libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies in their own layer for good caching.
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-install-project 2>/dev/null || uv sync --no-install-project

# Copy application code.
COPY app ./app

# Install the project itself (picks up app/ as a package).
RUN uv sync 2>/dev/null || true

EXPOSE 8000

# uvicorn in dev mode — switch to gunicorn+uvicorn workers for prod.
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
