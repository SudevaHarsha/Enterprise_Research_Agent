"""Unit tests for the STORM-style research planner (task_009).

Hermetic: the gateway is wired to the fake provider/cache/meter stack from
conftest, and persistence goes through the existing ``KVCache`` over an
in-memory ``FakeSessionFactory``. Covers: multi-perspective plan generation
from a seed topic, persistence bound to ``run_id`` with a ~30-day TTL under
the ``research_plan:{run_id}`` key, schema-contract quarantine (G-11, no
partial artifact), G-01 instruction/data separation, G-05 redaction of a
secret topic in the prompt and in the persisted plan, cheap-tier routing
through the gateway only (never a direct provider call), deterministic prompt
template, and empty-topic rejection before any LLM call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.db.models import KVEntry, Run
from app.services.cost_meter import CostMeter
from app.services.kv_cache import KVCache
from app.services.llm_gateway import LLMGateway, QuarantineError
from app.services.plan_schema import ResearchPlan
from app.services.planner import Planner, build_plan_prompt
from tests.conftest import (
    FakeProvider,
    FakeResponse,
    FakeSessionFactory,
    make_run_row,
    rows_of,
)

# Fake fixture secret matching the G-05 pattern (\bsk-[A-Za-z0-9_-]{16,}\b);
# asserted to never reach provider messages or the persisted plan.
SECRET = "sk-fake-test-1234567890"  # noqa: S105 - fake fixture value; must be redacted

SEED_TOPIC = "How is AI transforming retail operations?"

PLAN_JSON = json.dumps(
    {
        "topic": SEED_TOPIC,
        "sub_questions": [
            "How do AI demand-forecasting models change retail inventory planning?",
            "What is the economic impact of AI personalization on retail margins?",
            "How do regulators treat AI-driven dynamic pricing in retail?",
            "How does AI-assisted staffing affect retail labor and customer experience?",
        ],
        "hypotheses": [
            "AI demand forecasting reduces stockouts without increasing markdown spend."
        ],
        "taxonomy_hints": ["demand forecasting", "personalization", "dynamic pricing"],
        "source_domain_hints": ["retaildive.com", "mckinsey.com", "nist.gov"],
    }
)

INVALID_PLAN_JSON = json.dumps(
    {
        "topic": SEED_TOPIC,
        "sub_questions": ["Only one?", "Only two?"],  # violates min_length=3
        "hypotheses": [],
        "taxonomy_hints": [],
        "source_domain_hints": [],
    }
)


class PlannerHarness:
    """Wiring for one hermetic planner test: fake stack + seeded run."""

    def __init__(self) -> None:
        self.settings = Settings(
            llm_model_cheap="fake/cheap-model",
            llm_model_strong="fake/strong-model",
        )
        self.factory = FakeSessionFactory()
        self.provider = FakeProvider()
        self.cache = KVCache(session_factory=self.factory)
        self.meter = CostMeter(
            session_factory=self.factory,
            cost_fn=lambda response, model: Decimal("0.0010"),
        )
        self.gateway = LLMGateway(
            settings=self.settings,
            provider=self.provider,
            cache=self.cache,
            meter=self.meter,
        )
        self.planner = Planner(gateway=self.gateway, session_factory=self.factory)
        self.run: Run = make_run_row(cost_spent_usd=Decimal("0.0000"))
        self.factory.storage[self.run.id] = self.run

    def plan_key(self) -> str:
        """The namespaced persistence key for this run."""
        return f"research_plan:{self.run.id}"

    def stored_plan(self) -> KVEntry:
        """Return the persisted KVEntry for this run (test 2 shape)."""
        return self.factory.storage[self.plan_key()]


@pytest.fixture
def harness() -> PlannerHarness:
    """Fresh hermetic wiring per test."""
    return PlannerHarness()


async def test_seed_topic_yields_multi_perspective_plan(harness: PlannerHarness) -> None:
    """Seed topic -> >=3 distinct sub-questions plus hypotheses and hints."""
    harness.provider.queue(FakeResponse(PLAN_JSON))

    plan = await harness.planner.plan(SEED_TOPIC, harness.run.id)

    assert isinstance(plan, ResearchPlan)
    assert len(plan.sub_questions) >= 3
    assert len(set(plan.sub_questions)) == len(plan.sub_questions)
    assert plan.hypotheses, "expected at least one hypothesis"
    assert plan.taxonomy_hints, "expected taxonomy hints"
    assert plan.source_domain_hints, "expected source-domain hints"
    assert plan.topic == SEED_TOPIC


async def test_plan_persisted_bound_to_run_id_with_30_day_ttl(harness: PlannerHarness) -> None:
    """KVEntry at 'research_plan:{run_id}' roundtrips and expires ~30 days out."""
    harness.provider.queue(FakeResponse(PLAN_JSON))

    plan = await harness.planner.plan(SEED_TOPIC, harness.run.id)

    entries = rows_of(harness.factory.storage, KVEntry)
    assert len(entries) == 1, "exactly one KVEntry: the plan (gateway use_cache=False)"
    entry = harness.stored_plan()
    assert entry.key == harness.plan_key()
    assert entry.model == "fake/cheap-model"

    roundtrip = ResearchPlan.model_validate(entry.payload)
    assert roundtrip == plan
    assert roundtrip.sub_questions == plan.sub_questions

    assert entry.expires_at is not None
    delta = entry.expires_at - datetime.now(UTC)
    assert timedelta(days=29, hours=23) < delta < timedelta(days=30, hours=1)


async def test_invalid_model_output_is_quarantined_without_persist(harness: PlannerHarness) -> None:
    """G-11: a plan violating the schema (2 sub-questions) -> QuarantineError."""
    harness.provider.queue(FakeResponse(INVALID_PLAN_JSON))
    harness.provider.queue(FakeResponse(INVALID_PLAN_JSON))
    harness.provider.queue(FakeResponse(INVALID_PLAN_JSON))

    with pytest.raises(QuarantineError):
        await harness.planner.plan(SEED_TOPIC, harness.run.id)

    assert rows_of(harness.factory.storage, KVEntry) == []
    assert harness.plan_key() not in harness.factory.storage


async def test_quarantine_propagates_without_partial_artifact(harness: PlannerHarness) -> None:
    """Non-JSON output on every retry -> QuarantineError, nothing persisted."""
    harness.provider.queue(FakeResponse("not json"))
    harness.provider.queue(FakeResponse("also not json"))
    harness.provider.queue(FakeResponse("nope"))

    with pytest.raises(QuarantineError):
        await harness.planner.plan(SEED_TOPIC, harness.run.id)

    assert rows_of(harness.factory.storage, KVEntry) == []
    assert harness.plan_key() not in harness.factory.storage


async def test_prompt_separates_instructions_from_topic_block(harness: PlannerHarness) -> None:
    """G-01: system carries instructions; the user message is labeled data only."""
    harness.provider.queue(FakeResponse(PLAN_JSON))

    await harness.planner.plan(SEED_TOPIC, harness.run.id)

    messages = harness.provider.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    system_content = str(messages[0]["content"])
    assert "research" in system_content.lower()
    assert "<topic_data" not in system_content

    assert messages[1]["role"] == "user"
    user_content = str(messages[1]["content"])
    assert "<topic_data>" in user_content
    assert "</topic_data>" in user_content
    assert SEED_TOPIC in user_content
    # the gateway appends its own schema instruction as a final system message
    assert messages[-1]["role"] == "system"


async def test_secret_redacted_from_prompt_and_persisted_plan(harness: PlannerHarness) -> None:
    """G-05: a secret topic never reaches the provider, the plan, or storage."""
    secret_topic = f"Retail AI operations using {SECRET}"
    leaked_plan = json.dumps(
        {
            "topic": secret_topic,
            "sub_questions": [
                f"How does AI forecasting handle {SECRET}?",
                "What is the economic impact of AI personalization on retail margins?",
                "How do regulators treat AI-driven dynamic pricing in retail?",
            ],
            "hypotheses": [f"AI forecasting reduces stockouts with {SECRET}."],
            "taxonomy_hints": [f"demand forecasting {SECRET}"],
            "source_domain_hints": [f"retaildive.com {SECRET}"],
        }
    )
    harness.provider.queue(FakeResponse(leaked_plan))

    plan = await harness.planner.plan(secret_topic, harness.run.id)

    # redaction applied to the prompt before the call
    for call in harness.provider.calls:
        for message in call["messages"]:
            assert SECRET not in str(message.get("content"))
    assert "[REDACTED_API_KEY]" in str(harness.provider.calls[0]["messages"][1]["content"])

    # redaction applied to the plan fields before return and persist
    plan_json = json.dumps(plan.model_dump(mode="json"))
    assert SECRET not in plan_json
    assert "[REDACTED_API_KEY]" in plan_json

    entry = harness.stored_plan()
    entry_json = json.dumps(entry.payload)
    assert SECRET not in entry_json
    assert "[REDACTED_API_KEY]" in entry_json


async def test_planner_routes_through_cheap_tier_via_gateway_only(harness: PlannerHarness) -> None:
    """One gateway call on the cheap tier; never a direct provider call."""
    harness.provider.queue(FakeResponse(PLAN_JSON))

    await harness.planner.plan(SEED_TOPIC, harness.run.id)

    assert len(harness.provider.calls) == 1
    assert harness.provider.calls[0]["model"] == "fake/cheap-model"


def test_build_plan_prompt_is_deterministic_and_structured() -> None:
    """The prompt builder is a pure function with strict G-01 structure."""
    system_a, data_a = build_plan_prompt(SEED_TOPIC)
    system_b, data_b = build_plan_prompt(SEED_TOPIC)

    assert system_a == system_b
    assert data_a == data_b
    assert system_a != data_a
    assert "<topic_data>" in data_a
    assert "</topic_data>" in data_a
    assert SEED_TOPIC in data_a
    assert "<topic_data>" not in system_a

    # a different topic changes only the data block, never the instructions
    system_c, data_c = build_plan_prompt("How do supply chains handle disruptions?")
    assert system_c == system_a
    assert data_c != data_a
    assert "How do supply chains handle disruptions?" in data_c


async def test_empty_or_blank_topic_rejected_before_llm_call(harness: PlannerHarness) -> None:
    """Degenerate topics fail fast with ValueError and no provider/persist side effects."""
    for bad_topic in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="topic"):
            await harness.planner.plan(bad_topic, harness.run.id)

    assert harness.provider.calls == []
    assert rows_of(harness.factory.storage, KVEntry) == []
