"""Report generation — conclusions only from verified evidence (task_010, Step 10).

Synthesizes the run's final conclusions from **verified** statements and
**confirmed** contradictions (never draft/quarantined input, never naked
conclusions — every persisted conclusion carries >=1 ``conclusion_evidence``
row). Flow:

1. **Defensive filter** — only ``status='verified'`` statements are considered;
   an empty verified set raises :class:`ValueError` BEFORE any LLM call.
2. **G-01 prompt** — ``build_synthesis_prompt`` is a pure builder: system holds
   instructions only, the user message is delimited labeled
   ``<verified_statements>`` / ``<confirmed_contradictions>`` blocks.
3. **Strong-tier synthesis** — ``LLMGateway.complete(tier='strong',
   response_model=ConclusionExtraction, use_cache=False)``; a schema-failing
   model quarantines via :class:`QuarantineError` with nothing persisted (G-11).
4. **Deterministic checks per conclusion** — one-sidedness from source-domain
   diversity (:func:`one_sidedness_check`: fires when <2 distinct non-empty
   source domains OR the LLM flagged one-sided), contradiction warnings from
   confirmed contradictions citing the conclusion's statements, and
   ``human_review_required`` from high-stakes keywords or the LLM flag
   (:func:`is_high_stakes` / :data:`HIGH_STAKES_KEYWORDS`).
5. **Atomic persist** — one ``conclusions`` row + one
   ``conclusion_evidence`` row per cited statement + one ``audit_trace``
   verdict row (action='conclude', actor='report_generator') committed in ONE
   transaction; any failure rolls back every row.
6. **Observability** — a ``report.generate`` span with
   verified_count/contradiction_count/conclusions/one_sided_count/
   human_review_count attributes and a cumulative metrics log line.

Guarantees: G-01 (system = instructions only; user = delimited blocks), G-05
(statement text and conclusion text redacted before the prompt and before any
persisted field incl. audit evidence JSONB; ``use_cache=False``), G-11
(QuarantineError propagates; no partial rows). Returns the public
:class:`~app.services.report_renderer.Report`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.telemetry import get_tracer
from app.db.enums import ContradictionStatus, EvidenceScore, StatementStatus
from app.db.models import (
    Conclusion,
    ConclusionEvidence,
    Contradiction,
    EvidenceLink,
    Passage,
    Source,
    Statement,
)
from app.db.session import async_session_factory
from app.services.audit_writer import AuditWriter
from app.services.llm_gateway import LLMGateway
from app.services.normalizer import redact_secrets
from app.services.report_renderer import (
    Report,
    ReportConclusion,
    ReportEvidenceStatement,
    ReportSupportEntry,
)

logger = get_logger("app.services.report_generator")

SessionFactory = Callable[[], AsyncSession]

# High-stakes terms: medical, legal, regulatory, financial, safety, privacy.
# Substring match on the casefolded conclusion text — conservative by design so
# borderline content is escalated to a human reviewer instead of slipping through.
HIGH_STAKES_KEYWORDS = frozenset(
    {
        # medical / clinical
        "clinical trial",
        "dosage",
        "patient safety",
        "fda",
        "diagnosis",
        "prescription",
        "drug interaction",
        # legal / regulatory
        "lawsuit",
        "litigation",
        "regulatory",
        "compliance",
        "sanction",
        "liability",
        "consent order",
        # financial / securities
        "securities",
        "insider trading",
        "merger",
        "bankruptcy",
        "fraud",
        "audit",
        "money laundering",
        # safety / recalls
        "recall",
        "workplace safety",
        "food safety",
        "aviation safety",
        "nuclear",
        "explosive",
        # privacy / data protection
        "privacy",
        "data breach",
        "hipaa",
        "gdpr",
        "personally identifiable information",
        # military / defense
        "military",
        "weapon",
        "defense contract",
    }
)

_SYNTHESIS_SYSTEM_INSTRUCTIONS = (
    "You are a rigorous research synthesizer for an evidence-centric knowledge "
    "engine. Synthesize conclusions from the verified statements and confirmed "
    "contradictions provided in the user message ONLY. Follow these rules "
    "exactly: 1) text is a concise, factual prose conclusion (1-2000 "
    "characters) supported by the cited statements; 2) statement_ids must be "
    "non-empty (1-20 ids), and every id MUST reference a verified statement "
    "provided in the user message — never cite a statement that is not "
    "provided; 3) never invent facts, numbers, sources, or citations not "
    "present in the provided data; 4) confidence in [0.0, 1.0] rates how well "
    "the cited statements support the conclusion, or null when it cannot be "
    "judged; 5) one_sided=true when the conclusion rests on a single source "
    "domain or a single stance, false when it draws on multiple independent "
    "sources; 6) high_stakes=true when the conclusion touches medical, legal, "
    "regulatory, financial, safety, privacy, or defense matters that warrant "
    "human review; 7) output at most 50 conclusions; 8) mention confirmed "
    "contradictions in the conclusion text when they are material to the "
    "claim. Synthesize ONLY from the provided data."
)


class ConclusionDraft(BaseModel):
    """Structured LLM output: one proposed conclusion citing verified statements."""

    text: str = Field(
        min_length=1,
        max_length=2000,
        description="Concise factual conclusion prose (1-2000 characters).",
    )
    statement_ids: list[str] = Field(
        min_length=1,
        max_length=20,
        description=(
            "Verified statement ids cited by this conclusion (1-20, all present in the block)."
        ),
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in [0.0, 1.0] that the cited statements support the "
            "conclusion; null when unjudged."
        ),
    )
    one_sided: bool = Field(
        description="True when the conclusion rests on a single source domain or stance."
    )
    high_stakes: bool = Field(
        description=(
            "True when the conclusion touches medical/legal/regulatory/financial/safety matters."
        )
    )


class ConclusionExtraction(BaseModel):
    """Structured LLM output envelope: the full set of synthesized conclusions."""

    conclusions: list[ConclusionDraft] = Field(
        max_length=50, description="Synthesized conclusions (at most 50)."
    )


def is_high_stakes(text: str) -> bool:
    """Deterministic high-stakes check: any :data:`HIGH_STAKES_KEYWORDS` substring.

    Matches on the casefolded text so uppercase/lowercase variants are
    equivalent; conservative (any single keyword flags the conclusion).
    """
    folded = text.casefold()
    return any(keyword in folded for keyword in HIGH_STAKES_KEYWORDS)


def one_sidedness_check(
    conclusion_text: str,
    source_domains: list[str],
    llm_one_sided: bool,
) -> bool:
    """Deterministic one-sidedness rule.

    Fires when fewer than two distinct non-empty source domains back the
    conclusion OR the LLM flagged it one-sided. ``conclusion_text`` is part of
    the interface for future stance analysis but is not consulted by the
    current deterministic rule (which keys on source-domain diversity).
    """
    del conclusion_text  # reserved for future stance analysis (documented)
    distinct_domains = {domain for domain in source_domains if domain.strip()}
    return llm_one_sided or len(distinct_domains) < 2


def build_synthesis_prompt(
    verified_statements: list[tuple[str, str]],
    confirmed_contradictions: list[tuple[str, str, str]],
) -> tuple[str, str]:
    """Build ``(system, user_data_block)`` for the synthesis call (G-01).

    ``verified_statements`` is a list of ``(statement_id, text)`` tuples;
    ``confirmed_contradictions`` a list of ``(statement_a_id, statement_b_id,
    summary)`` tuples. The system message holds instructions ONLY; the user
    message is delimited labeled blocks. Pure and deterministic.
    """
    parts: list[str] = ["<verified_statements>"]
    for statement_id, text in verified_statements:
        parts.append(f'<statement id="{statement_id}">')
        parts.append(text)
        parts.append("</statement>")
    parts.append("</verified_statements>")
    parts.append("")
    parts.append("<confirmed_contradictions>")
    for a_id, b_id, summary in confirmed_contradictions:
        parts.append(f'<contradiction statement_a="{a_id}" statement_b="{b_id}">')
        parts.append(summary)
        parts.append("</contradiction>")
    parts.append("</confirmed_contradictions>")
    return _SYNTHESIS_SYSTEM_INSTRUCTIONS, "\n".join(parts)


def _source_domain(uri: str) -> str:
    """Derive the source domain from a source URI (hostname when present)."""
    parsed = urlparse(uri)
    host = parsed.hostname or parsed.netloc
    return host if host else uri


def _contradiction_summary(row: Contradiction) -> str:
    """Redacted human-readable summary for a confirmed contradiction row."""
    evidence = row.evidence or {}
    summary = evidence.get("confirm_reason") or evidence.get("flag_reason")
    return redact_secrets(str(summary)) if summary else "confirmed contradiction"


class ReportGenerator:
    """Synthesizes and atomically persists run conclusions from verified evidence."""

    def __init__(
        self,
        gateway: LLMGateway,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._gateway = gateway
        self._session_factory = session_factory or async_session_factory
        self._audit_writer = AuditWriter(session_factory=session_factory)

    async def generate(
        self,
        run_id: UUID | str,
        topic: str,
        verified_statements: Iterable[Statement],
        confirmed_contradictions: Iterable[Contradiction],
    ) -> Report:
        """Generate and atomically persist the run's conclusions.

        Raises :class:`ValueError` when no verified statements are supplied
        (before any LLM call) and propagates :class:`QuarantineError` (G-11)
        with no rows persisted on any failure.
        """
        async with self._session_factory() as session:
            try:
                with get_tracer("ecrke").start_as_current_span("report.generate") as span:
                    return await self._generate_locked(
                        session,
                        span,
                        run_id=run_id,
                        topic=topic,
                        verified_statements=verified_statements,
                        confirmed_contradictions=confirmed_contradictions,
                    )
            except Exception:
                await session.rollback()
                raise

    async def _generate_locked(
        self,
        session: AsyncSession,
        span: Any,
        *,
        run_id: UUID | str,
        topic: str,
        verified_statements: Iterable[Statement],
        confirmed_contradictions: Iterable[Contradiction],
    ) -> Report:
        verified = [s for s in verified_statements if s.status == StatementStatus.VERIFIED.value]
        verified.sort(key=lambda s: s.id)
        if not verified:
            raise ValueError("report generation requires at least one verified statement")

        confirmed = [
            c for c in confirmed_contradictions if c.status == ContradictionStatus.CONFIRMED.value
        ]
        by_id = {str(s.id): s for s in verified}

        # G-05: redact before the prompt and before any persisted field.
        statement_tuples = [(str(s.id), redact_secrets(s.text)) for s in verified]
        contradiction_tuples = [
            (str(c.statement_a_id), str(c.statement_b_id), _contradiction_summary(c))
            for c in confirmed
        ]
        system, data_block = build_synthesis_prompt(statement_tuples, contradiction_tuples)

        result = await self._gateway.complete(
            tier="strong",
            system=system,
            prompt=data_block,
            response_model=ConclusionExtraction,
            run_id=run_id,
            use_cache=False,
        )
        extraction: ConclusionExtraction = result.data

        # Deterministic resolution queries (batched IN clauses, no N+1).
        support_by_id = await self._latest_verify_scores(session, [s.id for s in verified])
        domain_by_id = await self._source_domains(session, by_id)

        report_conclusions: list[ReportConclusion] = []
        one_sided_count = 0
        human_review_count = 0

        for draft in extraction.conclusions:
            # A conclusion must cite at least one VERIFIED statement we know.
            cited_ids = [sid for sid in draft.statement_ids if sid in by_id]
            if not cited_ids:
                logger.warning(
                    "report_conclusion_skipped run_id=%s reason=no_verified_cited_statements",
                    run_id,
                )
                continue
            text = redact_secrets(draft.text)
            domains = [domain_by_id[sid] for sid in cited_ids]
            one_sided = one_sidedness_check(text, domains, draft.one_sided)
            human_review = is_high_stakes(text) or draft.high_stakes
            warnings = [
                summary
                for (a_id, b_id, summary) in contradiction_tuples
                if a_id in cited_ids or b_id in cited_ids
            ]
            support_matrix = [
                ReportSupportEntry(
                    statement_id=sid,
                    passage_id=str(by_id[sid].passage_id),
                    support_score=support_by_id.get(sid, EvidenceScore.NONE.value),
                )
                for sid in cited_ids
            ]
            evidence_statements = [
                ReportEvidenceStatement(
                    id=sid,
                    text=redact_secrets(by_id[sid].text),
                    source_domain=domain_by_id[sid],
                )
                for sid in cited_ids
            ]

            row = Conclusion(
                id=uuid4(),
                run_id=run_id,
                text=text,
                confidence=draft.confidence,
                human_review_required=human_review,
            )
            session.add(row)
            for sid in cited_ids:
                session.add(
                    ConclusionEvidence(
                        conclusion_id=row.id,
                        statement_id=by_id[sid].id,
                    )
                )
            self._audit_writer.append(
                session,
                run_id=run_id,
                entity_type="conclusion",
                entity_id=str(row.id),
                action="conclude",
                actor="report_generator",
                decision="concluded",
                reason=text,
                evidence={
                    "statement_ids": cited_ids,
                    "one_sided": one_sided,
                    "high_stakes": human_review,
                },
            )
            if one_sided:
                one_sided_count += 1
            if human_review:
                human_review_count += 1
            report_conclusions.append(
                ReportConclusion(
                    id=str(row.id),
                    text=text,
                    confidence=draft.confidence,
                    human_review_required=human_review,
                    one_sided=one_sided,
                    contradiction_warnings=warnings,
                    support_matrix=support_matrix,
                    evidence_statements=evidence_statements,
                )
            )

        # ONE transaction: conclusions + evidence + audit rows commit together.
        await session.commit()

        span.set_attribute("verified_count", len(verified))
        span.set_attribute("contradiction_count", len(confirmed))
        span.set_attribute("conclusions", len(report_conclusions))
        span.set_attribute("one_sided_count", one_sided_count)
        span.set_attribute("human_review_count", human_review_count)
        logger.info(
            "report_metrics verified_count=%s contradiction_count=%s conclusions=%s "
            "one_sided_count=%s human_review_count=%s",
            len(verified),
            len(confirmed),
            len(report_conclusions),
            one_sided_count,
            human_review_count,
        )

        return Report(
            run_id=str(run_id),
            topic=redact_secrets(topic),
            generated_at=datetime.now(UTC),
            conclusions=report_conclusions,
        )

    async def _latest_verify_scores(
        self, session: AsyncSession, statement_ids: list[Any]
    ) -> dict[str, str]:
        """Map statement id (string) -> latest method='verify' EvidenceLink score.

        Batched IN clause over the UUID statement ids; results keyed by string
        form for prompt/persist consistency.
        """
        if not statement_ids:
            return {}
        links = await session.scalars(
            select(EvidenceLink).where(EvidenceLink.statement_id.in_(statement_ids))
        )
        latest: dict[str, EvidenceLink] = {}
        for link in links:
            if link.method != "verify":
                continue
            sid = str(link.statement_id)
            current = latest.get(sid)
            if current is None or (link.created_at or datetime.min) >= (
                current.created_at or datetime.min
            ):
                latest[sid] = link
        return {sid: link.score for sid, link in latest.items()}

    async def _source_domains(
        self,
        session: AsyncSession,
        statements_by_id: dict[str, Statement],
    ) -> dict[str, str]:
        """Map statement id -> source domain via statement -> passage -> source (batched)."""
        passage_ids = {s.passage_id for s in statements_by_id.values() if s.passage_id is not None}
        if not passage_ids:
            return dict.fromkeys(statements_by_id, "")
        passages = await session.scalars(select(Passage).where(Passage.id.in_(passage_ids)))
        passage_by_id = {p.id: p for p in passages}
        source_ids = {p.source_id for p in passage_by_id.values() if p.source_id is not None}
        sources: dict[Any, Source] = {}
        if source_ids:
            source_rows = await session.scalars(select(Source).where(Source.id.in_(source_ids)))
            sources = {s.id: s for s in source_rows}
        domains: dict[str, str] = {}
        for sid, statement in statements_by_id.items():
            passage = passage_by_id.get(statement.passage_id)
            source = sources.get(passage.source_id) if passage is not None else None
            domains[sid] = _source_domain(source.uri) if source is not None else ""
        return domains
