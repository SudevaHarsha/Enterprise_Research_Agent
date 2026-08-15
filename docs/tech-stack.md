# Tech Stack Document — Evidence-Centric Research Knowledge Engine (ECRKE)

| Field | Value |
|---|---|
| **Document** | Enterprise AI Research Agent — Technology Stack |
| **Version** | 1.1 (added §4.13 Guardrails → Enforcement Mechanics) |
| **Status** | DRAFT (aligns with Design Doc v1.0; v1 scope only) |
| **Date** | 2026-08-15 |
| **Owner** | Orchestrator / Platform Lead |
| **Companion** | `docs/enterprise-research-agent-design.md` |

---

## 1. Purpose

This document selects the concrete technology stack for **v1 of the Evidence-Centric
Research Knowledge Engine** and records the reasoning. Every choice is judged against the
Council's binding constraints:

1. Ops budget of **1–3 engineers** — complexity is finite.
2. **$0.50–$5.00 all-in per research run**.
3. **Statement-level provenance + immutable audit** — relational core, no graph in v1.
4. **Verify-first** — eval harness and verification gate are runtime controls.
5. **Resumable, cost-bounded background jobs** — never synchronous LLM calls in request threads.

**Guiding principle: choose boring, proven technology; reserve novelty for the research
pipeline itself. Every subsystem must justify itself in year one.**

---

## 2. Stack at a Glance

```
┌────────────────────────────────────────────────────────────────┐
│  LANGUAGE          Python 3.12 (single runtime for all code)    │
├────────────────────────────────────────────────────────────────┤
│  API / CONTROL     FastAPI + Pydantic v2 + Uvicorn              │
├────────────────────────────────────────────────────────────────┤
│  ORCHESTRATION     Prefect 3 (DAG runner, checkpoints, retries) │
│                    LangGraph (agentic loops inside stages)      │
├────────────────────────────────────────────────────────────────┤
│  STORAGE           PostgreSQL 16 (provenance core)              │
│                    SQLAlchemy 2 + Alembic (migrations)          │
│                    S3-compatible blob store (raw documents)     │
│                    Postgres-backed queue/cache/locks (no Redis) │
├────────────────────────────────────────────────────────────────┤
│  LLM ACCESS        LiteLLM gateway (multi-provider routing)     │
├────────────────────────────────────────────────────────────────┤
│  RETRIEVAL         Postgres FTS + BM25 (v1) → pgvector (v1.5)   │
├────────────────────────────────────────────────────────────────┤
│  VERIFICATION      Deterministic support-matrix scorer (code)   │
│                    LLM judge (structured JSON output)           │
├────────────────────────────────────────────────────────────────┤
│  EVAL HARNESS      pytest + custom metric suite + gold dataset  │
├────────────────────────────────────────────────────────────────┤
│  OBSERVABILITY     OpenTelemetry + Prometheus + Grafana         │
├────────────────────────────────────────────────────────────────┤
│  DEPLOYMENT        Docker Compose (v1) → k8s (v2)               │
│                    GitHub Actions CI/CD                         │
└────────────────────────────────────────────────────────────────┘
```

---

## 3. Decision Drivers

| Driver | Implication |
|---|---|
| 1–3 engineer ops | Minimal moving parts; managed services preferred over self-hosted when cost allows; no microservices in v1 |
| $0.50–$5.00/run | Model tiering; deterministic code replaces LLM calls wherever possible; aggressive caching |
| Auditability | PostgreSQL relational core; append-only audit tables; SQL transactions give atomic writes |
| Resumable jobs | Durable workflow engine with checkpointing, not ad-hoc thread pools |
| Multi-tenant | Single Postgres instance + schema/RLS isolation (not one DB per tenant) |
| Future growth | pgvector extension ready; SQLAlchemy allows later scale-out; provider-agnostic LLM gateway |

---

## 4. Layer-by-Layer Decisions

### 4.1 Language & Runtime — **Python 3.12**

**Chosen:** Python 3.12 (single runtime).
**Why:**
- Entire AI/research ecosystem (LiteLLM, LangGraph, LlamaIndex, pydantic, pgvector) is Python-first.
- One language across API, workers, eval harness → one team of 1–3 can own it all.
- Fast iteration speed matters more than raw throughput at this scale.

**Rejected:** Node/TypeScript (strong for web, weak AI tooling), Go (great runtime, slower AI iteration), polyglot (fatal for a 3-person team).

### 4.2 API / Control Layer — **FastAPI + Pydantic v2**

