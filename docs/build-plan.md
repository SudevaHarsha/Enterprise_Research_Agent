# Build Plan — Evidence-Centric Research Knowledge Engine (ECRKE) v1

| Field | Value |
|---|---|
| **Goal** | Working app passing the evaluator test + full submission pack |
| **Plan ID** | `prompt_001` (machine-readable: `.agents/plans/prompt_001.plan.json`) |
| **Version** | 1.0 — DRAFT for approval |
| **Owner** | Orchestrator / Engineering Lead |
| **Inputs** | `docs/enterprise-research-agent-design.md` (v1.1) · `docs/tech-stack.md` (v1.1) · Council verdict `20260815-enterprise-research-agent-ideology.md` |
| **Estimate** | 15 steps · 5 phases · ~5–8 focused sessions · 1–3 engineers |

---

## 1. Objective

Build the **v1 vertical slice** of the Evidence-Centric Research Knowledge Engine so that:

1. An evaluator submits a **new research question** → a **10-stage pipeline operates observably**.
2. Every conclusion is **traceable** to a stored source passage (statement-level provenance).
3. The knowledge base is **reusable** — a second overlapping question inherits verified evidence.
4. Runs respect **cost bounds**, are **checkpointed/resumable**, and are protected by **guardrails**.
5. The **submission pack** covers all 9 required artifacts, including the scale answer for
   "1,000 processes instead of 100."

**The product is not the report — it is the verified knowledge base that survives the report.**

---

## 2. Submission Checklist → Build Coverage

| # | Submission requirement | Where it's produced |
|---|---|---|
| 1 | Source code repository | Step 1 (scaffold) + every step |
| 2 | README / setup instructions | Step 15 |
| 3 | Architecture diagram | Step 15 (`docs/architecture.md`, mermaid + rendered) |
| 4 | Database / data model | Step 2 (`docs/data-model.md`) |
| 5 | Model & library inventory with licenses | Step 15 (`docs/licenses.md`) |
| 6 | Sample / synthetic data | Step 14 (`sample_data/`) |
| 7 | Research sources | Step 14 (`research-sources.md`, from Council evidence manifest) |
| 8 | Working application | Steps 1–13 |
| 9 | 10–15 min live demonstration | Step 15 (`demo-script.md`) |
| 10 | AI-tool disclosure | Step 15 (`docs/ai-disclosure.md`) |
| 11 | **Scale answer (100 → 1,000 processes)** | Step 15 (`scale-answer.md`) — designed in Step 11 (checkpointed jobs, DB-backed state, cost circuit breakers) |

---

## 3. Architecture Reference (what we're building)

```
Client/evaluator → FastAPI → Prefect 3 DAG (10 stages)
                                  ├─ Retrieval/Collect (agentic loop, allowlist egress)
                                  ├─ Extract → Verify (support matrix + LLM judge) → Quarantine
                                  ├─ Detect contradictions (flag-first/confirm-second)
                                  └─ Conclude (verified-only, evidence links)
Storage: PostgreSQL (provenance core + kv_cache) · S3-compatible blobs (raw docs)
Controls: cost meter + circuit breakers · audit_trace (append-only) · guardrails G-01..G-13
LLM: LiteLLM gateway (cheap extract tier / strong judge tier)
Eval: pytest metric suite wired into CI and pipeline gates
```

Full detail: `docs/enterprise-research-agent-design.md` §5–§15 · `docs/tech-stack.md`.

---

## 4. Dependency Graph

```
        ┌── task_002 (data model) ──┐
task_001 ──┤                        ├── task_004 (LLM gateway) ──┬── task_005 (retrieval) ── task_006 (extract) ── task_007 (verify) ──┬── task_008 (contradictions) ──┐
        └── task_003 (config/otel)─┘                            │                                              │                        └── task_010 (report) ─────┘
                                                               ├── task_009 (planning) ────────────────────────┘
                                                               │
                                                               └── task_007 ── task_010 ── task_011 (pipeline)
task_002 ─────────────────────────────────────────────────────────────────────┴── task_011
task_011 ──┬── task_012 (API) ──┐
           ├── task_013 (workers)─┤── task_015 (submission pack)
           └── task_014 (eval) ───┘
```

**Parallel lanes:**
- `task_002 ∥ task_003` (after 001)
- `task_005 → 006 → 007 → 008` chain ∥ `task_009`
- `task_012 ∥ task_013 ∥ task_014` (after 011)
- `task_015` last (assembles everything)

---

## 5. The 15 Steps

Each step is cold-start executable: any agent can pick it up with just this section.

