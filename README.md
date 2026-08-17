# ECRKE — Evidence-Centric Research Knowledge Engine

Enterprise AI research agent: submit a research question, observe a 10-stage
research pipeline operate, and audit every conclusion back to the source passage
that supports it.

**The product is the verified, reusable knowledge base — not the report.**

```
submit question ──► 10-stage pipeline ──► verified statements + findings
                        │                       │
                        └── contradictions ─────┤
                                                ▼
                                  conclusions → evidence links → passages → sources
```

## Why this exists

LLM research tools produce confident answers with no chain of custody. ECRKE
inverts that: nothing enters the knowledge base unverified, every conclusion is
traceable to a stored source passage in ≤ 1 hop, and the audit trail of every
knowledge-base write decision is immutable. If a source is later corrected, the
affected evidence is re-verified — never silently rewritten.

## The stack

- **API / control:** FastAPI + Pydantic v2 (RFC 7807 errors, tenant isolation)
- **Orchestration:** Prefect 3 DAG (10 stages, checkpoints, circuit breaker) +
  LangGraph agentic loops
- **Storage:** PostgreSQL 16 (relational provenance core, 14 tables, append-only
  `evidence_links` + `audit_trace`), SQLAlchemy 2 + Alembic, Postgres-backed
  queue/cache (no Redis)
- **LLM access:** LiteLLM gateway — provider-agnostic, cost-metered
- **Observability:** OpenTelemetry + Prometheus + Grafana
- **Runtime:** Python ≥ 3.12 (developed against 3.12; tested in CI on 3.12)

## Quick start (development)

```powershell
git clone <repo-url>
cd ecrke
Copy-Item .env.example .env     # fill in your values (see docs/tech-stack.md)
docker compose up -d postgres   # start the provenance DB (host port 5433)
.\scripts\dev.ps1 setup         # create venv + install (editable, dev extras)
.\scripts\dev.ps1 verify        # ruff + mypy + pytest
```

> Docker daemon must be running for `docker compose up`. If it is not, you can
> still run the full unit + eval suite — only the Testcontainers-backed database
> tests (`tests/test_database.py`) require a running Postgres.

### Full Docker stack

```powershell
docker compose up -d            # postgres + prefect-server + api + worker
docker compose --profile observability up -d   # + prometheus + grafana
```

Open **http://localhost:3456/** for the web dashboard — submit a research
question, watch the 10-stage pipeline execute in real time, and inspect
conclusions with their provenance chains. The OpenAPI explorer is at
**http://localhost:3456/docs**.

| Service | Host URL | Notes |
|---|---|---|
| Dashboard | http://localhost:3456/ | Web UI — submit questions, watch the pipeline, view results |
| API | http://localhost:3456/docs | OpenAPI docs, interactive explorer |
| Prefect server | http://localhost:4200 | Flow/run UI (set `PREFECT_API_URL` to use it) |
| Prometheus | http://localhost:9090 | Scrapes `/metrics` (observability profile) |
| Grafana | http://localhost:3000 | Provisioned ECRKE dashboard (observability profile) |
| PostgreSQL | localhost:5433 | Databases `ecrke`, `ecrke_prefect` |

## API surface

All endpoints are tenant-scoped via the `X-Tenant-ID` header (403 on
cross-tenant access). Errors follow RFC 7807 Problem Details.

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/runs` | POST | Submit a research question (201) |
| `/v1/runs/{run_id}` | GET | Poll one run's lifecycle state |
| `/v1/runs/{run_id}/stages` | GET | Per-stage durable checkpoint info |
| `/v1/runs/{run_id}/conclusions` | GET | Final conclusions with evidence refs |
| `/v1/statements/{statement_id}/trace` | GET | Provenance chain statement → passage → source |
| `/v1/runs/{run_id}/contradictions` | GET | Flagged/confirmed contradiction records |
| `/v1/runs/{run_id}/report` | GET | Rendered markdown report (no LLM in API) |
| `/v1/runs/{run_id}/resume` | POST | Re-enter pipeline at first missing stage |
| `/v1/kb/search` | GET | Search verified statements across tenant |
| `/v1/runs/{run_id}/audit` | GET | Immutable audit trace export (redacted) |
| `/healthz`, `/readyz`, `/metrics` | GET | Liveness, readiness, Prometheus metrics |

### One run, end to end

```powershell
# 1. submit (execute=true runs the pipeline in the background)
$body = '{"question":"How is AI transforming retail operations?","execute":true,"cost_budget_usd":2.0}'
$run = Invoke-RestMethod -Method Post -Uri http://localhost:3456/v1/runs -Headers @{ "X-Tenant-ID" = "default" } -ContentType "application/json" -Body $body