**Chosen:** FastAPI with Pydantic v2 schemas, Uvicorn (Gunicorn in prod).
**Why:**
- Async by default; natural fit for run-intake + polling endpoints.
- Pydantic v2 doubles as the **structured-output contract** for LLM extraction and judge outputs (validation before persistence).
- OpenAPI generation is free → evaluator can explore the API immediately.

**Rejected:** Django/DRF (heavier, sync-centric), Flask (no built-in validation, slower).

### 4.3 Orchestration — **Prefect 3 (DAG runner) + LangGraph (agentic loops)**

**Chosen:** Prefect 3 for the pipeline DAG skeleton; LangGraph for agentic loops inside stages.
**Why Prefect:**
- Durable workflow engine with **built-in checkpointing/retries** — crash-safe resume without building a state machine.
- Declarative task DAGs map 1:1 to the 10-stage pipeline; per-task retries, timeouts, and caching.
- Native **concurrency limits and rate-limit policies**; observability UI included (or self-hosted).
- Local-first: runs on plain Python with a Postgres backend — no extra services needed beyond what we already run.
- Cost-safe: works with any Python function; we can enforce budget in task decorators.

**Why LangGraph:**
- Provides **durable, checkpointable ReAct loops** with tool-call state (retrieval loop, verification loop) — the "agentic loops inside the skeleton."
- Graph checkpoints persist to the same Postgres → resume mid-loop.
- Runs **inside** Prefect tasks (per-subtask isolation), not instead of the DAG.

**Rejected for v1:** Temporal (excellent but heavier: separate server, gRPC, more ops than 3 engineers need), Celery (weak workflow semantics; no durable DAG/checkpoints), Dagster (asset-centric; more opinionated than needed), Airflow (scheduler-centric, poor fit for interactive run lifecycle).

### 4.4 Storage — **PostgreSQL 16 + SQLAlchemy 2 + Alembic + S3-compatible object store**

**Chosen:** PostgreSQL 16 as the single source of truth.
**Why:**
- One database serves: provenance tables, findings, contradictions, conclusions, checkpoints, audit trace, **and** full-text search + JSONB + (later) pgvector.
- ACID transactions guarantee atomic KB writes (governance rule: no partial writes).
- Row-Level Security (RLS) gives multi-tenant isolation without schema sprawl.
- `pgvector` extension (v1.5) upgrades retrieval without migrating databases.
- Managed Postgres (RDS/Neon/Supabase) removes the largest ops burden.

**Why SQLAlchemy 2 + Alembic:**
- Type-safe ORM with explicit SQL; Alembic migrations are reviewable and versioned — knowledge is governed like code, so schema is governed like code too.
- Keeps us off bespoke JSON storage for the provenance core.

**Why S3-compatible object store (raw documents):**
- Raw PDFs/HTML/audio blobs are large and immutable → object store, not Postgres.
- Content-addressed keys (`sha256`) enable dedupe + re-fetch verification.

