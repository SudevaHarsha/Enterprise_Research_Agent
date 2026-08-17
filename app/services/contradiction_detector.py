"""Flag-first / confirm-second contradiction detection (task_008, build-plan Step 8).

Among **verified** statements the detector finds conflicting claims and records
them as confirmed ``contradictions`` rows — never silently merged (G5):

1. **Deterministic candidate generation** (``candidate_pairs``, $0, no LLM):
   only status='verified' statements are considered; pairs whose content-token
   Jaccard overlap is below :data:`CANDIDATE_OVERLAP_THRESHOLD` (0.15) AND carry
   no negation marker are pruned, so unrelated pairs NEVER reach the LLM (G-03).
2. **Flag-first**: a strong-tier judge over each candidate pair returns a
   :class:`ContradictionFlag`; ``no_flag`` pairs are dropped with no write.
3. **Confirm-second**: for flagged pairs, the deterministic negation signal
   (:func:`negation_signal` — shared core content above
   :data:`CONFIRM_OVERLAP_THRESHOLD` (0.4) with exactly one negated side)
   confirms WITHOUT a second judge call; otherwise a second strong-tier judge
   opinion (:class:`ConfirmVerdict`) decides confirmed | rejected.
4. **Atomic persist — confirmed only**: a ``contradictions`` row
   (status='confirmed', ``confirmed_at`` set, evidence JSONB) plus an
   ``audit_trace`` verdict row (action='contradiction', decision='confirmed',
   entity_type='contradiction') committed in ONE transaction; any failure rolls
   back both. Flagged-only and rejected pairs are logged + span-attributed,
   NEVER persisted (rows only on confirmed).

Guarantees: G-01 (system = instructions only; user = delimited
``<statement_a_data>``/``<statement_b_data>`` blocks); G-05 (statement text is
redacted before any prompt and before any persisted field incl. evidence JSONB;
``use_cache=False`` for all judge calls); G-11 (QuarantineError propagates; no
contradiction row on failure). Detection is idempotent: an already-confirmed
pair (either statement order) is skipped with zero LLM spend. ``detect``
returns every newly confirmed row so the Step 14 gold-set recall harness can
measure contradiction recall. Every call emits a ``contradiction.detect`` span
with pairs_considered/candidates/flagged/confirmed/rejected attributes and a
cumulative metrics log line.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.telemetry import get_tracer
from app.db.enums import ContradictionStatus, StatementStatus
from app.db.models import Contradiction, Statement
from app.db.session import async_session_factory
from app.services.audit_writer import AuditWriter, redact_json
from app.services.llm_gateway import LLMGateway
from app.services.normalizer import redact_secrets

logger = get_logger("app.services.contradiction_detector")

SessionFactory = Callable[[], AsyncSession]

# Candidate pruning: a pair is a candidate when the content-token Jaccard
# overlap is at or above this threshold OR either statement carries a negation
# marker (low-overlap negation pairs are semantically high-value).
CANDIDATE_OVERLAP_THRESHOLD = 0.15
# Confirm heuristic: min-containment at or above this threshold is required for
# the deterministic negation signal to fire (statements must share core content).
CONFIRM_OVERLAP_THRESHOLD = 0.4
# Marker words that flip a claim's polarity; a one-sided marker on shared-core
# content is a strong contradiction signal.
NEGATION_MARKERS = frozenset(
    {
        "not",
        "no",
        "never",
        "none",
        "without",
        "fails",
        "refutes",
        "denies",
        "disputes",
        "contradicts",
        "unable",
    }
)

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

# Rate-limit pacing: max candidate pairs to evaluate per run, and
# inter-call delay to respect the Gemini 15 RPM free-tier quota.
MAX_CONTRADICTION_PAIRS = 10
CONTRADICTION_DELAY_SECONDS = 5

_NEGATION_CONFIRM_REASON = (
    "deterministic negation signal: the statements share the same core content "
    "(min-containment at or above the confirm threshold) and exactly one side "
    "carries negation markers; an affirmative and a negated version of the same "
    "claim cannot both be true"
)


class ContradictionFlag(BaseModel):
    """Structured flag-judge output: does statement A contradict statement B?"""

    contradictory: bool = Field(
        description="True when statement A and statement B assert logically incompatible facts."
    )
    flag: Literal["flag", "no_flag"] = Field(
        description="Human-readable flag label matching the contradictory flag."
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "Evidence-based justification citing the specific conflict in both "
            "statements; non-empty and at most 2000 characters."
        ),
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in [0.0, 1.0] that the statements conflict; null when it cannot be judged."
        ),
    )


class ConfirmVerdict(BaseModel):
    """Structured confirm-judge output: is the flagged conflict real?"""

    contradictory: bool = Field(
        description="True when statement A and statement B cannot both be true."
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description=("Evidence-based justification; non-empty and at most 2000 characters."),
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in [0.0, 1.0] that the conflict is real; null when it cannot be judged."
        ),
    )


_FLAG_SYSTEM_INSTRUCTIONS = (
    "You are a rigorous contradiction flagger. Decide whether statement A "
    "contradicts statement B. Follow these rules exactly: 1) flag='flag' only "
    "when the two statements assert logically incompatible facts about the "
    "same subject — they cannot both be true; 2) flag='no_flag' when they "
    "agree, overlap without conflict, or address different subjects; 3) never "
    "use outside knowledge; 4) reason (1-2000 characters) must cite the "
    "specific conflict in both statements; 5) confidence in [0.0, 1.0] rates "
    "how clearly the statements conflict, or null when it cannot be judged. "
    "Judge only the statement_a_data and statement_b_data blocks."
)

_CONFIRM_SYSTEM_INSTRUCTIONS = (
    "You are a rigorous contradiction confirmer. A previous judge flagged that "
    "statement A contradicts statement B. Decide whether the conflict is real. "
    "Follow these rules exactly: 1) contradictory=true only when the statements "
    "cannot both be true about the same subject; 2) contradictory=false when "
    "they are compatible in context, address different subjects, or only "
    "appear to conflict; 3) never use outside knowledge; 4) reason (1-2000 "
    "characters) must cite the specific facts in both statements; 5) "
    "confidence in [0.0, 1.0] rates how clearly the conflict is real, or null "
    "when it cannot be judged. Judge only the statement_a_data and "
    "statement_b_data blocks."
)


def tokenize(text: str) -> set[str]:
    """Casefolded alphanumeric word set (punctuation and hyphens ignored)."""
    return set(_TOKEN_PATTERN.findall(text.casefold()))


def jaccard_overlap(a_tokens: set[str], b_tokens: set[str]) -> float:
    """Jaccard similarity of two token sets (0.0 when both are empty)."""
    union = a_tokens | b_tokens
    if not union:
        return 0.0
    return len(a_tokens & b_tokens) / len(union)


def containment_overlap(a_tokens: set[str], b_tokens: set[str]) -> float:
    """Min containment: the smaller shared fraction across both sets.

    Returns 0.0 when either set is empty; the result is always in [0.0, 1.0].
    """
    if not a_tokens or not b_tokens:
        return 0.0
    shared = a_tokens & b_tokens
    return min(len(shared) / len(a_tokens), len(shared) / len(b_tokens))


def has_negation_markers(text: str) -> bool:
    """True when the text carries any marker word from :data:`NEGATION_MARKERS`."""
    return bool(tokenize(text) & NEGATION_MARKERS)


def negation_signal(a_text: str, b_text: str) -> bool:
    """Deterministic strong-confirm signal for one-sided negation.

    True only when the statements share the same core content (min-containment
    at or above :data:`CONFIRM_OVERLAP_THRESHOLD`) and exactly one side carries
    negation markers — an affirmative and a negated version of the same claim
    cannot both be true. Direction-agnostic.
    """
    a_tokens = tokenize(a_text)
    b_tokens = tokenize(b_text)
    if containment_overlap(a_tokens, b_tokens) < CONFIRM_OVERLAP_THRESHOLD:
        return False
    return has_negation_markers(a_text) != has_negation_markers(b_text)


def candidate_pairs(statements: Iterable[Statement]) -> list[tuple[Statement, Statement]]:
    """Deterministic, $0 candidate generation among verified statements.

    Only status='verified' statements are considered (defensive filter). A pair
    is a candidate when its content-token Jaccard overlap is at or above
    :data:`CANDIDATE_OVERLAP_THRESHOLD` OR either statement carries a negation
    marker. Pairs are ordered by statement id so generation is deterministic;
    no LLM is touched (G-03).
    """
    verified = [s for s in statements if s.status == StatementStatus.VERIFIED.value]
    verified.sort(key=lambda s: s.id)
    pairs: list[tuple[Statement, Statement]] = []
    for index, a in enumerate(verified):
        for b in verified[index + 1 :]:
            a_text = redact_secrets(a.text)
            b_text = redact_secrets(b.text)
            ratio = jaccard_overlap(tokenize(a_text), tokenize(b_text))
            if (
                ratio >= CANDIDATE_OVERLAP_THRESHOLD
                or has_negation_markers(a_text)
                or has_negation_markers(b_text)
            ):
                pairs.append((a, b))
    return pairs


def build_flag_prompt(a_text: str, b_text: str) -> tuple[str, str]:
    """Build ``(system, user_data_block)`` for the flag judge call (G-01)."""
    data = (
        f"<statement_a_data>\n{a_text}\n</statement_a_data>\n\n"
        f"<statement_b_data>\n{b_text}\n</statement_b_data>"
    )
    return _FLAG_SYSTEM_INSTRUCTIONS, data


def build_confirm_prompt(a_text: str, b_text: str) -> tuple[str, str]:
    """Build ``(system, user_data_block)`` for the confirm judge call (G-01)."""
    data = (
        f"<statement_a_data>\n{a_text}\n</statement_a_data>\n\n"
        f"<statement_b_data>\n{b_text}\n</statement_b_data>"
    )
    return _CONFIRM_SYSTEM_INSTRUCTIONS, data


class ContradictionDetector:
    """Orchestrates flag-first / confirm-second contradiction detection."""

    def __init__(
        self,
        gateway: LLMGateway,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._gateway = gateway
        self._session_factory = session_factory or async_session_factory
        self._audit_writer = AuditWriter(session_factory=session_factory)

    async def detect(
        self,
        statements: Iterable[Statement],
        run_id: UUID | str,
    ) -> list[Contradiction]:
        """Detect confirmed contradictions among ``statements``.

        Returns every newly confirmed :class:`Contradiction` row (the Step 14
        gold-set recall hook). Raises :class:`QuarantineError` when a judge
        output never validates (G-11) or propagates any other failure — in
        every failure case no contradiction row is written.
        """
        async with self._session_factory() as session:
            try:
                with get_tracer("ecrke").start_as_current_span("contradiction.detect") as span:
                    return await self._detect_locked(session, span, statements, run_id)
            except Exception:
                await session.rollback()
                raise

    async def _detect_locked(
        self,
        session: AsyncSession,
        span: Any,
        statements: Iterable[Statement],
        run_id: UUID | str,
    ) -> list[Contradiction]:
        verified = [s for s in statements if s.status == StatementStatus.VERIFIED.value]
        pairs_considered = len(verified) * (len(verified) - 1) // 2
        candidates = candidate_pairs(verified)
        # Cap to control LLM spend under 15 RPM Gemini quota.
        candidates = candidates[:MAX_CONTRADICTION_PAIRS]
        flagged_count = 0
        confirmed_count = 0
        rejected_count = 0
        confirmed_rows: list[Contradiction] = []

        for pair_idx, (a, b) in enumerate(candidates):
            if pair_idx > 0:
                await asyncio.sleep(CONTRADICTION_DELAY_SECONDS)
            a_text = redact_secrets(a.text)
            b_text = redact_secrets(b.text)
            if await self._confirmed_exists(session, a, b):
                continue

            system, data = build_flag_prompt(a_text, b_text)
            result = await self._gateway.complete(
                tier="strong",
                system=system,
                prompt=data,
                response_model=ContradictionFlag,
                run_id=run_id,
                use_cache=False,
            )
            flag: ContradictionFlag = result.data
            if not (flag.flag == "flag" and flag.contradictory):
                continue
            flagged_count += 1

            overlap_ratio = jaccard_overlap(tokenize(a_text), tokenize(b_text))
            signal = negation_signal(a_text, b_text)
            if signal:
                method = "confirm:negation_signal"
                confirm_reason = _NEGATION_CONFIRM_REASON
                confirm_confidence: float | None = None
            else:
                method = "confirm:judge"
                confirm_system, confirm_data = build_confirm_prompt(a_text, b_text)
                confirm_result = await self._gateway.complete(
                    tier="strong",
                    system=confirm_system,
                    prompt=confirm_data,
                    response_model=ConfirmVerdict,
                    run_id=run_id,
                    use_cache=False,
                )
                verdict: ConfirmVerdict = confirm_result.data
                confirm_reason = verdict.reason
                confirm_confidence = verdict.confidence
                if not verdict.contradictory:
                    rejected_count += 1
                    logger.info(
                        "contradiction_rejected method=%s statement_a_id=%s statement_b_id=%s",
                        method,
                        a.id,
                        b.id,
                    )
                    continue

            confirmed_count += 1
            evidence: dict[str, Any] = {
                "flag_reason": flag.reason,
                "flag_confidence": flag.confidence,
                "confirm_reason": confirm_reason,
                "confirm_confidence": confirm_confidence,
                "overlap_ratio": overlap_ratio,
                "negation_signal": signal,
                "method": method,
            }
            row = Contradiction(
                id=uuid4(),
                run_id=run_id,
                statement_a_id=a.id,
                statement_b_id=b.id,
                status=ContradictionStatus.CONFIRMED.value,
                evidence=redact_json(evidence),
                confirmed_at=datetime.now(UTC),
            )
            session.add(row)
            self._audit_writer.append(
                session,
                run_id=run_id,
                entity_type="contradiction",
                entity_id=str(row.id),
                action="contradiction",
                actor="detector",
                decision=ContradictionStatus.CONFIRMED.value,
                reason=redact_secrets(confirm_reason),
                evidence=evidence,
            )
            await session.commit()
            confirmed_rows.append(row)

        span.set_attribute("pairs_considered", pairs_considered)
        span.set_attribute("candidates", len(candidates))
        span.set_attribute("flagged", flagged_count)
        span.set_attribute("confirmed", confirmed_count)
        span.set_attribute("rejected", rejected_count)
        logger.info(
            "contradiction_metrics pairs_considered=%s candidates=%s "
            "flagged=%s confirmed=%s rejected=%s",
            pairs_considered,
            len(candidates),
            flagged_count,
            confirmed_count,
            rejected_count,
        )
        return confirmed_rows

    async def _confirmed_exists(self, session: AsyncSession, a: Statement, b: Statement) -> bool:
        """True when a confirmed row already exists for the pair (either order)."""
        if await session.scalar(self._confirmed_stmt(a, b)) is not None:
            return True
        return await session.scalar(self._confirmed_stmt(b, a)) is not None

    @staticmethod
    def _confirmed_stmt(a: Statement, b: Statement) -> Any:
        """Select a confirmed contradiction for the ordered pair."""
        return select(Contradiction).where(
            and_(
                Contradiction.statement_a_id == a.id,
                Contradiction.statement_b_id == b.id,
                Contradiction.status == ContradictionStatus.CONFIRMED.value,
            )
        )
