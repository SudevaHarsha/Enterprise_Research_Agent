FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install CA certs for HTTPS fetches in the slim image
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

# Phase 1: Install Python dependencies first (best layer caching)
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Phase 2: Copy all source code and config
COPY app ./app
COPY static ./static
COPY scripts ./scripts
COPY sample_data ./sample_data
COPY alembic.ini ./
COPY alembic ./alembic

# Non-root user
RUN useradd --create-home --uid 1000 ecrke && chown -R ecrke:ecrke /app && mkdir -p /app/.blobs && chown -R ecrke:ecrke /app/.blobs
USER ecrke

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && gunicorn app.main:app --workers 2 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000"]
