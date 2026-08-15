"""Cost metering for LLM calls — accurate, never negative (G-03).

Recorded costs accumulate onto ``runs.cost_spent_usd`` (Numeric, 4 decimal
places). When the provider cost is unavailable the meter falls back to a
tiktoken token-count estimate at the cheap-model rate, so every metered call
produces a non-negative, deterministic number. ``run_id=None`` meters
standalone without persisting (idempotent by design).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models import Run
from app.db.session import async_session_factory

logger = get_logger("app.services.cost_meter")

DEFAULT_USD_PER_1K_TOKENS = Decimal("0.002")
_ROUNDING = Decimal("0.0001")
_USD_PER_TOKEN = DEFAULT_USD_PER_1K_TOKENS / Decimal(1000)


class NegativeCostError(ValueError):
    """Raised when a provider reports a negative cost (G-03)."""


class RunNotFoundError(LookupError):
    """Raised when a metered run_id does not resolve to a Run row."""


class ResponseLike(Protocol):
    """Minimal LiteLLM response shape the meter inspects for usage/cost."""

    usage: Any
    model: Any


ProviderCostFn = Callable[[ResponseLike, str], Decimal | None]


def _litellm_cost(response: ResponseLike, model: str) -> Decimal | None:
    """Best-effort LiteLLM cost for a completed response (None when unknown)."""
    try:
        import litellm  # local import: LiteLLM must stay an optional runtime dep

        usd = litellm.completion_cost(completion_response=response, model=model)
    except Exception:  # noqa: BLE001 - any provider/API quirk => unknown cost
        logger.debug("litellm.completion_cost unavailable; falling back to estimate")
        return None
    if usd is None:
        return None
    try:
        cost = Decimal(str(usd))
    except Exception:  # noqa: BLE001 - non-numeric cost => unknown
        return None
    if cost < 0:
        logger.warning(
            "provider reported negative cost for model=%s; treating as unknown",
            model,
        )
        return None
    return cost


def _estimate_tokens(model: str, text: str) -> int:
    """Token count via tiktoken for the model's encoding (cl100k_base fallback)."""
    try:
        import tiktoken

        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:  # noqa: BLE001 - tiktoken missing => coarse estimate
        return max(1, len(text) // 4)


def estimate_cost(
    *,
    model: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    prompt: str | None = None,
    completion: str | None = None,
) -> Decimal:
    """Return a positive deterministic USD estimate from token counts or text.

    Explicit token counts win; otherwise text is counted with tiktoken
    (falling back to a whitespace-style split when tiktoken is unavailable).
    """
    if prompt_tokens is None:
        prompt_tokens = _estimate_tokens(model, prompt or "") if prompt else 0
    if completion_tokens is None:
        completion_tokens = _estimate_tokens(model, completion or "") if completion else 0
    total = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))
    # NOT quantized here: tiny estimates must stay > 0; record_call rounds for storage.
    return Decimal(total) * _USD_PER_TOKEN


def _as_optional_int(value: Any) -> int | None:
    """Cast an arbitrary usage value to int (None-safe)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usage_tokens(response: ResponseLike | None) -> tuple[int | None, int | None]:
    """Extract (prompt_tokens, completion_tokens) from a LiteLLM response."""
    if response is None:
        return None, None
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None, None
    if hasattr(usage, "prompt_tokens"):
        return _as_optional_int(usage.prompt_tokens), _as_optional_int(usage.completion_tokens)
    return _as_optional_int(usage.get("prompt_tokens")), _as_optional_int(
        usage.get("completion_tokens")
    )


class CostMeter:
    """Meters LLM call costs onto ``runs.cost_spent_usd`` (injectable factory/cost fn)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        cost_fn: ProviderCostFn | None = None,
    ) -> None:
        self._session_factory = session_factory or async_session_factory
        self._cost_fn: ProviderCostFn = cost_fn or _litellm_cost

    async def record_call(
        self,
        *,
        model: str,
        run_id: UUID | str | None = None,
        cost_usd: Decimal | float | None = None,
        response: ResponseLike | None = None,
        prompt: str | None = None,
        completion: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> Decimal:
        """Record one LLM call's cost; returns the metered cost.

        ``run_id=None`` returns the cost without persisting. A negative cost
        raises ``NegativeCostError`` and is never persisted (G-03).
        """
        cost: Decimal | None
        if cost_usd is not None:
            cost = Decimal(str(cost_usd))
        else:
            cost = self._cost_fn(response, model) if response is not None else None
            if cost is None:
                if prompt_tokens is None and completion_tokens is None:
                    prompt_tokens, completion_tokens = _usage_tokens(response)
                cost = estimate_cost(
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    prompt=prompt,
                    completion=completion,
                )
        if cost is None:
            raise ValueError(f"could not determine cost for model={model!r}")
        cost = cost.quantize(_ROUNDING, rounding=ROUND_HALF_UP)
        if cost < 0:
            raise NegativeCostError(f"cost must never be negative, got {cost}")
        if run_id is None:
            return cost
        async with self._session_factory() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise RunNotFoundError(f"no Run row for run_id={run_id}")
            run.cost_spent_usd = (run.cost_spent_usd or Decimal("0")) + cost
            await session.commit()
        return cost
