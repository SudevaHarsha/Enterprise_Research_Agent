# Enterprise Design Document — Evidence-Centric Research Knowledge Engine (ECRKE)

| Field | Value |
|---|---|
| **Document** | Enterprise AI Research Agent — System Design |
| **Version** | 1.1 (added §15 Guardrails & Safety) |
| **Status** | DRAFT — approved as CONDITIONAL per Research Council verdict (70% confidence) |
| **Date** | 2026-08-15 |
| **Owner** | Orchestrator / Engineering Lead |
| **Source of authority** | Research Council verdict `20260815-enterprise-research-agent-ideology.md` |

---

## 1. Executive Summary

The enterprise must be able to submit a research question and receive — automatically and at
scale — a **traceable, reusable research knowledge base**, not merely a chat-style answer.

This document defines the design for the **Evidence-Centric Research Knowledge Engine (ECRKE)**:

> **The product is not the report. The product is the verified knowledge base that survives the report.**

Every stage of the research pipeline writes first-class, versioned, source-attributed data.
A **verify-first audit layer** — not the language model — decides what may enter the shared
knowledge base. The pipeline is a deterministic DAG skeleton with agentic loops inside
retrieval and verification, executed as checkpointed background jobs with hard cost bounds.

The verdict is CONDITIONAL: we ship a **thin v1** (relational provenance core + report
archive + LLM-judge verification pass), and stage graph/NLI machinery behind measured
usage floors. The evaluator acceptance test is: *give the system a new research question,
observe the pipeline operate, and audit every conclusion back to the source passage that
supports it.*

---

## 2. Problem Statement

### 2.1 The Challenge

Build an AI application capable of conducting **structured enterprise research at scale**.
Given a research topic — e.g., *"How is AI transforming retail operations?"* or *"What AI
technologies are changing manufacturing?"* — the system must:

1. Define research questions
2. Search sources
3. Collect information
4. Store sources
5. Extract findings
6. Compare evidence
7. Classify findings
8. Detect contradictions
9. Generate conclusions
10. Maintain traceability

The system is explicitly **not** "ChatGPT with web search." The backend must maintain a
**reusable research knowledge base**. The evaluator will submit a *new* research question
and observe the research pipeline operating end-to-end.

### 2.2 Why Naive RAG Fails This Test

| Failure mode | Consequence |
|---|---|
| Answers are ephemeral | Nothing reusable is retained; each question starts from zero |
| No statement-level provenance | Cannot audit a conclusion back to the exact supporting passage |
| Unsupported statements propagate | Independent audits (DeepTRACE, ICLR 2026) show 40–80% citation accuracy and large fractions of statements unsupported by cited sources in frontier systems |
| One-sided answers | 55–95% of frontier answers to debate queries were one-sided; no evidence balance is enforced |
| No contradiction handling | Conflicting evidence is ignored or merged, producing false confidence |
| No freshness lifecycle | 91% of model knowledge degrades over time; stale facts become "truth" |

### 2.3 Constraints (from Council Domain Expert)

1. **Auditability is a hard requirement** — statement→source traceability for every conclusion; persisted, immutable traces.
2. **Cost ceilings $0.50–$5.00 per query all-in** — research runs are checkpointed background jobs, never synchronous blocking calls.
3. **Freshness is a pipeline problem** — staleness alerts, source re-validation scheduling, confidence decay.
4. **Write governance into the shared KB** — nothing enters without validation + source attribution + confidence score.
5. **Multi-tenant isolation + RBAC + DLP** — per-tenant namespaces, no cross-tenant leakage.
6. **Ops budget 1–3 engineers** — complexity is finite; every subsystem justifies itself in year one.

---

## 3. Goals, Non-Goals, and Success Criteria

### 3.1 Goals
- **G1. Reusable knowledge base**: evidence persists across research questions; a new question inherits verified evidence from prior runs.
- **G2. Full traceability**: every finding, classification, contradiction record, and conclusion resolves to source passages (statement-level support matrix).
- **G3. Verification-gated writes**: no unsupported statement can enter the shared KB.
- **G4. Observability of the pipeline**: the evaluator can watch each of the 10 stages operate on a live run.
- **G5. Contradiction detection**: conflicting evidence is flagged, confirmed, and recorded — never silently merged.
- **G6. Cost-controlled execution**: per-run budget bounds enforced by circuit breakers; runs are resumable.
- **G7. Freshness management**: stale sources are detected, re-validated, or decayed from day one.