---

### Phase 0 — Foundation

#### Step 1 · Repo scaffold & project baseline — `task_001` (LOW, no brief)
**Objective:** repository skeleton + tooling so every later step lands cleanly.

- **Context:** empty `C:\Modus` workspace (no git yet). Stack per `docs/tech-stack.md`: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, Prefect 3, LangGraph, LiteLLM, Postgres 16, Docker Compose.
- **Deliverables:**
  - `git init` + `.gitignore` (`.env`, `__pycache__`, `*.db`, `.venv/`, blobs)
  - `pyproject.toml` (deps from tech-stack §5; `ruff`, `mypy`, `pytest` dev deps)
  - Package layout: `app/{api,core,db,pipeline,services,workers,models}`, `tests/`, `sample_data/`, `docs/`
  - `docker-compose.yml`: `postgres` (16, with `pgvector` ready), `api`, `worker` (Prefect), `scheduler`; `.env.example` (all keys by **name only** — Ironclad Rule 01)
  - `github/workflows/ci.yml`: lint (ruff), type (mypy), tests (pytest), build images
  - `Makefile` or `scripts/dev.ps1` convenience wrappers
- **Verify:** `docker compose config` passes; `python -c "import app"` works in venv; CI green on first push.
- **Exit criteria:** repo pushes; scaffold conventions frozen; branch protection off (solo) but PRs used.

#### Step 2 · Provenance data model + migrations — `task_002` (MED, brief required)
**Objective:** the relational provenance core — the audit backbone.

- **Context:** schema per design doc §7.1 (tables: `tenants, runs, sources, passages, statements, evidence_links, findings, finding_statements, contradictions, conclusions, conclusion_evidence, audit_trace, checkpoints, kv_cache`). SQLAlchemy 2 models, Alembic migration. `evidence_links` + `audit_trace` are **append-only** (no UPDATE/DELETE paths). Write-governance: statements have `status [draft|verified|quarantined]`; versioning via new rows.
- **Deliverables:** `app/db/models.py`, Alembic env + initial migration, `docs/data-model.md` (ERD text + mermaid), seed script for `tenants`.
- **Verify:** `alembic upgrade head` on fresh Postgres; FK/index checks; append-only enforced in code review; `pytest` model tests.
- **Exit criteria:** migration applies cleanly; every FK + index present; audit model documented.

#### Step 3 · Config, logging, observability scaffolding — `task_003` (LOW, no brief)
**Objective:** config that **cannot** disable guardrails, structured logs, health.

- **Context:** pydantic-settings `Settings` (env-driven, secrets by name); JSONL structured logging with `run_id`/`correlation_id`; OpenTelemetry scaffolding (traces stubbed, ready for Step 13); `/healthz` + `/readyz`; reject config that disables a guardrail (G-13).
- **Deliverables:** `app/core/config.py`, `app/core/logging.py`, `app/core/telemetry.py`, `app/api/health.py`.
- **Verify:** config loads from `.env`; logs are valid JSONL with correlation ids; `GUARDRAIL_*` disable attempt → validation error.
- **Exit criteria:** log/config/health wired; used by all later steps.

---

### Phase 1 — Core Services

#### Step 4 · LLM gateway + cost meter — `task_004` (MED, brief required)
**Objective:** the only door to LLMs — tiered, validated, metered, cached.

- **Context:** LiteLLM wrapper; **tiers**: `cheap` (extraction/planning) vs `strong` (judge/synthesis); structured output via Pydantic models (schema-enforced, bounded retry max 2 → quarantine on failure); `kv_cache` table for repeat calls (key = hash(model+prompt+inputs)); cost meter per call → `runs.cost_spent_usd`; integration point for circuit breaker (Step 11).
- **Deliverables:** `app/services/llm_gateway.py`, `app/services/cost_meter.py`, `app/services/kv_cache.py`, unit tests.
- **Verify:** mocked provider: validated outputs or errors; cache hit skips call; cost increments; schema-violation → retry → quarantine.
- **Exit criteria:** all later LLM use goes through this gateway; never direct provider calls.

#### Step 5 · Source retrieval & collection — `task_005` (MED, brief required)
**Objective:** bring sources in safely.

