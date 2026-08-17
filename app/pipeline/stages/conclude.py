"""Stage 9 — conclude: synthesize verified evidence into the run report."""

from __future__ import annotations

from prefect import task
from sqlalchemy import select

from app.db.models import Contradiction, Run, Statement
from app.pipeline.context import PipelineContext, StageResult
from app.services.report_renderer import render_markdown


@task(name="pipeline.conclude")
async def run_conclude(ctx: PipelineContext) -> StageResult:
    """Synthesize verified statements into a Report and persist artifacts.

    Delegates to the report generator (which persists conclusion rows
    atomically), then renders the report as markdown (blob store) and as
    JSON (checkpoint state consumed by the trace stage).
    """
    services = ctx.services
    async with ctx.session_factory() as session:
        run = await session.get(Run, ctx.run_id)
        if run is None:
            raise ValueError(f"no Run row for run_id={ctx.run_id}")
        topic = run.question
        verified = [
            statement
            for statement in await session.scalars(
                select(Statement).where(Statement.run_id == ctx.run_id)
            )
            if statement.status == "verified"
        ][:30]  # cap to control LLM calls in report generation
        confirmed = [
            contradiction
            for contradiction in await session.scalars(
                select(Contradiction).where(Contradiction.run_id == ctx.run_id)
            )
            if contradiction.status == "confirmed"
        ]
    report = await services.report_generator.generate(
        ctx.run_id,
        topic=topic,
        verified_statements=verified,
        confirmed_contradictions=confirmed,
    )
    markdown_ref = f"runs/{ctx.run_id}/reports/report.md"
    await services.blob_store.put(markdown_ref, render_markdown(report).encode("utf-8"))
    return StageResult(
        stage="conclude",
        ok=True,
        detail={
            "report": report.model_dump(mode="json"),
            "report_markdown_ref": markdown_ref,
            "conclusions": len(report.conclusions),
            "contradictions": len(confirmed),
        },
    )
