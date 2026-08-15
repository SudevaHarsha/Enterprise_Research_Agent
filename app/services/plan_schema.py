"""Pydantic response models for the research-plan artifact (task_009).

The ``ResearchPlan`` model is the boundary contract with the LLM: the gateway
validates model output against it before quarantine (G-11), and the field
descriptions are surfaced into ``model_json_schema`` for prompting (G-01).
Bounds follow the build-plan Step 9 contract: >=3 and <=20 non-empty,
length-capped sub-questions (one per perspective), with bounded hypothesis,
taxonomy-hint and source-domain-hint lists to guide later stages.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

# One research perspective, phrased as a standalone question. Non-empty and
# length-capped so the persisted artifact stays bounded (G-11 output caps).
SubQuestion = Annotated[str, Field(min_length=1, max_length=500)]

# Single testable hypothesis / retrieval hint. Same string bounds as above.
Hypothesis = Annotated[str, Field(min_length=1, max_length=500)]
TaxonomyHint = Annotated[str, Field(min_length=1, max_length=500)]
SourceDomainHint = Annotated[str, Field(min_length=1, max_length=500)]


class ResearchPlan(BaseModel):
    """STORM-style multi-perspective research plan (Stage 1 Define artifact).

    Persisted under ``research_plan:{run_id}`` and consumed by the search
    (Step 5) and report (Step 10) stages.
    """

    topic: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "The research topic this plan decomposes, echoed verbatim from the topic_data block."
        ),
    )
    sub_questions: list[SubQuestion] = Field(
        min_length=3,
        max_length=20,
        description=(
            "At least 3 and at most 20 research sub-questions, each covering a "
            "distinct perspective on the topic (technological, economic, "
            "regulatory, social, competitive, operational, etc.)."
        ),
    )
    hypotheses: list[Hypothesis] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Testable hypotheses that evidence collection could confirm or refute; 0 to 20 entries."
        ),
    )
    taxonomy_hints: list[TaxonomyHint] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Domain taxonomy terms, entities, and concepts that retrieval "
            "should look for; 0 to 20 entries."
        ),
    )
    source_domain_hints: list[SourceDomainHint] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Credible source domains, publications, or databases likely to "
            "hold evidence on this topic; 0 to 20 entries."
        ),
    )
