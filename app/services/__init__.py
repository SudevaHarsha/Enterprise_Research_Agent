"""Domain services for ECRKE.

Current implementation (build-plan Step 4):
- ``llm_gateway``  — tiered LiteLLM calls, schema-validated structured output,
  bounded retry -> quarantine (G-11), repeat-call cache, before-call hooks.
- ``cost_meter``   — accurate, never-negative cost accounting onto
  ``runs.cost_spent_usd`` (G-03), tiktoken fallback estimate.
- ``kv_cache``     — cache-aside store over the ``kv_cache`` table keyed by
  hash(model + prompt + inputs).

Planned: retrieval, extraction, verification, contradiction detection,
planning, report generation.
"""
