"""LiteLLM-backed LLM gateway for ECRKE (build-plan Step 4).

Provides tiered model routing (cheap/strong from Settings), Pydantic
schema-validated structured output with bounded retry -> quarantine
(G-11: max 2 retries, each a different approach), repeat-call caching
(key = hash(model + prompt + inputs)), and a before-call hook so the
task_011 circuit breaker can pause spend (G-03). Every successful call is
metered onto ``runs.cost_spent_usd``.

No credentials are ever logged or persisted: API keys are installed into the
LiteLLM environment by name only (Ironclad Rule 01) and cache payloads carry
answer data exclusively.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.cost_meter import CostMeter
from app.services.kv_cache import KVCache, build_key, build_prompt_hash

logger = get_logger("app.services.llm_gateway")

ModelTier = Literal["cheap", "strong"]

# LiteLLM environment variable names per Settings SecretStr field (installed by NAME only).
_PROVIDER_ENV_MAP: dict[str, str] = {
    "llm_openai_api_key": "OPENAI_API_KEY",
    "llm_anthropic_api_key": "ANTHROPIC_API_KEY",
    "llm_google_api_key": "GEMINI_API_KEY",
}


class ModelTierError(ValueError):
    """Raised when an unknown model tier is requested."""


class QuarantineError(RuntimeError):
    """Raised when structured output failed schema validation on every retry (G-11)."""


class CircuitBreakerOpenError(RuntimeError):
    """Raised by a before-call hook to pause spend (G-03, task_011 integration)."""


@dataclass(frozen=True)
class GatewayCallContext:
    """Snapshot of one provider attempt, handed to before-call hooks."""

    model: str
    tier: ModelTier
    messages: list[dict[str, Any]]
    run_id: UUID | str | None
    cache_key: str
    response_model: type[BaseModel] | None
    attempt: int


@dataclass(frozen=True)
class GatewayResult:
    """Normalized outcome of a gateway.complete() call."""

    content: str
    data: Any
    model: str
    tier: ModelTier
    cost_usd: Decimal | None
    cached: bool
    cache_key: str
    prompt_tokens: int | None
    completion_tokens: int | None


class Provider(Protocol):
    """Async callable matching ``litellm.acompletion``'s call signature."""

    async def __call__(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any: ...


BeforeCallHook = Callable[[GatewayCallContext], Awaitable[None]]


async def _default_provider(model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
    """Call LiteLLM's async completion endpoint (default provider)."""
    import litellm

    return await litellm.acompletion(model=model, messages=messages, **kwargs)


def _extract_content(response: Any) -> str:
    """Extract the assistant text from a LiteLLM response (object or dict shape)."""
    choices = getattr(response, "choices", None)
    if choices is not None:
        message = choices[0].message
        return str(message.content)
    data = response.get("choices")  # dict-shaped response
    return str(data[0]["message"]["content"])


def _as_optional_int(value: Any) -> int | None:
    """Cast an arbitrary usage value to int (None-safe, mypy-friendly)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_usage(response: Any) -> tuple[int | None, int | None]:
    """Extract (prompt_tokens, completion_tokens) from object or dict usage."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None, None
    if hasattr(usage, "prompt_tokens"):
        prompt_tokens = _as_optional_int(usage.prompt_tokens)
        completion_tokens = _as_optional_int(usage.completion_tokens)
    else:
        prompt_tokens = _as_optional_int(usage.get("prompt_tokens"))
        completion_tokens = _as_optional_int(usage.get("completion_tokens"))
    return prompt_tokens, completion_tokens


def _parse_json_strict(content: str) -> dict[str, Any]:
    """Strict JSON parse of the assistant text."""
    parsed: Any = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("assistant response is not a JSON object")
    return parsed


def _parse_json_lenient(content: str) -> dict[str, Any]:
    """Lenient JSON parse: extract the outermost {...} region, then loads."""
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in assistant response")
    parsed = json.loads(content[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("assistant response is not a JSON object")
    return parsed


class LLMGateway:
    """Facade for LLM calls with tiering, caching, metering, and quarantine."""

    def __init__(
        self,
        settings: Settings | None = None,
        provider: Provider | None = None,
        cache: KVCache | None = None,
        meter: CostMeter | None = None,
        max_retries: int = 2,
    ) -> None:
        self._settings = settings or get_settings()
        self._provider: Provider = provider or _default_provider
        self._cache = cache or KVCache()
        self._meter = meter or CostMeter()
        self._max_retries = max_retries
        self._before_call_hooks: list[BeforeCallHook] = []
        self._install_provider_keys()

    def _install_provider_keys(self) -> None:
        """Map Settings SecretStr fields to LiteLLM env vars by NAME only."""
        for field_name, env_name in _PROVIDER_ENV_MAP.items():
            value = getattr(self._settings, field_name, None)
            if value is None:
                continue
            os.environ.setdefault(env_name, value.get_secret_value())

    def resolve_model(self, tier: ModelTier) -> str:
        """Resolve a tier name to the configured model id."""
        if tier == "cheap":
            return self._settings.llm_model_cheap
        if tier == "strong":
            return self._settings.llm_model_strong
        raise ModelTierError(f"unknown model tier {tier!r}; expected 'cheap' or 'strong'")

    def register_before_call_hook(self, hook: BeforeCallHook) -> None:
        """Register an async hook invoked before each provider attempt."""
        self._before_call_hooks.append(hook)

    async def _run_before_call_hooks(self, ctx: GatewayCallContext) -> None:
        """Invoke all before-call hooks; a raise aborts the attempt (G-03)."""
        for hook in self._before_call_hooks:
            await hook(ctx)

    async def _call_with_rate_limit_retry(
        self, model: str, messages: list[dict[str, Any]], call_kwargs: dict[str, Any]
    ) -> Any:
        """Call the provider with exponential backoff on rate-limit errors.

        Retries up to 3 times with delays of 15s, 30s, 60s. Rate-limit errors
        are distinct from schema-validation errors (G-11): they are transient
        provider constraints, not malformed outputs.
        """
        import litellm as _litellm

        max_rate_retries = 3
        for rate_attempt in range(max_rate_retries + 1):
            try:
                return await self._provider(
                    model=model, messages=messages, **call_kwargs
                )
            except _litellm.exceptions.RateLimitError as exc:
                if rate_attempt >= max_rate_retries:
                    logger.error(
                        "rate_limit_exhausted model=%s retries=%d", model, max_rate_retries
                    )
                    raise
                delay = [15.0, 30.0, 60.0][rate_attempt]
                logger.warning(
                    "rate_limit_hit model=%s attempt=%d/%d retry_in=%.1fs",
                    model,
                    rate_attempt + 1,
                    max_rate_retries + 1,
                    delay,
                )
                await asyncio.sleep(delay)

    @staticmethod
    def _build_messages(
        *,
        prompt: str | None,
        messages: list[dict[str, Any]] | None,
        system: str | None,
    ) -> list[dict[str, Any]]:
        """Compose the conversation: system -> messages | user prompt."""
        if prompt is None and messages is None:
            raise ValueError("complete() requires either prompt or messages")
        built: list[dict[str, Any]] = []
        if system:
            built.append({"role": "system", "content": system})
        if messages is not None:
            built.extend(messages)
        else:
            built.append({"role": "user", "content": prompt})
        return built

    @staticmethod
    def _schema_instruction(response_model: type[BaseModel], attempt: int) -> str:
        """Build the per-attempt schema instruction (distinct text per attempt)."""
        schema = json.dumps(response_model.model_json_schema(), sort_keys=True)
        if attempt == 0:
            return (
                "Return a single JSON object (no prose, no markdown fences) that "
                f"matches this JSON schema exactly: {schema}"
            )
        if attempt == 1:
            return (
                "Return only a valid JSON object matching this schema, with no "
                f"surrounding text or code fences: {schema}"
            )
        return (
            "Return a single JSON object matching this schema; if any field is "
            "unknown, use a sensible default. Do not wrap in markdown fences. "
            f"Schema: {schema}"
        )

    @staticmethod
    def _attempt_kwargs(attempt: int) -> dict[str, Any]:
        """Per-attempt provider kwargs — each retry uses a different approach (G-11)."""
        if attempt == 0:
            return {}
        if attempt == 1:
            return {"response_format": {"type": "json_object"}}
        return {"response_format": {"type": "json_object"}, "temperature": 0.0}

    async def complete(
        self,
        *,
        tier: ModelTier = "cheap",
        prompt: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        system: str | None = None,
        response_model: type[BaseModel] | None = None,
        run_id: UUID | str | None = None,
        use_cache: bool = True,
        ttl_seconds: int | None = None,
        inputs: Mapping[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **provider_kwargs: Any,
    ) -> GatewayResult:
        """Complete an LLM call: tiered, cached, metered, schema-validated.

        Returns a :class:`GatewayResult`; raises :class:`QuarantineError` when
        structured output fails schema validation on every retry (G-11) and
        propagates :class:`CircuitBreakerOpenError` from before-call hooks.
        """
        model = self.resolve_model(tier)
        base_messages = self._build_messages(prompt=prompt, messages=messages, system=system)
        prompt_str = prompt or json.dumps(base_messages, sort_keys=True, default=str)
        cache_key = build_key(model, prompt_str, inputs)

        if use_cache and self._cache is not None:
            cached_payload = await self._cache.get(cache_key)
            if cached_payload is not None:
                cached_content = str(cached_payload["content"])
                cached_data: Any = cached_content
                if response_model is not None and "data" in cached_payload:
                    cached_data = response_model.model_validate(cached_payload["data"])
                return GatewayResult(
                    content=cached_content,
                    data=cached_data,
                    model=str(cached_payload["model"]),
                    tier=tier,
                    cost_usd=Decimal("0"),
                    cached=True,
                    cache_key=cache_key,
                    prompt_tokens=None,
                    completion_tokens=None,
                )

        response: Any = None
        content: str | None = None
        data: Any = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None

        for attempt in range(self._max_retries + 1):
            ctx = GatewayCallContext(
                model=model,
                tier=tier,
                messages=base_messages,
                run_id=run_id,
                cache_key=cache_key,
                response_model=response_model,
                attempt=attempt,
            )
            await self._run_before_call_hooks(ctx)

            attempt_messages = base_messages
            call_kwargs: dict[str, Any] = dict(provider_kwargs)
            if temperature is not None:
                call_kwargs["temperature"] = temperature
            if max_tokens is not None:
                call_kwargs["max_tokens"] = max_tokens

            if response_model is not None:
                call_kwargs.update(self._attempt_kwargs(attempt))
                instruction = self._schema_instruction(response_model, attempt)
                attempt_messages = [*base_messages, {"role": "system", "content": instruction}]

            attempt_response = await self._call_with_rate_limit_retry(
                model=model, messages=attempt_messages, call_kwargs=call_kwargs
            )
            response = attempt_response
            content = _extract_content(response)
            prompt_tokens, completion_tokens = _extract_usage(response)

            if response_model is None:
                data = content
                break
            try:
                parsed = (
                    _parse_json_strict(content) if attempt < 2 else _parse_json_lenient(content)
                )
                data = response_model.model_validate(parsed)
                break
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "structured output failed schema validation on attempt=%s: %s",
                    attempt,
                    exc,
                )
                continue
        else:
            raise QuarantineError(
                "LLM returned output that failed JSON schema validation after "
                f"{self._max_retries + 1} attempts (different approaches per "
                "attempt, G-11); quarantine the offending input."
            )

        if content is None:
            raise QuarantineError("LLM returned no content after retries")

        cost_usd: Decimal | None = None
        if self._meter is not None:
            cost_usd = await self._meter.record_call(
                model=model,
                run_id=run_id,
                response=response,
                prompt=prompt_str,
                completion=content,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        if use_cache and self._cache is not None:
            payload: dict[str, Any] = {"content": content, "model": model}
            if response_model is not None:
                payload["data"] = data.model_dump(mode="json")
            await self._cache.set(
                key=cache_key,
                model=model,
                prompt_hash=build_prompt_hash(prompt_str, inputs),
                payload=payload,
                ttl_seconds=ttl_seconds,
            )

        return GatewayResult(
            content=content,
            data=data,
            model=model,
            tier=tier,
            cost_usd=cost_usd,
            cached=False,
            cache_key=cache_key,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
