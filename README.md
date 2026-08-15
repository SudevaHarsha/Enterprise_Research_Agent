# ECRKE — Evidence-Centric Research Knowledge Engine

Enterprise AI research agent: submit a research question, observe the 10-stage
research pipeline operate, and audit every conclusion back to the source
passage that supports it. The product is the verified, reusable knowledge base
— not the report.

> **Status:** scaffold (`task_001`). Full README/setup docs land in `task_015`.

## Quick start (development)

```powershell
git clone <repo-url>
cd ecrke
Copy-Item .env.example .env     # fill in your values
docker compose up -d postgres   # start the provenance DB
.\scripts\dev.ps1 setup         # create venv + install
.\scripts\dev.ps1 verify        # lint + type + tests
```

## Layout

```
app/            Python package (api, core, db, models, pipeline, services, workers)
tests/          pytest suite (unit + eval harness + adversarial guardrail suite)
sample_data/    sample/synthetic sources for evaluation
docs/           design, tech stack, build plan, data model, licenses
scripts/        dev.ps1 convenience wrapper
.github/        CI workflows
```

## Reference

- Design doc: `docs/enterprise-research-agent-design.md`
- Tech stack: `docs/tech-stack.md`
- Build plan: `docs/build-plan.md`