- **Context:** connectors: **search API** (provider-agnostic), **RSS**, **direct URL**; **egress allowlist** (default-deny, `ALLOWED_DOMAINS` env); `httpx` fetch with per-connector rate limits; normalization (HTML/PDF/DOCX/RTF → text via bs4/pypdf/docx/striprtf); **content-hash dedupe** (`sha256`); raw blobs → S3-compatible store (R2-compatible endpoint config, content-addressed); write `sources` + `passages` rows with metadata (uri, fetched_at, allowlisted, hash). Guardrails G-06 (egress), G-04 (unsafe filter hook), G-05 (PII/secret redaction hook).
- **Deliverables:** `app/services/collectors/{search,rss,url}.py`, `fetcher.py`, `normalizer.py`, `allowlist.py`, blob client.
- **Verify:** non-allowlisted domain fetch refused; same URL twice → one source row; sample PDF → normalized text passages.
- **Exit criteria:** collection works for seed domain (e.g., retail/retail-technology sites); dedupe verified.

#### Step 6 · Statement extraction — `task_006` (MED, brief required)
**Objective:** atomic claims with passage-level provenance.

- **Context:** split passages into atomic statements via cheap-tier LLM with Pydantic structured output; every statement bound to `passage_id` via `evidence_links` at write time; status `draft`; schema violations → bounded retry → quarantine; redaction applied (G-05).
- **Deliverables:** `app/services/extractor.py`, `statement_schema.py`, tests.
- **Verify:** no statement without `passage_id`; sample passage → ≥1 statement + evidence link; status never `verified` here.
- **Exit criteria:** extraction chain (5→6) demonstrable on seed source.

#### Step 7 · Verification & audit layer — `task_007` (HIGH, brief required) ⭐
**Objective:** the verify-first gate — nothing enters the KB unsupported.

- **Context:** **deterministic support-matrix scorer** (statement↔passage alignment → `full|partial|none`; code, $0) + **LLM judge confirmation** (strong tier) with structured verdict; `draft → verified | quarantined`; every verdict appended to `audit_trace` with reason; support-ratio metric emitted (also `support matrix` per conclusion later in Step 10).
- **Deliverables:** `app/services/support_matrix.py`, `app/services/verifier.py`, `app/services/audit_writer.py`, tests.
- **Verify:** unsupported statement → quarantined; supported → verified; audit_trace has immutable verdict rows.
- **Exit criteria:** eval metric "support ratio" observable; quarantine path proven.

#### Step 8 · Contradiction detection — `task_008` (MED, brief required)
**Objective:** flag-first / confirm-second.

- **Context:** among **verified** statements, strong-tier judge flags candidate contradiction pairs; deterministic confirm pass (overlap/negation heuristics + judge second opinion) promotes `flagged → confirmed | rejected`; `contradictions` rows only on confirmed; record evidence used; hook to measure recall against gold set (Step 14).
- **Deliverables:** `app/services/contradiction_detector.py`, tests.
- **Verify:** synthetic contradictory pair → confirmed; non-contradictory → rejected/flagged; no direct write before confirm.
- **Exit criteria:** contradiction lifecycle demonstrable on seed data.

#### Step 9 · Research planning — `task_009` (LOW, no brief)
**Objective:** STORM-style multi-perspective decomposition.

- **Context:** given topic → `research_plan` artifact: sub-questions (≥3 perspectives), hypotheses, taxonomy hints, source-domain hints; persisted with `run_id`; cheap-tier LLM + deterministic template.
- **Deliverables:** `app/services/planner.py`, tests.
- **Verify:** seed topic "How is AI transforming retail operations?" → ≥3 sub-questions; plan persisted.
- **Exit criteria:** planning feeds Steps 5 and 10.

#### Step 10 · Report generator — `task_010` (MED, brief required)
**Objective:** conclusions only from verified evidence.

- **Context:** synthesize conclusions from **verified** statements + confirmed contradictions; every conclusion → `conclusion_evidence` links; **one-sidedness check** (evidence-balance flag when all evidence is single-stance); `human_review_required` for high-stakes; render markdown + JSON; support matrix per conclusion surfaced.
- **Deliverables:** `app/services/report_generator.py`, renderers, tests.
- **Verify:** conclusion without evidence link impossible; one-sidedness flag fires on asymmetric sample.
- **Exit criteria:** report generation demonstrable on verified KB slice.

---

### Phase 2 — Orchestration & API

#### Step 11 · DAG pipeline runner (Prefect 3) — `task_011` (HIGH, brief required) ⭐
**Objective:** the reproducible, resumable, cost-bounded spine — the scale answer.

