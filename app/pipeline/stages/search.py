"""Stage 2 — search: derive queries from the plan artifact, collect candidate URIs."""

from __future__ import annotations

from prefect import task

from app.pipeline.context import PipelineContext, StageResult
from app.services.planner import Planner


@task(name="pipeline.search")
async def run_search(ctx: PipelineContext) -> StageResult:
    """Run one search per plan sub-question; checkpoint the candidate URL list.

    Requires the ``research_plan:{run_id}`` kv_cache artifact produced by the
    define stage — a missing artifact raises ``ValueError`` BEFORE any search
    connector call.
    """
    services = ctx.services
    plan_key = Planner.plan_key(ctx.run_id)
    plan = await services.cache.get(plan_key)
    if plan is None:
        raise ValueError(
            f"research_plan artifact missing for run_id={ctx.run_id} (run the define stage first)"
        )
    queries = list(plan.get("sub_questions") or [])
    limit = services.settings.search_results_limit
    urls: list[str] = []
    seen: set[str] = set()
    for query in queries:
        found = await services.search_connector.search(query, limit)
        for uri in found:
            if uri not in seen:
                seen.add(uri)
                urls.append(uri)
    return StageResult(stage="search", ok=True, detail={"urls": urls})
