# ECRKE — End-to-End Demo Script

> Purpose: show an evaluator/auditor the full loop — submit a question, watch the
> 10-stage pipeline, audit a conclusion back to its source passage, and read the
> immutable audit trail.
>
> Assumes a running stack. Two options:
>
> **A — Docker stack** (needs Docker daemon): `docker compose up -d` then
> `docker compose --profile observability up -d` (optional). API at
> `http://localhost:3456`.
>
> **B — Local dev**: `docker compose up -d postgres`, `.\scripts\dev.ps1 setup`,
> `.\scripts\dev.ps1 verify`, then start the API
> (`uvicorn app.main:app --port 8000`). Runs execute with the in-process runner
> unless `PREFECT_API_URL` is set.
>
> All commands below use PowerShell + `Invoke-RestMethod`. `$T` is the tenant
> header used for every call.

```powershell
$T = @{ "X-Tenant-ID" = "default" }
$BASE = "http://localhost:3456"
```

---

## Step 0 — Health

```powershell
Invoke-RestMethod -Uri "$BASE/healthz" -Headers $T     # {"status":"ok"}
Invoke-RestMethod -Uri "$BASE/readyz" -Headers $T      # DB connectivity probe
```

## Step 1 — Submit a research question

Use one of the gold-sample questions (synthetic retail corpus ships in
`sample_data/`):

```powershell
$body = @{
  question = "How is AI transforming retail operations?"
  execute  = $true          # true = run the pipeline now
  cost_budget_usd = 2.0     # optional; defaults to RUN_BUDGET_USD
} | ConvertTo-Json

$run = Invoke-RestMethod -Method Post -Uri "$BASE/v1/runs" -Headers $T `
  -ContentType "application/json" -Body $body
$run | Format-List

$RUN = $run.run_id
```

Expected: `201 Created` with `status=submitted`, `progress=0.0`. The pipeline is
now running in the background (Prefect DAG / in-process runner).

## Step 2 — Poll the lifecycle

```powershell
1..120 | ForEach-Object {
  $r = Invoke-RestMethod -Uri "$BASE/v1/runs/$RUN" -Headers $T
  Write-Host ("{0}: status={1} stage={2} progress={3} cost=${4}" -f `
    $_.ToString().PadLeft(3), $r.status, $r.stage, $r.progress, $r.cost_spent_usd)
  if ($r.status -in @("completed","failed","cancelled")) { break }
  Start-Sleep -Seconds 2
}
```

Expected: you see the 10-stage lifecycle surface as
`planning → searching → collecting → storing → extracting → verifying →
comparing → detecting → concluding → tracing`, then `completed` with
`progress=1.0` and a metered `cost_spent_usd`.

## Step 3 — Per-stage checkpoints

```powershell
Invoke-RestMethod -Uri "$BASE/v1/runs/$RUN/stages" -Headers $T | Format-Table stage, ts
```

Expected: one row per durable checkpoint, in pipeline order (`define` first,
`trace` last).

## Step 4 — Conclusions with evidence

```powershell
$conclusions = Invoke-RestMethod -Uri "$BASE/v1/runs/$RUN/conclusions" -Headers $T
$conclusions | Format-Table id, confidence, human_review_required, @{n="evidence";e={$_.evidence.Count}}
```

Expected: each conclusion lists `evidence` refs (statement_id + finding_id).
Take the first `statement_id` from any conclusion's evidence for Step 5.

## Step 5 — Trace a statement to its source (the audit backbone)

```powershell
$sid = $conclusions[0].evidence[0].statement_id
Invoke-RestMethod -Uri "$BASE/v1/statements/$sid/trace" -Headers $T | ConvertTo-Json -Depth 5
```

Expected: `statement → passage → source` in at most one hop per edge — the
source shows `uri`, `title`, and the passage text that supports the statement.
This is the ≤ 1-hop provenance guarantee.

## Step 6 — Contradictions (if the corpus contains conflicting claims)

```powershell
Invoke-RestMethod -Uri "$BASE/v1/runs/$RUN/contradictions" -Headers $T | Format-Table id, status, statement_a_id, statement_b_id
```

Expected: `flagged` rows at minimum (G-11 flag-first). `confirmed` rows appear
when a strong judge agrees.

## Step 7 — Report (rendered markdown, no LLM in the API)

```powershell
$rep = Invoke-RestMethod -Uri "$BASE/v1/runs/$RUN/report" -Headers $T
$rep.markdown | Out-File -Encoding utf8 "$env:TEMP\ecrke_report.md"
Invoke-Item "$env:TEMP\ecrke_report.md"
```

## Step 8 — Immutable audit trail

```powershell
$audit = Invoke-RestMethod -Uri "$BASE/v1/runs/$RUN/audit" -Headers $T
Write-Host "audit rows: $($audit.count)"
$audit.rows | Format-Table ts, entity_type, action, actor, decision, reason
```

Expected: an append-only log of every KB write decision — statement promotions,
evidence links, contradiction decisions, conclusion writes. Try to spot a
`statement` row with `decision=verified` and the verify-first gate reason.

## Step 9 — Reuse: search the verified knowledge base

```powershell
Invoke-RestMethod -Uri "$BASE/v1/kb/search?q=pricing" -Headers $T | Format-Table statement_id, confidence, source_uri
```

Expected: only `verified` statements across the tenant's runs (never drafts or
quarantined).

## Step 10 — Resume semantics (failure recovery story)

```powershell
# A run that paused/failed mid-pipeline can be resumed:
Invoke-RestMethod -Method Post -Uri "$BASE/v1/runs/$RUN/resume" -Headers $T | Format-List status, stage
```

Expected: pipeline re-enters at the first missing stage; already checkpointed
stages are skipped (one checkpoint per run+stage).

---

## Bonus — Observability

With the `observability` compose profile up:

- Grafana: http://localhost:3000 → ECRKE dashboard (run counts, stage
  durations, cost spent, error rates from the 10 `ecrke_` instruments).
- Prometheus: http://localhost:9090 → query `ecrke_runs_total` or
  `ecrke_stage_duration_seconds`.
- Prefect UI: http://localhost:4200 → flow runs, retries, checkpoints.

## Failure-path demos (optional, honest)

| Scenario | What to show | Expected |
|---|---|---|
| Unverified claim rejected | Search KB for a claim that failed verify-first | Not returned (only `verified` rows) |
| Cross-tenant access denied | Repeat Step 1 with a different tenant UUID, then read the run | `403 cross-tenant access denied` (RFC 7807) |
| Bad input | POST `/v1/runs` with empty question | `422` validation error |
| Append-only enforcement | Direct SQL `DELETE FROM evidence_links` (if you have DB access) | Trigger raises `restrict_violation` |
