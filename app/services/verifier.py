"""Verify-first gate (task_007, build-plan Step 7, design doc §7.2).

Nothing enters the KB unsupported. ``Verifier.verify`` promotes a draft
statement to ``verified`` or ``quarantined`` in three stages, all inside ONE
atomic transaction:

1. **Deterministic support matrix** (``score_support``, $0, no LLM): full or
   partial alignment proceeds to the judge; none short-circuits to
   ``quarantined`` with zero LLM spend (G-03).
2. **LLM judge confirmation** via ``LLMGateway.complete(tier='strong',
   response_model=VerificationVerdict)`` — never a direct provider call.
3. **Atomic persist**: statement status + a NEW append-only ``EvidenceLink``
   (method='verify', score=matrix result — the extractor's method='extract'
   link is never touched) + an immutable ``audit_trace`` verdict row, committed
   together; any failure rolls back all three.

Guarantees: G-01 (system message = instructions only; user message = delimited
``<statement_data>``/``<passage_data>`` blocks); G-05 (statement/passage text
and the judge reason are redacted before they leave the process or persist;
``use_cache=False`` so model output is never cached); G-11 (QuarantineError
propagates; on failure the statement stays draft with no audit row and no new
link). Verification is idempotent: non-draft statements are skipped unless
``force=True``. Every verification emits a ``statement.verify`` span with
decision attributes and a cumulative support-ratio log line.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.telemetry import get_tracer
from app.db.enums import EvidenceScore, StatementStatus
from app.db.models import EvidenceLink, Passage, Statement
from app.db.session import async_session_factory
from app.services.audit_writer import AuditWriter
from app.services.llm_gateway import LLMGateway
from app.services.normalizer import redact_secrets
from app.services.support_matrix import score_support

logger = get_logger("app.services.verifier")

SessionFactory = Callable[[], AsyncSession]

_SYSTEM_INSTRUCTIONS = (
    "You are a rigorous evidence judge. Decide whether the statement is "
    "directly supported by the passage. Follow these rules exactly: 1) verdict "
    "is 'supported' only when the passage directly states the claim or its "
    "essential facts; 2) 'unsupported' when the passage contradicts, omits, or "
    "only weakly implies the claim; 3) never use outside knowledge; 4) reason "
    "(1-2000 characters) must cite the specific passage evidence; 5) "
    "confidence in [0.0, 1.0] rates how clearly the passage supports the "
    "verdict, or null when it cannot be judged. Judge only the statement_data "
    "and passage_data blocks."
)


class VerificationVerdict(BaseModel):
    """Structured judge output: is the statement directly supported by the passage?"""

    supported: bool = Field(description="True when the passage directly supports the statement.")
    verdict: Literal["supported", "unsupported"] = Field(
        description="Human-readable verdict label matching the supported flag."
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "Evidence-based justification citing specific passage content; "
            "non-empty and at most 2000 characters."
        ),
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in [0.0, 1.0] that the passage supports the verdict; "
            "null when it cannot be judged."
        ),
    )


@dataclass(frozen=True)
class VerificationOutcome:
    """Result of one statement verification."""

    decision: StatementStatus
    support_score: EvidenceScore | None = None
    matrix_ratio: float | None = None
    judge_supported: bool | None = None
    judge_confidence: float | None = None
    skipped: bool = False


def build_judge_prompt(statement_text: str, passage_text: str) -> tuple[str, str]:
    """Build ``(system, user_data_block)`` for the judge call (G-01).

    The system message holds instructions only; the user message holds the
    labeled, delimited statement and passage data blocks.
    """
    data = (
        f"<statement_data>\n{statement_text}\n</statement_data>\n\n"
        f"<passage_data>\n{passage_text}\n</passage_data>"
    )
    return _SYSTEM_INSTRUCTIONS, data


class Verifier:
    """Orchestrates the verify-first gate: matrix -> judge -> atomic persist."""

    def __init__(
        self,
        gateway: LLMGateway,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._gateway = gateway
        self._session_factory = session_factory or async_session_factory
        self._audit_writer = AuditWriter(session_factory=session_factory)
        self._verified_count = 0
        self._processed_count = 0

    async def verify(
        self,
        statement: Statement,
        passage: Passage,
        run_id: UUID | str,
        *,
        force: bool = False,
    ) -> VerificationOutcome:
        """Run the verify-first gate on one draft statement.

        Returns the :class:`VerificationOutcome`; raises
        :class:`QuarantineError` when the judge output never validates (G-11)
        or propagates any other failure — in every failure case the statement
        stays ``draft`` and no audit row or evidence link is written.
        """
        async with self._session_factory() as session:
            try:
                with get_tracer("ecrke").start_as_current_span("statement.verify") as span:
                    return await self._verify_locked(
                        session, span, statement, passage, run_id, force
                    )
            except Exception:
                await session.rollback()
                raise

    async def _verify_locked(
        self,
        session: AsyncSession,
        span: Any,
        statement: Statement,
        passage: Passage,
        run_id: UUID | str,
        force: bool,
    ) -> VerificationOutcome:
        # Merge the detached statement into THIS session so status updates
        # are tracked and persisted on commit.
        statement = await session.merge(statement)

        if statement.status != StatementStatus.DRAFT.value and not force:
            return VerificationOutcome(decision=StatementStatus(statement.status), skipped=True)

        statement_text = redact_secrets(statement.text)
        passage_text = redact_secrets(passage.text)
        score, ratio = score_support(statement_text, passage_text)

        judge_supported: bool | None = None
        judge_confidence: float | None = None
        reason: str
        if score in (EvidenceScore.FULL, EvidenceScore.PARTIAL):
            system, data = build_judge_prompt(statement_text, passage_text)
            result = await self._gateway.complete(
                tier="strong",
                system=system,
                prompt=data,
                response_model=VerificationVerdict,
                run_id=run_id,
                use_cache=False,
            )
            verdict: VerificationVerdict = result.data
            judge_supported = verdict.supported
            judge_confidence = verdict.confidence
            reason = verdict.reason
            decision = (
                StatementStatus.VERIFIED if verdict.supported else StatementStatus.QUARANTINED
            )
        else:
            judge_supported = False
            reason = (
                f"lexical support ratio {ratio:.3f} below partial threshold; "
                "quarantined by deterministic support matrix"
            )
            decision = StatementStatus.QUARANTINED

        evidence: dict[str, Any] = {
            "support_score": score.value,
            "matrix_ratio": ratio,
            "judge_supported": judge_supported,
            "judge_confidence": judge_confidence,
        }

        # Atomic persist: status + NEW append-only link + audit verdict row.
        statement.status = decision.value
        session.add(
            EvidenceLink(
                id=uuid4(),
                statement_id=statement.id,
                passage_id=passage.id,
                run_id=run_id,
                score=score.value,
                method="verify",
            )
        )
        self._audit_writer.append(
            session,
            run_id=run_id,
            entity_type="statement",
            entity_id=str(statement.id),
            action="verify",
            actor="verifier",
            decision=decision.value,
            reason=redact_secrets(reason),
            evidence=evidence,
        )
        await session.commit()

        span.set_attribute("decision", decision.value)
        span.set_attribute("support_score", score.value)
        span.set_attribute("matrix_ratio", ratio)

        self._processed_count += 1
        if decision is StatementStatus.VERIFIED:
            self._verified_count += 1
        logger.info(
            "support_ratio verified=%s total=%s ratio=%s",
            self._verified_count,
            self._processed_count,
            f"{self._verified_count / self._processed_count:.3f}",
        )

        return VerificationOutcome(
            decision=decision,
            support_score=score,
            matrix_ratio=ratio,
            judge_supported=judge_supported,
            judge_confidence=judge_confidence,
        )

    def log_support_ratio(self) -> tuple[int, int]:
        """Log the cumulative support ratio; returns ``(verified, total)``."""
        ratio = (
            f"{self._verified_count / self._processed_count:.3f}"
            if self._processed_count
            else "0.000"
        )
        logger.info(
            "support_ratio verified=%s total=%s ratio=%s",
            self._verified_count,
            self._processed_count,
            ratio,
        )
        return self._verified_count, self._processed_count