- **Context:** Prefect flow `research_pipeline(run_id)` with a task per stage; **checkpoints** after every stage (`checkpoints` table → crash-safe resume); **per-stage cost budgets** (fractions of run budget, default $0.50–$5.00) + **circuit breakers** (pause + alert on breach); **per-subtask isolation** (no shared mutable state across agentic loops); **LangGraph agentic loops** inside retrieval (Step 5) and verification (Step 7) with checkpointed state; progress model (stage %, artifact counts).
- **Deliverables:** `app/pipeline/flows.py`, `checkpoint.py`, `circuit_breaker.py`, `app/pipeline/stages/*`.
- **Verify:** seed run executes all 10 stages end-to-end; simulated kill mid-run → resume completes; budget-breach → pause; progress observable via DB.
- **Exit criteria:** **this is the evaluator's core observation** — full pipeline operable on retail-operations seed.

#### Step 12 · FastAPI application — `task_012` (MED, brief required)
**Objective:** the evaluator's door.

- **Context:** endpoints per design §10: `POST /v1/runs`, `GET /v1/runs/{id}`, `/runs/{id}/stages`, `/conclusions`, `/statements/{id}/trace`, `/contradictions`, `/report`, `/resume`, `/kb/search`, `/audit`; tenant scoping + RLS hooks; Pydantic validation; OpenAPI; health.
- **Deliverables:** `app/api/{routes,schemas}.py`, `app/main.py`, integration tests.
- **Verify:** OpenAPI renders; trace endpoint resolves statement→passage→source ≤1 hop; cross-tenant attempt → 403.
- **Exit criteria:** evaluator flow (submit → poll → conclusions → trace) works over HTTP against a live run.

#### Step 13 · Background execution & observability — `task_013` (MED, brief required)
**Objective:** runs as jobs, not requests; telemetry live.

- **Context:** Prefect worker deployment config (Postgres-backed queue — no Redis needed); resume mechanics exposed; OTel metrics: per-run cost, stage durations, verification pass rate, contradiction counts, KB growth; Prometheus scrape + Grafana dashboard JSON; structured run lifecycle events.
- **Deliverables:** worker config, `app/core/metrics.py`, `grafana/dashboard.json`, deployment compose profiles.
- **Verify:** worker consumes a run end-to-end; metrics appear; dashboard renders.
- **Exit criteria:** observability matches design §14; long-run resume proven under worker process.

---

### Phase 3 — Eval & Data

#### Step 14 · Eval harness + gold set + adversarial suite + data — `task_014` (MED, brief required)
**Objective:** the trust instrument — built **first** in spirit, wired as runtime gate.

- **Context:** pytest metric suite: **statement decomposition**, **citation accuracy**, **support ratio**, **one-sidedness**, **contradiction recall/precision**, **traceability** (≤1 hop); **5-question seed set** (incl. retail-operations) + **50–100-pair human-labeled contradiction gold set** (`tests/gold/*.jsonl`); **adversarial guardrail suite** — one test per guardrail G-01..G-13 (injection samples, secret-leak, unsafe content, unsupported statements, budget-breach, cross-tenant, schema violations); `sample_data/` (sample sources, synthetic passages, demo seeds); `research-sources.md` (from Council evidence manifest: DeepTRACE arXiv:2509.04499, STORM arXiv:2402.14207, Microsoft traceability docs, etc.).
- **Deliverables:** `tests/eval/*`, `tests/gold/*`, `sample_data/*`, `docs/research-sources.md`, CI wiring.
- **Verify:** `pytest tests/eval` green; every guardrail test exists and runs; retail seed question passes end-to-end with metrics.
- **Exit criteria:** eval suite is the **gate of record** — merge blocked on failures.

---

### Phase 4 — Deliverables

#### Step 15 · Submission pack — `task_015` (LOW, no brief)
**Objective:** everything the judge reads/holds.

- **Deliverables:**
  - `README.md` — what/why/architecture/quickstart/setup/API/demo walkthrough
  - `docs/architecture.md` — layered diagram (mermaid + rendered PNG) per design §6
  - `docs/data-model.md` — from Step 2, polished with ERD
  - `docs/licenses.md` — model & library inventory with licenses (Python deps via `pip-licenses`, LLM provider terms, icons/fonts)
  - `docs/ai-disclosure.md` — honest statement: AI tools used (this agent system, coding assistants), what the human designed/implemented (ideology, architecture, data model, pipeline semantics, guardrails, eval criteria, scale strategy), what AI generated/assembled
  - `demo-script.md` — 10–15 min live demo narrative: setup (2 min) → submit new question (2 min) → watch stages (3 min) → trace a conclusion (3 min) → second overlapping question shows KB reuse (2 min) → guardrail/cost/demo close (2 min)
  - `scale-answer.md` — **the 100→1,000 processes answer** (below)
