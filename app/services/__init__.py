"""Domain services for ECRKE.

Current implementation:
- ``llm_gateway``  — tiered LiteLLM calls, schema-validated structured output,
  bounded retry -> quarantine (G-11), repeat-call cache, before-call hooks.
- ``cost_meter``   — accurate, never-negative cost accounting onto
  ``runs.cost_spent_usd`` (G-03), tiktoken fallback estimate.
- ``kv_cache``     — cache-aside store over the ``kv_cache`` table keyed by
  hash(model + prompt + inputs).
- ``allowlist``    — G-06 default-deny egress gate (ALLOWED_DOMAINS).
- ``fetcher``      — allowlist-gated, per-connector rate-limited httpx fetch.
- ``normalizer``   — HTML/PDF/DOCX/RTF -> text, paragraph-aware chunking,
  content hashing, G-04 unsafe filter, G-05 secret redaction.
- ``blob_store``   — content-addressed raw-source store (local default; S3/R2
  optional behind the ``[s3]`` extra).
- ``collectors``   — search (provider-agnostic), RSS/Atom, and direct-URL
  connectors that write ``sources`` + ``passages`` rows.
- ``statement_schema`` — Pydantic boundary contract for LLM statement output
  (text bounds + confidence range mirrored from the ``statements`` table).
- ``extractor``   — passage -> draft statements + evidence links via the cheap
  tier (G-01 data/instruction separation, G-05 redaction, G-11 rollback).
- ``plan_schema`` — Pydantic boundary contract for the research-plan LLM
  output (>=3 non-empty bounded sub-questions; bounded hypothesis and hint
  lists; descriptions surfaced into model_json_schema for G-01 prompting).
- ``planner``     — STORM-style multi-perspective planning: cheap tier +
  deterministic prompt template -> persisted ``research_plan:{run_id}``
  artifact with a 30-day TTL (G-01 data/instruction separation, G-05
  redaction, G-11 quarantine without partial persist).
- ``support_matrix`` — deterministic, $0 lexical support scorer mapping
  statement<->passage token overlap to ``EvidenceScore`` full|partial|none
  with a numeric ratio in [0, 1] (verify-first gate stage 1).
- ``audit_writer`` — insert-only ``audit_trace`` writer that participates in
  the caller's transaction (no commit) or owns one (G-05 redaction, bounded
  validation, append-only governance).
- ``verifier``     — verify-first gate: deterministic support matrix first,
  then strong-tier LLM judge confirmation (structured ``VerificationVerdict``)
  -> draft -> verified|quarantined, appending a NEW append-only evidence link
  (method='verify', never touching the extractor's link) plus an audit verdict
  row in ONE atomic transaction; idempotent unless force=True; emits a
  support-ratio metric (span + log).
- ``contradiction_detector`` — flag-first / confirm-second detection among
  verified statements: deterministic $0 candidate pruning (content-token
  Jaccard threshold + negation markers, G-03), strong-tier flag judge, then a
  deterministic negation signal (shared-core, one-sided negation) or a second
  strong-tier confirm judge -> confirmed|rejected; ``contradictions`` rows
  (status='confirmed', confirmed_at, evidence) + atomic ``audit_trace`` verdict
  rows written ONLY on confirmed, never flagged-only/rejected; idempotent
  skip of already-confirmed pairs; ``detect`` returns confirmed rows for the
  Step 14 gold-set recall hook; emits a contradiction.detect span + metrics log
  line (G-01 prompt separation, G-05 redaction, G-11 quarantine).
- ``report_renderer`` — pure report contract (``Report`` / ``ReportConclusion``
  with support matrix + evidence statements) and deterministic markdown/JSON
  renderers; no DB or LLM coupling.
- ``report_generator`` — report stage: synthesizes conclusions from verified
  statements + confirmed contradictions ONLY via the strong tier (G-01
  delimited data blocks, G-05 redaction, ``use_cache=False``, G-11
  quarantine -> no rows), applies deterministic one-sidedness (source-domain
  diversity) and high-stakes human-review checks, and atomically persists
  conclusions + conclusion_evidence links (every conclusion cites >=1
  statement) + audit verdict rows (action='conclude') in ONE transaction;
  emits a report.generate span + metrics log line.
"""