### 3.2 Non-Goals (v1)
- **NG1.** No temporal knowledge graph — provenance is a data model, not a topology.
- **NG2.** No dedicated NLI classifier at v1 — flag-first/confirm-second with an LLM judge (promotion gate at v1.5).
- **NG3.** No real-time streaming research; research runs are batch background jobs.
- **NG4.** No public internet crawl infrastructure; v1 uses targeted, configurable source sets (RSS, URLs, search APIs, uploaded documents) with allowlist controls.
- **NG5.** No auto-publish to production knowledge bases without human review for high-stakes beliefs (A/B promotion path).

### 3.3 Success Criteria (Definition of Done)
1. Evaluator submits a novel research question → pipeline completes with all 10 stages observable.
2. ≥95% of conclusion statements have a persisted, one-hop resolve to a stored source passage (measured by the eval harness).
3. Contradiction records exist for at least all gold-labeled contradiction pairs in the seed eval set.
4. Any statement can be traced: `conclusion → evidence link → source passage → source metadata → fetchable URL/document`.
5. Runs respect cost bounds ($0.50–$5.00) enforced by circuit breakers; partial runs resume from checkpoint.
6. A second research question on an overlapping domain reuses previously verified evidence (provenance-based reuse, measurable in the audit log).

---

## 4. Personas & User Stories

| Persona | Need | Primary stories |
|---|---|---|
| **Evaluator / Auditor** | Verify pipeline operation and traceability | Submit a question; observe stage-by-stage status; audit any conclusion to its source passage |
| **Analyst (knowledge consumer)** | Trustworthy answers with evidence | Ask a question; read conclusions with inline citations and contradiction warnings |
| **Research operator (admin)** | Govern the KB, budgets, sources | Manage source allowlists, tenant quotas, freshness schedules, promotion of high-stakes beliefs |
| **Platform engineer (1–3 ops)** | Operate cheaply, debug failures | Restart failed runs from checkpoint; watch cost telemetry; add a new source connector |
| **Compliance reviewer** | Regulatory audit trails (SOX/GDPR/EU AI Act-class) | Export an immutable audit trace for any statement or run |

---

## 5. Solution Overview — ECRKE

### 5.1 Core Thesis

> The engine's value is a **persistent, provenance-native knowledge base** where each
> pipeline stage writes validated, versioned, source-attributed data. A **verify-first audit
> layer** gates every write. Retrieval is agentic; the skeleton is deterministic.

### 5.2 Five Doctrines
1. **Knowledge engine, not answer generator** — the KB is the product.
2. **Verify-first — the audit layer is the architecture.**
3. **Statement-level provenance is mandatory; graph storage is optional.**
4. **Hybrid pipeline: deterministic DAG skeleton, agentic loops inside.**
5. **Knowledge is governed like code** (validation, versioning, A/B promotion, freshness).

---

## 6. System Architecture

### 6.1 Logical View

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            CLIENT SURFACES                                │
│   REST API (evaluator/analyst/admin)   ·   Admin UI (optional)           │
└───────────────▲───────────────────────────────────────────▲──────────────┘
                │                                             │
┌───────────────┴───────────────────────────────────────────┴──────────────┐
│                        API / CONTROL LAYER                                │
│   FastAPI service: question intake, run lifecycle, artifact retrieval,    │
│   audit-trace queries, tenant & budget policy enforcement                 │
└───────────────▲───────────────────────────────────────────▲──────────────┘
                │                                             │
┌───────────────┴───────────────────────────────────────────┴──────────────┐
│                    ORCHESTRATION / EXECUTION LAYER                        │
│   DAG runner (10 stages) · agentic loops (retrieval/verification)        │
│   Checkpointing · circuit breakers · cost meters · retries               │
└───────────────▲───────────────────────────────────────────▲──────────────┘
                │                                             │
