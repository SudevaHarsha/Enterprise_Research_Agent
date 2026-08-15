"""Unit tests for the LLM gateway (hermetic — provider, cache, meter are fakes).

Covers: tiered model routing from Settings, LiteLLM-compatible provider
calls, schema-enforced structured output with bounded retry -> quarantine
(G-11), cache hit skipping the provider call (key = hash(model+prompt+inputs)),
cache expiry, the circuit-breaker before-call hook (task_011 integration
point), and Ironclad Rule 01 (no secrets in cache payloads or logs).
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.services.cost_meter import CostMeter
from app.services.kv_cache import KVCache, build_key
from app.services.llm_gateway import (
    CircuitBreakerOpenError,
    GatewayCallContext,
    LLMGateway,
    ModelTierError,
    QuarantineError,
)
from tests.conftest import (
    FakeClock,
    FakeProvider,
    FakeResponse,
    FakeSessionFactory,
    SampleOutput,
    make_run_row,
)

VALID_JSON = json.dumps({"topic": "AI in retail", "confidence": 0.9, "tags": ["retail"]})


@pytest.fixture
def settings() -> Settings:
    """Settings with clearly distinguishable fake models for tier tests."""
    return Settings(
        llm_model_cheap="fake/cheap-model",
        llm_model_strong="fake/strong-model",
    )


@pytest.fixture
def gateway(
    settings: Settings,
    fake_provider: FakeProvider,
    fake_session_factory: FakeSessionFactory,
) -> LLMGateway:
    """Gateway wired to a fake provider, in-memory cache, and fixed-cost meter."""
    cache = KVCache(session_factory=fake_session_factory)
    meter = CostMeter(
        session_factory=fake_session_factory,
        cost_fn=lambda response, model: Decimal("0.0010"),
    )
    return LLMGateway(
        settings=settings,
        provider=fake_provider,
        cache=cache,
        meter=meter,
    )


async def test_cheap_tier_resolves_to_configured_model(
    gateway: LLMGateway,
    fake_provider: FakeProvider,
) -> None:
    """Gateway.complete(tier='cheap') uses llm_model_cheap from Settings."""
    result = await gateway.complete(tier="cheap", prompt="hello")

    assert fake_provider.calls[0]["model"] == "fake/cheap-model"
    assert result.model == "fake/cheap-model"
    assert result.tier == "cheap"


async def test_strong_tier_resolves_to_configured_model(
    gateway: LLMGateway,
    fake_provider: FakeProvider,
) -> None:
    """Gateway.complete(tier='strong') uses llm_model_strong from Settings."""
    result = await gateway.complete(tier="strong", prompt="hello")

    assert fake_provider.calls[0]["model"] == "fake/strong-model"
    assert result.model == "fake/strong-model"
    assert result.tier == "strong"


async def test_provider_calls_pass_through_litellm_compatible_interface(
    gateway: LLMGateway,
    fake_provider: FakeProvider,
) -> None:
    """The provider receives model, messages, and passthrough kwargs."""
    await gateway.complete(
        tier="cheap",
        prompt="hello",
        system="You are helpful",
        temperature=0.2,
        max_tokens=64,
        extra_provider_param="x",
    )

    call = fake_provider.calls[0]
    assert call["model"] == "fake/cheap-model"
    assert call["messages"][0] == {"role": "system", "content": "You are helpful"}
    assert call["messages"][-1] == {"role": "user", "content": "hello"}
    assert call["kwargs"]["temperature"] == 0.2
    assert call["kwargs"]["max_tokens"] == 64
    assert call["kwargs"]["extra_provider_param"] == "x"


async def test_unknown_tier_raises(gateway: LLMGateway) -> None:
    """Only 'cheap' and 'strong' tiers exist; anything else is rejected."""
    with pytest.raises(ModelTierError):
        await gateway.complete(tier="turbo", prompt="hello")


async def test_plain_completion_returns_content_and_metered_cost(
    gateway: LLMGateway,
) -> None:
    """A plain completion returns raw content, cost, and usage metadata."""
    result = await gateway.complete(tier="cheap", prompt="hello")

    assert result.content == "hello from fake provider"
    assert result.data == "hello from fake provider"
    assert result.cost_usd == Decimal("0.0010")
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 5
    assert result.cached is False


async def test_structured_output_returns_validated_model_instance(
    gateway: LLMGateway,
    fake_provider: FakeProvider,
) -> None:
    """Passing a Pydantic response_model returns a validated model instance."""
    fake_provider.queue(FakeResponse(VALID_JSON))
    result = await gateway.complete(
        tier="cheap",
        prompt="extract",
        response_model=SampleOutput,
    )

    assert isinstance(result.data, SampleOutput)
    assert result.data.topic == "AI in retail"
    assert result.data.confidence == 0.9
    assert result.data.tags == ["retail"]
    assert result.cached is False


async def test_structured_output_invalid_retries_then_quarantine(
    gateway: LLMGateway,
    fake_provider: FakeProvider,
) -> None:
    """Schema violation -> bounded retry (max 2) -> QuarantineError (G-11)."""
    fake_provider.queue(FakeResponse("not json"))
    fake_provider.queue(FakeResponse("also not json"))
    fake_provider.queue(FakeResponse("nope"))

    with pytest.raises(QuarantineError) as exc_info:
        await gateway.complete(
            tier="cheap",
            prompt="extract",
            response_model=SampleOutput,
        )

    assert "schema" in str(exc_info.value).lower()
    # initial attempt + 2 retries, nothing beyond
    assert len(fake_provider.calls) == 3
    # retries used a different approach (G-11)
    assert "response_format" not in fake_provider.calls[0]["kwargs"]
    assert fake_provider.calls[1]["kwargs"].get("response_format") == {"type": "json_object"}
    assert fake_provider.calls[0]["messages"] != fake_provider.calls[1]["messages"]


async def test_structured_output_recovers_on_retry_with_different_approach(
    gateway: LLMGateway,
    fake_provider: FakeProvider,
) -> None:
    """A schema failure on attempt 1 can recover on a different-approach retry."""
    fake_provider.queue(FakeResponse("not json"))
    fake_provider.queue(FakeResponse(VALID_JSON))

    result = await gateway.complete(
        tier="cheap",
        prompt="extract",
        response_model=SampleOutput,
    )

    assert isinstance(result.data, SampleOutput)
    assert result.data.topic == "AI in retail"
    assert len(fake_provider.calls) == 2


async def test_cache_hit_skips_provider_call(
    gateway: LLMGateway,
    fake_provider: FakeProvider,
) -> None:
    """Second identical call is served from cache; the provider is not called."""
    first = await gateway.complete(tier="cheap", prompt="same prompt")
    second = await gateway.complete(tier="cheap", prompt="same prompt")

    assert len(fake_provider.calls) == 1  # provider called exactly once
    assert first.cached is False
    assert second.cached is True
    assert second.content == first.content
    assert second.cost_usd == Decimal("0")  # cache hits cost nothing


async def test_cache_key_includes_inputs(
    gateway: LLMGateway,
    fake_provider: FakeProvider,
) -> None:
    """Identical prompt with different inputs yields different cache entries."""
    await gateway.complete(tier="cheap", prompt="p", inputs={"passage": "a"})
    await gateway.complete(tier="cheap", prompt="p", inputs={"passage": "b"})
    await gateway.complete(tier="cheap", prompt="p", inputs={"passage": "a"})

    assert len(fake_provider.calls) == 2  # 1st miss, 2nd miss, 3rd hit


async def test_cache_expired_entry_is_treated_as_miss(
    settings: Settings,
    fake_provider: FakeProvider,
    fake_session_factory: FakeSessionFactory,
) -> None:
    """Expired entries are misses at the gateway level and get refreshed."""
    clock = FakeClock()
    cache = KVCache(session_factory=fake_session_factory, clock=clock)
    meter = CostMeter(
        session_factory=fake_session_factory,
        cost_fn=lambda response, model: Decimal("0.0010"),
    )
    gateway = LLMGateway(
        settings=settings,
        provider=fake_provider,
        cache=cache,
        meter=meter,
    )

    first = await gateway.complete(tier="cheap", prompt="expirable", ttl_seconds=60)
    assert first.cached is False
    assert len(fake_provider.calls) == 1

    clock.advance(61)
    second = await gateway.complete(tier="cheap", prompt="expirable", ttl_seconds=60)

    assert second.cached is False  # expired -> miss -> provider called again
    assert len(fake_provider.calls) == 2


async def test_before_call_hook_can_pause_on_budget_breach(
    gateway: LLMGateway,
    fake_provider: FakeProvider,
    fake_session_factory: FakeSessionFactory,
) -> None:
    """Circuit-breaker hook (task_011) can abort the call before the provider runs."""

    async def pause_on_budget_breach(ctx: GatewayCallContext) -> None:
        raise CircuitBreakerOpenError("run budget breached — pausing (G-03)")

    gateway.register_before_call_hook(pause_on_budget_breach)

    with pytest.raises(CircuitBreakerOpenError, match="budget"):
        await gateway.complete(tier="cheap", prompt="hello")

    assert len(fake_provider.calls) == 0  # no provider call happened
    assert fake_session_factory.storage == {}  # nothing cached


async def test_before_call_hook_receives_call_context(
    gateway: LLMGateway,
    fake_session_factory: FakeSessionFactory,
) -> None:
    """Hooks receive model/tier/messages/run_id/cache_key for their decision."""
    run = make_run_row()
    fake_session_factory.storage[run.id] = run
    run_id = str(run.id)
    seen: list[GatewayCallContext] = []

    async def record(ctx: GatewayCallContext) -> None:
        seen.append(ctx)

    gateway.register_before_call_hook(record)
    await gateway.complete(tier="strong", prompt="hello", run_id=run_id)

    assert len(seen) == 1
    ctx = seen[0]
    assert ctx.model == "fake/strong-model"
    assert ctx.tier == "strong"
    assert ctx.run_id == run_id
    assert ctx.cache_key == build_key("fake/strong-model", "hello", {})
    assert ctx.messages[-1] == {"role": "user", "content": "hello"}


async def test_cache_payload_never_contains_credentials(
    settings: Settings,
    fake_provider: FakeProvider,
    fake_session_factory: FakeSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ironclad Rule 01: no API key value, no messages, no secret-hint keys in cache."""
    fake_key = "sk-fake-test-123"  # noqa: S105 - fake fixture value; asserted never persisted
    keyed_settings = Settings(llm_openai_api_key=fake_key)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cache = KVCache(session_factory=fake_session_factory)
    meter = CostMeter(
        session_factory=fake_session_factory,
        cost_fn=lambda response, model: Decimal("0.0010"),
    )
    gateway = LLMGateway(
        settings=keyed_settings,
        provider=fake_provider,
        cache=cache,
        meter=meter,
    )
    fake_provider.queue(FakeResponse(VALID_JSON))

    await gateway.complete(
        tier="cheap",
        prompt="extract",
        response_model=SampleOutput,
    )

    persisted = json.dumps([entry.payload for entry in fake_session_factory.storage.values()])
    assert fake_key not in persisted
    assert "authorization" not in persisted
    assert "api_key" not in persisted
    assert "OPENAI_API_KEY" not in persisted
    # payload holds the answer, never the request messages
    stored_payload = next(iter(fake_session_factory.storage.values())).payload
    assert "content" in stored_payload
    assert "messages" not in stored_payload
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


async def test_gateway_meters_cost_onto_run(
    settings: Settings,
    fake_provider: FakeProvider,
    fake_session_factory: FakeSessionFactory,
) -> None:
    """A successful call with run_id increments runs.cost_spent_usd."""
    run = make_run_row(cost_spent_usd=Decimal("0.0000"))
    fake_session_factory.storage[run.id] = run
    meter = CostMeter(
        session_factory=fake_session_factory,
        cost_fn=lambda response, model: Decimal("0.0010"),
    )
    gateway = LLMGateway(
        settings=settings,
        provider=fake_provider,
        cache=KVCache(session_factory=fake_session_factory),
        meter=meter,
    )

    result = await gateway.complete(tier="cheap", prompt="hello", run_id=str(run.id))

    assert result.cost_usd == Decimal("0.0010")
    assert run.cost_spent_usd == Decimal("0.0010")


async def test_complete_requires_prompt_or_messages(gateway: LLMGateway) -> None:
    """No prompt and no messages is a caller error, not a provider call."""
    with pytest.raises(ValueError, match="prompt or messages"):
        await gateway.complete(tier="cheap")
