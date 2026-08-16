"""Stage 1 — define: generate + persist the research plan artifact."""

from __future__ import annotations

from prefect import task

from app.db.models import Run
from app.pipeline.context import PipelineContext, StageResult


@task(name="pipeline.define")
async def run_define(ctx: PipelineContext) -> StageResult:
    """Generate the multi-perspective plan bound to ``research_plan:{run_id}``.

    Reads the run's question, delegates to the planner (which persists the
    plan artifact itself), and returns a JSON-serializable, already-redacted
    summary for the define checkpoint.
    """
    async with ctx.session_factory() as session:
        run = await session.get(Run, ctx.run_id)
        if run is None:
            raise ValueError(f"no Run row for run_id={ctx.run_id}")
        topic = run.question
    plan = await ctx.services.planner.plan(topic, ctx.run_id)
    return StageResult(
        stage="define",
        ok=True,
        detail={
            "plan_topic": plan.topic,
            "sub_questions": list(plan.sub_questions),
        },
    )
