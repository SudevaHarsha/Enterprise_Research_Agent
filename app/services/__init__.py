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

Planned: retrieval, extraction, verification, contradiction detection,
planning, report generation.
"""