┌───────────────┴───────────────┐   ┌─────────────────────────┴────────────┐
│    RETRIEVAL / COLLECTION     │   │       VERIFICATION / AUDIT           │
│   Source connectors           │   │   LLM judge (flag/confirm)           │
│   Search APIs / RSS / uploads │   │   Support-matrix scorer (deterministic│
│   Crawl worker (allowlisted)  │   │   Evidence-balance (one-sidedness)   │
└───────────────▲───────────────┘   └─────────────────────────▲────────────┘
                │                                             │
┌───────────────┴───────────────────────────────────────────┴──────────────┐
│                         KNOWLEDGE / STORAGE LAYER                         │
│   PostgreSQL: sources · statements · findings · evidence_links ·          │
│   contradictions · conclusions · runs · checkpoints · audit_trace         │
│   + object/blob store for raw documents (S3-compatible)                  │
└──────────────────────────────────────────────────────────────────────────┘
```

### 6.2 Hybrid Execution Model
- **Deterministic DAG skeleton** — the 10 stages, their inputs/outputs, and their ordering are
  declared and reproducible. Stage state is persisted (checkpointed), so a crash resumes the
  same run rather than restarting.
- **Agentic loops inside stages** — Stage 1 (Define) uses multi-perspective questioning
  (STORM-style); Stage 2 (Search/Collect) uses ReAct-style retrieval loops over external memory
  (search API + KB); Stage 7 (Verify) uses an LLM judge with structured output. Each loop runs
  in per-subtask isolation to avoid shared-state races.
- **Background execution** — research runs are jobs with a progress model, cost meter, and
  circuit breakers. They never block a request thread.

### 6.3 Component Responsibilities

| Component | Responsibility |
|---|---|
| **API/Control** | Intake new questions; create/resume/cancel runs; serve artifacts, verdicts, audit traces; enforce tenant + budget policy |
| **DAG Runner** | Executes the 10-stage pipeline; persists stage checkpoints; emits lifecycle events; applies circuit breakers |
| **Retrieval Workers** | Query search APIs / RSS / allowlisted crawl targets; normalize and store raw sources + metadata; dedupe |
| **Extraction Workers** | Split sources into passages; extract atomic statements; attach passage-level provenance |
| **Comparison/Classification Workers** | Cluster/group statements into findings; classify by evidence tier (Tier 1–4), stance, domain taxonomy |
| **Contradiction Detector** | Flag-first (LLM judge over statement pairs) → confirm-second (deterministic/labelled pass) → record |
| **Verification Gate** | Support-matrix scoring: every statement must resolve to a stored passage; unverifiable statements are quarantined, never written to KB |
| **Report Generator** | Synthesizes conclusions from verified findings; every conclusion carries evidence links; renders human/API-readable report |
| **Eval Harness** | Statement decomposition, citation accuracy, support, one-sidedness; runs against human-labeled gold set; gates promotions |
| **Freshness Scheduler** | Staleness alerts, re-validation scheduling, confidence decay |

---

## 7. Data Model — Relational Provenance Core

Provenance is a **data model**, not a database topology. v1 uses a relational core.

### 7.1 Core Tables (conceptual DDL)

```sql
-- Tenants isolate all research data
tenants        (id, name, namespace, rbac_policy, created_at)

-- Research runs (one per submitted question)
runs           (id, tenant_id, question, status, stage, progress,
                cost_budget_usd, cost_spent_usd, checkpoint jsonb,
                created_at, updated_at, completed_at)

-- Sources: anything fetched (web page, PDF, RSS item, uploaded doc)
sources        (id, run_id, uri, title, source_type, fetched_at,
                content_hash, raw_ref, allowlisted_uri bool, status)

-- Passages: atomic retrievable units of a source
passages       (id, source_id, seq, text, start_char, end_char, hash)

-- Statements: atomic claims extracted from passages
statements     (id, passage_id, run_id, text, status [draft|verified|quarantined],
                confidence float, created_at)

-- Evidence links: statement -> passage resolution (the audit backbone)
evidence_links (id, statement_id, passage_id, run_id, score, method,
                created_at)   -- immutable, append-only

-- Findings: grouped/classified statements
findings       (id, run_id, title, evidence_tier, domain_tags, stance,
                summary, confidence, created_at)
finding_statements (finding_id, statement_id)

-- Contradictions: flagged + confirmed conflicts
contradictions (id, run_id, statement_a_id, statement_b_id,
                status [flagged|confirmed|rejected], evidence jsonb,
                created_at, confirmed_at)

-- Conclusions: final output, each linked to evidence
conclusions    (id, run_id, text, confidence, created_at)
conclusion_evidence (conclusion_id, statement_id, finding_id)

-- Audit trace: immutable log of every KB write decision
audit_trace    (id, run_id, entity_type, entity_id, action,
                actor, decision, reason, evidence jsonb, ts)

-- Checkpoints: durable resume points for long runs
checkpoints    (id, run_id, stage, state jsonb, ts)
```

### 7.2 Write Governance Rules
1. **No statement enters the KB unverified.** Draft → support-matrix check → verified (or quarantined).
2. **Versioning**: updates create new rows (versioned); historical versions remain queryable.
3. **A/B promotion**: high-stakes beliefs require human review before promotion to canonical status.
4. **Confidence scores** are mandatory on statements, findings, and conclusions; they decay with time.
5. **Contamination rollback path**: if a shared misconception is detected, all inherited evidence can be re-verified in one sweep (affected runs are re-run through the verification gate only).

---

## 8. Pipeline Stage Design

| # | Stage | Input | Output | Key mechanisms |
|---|---|---|---|---|
| 1 | **Define** | Question | Research plan: sub-questions, hypotheses, taxonomy, source allowlist | STORM-style multi-perspective questioning; plan is a persisted artifact |
| 2 | **Search** | Research plan | Candidate source list | Agentic retrieval loop (search API + KB reuse + RSS); queries recorded |
| 3 | **Collect** | Source list | Raw sources + passages | Fetch workers; normalization; dedupe by content hash; rate-limit aware |
| 4 | **Store** | Raw sources | Sources/passages rows + blobs | Immutable storage; content-addressed |
| 5 | **Extract** | Passages | Atomic statements + evidence links | LLM extraction with structured output; statement↔passage binding |
| 6 | **Compare** | Statements | Findings (grouped) + evidence tiers | Clustering, stance classification, Tier 1–4 evidence assignment |
| 7 | **Verify** | Statements/findings | Verified/quarantined status | Support-matrix scorer (deterministic) + LLM judge confirmation |
| 8 | **Detect** | Verified findings | Contradiction records | Flag-first (LLM judge) / confirm-second (labeled/deterministic pass) |
| 9 | **Conclude** | Verified + contradictions | Conclusions with evidence links | Synthesis constrained to verified statements; one-sidedness balance check |
| 10 | **Trace** | All artifacts | Immutable audit trace + report bundle | Export service; audit_trace rows; per-statement resolve endpoint |

---

## 9. Verification & Audit Layer (Verify-First)

### 9.1 Support Matrix
For each conclusion statement, the system maintains a **support matrix**:
- The statement's constituent atomic claims
- The evidence links to passages
- The source metadata (URL, fetched date, allowlist status)
- A per-link score (`full | partial | none`)

Gating rule: **a conclusion cannot be emitted unless every claim in its support matrix has
at least one `full`-scored evidence link.** Claims with `partial`/`none` are either re-queried
or dropped with a documented reason.

### 9.2 Gate Placement
- **Extraction → KB**: statements without a resolvable passage are quarantined.
- **Findings → KB**: findings whose constituent statements are not verified are rejected.
- **Conclusions → Report**: one-sidedness check — if all evidence for a conclusion comes from
  one stance/source class, the report must surface the counter-evidence (or explicitly mark
  the gap), mirroring the DeepTRACE one-sidedness metric.

### 9.3 Immutability
`evidence_links` and `audit_trace` are append-only. No in-place edits. Deletion requires a
tombstone + new version.

---

## 10. API Surface (Evaluator-Facing)

| Endpoint | Purpose |
|---|---|
| `POST /v1/runs` | Submit a research question → creates a run (returns run_id) |
| `GET /v1/runs/{id}` | Run status, current stage, progress %, cost spent |
| `GET /v1/runs/{id}/stages` | Per-stage status + artifact links (observable pipeline) |
| `GET /v1/runs/{id}/conclusions` | Final conclusions with evidence links |
| `GET /v1/statements/{id}/trace` | Full provenance chain: statement → passage → source |
| `GET /v1/runs/{id}/contradictions` | Contradiction records for the run |
| `GET /v1/runs/{id}/report` | Rendered report (markdown/PDF/JSON) |
| `POST /v1/runs/{id}/resume` | Resume a checkpointed run |
| `GET /v1/kb/search` | Search the reusable KB (across runs, tenant-scoped) |
| `GET /v1/runs/{id}/audit` | Export immutable audit trace |

Evaluator test flow: `POST /v1/runs` with a new question → poll `GET /runs/{id}` while
watching stages progress → retrieve conclusions → call `/statements/{id}/trace` on any
conclusion and observe the chain to a stored source passage.

---

## 11. Cost Control & Background Execution

- **Per-run budget** (`cost_budget_usd`): default within $0.50–$5.00. A **cost meter** tracks
  model + infra spend per stage.
- **Circuit breakers**: on budget breach, stage failure-rate breach, or provider rate-limit
  storm → run pauses, checkpoint saved, alert emitted. Resume restores the meter state.
- **Checkpointing**: every stage writes `checkpoints` rows; resume replays only incomplete stages.
- **Model tiering**: cheap models for extraction/summarization; strong models for judge/verification;
  deterministic code for support scoring where possible (see Tech Stack doc §Model Routing).
- **Progress model**: stage-level progress % and artifact counts are observable to the evaluator.

---

## 12. Freshness Lifecycle

- Every source/statement carries a `fetched_at` / `verified_at`.
- **Staleness policy**: sources beyond TTL are flagged; scheduled **re-validation jobs** re-fetch
  and re-run the verification gate; changed content spawns new versions.
- **Confidence decay**: statement/finding/conclusion confidence decays over time; decayed-below-threshold
  items are marked for re-verification, never silently trusted.
- **Contamination sweep**: a newly discovered contradiction triggers re-verification of all
  runs that inherited the affected evidence (targeted, not full re-runs).

---

## 13. Security, Tenancy & Compliance

- **Multi-tenant isolation**: per-tenant namespaces (schema/row-level RLS), RBAC on sources/findings, no cross-tenant leakage.
- **DLP**: secrets and PII redaction on ingestion (regex + LLM-assisted detection); never log raw credentials.
- **Source allowlist**: unapproved external HTTP is blocked by default; allowlisted domains only (Ironclad Rule: no unapproved external HTTP).
- **Secrets**: environment variables / secret manager only — never in code, logs, or KB.
- **Audit & compliance**: `audit_trace` exports support SOX/GDPR/EU AI Act-class review; data retention and deletion policies per tenant.
- **AI Act-readiness**: high-stakes uses are human-in-the-loop (A/B promotion); system logs document training-data and source lineage.

---

## 14. Observability

- **Tracing**: OpenTelemetry spans per stage/loop/tool call; correlation IDs per run.
- **Metrics**: per-run cost, stage durations, verification pass rates, contradiction counts,
  KB growth, freshness coverage.
- **Logs**: structured JSONL; per-run and per-tenant views.
- **Dashboards**: cost by model tier; pipeline health; eval scores over time (see Eval Harness).

---

## 15. Guardrails & Safety

### 15.1 Why Guardrails Are First-Class

The engine ingests **untrusted web content** and produces **trusted, reusable knowledge**.
Without explicit guardrails, the system's defining failure modes — prompt injection,
hallucination propagation, cost runaway, unsafe content, PII leakage, tool misuse — become
systemic risks that compound across every future research run. Guardrails are therefore
**architecture, not bolt-ons**: they are enforced at the same gates as verification (§9),
are covered by dedicated test cases in the eval harness (§16), and **cannot be disabled by
configuration** (project rules never override security — the product inherits this principle
from the system's own Ironclad Rules, Layer 1).

### 15.2 Guardrail Inventory

| # | Guardrail | Trigger | Enforcement / Action |
|---|---|---|---|
| G-01 | Prompt-injection containment | Collected source content contains instruction-like text ("ignore previous instructions", "system:", tool-call syntax) | Retrieved content is **data, never instructions**. System prompt strictly separated from delimited, labeled `data` blocks; the agent's tool set is allowlisted (search, fetch, extract, KB-write via gate) and never executes retrieved content. Injection attempts are quoted as evidence, never followed. |
| G-02 | Hallucination containment | Any statement lacking a full-scored evidence link (§9) | Verify-first gates at extraction/finding/conclusion: unverifiable statements are quarantined, never written to KB. Eval metrics (support ratio, citation accuracy) run as runtime gates, not post-hoc reports. |
| G-03 | Cost runaway | Run or stage budget threshold breached | Cost meter (LiteLLM) + per-stage budgets; circuit breaker checkpoints and pauses the run; alert emitted; resume restores meter state. No synchronous unbounded LLM calls. |
| G-04 | Unsafe / illicit content | Collected sources or generated outputs contain hate, violence, sexual, or otherwise prohibited content | Provider moderation endpoint + deterministic filters at ingestion and report generation. Unsafe sources are flagged and excluded from KB amplification. |
| G-05 | PII / secret leakage (DLP) | Ingestion or output contains credentials, tokens, personal data | Redaction on ingestion and on output; secret patterns never logged or stored (system Rule 01); audit logs redact sensitive fields. |
| G-06 | Tool / egress sandbox | Any external HTTP fetch outside the source allowlist | Default-deny egress; allowlisted domains only; fetch workers run without credentials; no shell/code execution from research content; per-connector rate limits. |
| G-07 | Human-in-the-loop for high-stakes | High-stakes belief promoted to canonical status; high-impact conclusions | A/B promotion requires explicit human approval; high-stakes conclusions carry a "human review recommended" flag; nothing auto-publishes canonical beliefs. |
| G-08 | Data governance / deletion | Tenant deletion or regulatory erasure request | Tombstone + versioned cascade; targeted re-verification sweep for contaminated evidence; retention policies per tenant. |
| G-09 | Fail-loud, never silent | Any tool failure, partial result, or ambiguous state | Structured error propagation; PARTIAL status with done/remaining; run status never fabricates SUCCESS (system Rules 04/07); circuit-breaker alerting. |
| G-10 | Tenant isolation & quotas | Cross-tenant access attempt or quota breach | Postgres RLS + RBAC at every query path; per-tenant rate and budget quotas; audit exports scoped per tenant. |
| G-11 | Output validation | LLM output fails schema or exceeds bounds | Pydantic-validated structured outputs; schema rejection → bounded retry (max 2, different approach) → quarantine; output length caps per stage. |
| G-12 | Versioned rollback | Any bad write detected (contamination, error, policy) | All KB writes are versioned; rollback to a prior version is always possible; contamination sweep re-verifies affected runs without full re-runs. |
| G-13 | Guardrails are non-negotiable | Attempt to weaken/disable a guardrail via tenant config or task override | Configuration schema rejects it; guardrail changes require design review and eval-suite re-run (mirrors Rule 12). |

### 15.3 Guardrail Enforcement Points

Guardrails attach to **pipeline boundaries**, not to individual model calls:

| Boundary | Guardrails enforced |
|---|---|
| Ingestion (collect/store) | G-01 (containment), G-04, G-05, G-06 |
| Extraction → KB write | G-02, G-05, G-11 |
| Findings / comparison | G-02 |
| Contradiction records | G-02, G-11 |
| Conclusion → report | G-02, G-03, G-04, G-07, G-11 |
| API / control layer | G-09, G-10, G-12 |
| Background execution | G-03, G-09, G-12 |

### 15.4 Guardrail Testing (adversarial eval set)

The eval harness includes a dedicated **adversarial suite** — every guardrail has at least one automated test:

- **Prompt-injection samples** (hidden instructions in fetched pages/PDFs) → must not alter pipeline behavior or enter the KB as instructions (G-01).
- **Secret-leak samples** (fake API keys, tokens inside documents) → must be redacted, never persisted or logged (G-05).
- **Unsafe-content samples** → must be flagged and excluded (G-04).
- **Unsupported-statement samples** → must be quarantined (G-02).
- **Budget-breach simulation** → circuit breaker pauses the run (G-03).
- **Cross-tenant access attempts** → denied (G-10).
- **Schema-violation samples** → bounded retry + quarantine (G-11).

No guardrail may be removed or weakened via tenant configuration (G-13).

### 15.5 Alignment with System Ironclad Rules

| Product guardrail | Mirrors system rule |
|---|---|
| G-05 PII / secrets | Rule 01 — No secrets in output |
| G-02 hallucination containment | Rule 04 — No hallucinated results |
| G-09 fail-loud | Rule 07 — Fail loud, never silent |
| G-12 rollback | Rule 08 — Rollback always planned |
| G-06 egress sandbox | Hard stop — Unapproved external HTTP |
| G-13 non-negotiable | Rule 12 — Project rules never override security |

---

## 16. Eval Harness (Built First — Condition #1)

The DeepTRACE-style harness is the **first artifact**, wired into pipeline gates as a runtime
control (never a post-hoc evaluator):

| Metric | Definition |
|---|---|
| **Statement decomposition** | Ground-truth atomic claims per question; measure coverage |
| **Citation accuracy** | Fraction of citations that actually support the claim they're attached to |
| **Support ratio** | Fraction of conclusion statements with a `full`-scored evidence link |
| **One-sidedness** | Fraction of answers presenting only one stance when balanced evidence exists |
| **Contradiction recall** | Fraction of gold-labeled contradiction pairs detected (flag+confirm) |
| **Traceability** | % of statements resolving to a stored source passage in ≤1 hop |

Seeding: 5 questions including the challenge example ("How is AI transforming retail
operations?") with a 50–100-pair human-labeled contradiction gold set. **Architecture review
at the 5-question mark** before any graph/NLI investment.

---

## 17. Phasing & Roadmap

### v1 (Thin Core — this design)
Relational provenance core · report archive · LLM-judge verification pass · DAG skeleton ·
one agentic retrieval loop · flag-first/confirm-second contradiction handling · background
job shell with cost bounds · eval harness wired into gates.

### v1.5 (Gated Promotions)
- **NLI classifier** promotion if contradiction recall floor is breached on gold set.
- **Hybrid retrieval (pgvector)** when KB crosses document scale threshold.
- **Temporal knowledge graph** only if cross-run temporal queries demonstrably pay for themselves.

### v2 (Enterprise scale)
- Multi-region deployment, k8s, horizontal worker pools.
- Advanced compliance integrations (DLP, eDiscovery export, audit APIs).
- Federated tenant knowledge (opt-in sharing with provenance).

---

## 18. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Unsupported statements enter KB | High | Critical | Verify-first gates + support matrix + eval harness as runtime control |
| Cost overruns on long runs | Medium | High | Circuit breakers, per-stage budget, model tiering, checkpoint resume |
| Contradiction miss poisons KB | Medium | High | Flag-first/confirm-second; contamination sweep; v1.5 NLI gate |
| Freshness neglect | Medium | Medium | Freshness pipeline operational from day one (Condition #6) |
| Over-engineering by team of 1–3 | High | High | Thin v1; every subsystem justifies itself; staging gates |
| Evasion via "fewer, safer statements" | Medium | Medium | Verification co-designed with retrieval coverage; coverage metrics |
| Provider outage / rate limits | Medium | Medium | Multi-provider routing via gateway; retry with backoff; circuit breakers |
| Prompt injection via collected content | High | Critical | Guardrail G-01: data/instruction separation, tool allowlist, injection samples in eval suite |
| Unsafe content enters KB | Medium | High | Guardrail G-04: provider moderation + deterministic filters at ingestion and report generation |

---

## 19. Acceptance Criteria (Evaluator Test)

Given a **new** research question (not in the seed set):

1. `POST /v1/runs` returns a run id; the pipeline proceeds through all 10 stages with observable status.
2. Sources are collected, stored, and deduplicated (sources + passages persisted).
3. Statements are extracted with passage-level provenance; unverifiable ones quarantined.
4. Findings are grouped and classified with evidence tiers; contradictions flagged and confirmed.
5. Conclusions are generated **only** from verified statements, each with evidence links.
6. The audit trace is immutable and exportable; every conclusion resolves to a stored source passage in ≤1 hop.
7. The run completes within its cost budget; a simulated crash mid-run resumes from checkpoint.
8. A second overlapping question demonstrably **reuses** verified evidence from the first run (provenance-based reuse visible in audit log).

---

## 20. Glossary

| Term | Meaning |
|---|---|
| **Statement** | Atomic claim extracted from a passage |
| **Passage** | Atomic retrievable unit of a source |
| **Evidence link** | Immutable binding: statement → passage |
| **Finding** | Grouped/classified set of statements |
| **Contradiction record** | Flagged + confirmed conflict between statements |
| **Support matrix** | Per-claim resolution of a conclusion to source passages |
| **Quarantine** | Holding state for statements that fail verification |
| **A/B promotion** | Human-reviewed elevation of a high-stakes belief to canonical status |
| **Circuit breaker** | Mechanism that pauses a run on budget/error threshold breach |

---

*This design implements the Research Council verdict `20260815-enterprise-research-agent-ideology.md`.
Any deviation from the seven binding conditions requires a Council re-review.*
