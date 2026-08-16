# ECRKE — The Scale Answer

> **Question:** "If we give your application 1,000 processes tomorrow instead of
> 100, what happens?"
>
> **Short answer:** nothing breaks — and nothing needs to be rewritten. The
> architecture was designed so that scale-out is *configuration and worker
> count*, not code. Below is the evidence, tied to the actual implementation.

---

## 1. State lives in Postgres, not in process memory

Every piece of durable state is a database row:

| State | Where it lives |
|---|---|
| Runs, lifecycle status, progress | `runs` |
| Sources, passages, statements | `sources`, `passages`, `statements` |
| Checkpoints (resume points) | `checkpoints` (unique per run+stage) |
| Cost meter state | `runs.cost_spent_usd`, `runs.cost_budget_usd` |
| Repeat-call cache (LLM answers, rate counters) | `kv_cache` (replaces Redis) |
| Immutable audit trail | `audit_trace` (append-only) |

Killing any worker loses **nothing**. A new worker picks up queued runs and
resumes any checkpointed run exactly where it stopped (`POST /v1/runs/{id}/resume`
re-enters at the first missing stage). There is no hot state to drain, no
in-memory queue to rebuild, no leader to elect.

## 2. The pipeline is a durable DAG, not a script

The 10-stage pipeline is a **Prefect 3 flow** (`app/pipeline/flows.py`):

- 1,000 concurrent runs are 1,000 **checkpointed jobs**, not 1,000 threads.
- Prefect schedules work with concurrency limits and per-run isolation against
  the same Postgres — no Redis dependency in the whole system.
- Horizontal scale-out = **adding workers** (`prefect worker start --pool research`)
  against the same database. The worker is stateless; it pulls work from the
  queue and writes results to Postgres.
- The API layer stays thin: it enqueues runs and reads rows. It never performs
  LLM calls in request threads, so API instances scale trivially behind a load
  balancer.

## 3. Cost is bounded by construction — a 10× workload cannot be a 10× bill surprise

- Every run carries `cost_budget_usd` (default from `RUN_BUDGET_USD`); the cost
  meter enforces the ceiling at the call level (`app/services/cost_meter.py`).
- The circuit breaker (`CIRCUIT_BREAKER_MAX_STAGE_FAILURES`) stops a run that
  repeatedly fails a stage instead of burning budget on retries.
- Per-stage budgets (G-03) cap spend inside a run.
- Heavy stages are **deterministic code ($0)**: normalization, support-matrix
  scoring, contradiction flagging. Only planning/judging calls hit the LLM
  gateway, and the `kv_cache` dedupes repeat calls (same model+prompt hash).
- The gateway is provider-agnostic (LiteLLM): at 1,000 processes/day the real
  bottleneck is inference budget and provider rate limits, which LiteLLM routes
  across providers/tiers (cheap `gemini/gemini-2.0-flash` vs strong
  `gemini/gemini-2.0-pro`) with retry/backoff.

## 4. Verification keeps the KB trustworthy under load

- The verify-first gate (G-02) + eval harness run **per write** — the trust
  instrument is not a batch job that falls behind at scale.
- Contamination from one bad run is rolled back via a targeted re-verification
  sweep of the affected evidence — never a global rebuild.
- The append-only backbone (`evidence_links`, `audit_trace`) means a 1,000-run
  day produces a *larger* audit corpus, which is exactly what an auditor wants.

## 5. What we would change at 1,000 (and why it's config, not code)

| Concern | Change at 1,000 | Effort |
|---|---|---|
| Postgres HA / backups | Managed Postgres (RDS/Cloud SQL) with PITR | Config + infra |
| Worker scheduling | k8s worker pools instead of compose workers | Infra only |
| Object-store scale-out | `BLOB_STORE_BACKEND=s3` (already implemented) instead of local | Env config |
| Queue/retry tuning | Prefect concurrency limits, rate-limit policies | Config |
| Retrieval recall floor | pgvector hybrid retrieval, NLI judge (v1.5 promotions, pre-planned in `docs/tech-stack.md` §9) | Gated by eval metrics |
| Observability | Prometheus/Grafana already shipped; add alerting rules | Config |

**Nothing in the architecture breaks.** The v1.5 promotions (pgvector, NLI)
were designed in from day one and are gated on measured eval floors
(`docs/tech-stack.md` §9) — we do not adopt them "because scale" until the
eval harness says recall dropped.

## 6. The honest caveats

- **Postgres is the single write path.** At very high concurrency the write path
  (checkpoints + audit rows) is the first measured bottleneck. The design
  already routes around the worst case: `kv_cache` absorbs repeated LLM reads,
  and checkpoint writes are one row per (run, stage). If advisory-lock
  contention or queue throughput becomes a measured bottleneck, the exit is
  documented (`docs/tech-stack.md` §9).
- **The API is stateless and trivially parallel** — it is not the limit.
- **Eval gates are the throttle.** The hermetic eval suite
  (`pytest tests/eval -o addopts="" -p no:warnings`) is wired into CI; a
  workload 10× bigger is only useful if the *same* quality floors hold, so the
  gates run per merge regardless of scale.

---

**Bottom line:** 100 → 1,000 processes is adding workers to the same Postgres,
tuning Prefect concurrency, and — if eval floors demand it — flipping
config-gated v1.5 features on. The knowledge base stays verified, auditable,
and cost-bounded at both numbers.
