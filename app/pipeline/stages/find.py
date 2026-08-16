"""Stage 7 — find: $0 grouping of verified statements into tiered findings.

Deterministic and LLM-free (``provider.calls == 0`` is asserted in tests):

- resolve each verified statement's source domain from its passage's source URI
- best evidence score per statement from its ``method='verify'`` links
  (full > partial > none)
- group statements by domain; tier = t1 if any full, t2 if any partial,
  else t3
- persist one Finding per domain + FindingStatement links

Uses ONE query per entity (statements, sources, passages, links) keyed by
run_id — no N+1.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlparse
from uuid import uuid4

from prefect import task
from sqlalchemy import select

from app.db.models import EvidenceLink, Finding, FindingStatement, Passage, Source, Statement
from app.pipeline.context import PipelineContext, StageResult

_SCORE_RANK = {"none": 0, "partial": 1, "full": 2}
_TIER_BY_BEST = {"none": "t3", "partial": "t2", "full": "t1"}


def _best_score(statement_id: object, links: Iterable[EvidenceLink]) -> str:
    """Best evidence score for a statement across its verify links."""
    best = "none"
    for link in links:
        if link.statement_id != statement_id or link.method != "verify":
            continue
        score = (link.score or "none").lower()
        if _SCORE_RANK.get(score, 0) > _SCORE_RANK[best]:
            best = score
    return best


@task(name="pipeline.find")
async def run_find(ctx: PipelineContext) -> StageResult:
    """Group verified statements by source domain into tiered findings."""
    async with ctx.session_factory() as session:
        sources = {
            source.id: source.uri
            for source in await session.scalars(select(Source).where(Source.run_id == ctx.run_id))
        }
        source_ids = list(sources)
        passages = {
            passage.id: passage
            for passage in (
                await session.scalars(select(Passage).where(Passage.source_id.in_(source_ids)))
                if source_ids
                else []
            )
        }
        statements = [
            statement
            for statement in await session.scalars(
                select(Statement).where(Statement.run_id == ctx.run_id)
            )
            if statement.status == "verified"
        ]
        links = list(
            await session.scalars(select(EvidenceLink).where(EvidenceLink.run_id == ctx.run_id))
        )
    # domain -> statements, derived from passage -> source URI (never from links)
    domains: dict[str, list[Statement]] = {}
    for statement in statements:
        passage = passages.get(statement.passage_id)
        if passage is None:
            continue
        uri = sources.get(passage.source_id, "")
        domain = urlparse(uri).netloc or "unknown"
        domains.setdefault(domain, []).append(statement)

    created = 0
    for domain, domain_statements in domains.items():
        scores = {_best_score(statement.id, links) for statement in domain_statements}
        tier = (
            _TIER_BY_BEST["full"]
            if "full" in scores
            else (_TIER_BY_BEST["partial"] if "partial" in scores else _TIER_BY_BEST["none"])
        )
        finding = Finding(
            id=uuid4(),
            run_id=ctx.run_id,
            title=f"Evidence cluster: {domain}",
            evidence_tier=tier,
            domain_tags=[domain],
            summary=f"{len(domain_statements)} verified statement(s) from {domain}",
        )
        async with ctx.session_factory() as session:
            session.add(finding)
            for statement in domain_statements:
                session.add(FindingStatement(finding_id=finding.id, statement_id=statement.id))
            await session.commit()
        created += 1
    return StageResult(stage="find", ok=True, detail={"findings": created})
