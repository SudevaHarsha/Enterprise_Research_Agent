# ECRKE — Live Demo Script (10–15 minutes)

> **Purpose:** A structured walkthrough for evaluators. Each section has a
> **narrative goal** (what the audience should understand), **timing**, and
> **exact commands/UI actions** to execute live.
>
> **Prerequisites:** Docker stack running (`docker compose up -d`), API on
> `http://localhost:3456`, dashboard at `http://localhost:3456/`.
>
> **Key principle:** We never fake anything. Every demo step hits the real API.
> If something fails, we show the honest failure and explain why — that's
> the point of a provenance system.

---

## PART 1 — Opening & Motivation (2 min)

**Goal:** The audience understands *why* ECRKE exists before seeing *how* it works.

### Talking points (1 min)

> "LLM research tools produce confident answers with no chain of custody. You
> get a paragraph that sounds authoritative, but you can't verify it. You can't
> trace a claim back to the source passage that supports it. And if two sources
> contradict each other, the system doesn't notice.
>
> ECRKE inverts that: nothing enters the knowledge base unverified, every
> conclusion is traceable to a stored source passage in one hop, and the audit
> trail of every KB write decision is immutable."

### Architecture overview (1 min)

Show the 5-layer architecture diagram (`docs/architecture.png` or the dashboard):

> "The system has five layers:
> 1. **User Interface** — this dashboard, where you submit questions and see results
> 2. **API Layer** — FastAPI with tenant isolation, RFC 7807 errors, redaction on every response
> 3. **AI Intelligence Layer** — a 10-stage Prefect DAG with LLM judges, agentic retrieval loops, and verification gates
> 4. **Data Layer** — PostgreSQL 16 with 14 tables, append-only evidence links and audit traces
> 5. **External Research** — search APIs, RSS feeds, URL fetchers behind a default-deny egress allowlist"

---

## PART 2 — The Surprise Record Test (4 min)

**Goal:** Prove this is a real application, not prepared demo content. Enter a
*new* question live that we haven't rehearsed.

### Action (2 min)

1. Open `http://localhost:3456/` in the browser.
2. Type a **fresh research question** that was not pre-loaded:
   - Suggestion: *"What are the emerging risks of autonomous AI agents in enterprise settings?"*
   - Or let an evaluator choose a question on the spot.
3. Click **Run Pipeline** (or `POST /v1/runs` via curl).

> "I'm entering a question we haven't prepared for. Watch the pipeline
> execute in real time."

### Observe (2 min)

The dashboard shows:
- Status: `submitted` → `running`
- Stage 01 (Define) turns active — the LLM plans a search strategy
- Stage 02 (Search) begins — the system decides what to search for
- Progress bar advances

> "The system is deciding what to search for, how to verify what it finds,
> and what contradictions to look for — all autonomously."

**If the pipeline completes (needs LLM keys):** Move to Part 3.
**If it fails at the first LLM stage (no keys):** That's fine — show it honestly:

> "We don't have API keys in this environment, so the pipeline fails at the
> first LLM call. That's actually the correct behavior — the system doesn't
> fabricate results. Let me show you what a completed run looks like using our
> pre-seeded data."

Then load a pre-seeded run from the DB or show the sample data path.

---

## PART 3 — Provenance & Audit (3 min)

**Goal:** Demonstrate the core value prop: every conclusion traces to a source.

### Conclusions with evidence (1 min)

Open the dashboard's **Conclusions** section, or:

```powershell
$T = @{ "X-Tenant-ID" = "default" }
Invoke-RestMethod -Uri "http://localhost:3456/v1/runs/{run_id}/conclusions" -Headers $T
```

> "Each conclusion carries evidence links. These aren't decorative — they're
> foreign keys to stored source passages."

### Trace to source (1 min)

Click "show evidence chain" on a conclusion, or:

```powershell
Invoke-RestMethod -Uri "http://localhost:3456/v1/statements/{statement_id}/trace" -Headers $T
```

> "Here's the provenance chain: statement → passage → source. One hop per
> edge. You can verify this claim by reading the original document yourself."

### Immutable audit trail (1 min)

Open the dashboard's **Audit Trail** section, or:

```powershell
Invoke-RestMethod -Uri "http://localhost:3456/v1/runs/{run_id}/audit" -Headers $T
```

> "Every knowledge-base write decision is recorded immutably. The
> `evidence_links` and `audit_trace` tables are append-only at both the ORM
> and database level — you can't delete or modify an audit row, even with
> direct SQL access. The Postgres triggers enforce this."

