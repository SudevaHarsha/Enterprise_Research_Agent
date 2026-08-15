"""Pydantic schemas for LLM statement extraction (task_006).

These models are the boundary contract with the LLM: the gateway validates
model output against them before quarantine (G-11), and their field
descriptions are surfaced into ``model_json_schema`` for prompting (G-01).
Bounds mirror the relational ``statements`` table check constraints:
non-empty text capped at 2000 chars, and confidence within [0.0, 1.0].
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExtractedStatement(BaseModel):
    """One atomic claim extracted from a passage."""

    text: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "One atomic claim, directly supported by the passage, stated "
            "faithfully with its names and figures."
        ),
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in [0.0, 1.0] that the passage supports this claim; "
            "null when it cannot be judged."
        ),
    )


class StatementExtraction(BaseModel):
    """Structured LLM output: atomic statements from one passage."""

    statements: list[ExtractedStatement] = Field(
        default_factory=list,
        max_length=50,
        description="Atomic statements extracted from the passage, one claim each.",
    )
