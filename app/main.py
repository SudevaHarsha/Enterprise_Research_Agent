"""API entrypoint.

FastAPI application: health endpoints (liveness/readiness) are mounted from
``app.api.health`` and the evaluator-facing ``/v1`` surface from
``app.api.routes``. Structured JSONL logging and the OpenTelemetry scaffold are
configured in the startup lifespan. Error responses follow RFC 7807 (Problem
Details for HTTP APIs): ``HTTPException`` and ``RequestValidationError`` are
rendered as ``application/problem+json`` with G-05 redaction applied to detail
text, so secret-looking substrings never reach an error body (Rule 01).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api.health import router as health_router
from app.api.routes import router as api_router
from app.api.schemas import ErrorDetail
from app.core.logging import configure_logging
from app.core.telemetry import setup_telemetry
from app.services.audit_writer import redact_json
from app.services.normalizer import redact_secrets


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    setup_telemetry()
    yield


def _status_title(status_code: int) -> str:
    """RFC 7807 title: the HTTP status reason phrase."""
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _problem_response(request: Request, status_code: int, detail: str) -> JSONResponse:
    """Render an RFC 7807 Problem Details JSON response (detail redacted)."""
    body = ErrorDetail(
        title=_status_title(status_code),
        status=status_code,
        detail=redact_secrets(detail),
        instance=str(request.url),
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        media_type="application/problem+json",
    )


def _register_exception_handlers(app: FastAPI) -> None:
    """Register RFC 7807 handlers for HTTPException and validation errors."""

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return _problem_response(request, exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        detail = redact_json(json.dumps(exc.errors(), default=str))
        return _problem_response(request, 422, detail)


def create_app() -> FastAPI:
    app = FastAPI(
        title="ECRKE - Evidence-Centric Research Knowledge Engine",
        description=(
            "Enterprise AI research agent: submit a research question, observe the "
            "10-stage pipeline, and audit every conclusion back to its source passage."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(api_router)
    _register_exception_handlers(app)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": "ecrke", "version": __version__}

    return app


app = create_app()