- **Verify:** fresh clone → `docker compose up` → seed question runs; demo script rehearsed end-to-end; all 9 requirements mapped in README table.
- **Exit criteria:** repository is self-contained and demo-ready; scale answer crisp.

---

## 6. The Scale Answer (Step 15 target — draft)

> **"If we give your application 1,000 processes tomorrow instead of 100, what happens?"**

- **State lives in Postgres, not in process memory.** Runs, statements, checkpoints, cost meters, and audit trails are DB rows. Killing a worker loses nothing; a new worker picks up queued runs and resumes any checkpointed run exactly where it stopped.
- **The pipeline is a durable DAG (Prefect 3), not a script.** 1,000 concurrent runs are 1,000 *checkpointed jobs* — the system doesn't spawn 1,000 threads, it schedules work with concurrency limits and per-run isolation. Horizontal scale-out = adding workers (Step 13) against the same Postgres.
- **Cost is bounded by construction.** Per-run and per-stage budgets + circuit breakers mean a 10× workload cannot produce a 10× bill surprise; the cost meter enforces ceilings at the call level.
- **The LLM gateway is provider-agnostic.** At 1,000 processes/day the bottleneck is inference budget and rate limits — LiteLLM routes across providers/tiers, and heavy stages are deterministic code ($0).
- **Verification keeps the KB trustworthy under load.** The verify-first gate + eval harness run per write; contamination from one bad run is rolled back via targeted re-verification sweep, not a global rebuild.
- **What we would change at 1,000:** managed Postgres (HA/backups), k8s worker pools, object-store scale-out, queue/retry tuning, and the v1.5 promotions (pgvector, NLI) if eval floors demand — all pre-planned in `docs/tech-stack.md` §9. Nothing in the architecture breaks; it scales by configuration and worker count.

---

## 7. Quality Gates & Working Agreement

| Gate | When | Requirement |
|---|---|---|
| **TDD** | Every code step | Write failing test → implement → refactor; ≥ meaningful coverage on core (verify, pipeline, API) |
| **/review (6-axis)** | End of each step | Score ≥ 8.0/10 (correctness, security, performance, style, tests, docs) |
| **Guardrails** | Every merge | G-01..G-13 enforced; adversarial suite green (Step 14) |
| **Eval suite** | Every merge after Step 14 | `pytest tests/eval` green; support ratio + contradiction recall above floor |
| **Cost check** | Every merge | No unbounded LLM call; all calls through gateway; budgets respected in tests |
| **CI** | Every push | ruff, mypy, pytest, build |

---

## 8. Session Plan (suggested)

| Session | Steps | Outcome checkpoint |
|---|---|---|
| S1 | 1, 2, 3 | Repo + DB + config green; foundation frozen |
| S2 | 4, 5, 6 | Gateway + collect + extract working on seed source |
| S3 | 7, 8, 9, 10 | Verify/contradict/plan/report services unit-tested |
| S4 | 11 | **Milestone:** full pipeline runs retail-operations end-to-end |
| S5 | 12, 13 | API + workers + observability; evaluator HTTP flow works |
| S6 | 14 | Eval suite + gold set + adversarial + sample data |
| S7 | 15 | Submission pack complete; demo rehearsed |

---

## 9. Risks & Mitigations (build-specific)

| Risk | Mitigation |
|---|---|
| Scope creep beyond v1 thin core | Steps frozen to design §16 v1; graph/NLI/vector strictly gated |
| LLM provider rate limits during eval | Gateway retries/backoff; free-tier friendly config; gold set size kept ≤ 100 pairs |
| Resume/checkpoint bugs | Step 11 includes crash-simulation test as acceptance |
| Guardrail evasion by LLM | Adversarial suite in CI; G-13 config lock |
| Time overrun on demo polish | Demo script drafted early (Step 15 early drafts) — polish at end |

---

## 10. Definition of Done (evaluator test)

1. Fresh clone → `docker compose up` → working app.
2. `POST /v1/runs` with a **new** question → 10 stages observable.
3. `GET /statements/{id}/trace` on any conclusion resolves ≤1 hop to stored source passage.
4. Contradiction records exist where gold set expects them.
5. Second overlapping question reuses verified evidence (visible in audit log).
6. Simulated crash mid-run → resume completes; budget breach → pause.
7. Eval suite + adversarial suite green in CI.
8. All 9 submission artifacts present and coherent.
9. Scale answer delivered and defensible under questioning.

---

*Plan implements design v1.1 + Council verdict. Deviation from any step requires plan-mutation protocol and Orchestrator approval.*
