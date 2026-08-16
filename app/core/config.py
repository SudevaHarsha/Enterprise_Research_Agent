"""Environment-driven application settings (pydantic-settings).

Values come from environment variables and/or a local ``.env`` file. Secret
fields are typed ``SecretStr`` and referenced by environment variable NAME
only — never paste a credential value into code, logs, or any repository
artifact (Ironclad Rule 01).

Guardrail flags are immutable: any attempt to disable a ``GUARDRAIL_*`` flag —
via environment variable, ``.env`` file, or constructor kwarg — raises a
pydantic ``ValidationError`` (G-13; mirrors system Rule 12).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_GUARDRAIL_FIELD_PREFIX = "guardrail"
_DISABLED_VALUES = frozenset({"", "0", "false", "off", "no", "f", "n", "disabled", "disable"})


def _is_disabled_value(value: Any) -> bool:
    """Return True when a config value represents a disabled switch."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return value.strip().lower() in _DISABLED_VALUES
    if isinstance(value, (int, float)):
        return value == 0
    return False


class Settings(BaseSettings):
    """Application configuration; every field has a safe default."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_env: AppEnv = "development"
    app_debug: bool = False
    log_level: LogLevel = "INFO"

    # Database (relational provenance core; dev defaults match docker-compose)
    database_url: str = "postgresql+asyncpg://ecrke:ecrke_dev@localhost:5433/ecrke"
    postgres_db: str = "ecrke"
    postgres_user: str = "ecrke"
    postgres_password: SecretStr | None = None
    prefect_api_database_connection_url: str = (
        "postgresql+asyncpg://ecrke:ecrke_dev@localhost:5433/ecrke_prefect"
    )
    # Empty by default: the pipeline runner stays in-process unless the
    # operator explicitly sets PREFECT_API_URL (task_013 submission mode).
    prefect_api_url: str = ""

    # LLM providers — keys are secrets; set by NAME via env only
    llm_openai_api_key: SecretStr | None = None
    llm_anthropic_api_key: SecretStr | None = None
    llm_google_api_key: SecretStr | None = None
    llm_model_cheap: str = "gemini/gemini-2.0-flash"
    llm_model_strong: str = "gemini/gemini-2.0-pro"

    # Search / retrieval
    search_api_provider: str = ""
    search_api_key: SecretStr | None = None
    # Canonical provider selector (SEARCH_PROVIDER); falls back to the legacy
    # SEARCH_API_PROVIDER field when unset. Supported: mock, brave, serpapi.
    search_provider: str = ""
    search_results_limit: int = 10
    # Comma-separated; parsed by the egress allowlist (task_005)
    allowed_domains: str = ""

    # Fetching (G-06 egress sandbox; task_005)
    fetch_min_interval_seconds: float = 1.0
    fetch_timeout_seconds: float = 30.0

    # Blob storage (S3-compatible) — keys are secrets
    blob_endpoint: str = ""
    blob_bucket: str = ""
    blob_access_key: SecretStr | None = None
    blob_secret_key: SecretStr | None = None
    # Content-addressed raw-source store: "local" (default) or "s3" (optional)
    blob_store_backend: str = "local"
    blob_store_dir: str = ".blobs"

    # Run governance
    run_budget_usd: float = 2.0
    circuit_breaker_max_stage_failures: int = 3

    # Guardrails — non-negotiable (G-13). Default on; disabling raises.
    guardrail_egress_allowlist_enabled: bool = True
    guardrail_verify_first_enabled: bool = True
    guardrail_redaction_enabled: bool = True
    guardrail_unsafe_content_enabled: bool = True

    # Observability (task_003 scaffolding; task_013 wires exporters/metrics)
    otel_service_name: str = "ecrke"
    otel_exporter_otlp_endpoint: str = ""

    @model_validator(mode="before")
    @classmethod
    def _reject_guardrail_disable(cls, data: Any) -> Any:
        """Reject any GUARDRAIL_* flag set to a disabled value (G-13)."""
        if not isinstance(data, dict):
            return data
        for key, value in data.items():
            if (
                isinstance(key, str)
                and key.lower().startswith(_GUARDRAIL_FIELD_PREFIX)
                and _is_disabled_value(value)
            ):
                raise ValueError(
                    f"Guardrail flag {key.upper()} cannot be disabled (G-13): "
                    "guardrails are non-negotiable and may not be turned off via "
                    "configuration. Remove the override and redeploy."
                )
        return data


@lru_cache
def get_settings() -> Settings:
    """Return the application settings singleton (cached per process)."""
    return Settings()
