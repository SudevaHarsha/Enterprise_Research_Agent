"""Unit tests for ``app.core.config``.

Covers: env-driven settings with safe defaults when no ``.env`` exists,
G-13 guardrail locking (any ``GUARDRAIL_*`` disable attempt raises a pydantic
``ValidationError``), secret fields referenced by NAME only (Ironclad Rule 01),
and the cached settings singleton.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings, get_settings

GUARDRAIL_ENV_KEYS = (
    "GUARDRAIL_EGRESS_ALLOWLIST_ENABLED",
    "GUARDRAIL_VERIFY_FIRST_ENABLED",
    "GUARDRAIL_REDACTION_ENABLED",
    "GUARDRAIL_UNSAFE_CONTENT_ENABLED",
)

GUARDRAIL_FIELDS = (
    "guardrail_egress_allowlist_enabled",
    "guardrail_verify_first_enabled",
    "guardrail_redaction_enabled",
    "guardrail_unsafe_content_enabled",
)


def _clear_ecrke_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ECRKE-related environment variables for a hermetic settings test."""
    for key in (
        "APP_ENV",
        "APP_DEBUG",
        "LOG_LEVEL",
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "LLM_OPENAI_API_KEY",
        "LLM_ANTHROPIC_API_KEY",
        "LLM_GOOGLE_API_KEY",
        "SEARCH_API_KEY",
        "BLOB_ACCESS_KEY",
        "BLOB_SECRET_KEY",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        *GUARDRAIL_ENV_KEYS,
    ):
        monkeypatch.delenv(key, raising=False)


def test_config_loads_with_defaults_when_no_env_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``.env`` present -> defaults apply, no crash."""
    _clear_ecrke_env(monkeypatch)
    settings = Settings()
    assert settings.app_env == "development"
    assert settings.app_debug is False
    assert settings.log_level == "INFO"
    assert settings.otel_exporter_otlp_endpoint == ""
    assert settings.guardrail_egress_allowlist_enabled is True
    assert settings.guardrail_verify_first_enabled is True
    assert settings.guardrail_redaction_enabled is True
    assert settings.guardrail_unsafe_content_enabled is True


def test_config_loads_from_env_file(tmp_path: Path) -> None:
    """A valid ``.env`` file is honored without error."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=test\nLOG_LEVEL=DEBUG\nGUARDRAIL_EGRESS_ALLOWLIST_ENABLED=true\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)
    assert settings.app_env == "test"
    assert settings.log_level == "DEBUG"
    assert settings.guardrail_egress_allowlist_enabled is True


@pytest.mark.parametrize(
    ("env_key", "disabled_value"),
    [
        ("GUARDRAIL_EGRESS_ALLOWLIST_ENABLED", "false"),
        ("GUARDRAIL_VERIFY_FIRST_ENABLED", "0"),
        ("GUARDRAIL_REDACTION_ENABLED", "off"),
        ("GUARDRAIL_UNSAFE_CONTENT_ENABLED", "no"),
    ],
)
def test_guardrail_disable_via_env_var_raises(
    monkeypatch: pytest.MonkeyPatch,
    env_key: str,
    disabled_value: str,
) -> None:
    """A ``GUARDRAIL_*`` env var set to a disabled value must raise (G-13)."""
    _clear_ecrke_env(monkeypatch)
    monkeypatch.setenv(env_key, disabled_value)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("guardrail_field", GUARDRAIL_FIELDS)
def test_guardrail_disable_via_kwarg_raises(guardrail_field: str) -> None:
    """A ``GUARDRAIL_*`` constructor kwarg set to False must raise (G-13)."""
    with pytest.raises(ValidationError):
        Settings(**{guardrail_field: False})


def test_guardrail_disable_via_env_file_raises(tmp_path: Path) -> None:
    """A ``.env`` file trying to disable a guardrail must raise (G-13)."""
    env_file = tmp_path / ".env"
    env_file.write_text("GUARDRAIL_UNSAFE_CONTENT_ENABLED=off\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        Settings(_env_file=env_file)


def test_secret_fields_are_secretstr_and_never_serialized() -> None:
    """Secrets are typed ``SecretStr`` and never appear in plaintext output."""
    settings = Settings(
        llm_openai_api_key="sk-test-value-123",
        blob_secret_key="super-secret-value-456",
    )
    assert isinstance(settings.llm_openai_api_key, SecretStr)
    assert settings.llm_openai_api_key.get_secret_value() == "sk-test-value-123"
    assert settings.blob_secret_key.get_secret_value() == "super-secret-value-456"

    dumped = settings.model_dump_json()
    assert "sk-test-value-123" not in dumped
    assert "super-secret-value-456" not in dumped
    assert "llm_openai_api_key" in dumped
    assert "blob_secret_key" in dumped

    assert "sk-test-value-123" not in repr(settings)
    assert "super-secret-value-456" not in repr(settings)


def test_get_settings_returns_cached_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_ecrke_env(monkeypatch)
    assert get_settings() is get_settings()