**Rejected for v1:** knowledge-graph store (Neo4j) — provenance is a data model, not a topology (Council condition #2); separate vector DB (Qdrant/Weaviate) — pgvector suffices at v1 scale.

### 4.5 LLM Access — **LiteLLM gateway**

**Chosen:** LiteLLM (Python SDK, OpenAI-compatible).
**Why:**
- **Provider-agnostic routing** — one codebase for OpenAI, Anthropic, Google, local (Ollama) models → resilience to outages/rate limits and cost optimization via model tiering.
- Consistent `messages`/tool-call interface; built-in retries, timeouts, cost tracking per call (feeds the cost meter).
- No vendor lock-in for the KB — provider choice becomes config, not code.

**Model tiering (maps to cost driver):**

| Task | Tier | Model class (example) | Notes |
|---|---|---|---|
| Extraction, tagging, summarization | Cheap | Fast small model (e.g., GPT-4o-mini class / local) | High volume, low stakes |
| Retrieval query expansion, chunking | Cheap | Fast small model | Token heavy |
| LLM judge (verification, contradiction flag) | Strong | Frontier model | Lower volume, high stakes; structured output |
| Synthesis / conclusion drafting | Strong | Frontier model | One pass per run |

### 4.6 Retrieval — **Postgres FTS + BM25 (v1) → pgvector hybrid (v1.5)**

**Chosen:** Postgres built-in full-text search with tsvector/tsquery for v1; keep an embeddings
column ready for `pgvector` promotion.
**Why:**
- Zero extra infrastructure; deterministic and debuggable for the eval harness.
- Hybrid retrieval (BM25 + embeddings) promoted at v1.5 **only when** the KB crosses the
  document-scale threshold where keyword recall measurably drops — gated by eval metrics
  (Council condition: staged promotions behind measured floors).

**Rejected for v1:** dedicated vector DB, external search engine (Elasticsearch) — extra ops,
not yet justified.

### 4.7 Verification — **Deterministic support-matrix scorer + LLM judge with structured output**

**Chosen:** two-part gate.
- **Deterministic scorer (code):** statement→passage alignment via overlap/lexical scoring +
  optionally embeddings later; produces the support matrix (`full | partial | none`) — cheap,
  reproducible, no LLM call.
- **LLM judge (LLM):** confirmation pass with **Pydantic-validated structured output**
  (JSON schema enforced before persistence), for citation accuracy and one-sidedness checks.
  The LLM judge is the Tier-1-validated instrument (r=0.72, DeepTRACE) — not a bespoke NLI model in v1.

**Why not a dedicated NLI classifier at v1:** no Tier-1 enterprise evidence; pairwise O(n²) cost;
domain-fragile. **Promotion gate:** if contradiction recall on the 50–100-pair human-labeled gold
set breaches the floor at v1.5, promote a fine-tuned NLI cross-encoder. (Council: Unresolved Dispute #1.)

### 4.8 Eval Harness — **pytest + custom metric suite**

**Chosen:** pytest as the runner; metrics computed by first-class Python modules:
- statement decomposition (coverage vs. gold claims)
- citation accuracy, support ratio, one-sidedness
- contradiction recall/precision (against the 50–100-pair gold set)
- traceability (≤1-hop statement→source resolution)

**Why:**
- The eval harness is a **runtime control** wired into pipeline gates, so it must be code, not a notebook.
- pytest integrates with CI (GitHub Actions) → every merge runs the metric suite; promotions
  (NLI, vector search) require passing floors.

### 4.9 Queue / Cache / Locks — **PostgreSQL (Redis-free v1)**

**Chosen:** no Redis in v1. Task queuing uses Prefect 3's Postgres-backed work queues;
result caching uses the `kv_cache` table (key = hash(model + prompt + inputs)); distributed
locks use `pg_advisory_lock`; Prefect metadata lives in Postgres.
**Why:** removes an entire runtime from the stack and from the hosting budget — one managed
Postgres serves the provenance core, the queue, and the cache. Redis remains a v2 option if
advisory-lock contention or queue throughput becomes a measured bottleneck.

### 4.10 Observability — **OpenTelemetry + Prometheus + Grafana (or managed)**

**Chosen:** OpenTelemetry instrumentation (traces per run/stage/tool-call) → Prometheus metrics
→ Grafana dashboards (cost by model tier, stage duration, verification pass rates, KB growth).
**Why:**
- The evaluator must *observe the pipeline operating*; per-run traces are the audit layer's
  operational twin.
- Structured JSONL logs to stdout (collected by Docker/k8s logging), correlation IDs per run.

**Rejected for v1:** full APM suites (New Relic/Datadog) — cost overkill for 3 engineers; keep
self-hosted/OSS until scale demands otherwise.

### 4.11 Security — **Secret manager (env/cloud), no secrets in code**

- Secrets: environment variables / cloud secret manager; referenced by name only (Ironclad Rule 01).
- HTTP egress: allowlist enforced in source connectors; unapproved external HTTP blocked by default.
- Input validation: Pydantic everywhere; redaction utilities for PII/DLP on ingestion.
- Multi-tenant: Postgres RLS; per-tenant RBAC; audit exports scoped per tenant.
- AI-generated content: no execution of retrieved content (no prompt-injection → code path);
  retrieved documents treated as untrusted data.

### 4.12 Deployment — **Docker Compose (v1) → k8s (v2)**

**Chosen:** Docker Compose for v1: `api` (FastAPI), `worker` (Prefect worker), `scheduler`,
`postgres`, `prefect-server`, `grafana/prometheus`. One command to run locally and in a single VM.
**Why:** 1–3 engineers; a k8s cluster is unjustified ops load at v1 scale. CI/CD: GitHub Actions
(lint, type-check, pytest + eval suite, build & push images).

**Promotion path to v2:** containerize as-is → deploy to k8s with horizontal worker pools;
DB moves to managed Postgres; object store to managed S3.

### 4.13 Guardrails → Enforcement Mechanics (maps to Design Doc §15)

| Guardrail (Design §15.2) | Enforcement mechanism in the stack |
|---|---|
| G-01 Prompt-injection containment | Prompt templates strictly separate system instructions from delimited `data` blocks; tool allowlist enforced in LangGraph node definitions; retrieved content never passes through a tool executor |
| G-02 Hallucination containment | Deterministic support-matrix scorer (§4.7) + LLM judge; quarantine state in Postgres; eval metrics wired into Prefect gates |
| G-03 Cost runaway | LiteLLM cost tracking + per-stage budget decorators on Prefect tasks; Postgres-backed cost meter/circuit breaker; checkpoint/resume |
| G-04 Unsafe / illicit content | Provider moderation API via LiteLLM (`litellm.moderation`) + deterministic filter rules; flagged-source exclusion in the collector |
| G-05 PII / secret leakage (DLP) | Redaction pipeline (regex + LLM-assisted) at ingestion and report generation; structured logs never include raw values (RULE 01) |
| G-06 Tool / egress sandbox | Allowlist config (env) consumed by fetch workers; `httpx` client with per-connector rate limits; workers run credential-less |
| G-07 Human-in-the-loop | A/B promotion workflow in Prefect with approval task; `conclusions.human_review_required` flag surfaced via API |
| G-08 Data governance / deletion | Soft-delete tombstones + Alembic versioned schema; targeted re-verification sweep as a Prefect flow |
| G-09 Fail-loud | Prefect retry/error handling → structured Result Message; run status is derived from DB state, never fabricated |
| G-10 Tenant isolation & quotas | Postgres RLS + FastAPI dependency per-tenant scoping; per-tenant budget rows; rate-limit counters in `kv_cache` |
| G-11 Output validation | Pydantic v2 models for every LLM structured output; bounded retry (max 2) then quarantine |
| G-12 Versioned rollback | Append-only evidence/audit tables; versioned statement/finding/conclusion rows |
| G-13 Non-negotiable | Config schema (pydantic-settings) rejects guardrail-disabling values; CI fails on attempted removal |

The adversarial eval suite (Design §15.4) runs inside the same pytest eval harness so every
guardrail is continuously tested in CI.

---

## 5. Full Dependency List (v1)

| Package | Purpose |
|---|---|
| `fastapi`, `uvicorn[standard]`, `gunicorn` | API server |
| `pydantic>=2`, `pydantic-settings` | Contracts, config |
| `sqlalchemy>=2`, `alembic` | ORM + migrations |
| `asyncpg` | Postgres driver |
| `prefect>=3` | DAG orchestration, checkpoints (Postgres-backed queue) |
| `langgraph`, `langchain-core` | Agentic loops |
| `litellm` | LLM gateway + cost tracking |
| `tiktoken` (or litellm cost) | Token/cost metering |
| `httpx` | Source fetching |
| `beautifulsoup4`, `lxml`, `markdownify` | HTML/doc normalization |
| `pypdf`, `docx`, `striprtf` | Document parsing (PDF/DOCX/RTF) |
| `orjson` | Fast JSON (structured LLM output) |
| `nltk` or `spacy` (optional) | Text segmentation fallback (chunking; LLM preferred for extraction) |
| `opentelemetry-*`, `prometheus-client` | Tracing/metrics |
| `presidio-analyzer` (or regex + LLM redaction) | PII redaction / DLP (G-05) |
| `tenacity` | Retry/backoff for connectors and LLM calls (G-03/G-09) |
| `pytest`, `pytest-asyncio`, `testcontainers[postgres]` | Tests + eval harness |
| `ruff`, `mypy` | Lint/type gates |

---

## 6. Data Flow with the Stack (one run)

```
POST /v1/runs {question}
   │  FastAPI validates (Pydantic) → creates run row + checkpoints
   ▼
Prefect flow: research_pipeline(run_id)
   ├─ stage_define:      LangGraph ReAct loop w/ multi-perspective questions
   │                     → research_plan (Postgres)
   ├─ stage_search:      agentic retrieval loop (search API + KB reuse) → source candidates
   ├─ stage_collect:     httpx fetchers (allowlist, rate limits) → raw blobs (S3)
   ├─ stage_store:       normalize → sources + passages (Postgres), content-hash dedupe
   ├─ stage_extract:     LLM extraction (cheap tier) → statements + evidence links
   ├─ stage_compare:     grouping/clustering + evidence-tier classification → findings
   ├─ stage_verify:      deterministic support-matrix scorer + LLM judge (strong tier)
   │                     → verified / quarantined
   ├─ stage_detect:      flag-first (LLM judge) → confirm-second → contradictions
   ├─ stage_conclude:    synthesis (strong tier, constrained to verified) → conclusions
   └─ stage_trace:       audit_trace export + report bundle → evaluator endpoints
```

Every step writes through SQLAlchemy inside a Prefect task with retries/checkpoints; the cost
meter increments via LiteLLM call metadata; a circuit breaker pauses the run if budget/stage
failure thresholds are hit. `GET /v1/runs/{id}` polls the same DB → the evaluator watches the
pipeline operate live.

---

## 7. Cost Control Mechanics (per run)

1. **LiteLLM cost tracking** per call → run-level meter.
2. **Stage budgets** (fractions of the $0.50–$5.00 ceiling) enforced in Prefect task decorators.
3. **Model tiering** (§4.5) keeps the volume-heavy stages on cheap models.
4. **Result caching** (`kv_cache` table): identical prompt+input hash → cached answer, no re-pay.
5. **Deterministic code first**: support scoring, dedupe, chunking run at $0 LLM cost.
6. **Circuit breakers**: budget breach → checkpoint + pause + alert; resume restores meter.
7. **Eval harness gates** prevent "run-away retrieval" regressions by measuring stage costs in CI.

---

## 8. Local Development Environment

```
docker compose up -d postgres prefect-server       # infra
prefect server start                              # local orchestration backend (SQLite for dev)
uvicorn api.main:app --reload                     # API
prefect worker start -p research                   # worker pool
pytest tests/ -m "not slow"                        # unit + eval smoke
```

- SQLite fallback for Postgres in pure-unit tests (testcontainers for integration).
- `.env` with provider API keys referenced by name (never committed).
- Seeded gold dataset (`tests/gold/*.jsonl`) versioned in repo for the eval suite.

---

## 9. Promotion Gates (v1 → v1.5) — measured, never assumed

| Capability | Promotion condition | Trigger metric |
|---|---|---|
| Hybrid retrieval (pgvector) | KB scale threshold or recall floor breach | `kb_retrieval_recall < floor` on eval set |
| NLI contradiction classifier | Contradiction recall floor breach on gold set | `contradiction_recall < floor` (50–100 pairs) |
| Temporal knowledge graph | Demonstrated cross-run temporal query demand | usage + query latency evidence |
| Microservices / k8s | Team > 5 or sustained concurrency demand | ops load metrics |

Every promotion requires: passing eval floors in CI + Architecture Review (5-question mark) +
Council re-review per the design doc.

---

## 10. Decision Records Summary

| # | Decision | Rationale (one line) |
|---|---|---|
| 1 | Python 3.12 everywhere | AI ecosystem + one language for 3 engineers |
| 2 | FastAPI + Pydantic v2 | Async, typed, OpenAPI, structured-LLM-output validation |
| 3 | Prefect 3 + LangGraph | Durable DAG checkpoints + agentic loops, local-first, low ops |
| 4 | PostgreSQL 16 + SQLAlchemy + Alembic | Single ACID provenance core; RLS tenancy; pgvector-ready |
| 5 | S3-compatible blob store | Immutable raw docs, content-addressed, cheap |
| 6 | LiteLLM gateway | Provider-agnostic, cost tracking, model tiering |
| 7 | Postgres FTS v1 → pgvector v1.5 | Zero-extra-infra retrieval; staged promotion |
| 8 | Deterministic scorer + LLM judge | Verify-first gate; Tier-1-validated instrument; no NLI at v1 |
| 9 | pytest-based eval harness | Runtime control, CI-wired, gate of record |
| 10 | Docker Compose v1 → k8s v2 | 1–3 engineer ops; scale later with evidence |
| 11 | OTel + Prometheus + Grafana | Per-run traceability = operational audit twin |

---

## 11. What We Deliberately Did NOT Choose (and why)

| Rejected | Reason |
|---|---|
| Temporal | Excellent but adds a server + gRPC + ops load beyond a 3-person team for v1 |
| Celery | No durable DAG/checkpoint semantics; resume is DIY |
| Airflow / Dagster | Scheduler/asset-centric; overkill for run-centric research jobs |
| Neo4j / knowledge graph | Provenance is a data model; relational satisfies audit in v1 (Council) |
| Qdrant/Weaviate | pgvector covers v1 scale; avoid extra services |
| Elasticsearch | FTS in Postgres is enough until measured demand |
| Fine-tuned NLI at v1 | No Tier-1 enterprise evidence; LLM judge validated; promotion-gated |
| Microservices | Fatal ops complexity for 3 engineers; modular monolith only |
| Node/Go/TypeScript backend | AI ecosystem and iteration speed favor Python for this workload |

---

*This stack implements Design Doc v1.0 and the Council's seven binding conditions. Any change
to a "Chosen" technology requires a decision record update and Council re-review.*
