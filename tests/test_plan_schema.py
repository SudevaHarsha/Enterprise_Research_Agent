"""Unit tests for the research-plan schemas (task_009).

The ``ResearchPlan`` model is the boundary contract with the LLM: the gateway
validates model output against it before quarantine (G-11), and field
descriptions are surfaced into ``model_json_schema`` for prompting (G-01).
Covers: >=3 / <=20 non-empty bounded sub-questions, bounded hypothesis,
taxonomy-hint and source-domain-hint lists, a non-empty topic, JSON-schema
descriptions, and roundtrip validation of a persisted plan payload.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.plan_schema import ResearchPlan

TOPIC = "How is AI transforming retail operations?"


def _plan(**overrides: object) -> ResearchPlan:
    """Build a valid plan, overriding any field for a specific test."""
    defaults: dict[str, object] = {
        "topic": TOPIC,
        "sub_questions": [
            "How do AI demand-forecasting models change retail inventory planning?",
            "What is the economic impact of AI personalization on retail margins?",
            "How do regulators treat AI-driven dynamic pricing in retail?",
            "How does AI-assisted staffing affect retail labor and customer experience?",
        ],
        "hypotheses": ["AI demand forecasting reduces stockouts without increasing markdowns."],
        "taxonomy_hints": ["demand forecasting", "personalization", "dynamic pricing"],
        "source_domain_hints": ["retaildive.com", "mckinsey.com", "nist.gov"],
    }
    defaults.update(overrides)
    # None means "leave the field at its default" (e.g. hypotheses default_factory)
    defaults = {key: value for key, value in defaults.items() if value is not None}
    return ResearchPlan(**defaults)


def test_research_plan_requires_non_empty_topic() -> None:
    """The topic field is mandatory and cannot be blank."""
    with pytest.raises(ValidationError):
        _plan(topic="")


def test_research_plan_requires_at_least_three_sub_questions() -> None:
    """min_length=3: a plan with only two sub-questions is rejected."""
    with pytest.raises(ValidationError):
        _plan(sub_questions=["Only one?", "Only two?"])


def test_research_plan_accepts_three_sub_questions() -> None:
    """min_length=3: exactly three sub-questions validate."""
    plan = _plan(sub_questions=["First?", "Second?", "Third?"])
    assert len(plan.sub_questions) == 3


def test_research_plan_rejects_more_than_twenty_sub_questions() -> None:
    """max_length=20: a twenty-first sub-question is rejected."""
    with pytest.raises(ValidationError):
        _plan(sub_questions=[f"Question {i}?" for i in range(21)])


def test_sub_question_must_be_non_empty() -> None:
    """Each sub-question has min_length=1; empty strings are rejected."""
    with pytest.raises(ValidationError):
        _plan(sub_questions=["", "A real question?", "Another real question?"])


def test_sub_question_rejects_overlong_text() -> None:
    """Each sub-question is capped at 500 characters."""
    with pytest.raises(ValidationError):
        _plan(sub_questions=["x" * 501, "A real question?", "Another real question?"])


def test_hypotheses_default_to_empty() -> None:
    """Hypotheses are optional: a bare plan carries an empty list."""
    plan = _plan(hypotheses=None)
    assert plan.hypotheses == []


def test_hypotheses_list_is_bounded() -> None:
    """max_length=20: a twenty-first hypothesis is rejected."""
    with pytest.raises(ValidationError):
        _plan(hypotheses=[f"Hypothesis {i}." for i in range(21)])


def test_taxonomy_hints_list_is_bounded() -> None:
    """max_length=20: a twenty-first taxonomy hint is rejected."""
    with pytest.raises(ValidationError):
        _plan(taxonomy_hints=[f"taxonomy-{i}" for i in range(21)])


def test_source_domain_hints_list_is_bounded() -> None:
    """max_length=20: a twenty-first source-domain hint is rejected."""
    with pytest.raises(ValidationError):
        _plan(source_domain_hints=[f"domain-{i}.com" for i in range(21)])


def test_plan_roundtrips_through_json_payload() -> None:
    """A persisted plan payload (model_dump json) revalidates unchanged."""
    plan = _plan()
    payload = plan.model_dump(mode="json")
    roundtrip = ResearchPlan.model_validate(payload)
    assert roundtrip == plan


def test_schema_carries_field_descriptions_for_prompting() -> None:
    """G-01: descriptions surface in model_json_schema for the prompt layer."""
    schema = ResearchPlan.model_json_schema()
    props = schema["properties"]
    assert "description" in props["topic"]
    assert "description" in props["sub_questions"]
    assert "description" in props["hypotheses"]
    assert "description" in props["taxonomy_hints"]
    assert "description" in props["source_domain_hints"]


def test_schema_encodes_plan_bounds() -> None:
    """The JSON schema encodes the same bounds the service enforces."""
    schema = ResearchPlan.model_json_schema()
    sub_questions = schema["properties"]["sub_questions"]
    assert sub_questions["minItems"] == 3
    assert sub_questions["maxItems"] == 20
    items = sub_questions["items"]
    assert items["type"] == "string"
    assert items["minLength"] == 1
    assert items["maxLength"] == 500
    assert schema["properties"]["hypotheses"]["maxItems"] == 20
    assert schema["properties"]["taxonomy_hints"]["maxItems"] == 20
    assert schema["properties"]["source_domain_hints"]["maxItems"] == 20
