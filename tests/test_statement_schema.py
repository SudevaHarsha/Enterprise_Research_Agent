"""Unit tests for the statement extraction schemas (task_006).

The schema is the boundary contract with the LLM: the gateway validates model
output against these schemas before quarantine, so field bounds and
descriptions are exactly what the prompt layer promises to the model (G-01).
Covers: non-empty bounded text, confidence within [0.0, 1.0] (or null), a
bounded statements list, and JSON-schema descriptions surfaced for prompting.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.statement_schema import ExtractedStatement, StatementExtraction


def test_extracted_statement_requires_non_empty_text() -> None:
    """min_length=1: an empty statement text is rejected."""
    with pytest.raises(ValidationError):
        ExtractedStatement(text="")


def test_extracted_statement_accepts_short_text() -> None:
    """A normal one-sentence claim validates and round-trips."""
    statement = ExtractedStatement(text="Retail sales rose 4% last quarter.")
    assert statement.text == "Retail sales rose 4% last quarter."


def test_extracted_statement_rejects_overlong_text() -> None:
    """max_length=2000: a statement text longer than the cap is rejected."""
    with pytest.raises(ValidationError):
        ExtractedStatement(text="x" * 2001)


def test_extracted_statement_confidence_is_optional() -> None:
    """confidence defaults to None and may be omitted."""
    statement = ExtractedStatement(text="A claim.")
    assert statement.confidence is None


def test_extracted_statement_accepts_in_range_confidence() -> None:
    """A confidence inside [0.0, 1.0] is accepted and preserved."""
    statement = ExtractedStatement(text="A claim.", confidence=0.85)
    assert statement.confidence == 0.85


def test_extracted_statement_rejects_confidence_above_one() -> None:
    """confidence > 1.0 violates the DB check constraint contract."""
    with pytest.raises(ValidationError):
        ExtractedStatement(text="A claim.", confidence=1.01)


def test_extracted_statement_rejects_negative_confidence() -> None:
    """confidence < 0.0 violates the DB check constraint contract."""
    with pytest.raises(ValidationError):
        ExtractedStatement(text="A claim.", confidence=-0.1)


def test_statement_extraction_defaults_to_empty_list() -> None:
    """A bare extraction object carries an empty statements list."""
    extraction = StatementExtraction()
    assert extraction.statements == []


def test_statement_extraction_accepts_up_to_fifty_statements() -> None:
    """max_length=50: fifty statements are accepted."""
    many = [ExtractedStatement(text=f"Claim {i}.") for i in range(50)]
    extraction = StatementExtraction(statements=many)
    assert len(extraction.statements) == 50


def test_statement_extraction_rejects_more_than_fifty_statements() -> None:
    """max_length=50: a fifty-first statement is rejected."""
    many = [ExtractedStatement(text=f"Claim {i}.") for i in range(51)]
    with pytest.raises(ValidationError):
        StatementExtraction(statements=many)


def test_schema_carries_field_descriptions_for_prompting() -> None:
    """G-01: descriptions surface in model_json_schema for the prompt layer."""
    schema = StatementExtraction.model_json_schema()
    props = schema["properties"]
    assert "description" in props["statements"]
    nested = schema["$defs"]["ExtractedStatement"]["properties"]
    assert "description" in nested["text"]
    assert "description" in nested["confidence"]


def test_schema_encodes_field_bounds() -> None:
    """The JSON schema encodes the same bounds the DB enforces."""
    schema = StatementExtraction.model_json_schema()
    nested = schema["$defs"]["ExtractedStatement"]
    text = nested["properties"]["text"]
    assert text["minLength"] == 1
    assert text["maxLength"] == 2000
    confidence = nested["properties"]["confidence"]
    # Optional[float] emits constraints inside anyOf (number + null) in pydantic v2
    confidence_schema = confidence.get("anyOf", [confidence])[0]
    assert confidence_schema["minimum"] == 0.0
    assert confidence_schema["maximum"] == 1.0
    assert schema["properties"]["statements"]["maxItems"] == 50
