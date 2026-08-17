FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install project + dependencies first for layer caching
COPY pyproject.toml README.md ./
COPY app ./app
COPY static ./static
COPY scripts ./scripts
COPY sample_data ./sample_data
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --upgrade pip && pip install .

# Non-root user
RUN useradd --create-home --uid 1000 ecrke && chown -R ecrke:ecrke /app && mkdir -p /app/.blobs && chown -R ecrke:ecrke /app/.blobs
USER ecrke

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
