# AI Usage Disclosure — ECRKE

> Applies to the **development of this repository** (what AI wrote here) and to
> the **runtime behavior of the product** (what AI does when you run it). The two
> are deliberately separate.

## 1. This repository was developed with AI assistance

This codebase (ECRKE) was built with heavy AI assistance:

- **Who:** an orchestrating AI agent system (multi-agent "Orchestrator +
  Division Leads + Specialists") planned and implemented the features, guided by
  a human operator at each review gate. Individual developers, if any, used
  AI pair-programming tools.
- **What:** code, tests, migrations, CI workflows, docs, and this file were
  drafted by AI and reviewed by humans.
- **Where:** the build plan (`docs/build-plan.md`) records the step-by-step
  task breakdown; the design doc and tech-stack doc record the intent.
- **How we kept it honest:** every implementation step is test-backed
  (365 tests), lint/type-checked (`ruff`, `mypy`), and reviewed before
  acceptance. AI output is never trusted without the verification suite passing.

**Limitation disclosure:** AI-generated code can contain subtle logic errors,
blind spots, or hallucinated "facts" about its own output. We mitigate this with
the eval harness and the same verification gates the product uses — but the
usual code-review responsibilities still apply to any human operator.

## 2. The product itself uses LLMs — and is built to audit them

ECRKE is an LLM-powered research agent. It calls LLM providers (via LiteLLM) to
plan searches, extract statements, judge evidence, and detect contradictions.
This is a **feature, not a bug** — but it is why the provenance core exists:

- **Verify-first gate (G-02):** no LLM-produced statement enters the knowledge
  base as `verified` without passing a deterministic support-matrix check plus a
  second, independent LLM judge pass. Statements that fail are `quarantined`.
- **Evidence links:** every verified statement and every conclusion is linked to
  the stored source passage that supports it (≤ 1 hop trace).
- **Contradiction protocol (G-11):** conflicts are flagged by a cheap detector
  and confirmed by a strong judge before they surface in reports.
- **Human-review flags (G-07):** conclusions that lack opposing evidence are
  marked `human_review_required`.
- **Audit trail:** every knowledge-base write decision is recorded immutably in
  `audit_trace` — you can see what the system believed, when, and why.

### LLM provider usage

- Providers are configured by environment variable NAME only
  (e.g. `LLM_OPENAI_API_KEY`, `LLM_ANTHROPIC_API_KEY`, `LLM_GOOGLE_API_KEY`).
  No key value is stored in this repository.
- Default models are `gemini/gemini-2.0-flash` (cheap tier) and
  `gemini/gemini-2.0-pro` (strong tier); swap via env config.
- Cost per run is metered (`runs.cost_spent_usd`, `cost_budget_usd`) with a
  circuit breaker (G-03).

### What the LLM does NOT do

- It does not run in API request threads (pipeline runs are background jobs).
- It cannot write to the knowledge base without passing verification.
- It cannot disable the guardrails (`GUARDRAIL_*` flags are immutable, G-13).
- It does not decide egress: fetch targets are restricted to the allowlist
  (`ALLOWED_DOMAINS`, G-06).

## 3. Evaluator / auditor disclosure

The evaluation harness (`tests/eval/`) runs LLM judges against gold data to
measure pipeline quality. Gold answers in `tests/gold/questions.jsonl` are
synthetic or human-authored seeds — see `docs/data-model.md` / sample-data
section of `docs/licenses.md` for provenance of `sample_data/`.

## 4. Contact

Questions about AI usage in this repo should be raised with the repository
maintainers (or opened as an issue). This file exists to make the disclosure
explicit — not to hide anything.