# 2. poll the lifecycle
Invoke-RestMethod -Uri "http://localhost:3456/v1/runs/$($run.run_id)" -Headers @{ "X-Tenant-ID" = "default" }

# 3. read conclusions and trace one back to its source
Invoke-RestMethod -Uri "http://localhost:3456/v1/runs/$($run.run_id)/conclusions" -Headers @{ "X-Tenant-ID" = "default" }
Invoke-RestMethod -Uri "http://localhost:3456/v1/statements/<statement_id>/trace" -Headers @{ "X-Tenant-ID" = "default" }
```

See [`demo-script.md`](demo-script.md) for a full walkthrough with sample
questions, and `docs/architecture.md` for the layer-by-layer design.

## Tests

```powershell
.\scripts\dev.ps1 verify              # ruff + mypy + full pytest run
pytest                                # full suite (includes eval harness)
pytest tests/eval -o addopts="" -p no:warnings   # hermetic eval gate (CI)
pytest tests/test_database.py         # Testcontainers Postgres (needs Docker)
```

CI (`.github/workflows/ci.yml`) runs lint/typecheck, the full test suite, the
hermetic eval gate, and a Docker image build on every PR.

## Guardrails

The system's non-negotiable safety properties are encoded as `GUARDRAIL_*`
flags in `.env.example`. They **cannot be disabled** — any attempt to turn one
off is rejected by the config schema with a `ValidationError` (G-13):

- **G-01** Evidence-first pipeline (conclusions require evidence links)
- **G-02** Verify-first gate (draft → verified | quarantined)
- **G-03** Budget/cost bounds (per-run budget + circuit breaker)
- **G-04** Unsafe-content filtering
- **G-05** Redaction on every API response
- **G-06** Egress allowlist (default-deny fetch)
- **G-07** One-sidedness flag (`human_review_required`)
- **G-11** Contradiction protocol (flag-first → confirm-second)

## Repo layout

```
app/
  api/         FastAPI routes, schemas, health, metrics, tenant scoping
  core/        Settings (env-driven), metrics instruments, logging
  db/          SQLAlchemy models, enums, append-only base, session
  models/      Domain service models
  pipeline/    10-stage Prefect DAG, checkpoints, context contracts
  services/    Retrieval, collection, verification, contradiction, report, ...
  workers/     Prefect worker entrypoint
static/        Web dashboard (HTML/CSS/JS) — served at /
tests/         pytest suite (unit + eval harness + adversarial guardrails)
sample_data/   Synthetic sources for evaluation (RSS, PDF, HTML, seeds)
grafana/       Provisioned dashboard + datasources (observability profile)
prometheus/    Scrape config for /metrics
scripts/       dev.ps1, generate_licenses.py, generate_sample_data.py
docs/          Design, tech stack, build plan, architecture, data model, licenses
alembic/       Migrations (expand-contract, append-only triggers)
```

## Documentation index

| Doc | What it covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Implemented architecture, layer diagram, endpoint table |
| [`docs/architecture.mmd`](docs/architecture.mmd) | Architecture diagram source (mermaid) |
| [`docs/data-model.md`](docs/data-model.md) | Provenance core: 14 tables, ERD, write governance |
| [`docs/data-model-erd.mmd`](docs/data-model-erd.mmd) | ERD diagram source (mermaid) |
| [`docs/tech-stack.md`](docs/tech-stack.md) | Technology selections + rationale |
| [`docs/build-plan.md`](docs/build-plan.md) | Step-by-step build plan |
| [`docs/enterprise-research-agent-design.md`](docs/enterprise-research-agent-design.md) | Full design doc (v1) |
| [`docs/research-sources.md`](docs/research-sources.md) | Source material for the design |
| [`docs/ai-disclosure.md`](docs/ai-disclosure.md) | AI usage disclosure for this repo |
| [`docs/licenses.md`](docs/licenses.md) | Dependency licenses (generated) |
| [`demo-script.md`](demo-script.md) | End-to-end demo walkthrough |
| [`scale-answer.md`](scale-answer.md) | Scale/limits write-up |

## License

MIT — see `LICENSE` (project metadata in `pyproject.toml`; dependency licenses
in `docs/licenses.md`).
