# Research Sources — Eval Harness & Trust Instrument (Step 14)

Sources referenced by the Step 14 trust instrument (`tests/eval/*`,
`tests/gold/*`, `sample_data/*`, CI eval gate). Each entry records what the
source informed and where it is applied in this repository.

## Architecture / pipeline provenance

| Source | What it informed | Applied in |
|---|---|---|
| **DeepTRACE** — "DeepTRACE: A Deep Learning-based Approach for Traceability Link Recovery" (arXiv:2509.04499) | Link-recovery framing for traceability: statements↔passages↔sources as first-class, recoverable links rather than prose claims. Justifies the `evidence_links` table and the `traceability` metric's ≤1-hop chain definition. | `app/db/models.py` (`EvidenceLink`), `tests/eval/eval_metrics.py` (`traceability`), `app/api/routes.py` (`/v1/runs/{run_id}/trace`) |
| **STORM** — "Assisting in Writing Wikipedia-like Articles From Scratch with Large Language Models" (arXiv:2402.14207) | Multi-stage research pipeline: plan → search/collect → outline → write; stage checkpoints with resumable state. ECRKE's Prefect stage DAG and checkpoint store follow this decomposition. | `app/pipeline/flows.py`, `app/pipeline/checkpoint.py`, `app/pipeline/stages/*` |
| **Microsoft — "Traceability in LLM-based systems" (azure docs: responsible AI traceability)** | Traceability as a guardrail: record model calls, data lineage, and decisions so every output can be reconstructed and audited. Informed `AuditTrace` rows, `audit_writer`, and the trace endpoint. | `app/services/audit_writer.py`, `app/db/models.py` (`AuditTrace`), `app/api/routes.py` |

## Eval metrics / adversarial measurement

| Source | What it informed | Applied in |
|---|---|---|
| **TruthfulQA** — "Measuring How Models Reproduce Misinformation" (arXiv:2109.07958) | Measuring whether model statements are *true* and *informative*, not merely fluent. Grounds the statement-decomposition coverage metric (claim token containment in passages) and the seed-run trust floors. | `tests/eval/eval_metrics.py` (`statement_decomposition_coverage`), `tests/eval/test_retail_seed.py` |
| **FActScore** — "FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation" (arXiv:2305.14251) | Sentence-level atomic decomposition for precision measurement. ECRKE's sentence extractor in the seed harness and the decomposition coverage metric apply the same atomicity idea. | `tests/eval/eval_metrics.py`, `tests/eval/test_retail_seed.py` (`RetailExtractor`) |
| **HaluEval** — "HaluEval: A Large-Scale Hallucination Evaluation Benchmark for Large Language Models" (arXiv:2305.11747) | Hallucination categories (conflicting, unfaithful) and the need for adversarial samples. Informed the gold `contradictions.jsonl` label schema (`confirmed`/`rejected`/`flagged`) and the one-sidedness metric. | `tests/gold/contradictions.jsonl`, `tests/eval/eval_metrics.py` (`one_sidedness`) |
| **RAGAS** — "RAGAS: Automated Evaluation of Retrieval Augmented Generation" (arXiv:2309.15217) | Reference-free automated metrics for RAG (faithfulness, answer relevance, context recall). Informed `claim_covered` (context containment) and `citation_accuracy` (claim-token traceability to cited statements). | `tests/eval/eval_metrics.py` (`claim_covered`, `citation_accuracy`) |

## Local canonical artifacts

- `tests/gold/questions.jsonl` — 5-question seed set (incl. `retail-operations`).
- `tests/gold/contradictions.jsonl` — 62-pair human-labeled contradiction gold set
  (44 confirmed, 10 rejected, 8 flagged) across 5 domains.
- `sample_data/ecrke_seed_report.pdf` — script-generated demo report with
  `/ToUnicode` CMaps (reproducible via `scripts/generate_sample_data.py`).

## Application notes

- Every metric in `tests/eval/eval_metrics.py` is **pure and hermetic** — no
  LLM, DB, or network — so the eval gate is deterministic.
- The contradiction recall harness (`tests/eval/test_contradiction_recall.py`)
  measures the real detector against the gold set with a cooperative judge
  and asserts precision == 1.0 (no gold pair missed, no false positives).
- The seed-run harness (`tests/eval/test_retail_seed.py`) runs the **real**
  `research_pipeline` over fakes and asserts the trust floors
  (decomposition, support ratio, traceability ≥ 0.5).
- CI runs the hermetic eval gate on every push/PR (`.github/workflows/ci.yml`
  `eval-gate` job).
