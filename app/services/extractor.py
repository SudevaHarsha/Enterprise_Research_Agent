"""Statement extraction from passages (task_006, build-plan Step 6).

Extracts atomic, draft-only statements from a passage via the cheap LLM tier
under a strict Pydantic schema. G-01: extraction instructions live in the
system message; the user message carries only a labeled, delimited passage
data block. Every statement is persisted with a passage provenance row in
``evidence_links`` (method='extract', score='none' until the verify-first
gate). Guarantees:

- G-05: the passage text is redacted before it leaves the process, the model
  output is never persisted to the cache (``use_cache=False``), and statement
  text is redacted again before persist.
- G-11: schema failure on every retry raises ``QuarantineError`` and rolls
  back the transaction — no partial writes.
- Verify-first: only ``draft`` statements are ever written.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import EvidenceScore, StatementStatus
from app.db.models import EvidenceLink, Passage, Statement
from app.db.session import async_session_factory
from app.services.llm_gateway import LLMGateway
from app.services.normalizer import redact_secrets
from app.services.statement_schema import StatementExtraction

logger = get_logger("app.services.extractor")

SessionFactory = Callable[[], AsyncSession]

_SYSTEM_INSTRUCTIONS = (
    "You extract atomic factual statements from a research passage. Follow these "
    "rules exactly: 1) each statement is one atomic claim directly supported by "
    "the passage; 2) preserve the claim's meaning, names, and figures faithfully; "
    "3) add no outside knowledge, interpretation, or speculation; 4) if the "
    "passage contains no extractable claims, return an empty statements list; "
    "5) confidence (0.0-1.0) rates how directly the passage supports the "
    "statement, or null when you cannot judge it. Extract only from the "
    "passage_data block."
)


def _build_data_block(passage_id: UUID, text: str) -> str:
    """Labeled, delimited passage block placed in the user message (G-01)."""
    return f'<passage_data passage_id="{passage_id}">\n{text}\n</passage_data>'


class Extractor:
    """Splits a passage into atomic draft statements with evidence links."""

    def __init__(
        self,
        gateway: LLMGateway,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._gateway = gateway
        self._session_factory = session_factory or async_session_factory

    async def extract(self, passage: Passage, run_id: UUID | str) -> list[Statement]:
        """Extract and persist draft statements for ``passage``.

        Returns the persisted :class:`Statement` rows (empty when the passage
        has no extractable claims). Raises :class:`QuarantineError` when the
        LLM output never validates (G-11); no rows are written in that case.
        """
        text = redact_secrets(passage.text)
        if not text.strip():
            raise ValueError("cannot extract statements from an empty passage")
        async with self._session_factory() as session:
            try:
                result = await self._gateway.complete(
                    tier="cheap",
                    system=_SYSTEM_INSTRUCTIONS,
                    prompt=_build_data_block(passage.id, text),
                    response_model=StatementExtraction,
                    run_id=run_id,
                    use_cache=False,
                )
                extraction: StatementExtraction = result.data
                if not extraction.statements:
                    return []
                rows: list[Statement] = []
                link_statements: list[UUID] = []
                for item in extraction.statements:
                    statement = Statement(
                        id=uuid4(),
                        passage_id=passage.id,
                        run_id=run_id,
                        text=redact_secrets(item.text),
                        status=StatementStatus.DRAFT.value,
                        confidence=item.confidence,
                    )
                    session.add(statement)
                    rows.append(statement)
                    link_statements.append(statement.id)
                # Flush so statement rows exist in DB before the FK-referencing
                # evidence_links are inserted (avoids IntegrityError).
                await session.flush()
                for stmt_id in link_statements:
                    session.add(
                        EvidenceLink(
                            id=uuid4(),
                            statement_id=stmt_id,
                            passage_id=passage.id,
                            run_id=run_id,
                            score=EvidenceScore.NONE.value,
                            method="extract",
                        )
                    )
                await session.commit()
                logger.info(
                    "extracted %s statement(s) for passage_id=%s run_id=%s",
                    len(rows),
                    passage.id,
                    run_id,
                )
                return rows
            except Exception:
                await session.rollback()
                raise