---

## PART 4 — Guardrails & Scale (2 min)

**Goal:** Show the system can't be tricked, and it scales.

### Guardrails demo (1 min)

Try to disable a guardrail:

```powershell
# Try to turn off evidence-first (G-01) — this should fail
Invoke-RestMethod -Method Post -Uri "http://localhost:3456/v1/runs" `
  -Headers $T -ContentType "application/json" `
  -Body '{"question":"test","execute":false}'
```

> "The guardrails are immutable at runtime (G-13). The config schema rejects
> any attempt to disable them. Evidence-first, verify-first, budget bounds,
> egress allowlist — they're all hard-wired."

Show the cross-tenant isolation:

```powershell
# Try to read a run with a different tenant
Invoke-RestMethod -Uri "http://localhost:3456/v1/runs/{run_id}" `
  -Headers @{ "X-Tenant-ID = "attacker" }
```

> "403 — cross-tenant access denied. Every endpoint enforces tenant isolation."

### Scale answer (1 min)

> "If we give your application 1,000 processes tomorrow instead of 100, what
> happens?
>
> Nothing breaks. State lives in Postgres, not in process memory. The
> pipeline is a durable Prefect DAG with checkpoints. Workers are stateless —
> they pull work from the queue and write results to Postgres. Scaling out
> means adding workers, not rewriting code. Cost is bounded by construction —
> every run has a budget ceiling and a circuit breaker. See `scale-answer.md`
> for the full analysis."

---

## PART 5 — Closing (1 min)

### What we built

> "ECRKE is:
> - A 10-stage research pipeline with LLM-powered intelligence
> - A provenance core where every conclusion traces to its source in one hop
> - An immutable audit trail of every KB write decision
> - Tenant-isolated, cost-bounded, guardrailed by construction
> - Built with open-source tools: PostgreSQL, Python, FastAPI, Prefect, LiteLLM
> - Tested with 370+ automated tests including adversarial guardrail tests"

### AI disclosure

> "This codebase was built with AI assistance — an orchestrating AI agent
> system planned and implemented the features, guided by a human operator at
> each review gate. Every implementation step is test-backed and reviewed
> before acceptance. See `docs/ai-disclosure.md` for the full disclosure."

### Final line

> "The product is the verified, reusable knowledge base — not the report.
> Every conclusion is traceable. Every decision is auditable. That's ECRKE."

---

## Appendix — Quick Reference Commands

```powershell
$T = @{ "X-Tenant-ID" = "default" }
$BASE = "http://localhost:3456"

# Health
Invoke-RestMethod "$BASE/healthz" -Headers $T
Invoke-RestMethod "$BASE/readyz" -Headers $T

# Submit
$body = '{"question":"YOUR QUESTION","execute":true,"cost_budget_usd":2.0}'
$run = Invoke-RestMethod -Method Post "$BASE/v1/runs" -Headers $T -ContentType "application/json" -Body $body
$R = $run.run_id

# Poll
Invoke-RestMethod "$BASE/v1/runs/$R" -Headers $T

# Checkpoints
Invoke-RestMethod "$BASE/v1/runs/$R/stages" -Headers $T

# Conclusions
Invoke-RestMethod "$BASE/v1/runs/$R/conclusions" -Headers $T

# Trace
Invoke-RestMethod "$BASE/v1/statements/{id}/trace" -Headers $T

# Contradictions
Invoke-RestMethod "$BASE/v1/runs/$R/contradictions" -Headers $T

# Report
Invoke-RestMethod "$BASE/v1/runs/$R/report" -Headers $T

# Audit trail
Invoke-RestMethod "$BASE/v1/runs/$R/audit" -Headers $T

# Knowledge base search
Invoke-RestMethod "$BASE/v1/kb/search?q=keyword" -Headers $T

# Resume a failed run
Invoke-RestMethod -Method Post "$BASE/v1/runs/$R/resume" -Headers $T
```

## Appendix — If Things Go Wrong

| Issue | Recovery |
|---|---|
| Pipeline hangs | `POST /v1/runs/{id}/resume` — checkpoint lets it pick up where it stopped |
| LLM key missing | System fails honestly at the first LLM stage with a clear error; shows the error in audit trail |
| Container crash | Worker is stateless; restart it and queued runs resume from their last checkpoint |
| Cross-tenant access | Returns RFC 7807 `403` with redacted detail — no information leakage |
