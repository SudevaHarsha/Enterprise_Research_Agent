"""Unit tests for the pure eval metrics (build-plan Step 14).

Every expected value below was computed by hand against the documented
definitions — the metrics are deterministic and free of LLM/DB/network.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests.eval.eval_metrics import (
    RECALL_FLOOR,
    SUPPORT_FLOOR,
    citation_accuracy,
    claim_covered,
    content_tokens,
    contradiction_precision,
    contradiction_recall,
    one_sidedness,
    statement_decomposition_coverage,
    support_ratio,
    traceability,
)


def test_content_tokens_casefolds_and_filters_short_tokens() -> None:
    assert content_tokens("Retailers report stronger same-store sales growth!") == {
        "retailers",
        "report",
        "stronger",
        "same",
        "store",
        "sales",
        "growth",
    }
    assert content_tokens("AI and IoT cut costs") == {"costs"}


def test_claim_covered_is_true_when_all_claim_tokens_present() -> None:
    claim = "Retailers report stronger sales growth"
    passage = "Retailers report stronger sales growth in the latest quarter."
    assert claim_covered(claim, [passage])


def test_claim_covered_is_false_when_no_claim_tokens_present() -> None:
    claim = "AI demand forecasting lowers retail costs"
    passage = "Retailers report stronger sales growth in the latest quarter."
    assert not claim_covered(claim, [passage])


def test_claim_covered_respects_threshold() -> None:
    claim = "Retailers report stronger sales growth"
    passage = "Retailers report stronger sales in the quarter."  # 4 of 5 tokens
    assert claim_covered(claim, [passage])  # 4/5 = 0.8 >= 0.6
    assert not claim_covered(claim, [passage], threshold=0.9)  # 0.8 < 0.9


def test_claim_covered_vacuously_true_for_tokenless_claim() -> None:
    assert claim_covered("AI and IoT", [])  # no content tokens to verify


def test_statement_decomposition_coverage_measures_fraction_covered() -> None:
    passages = [
        "Retailers report stronger sales growth.",
        "E-commerce expands its share of retail spending.",
    ]
    statements = [
        SimpleNamespace(text="Retailers report stronger sales growth."),
        SimpleNamespace(text="E-commerce expands its share of retail spending."),
        SimpleNamespace(text="AI demand forecasting lowers retail inventory costs."),
    ]
    # 2 of 3 claims are fully covered by the passages -> 2/3.
    assert statement_decomposition_coverage(statements, passages) == 2 / 3
    assert statement_decomposition_coverage([], passages) == 1.0


def test_citation_accuracy_full_and_partial() -> None:
    conclusion = (
        "Retailers report stronger sales growth as e-commerce expands its share of retail spending."
    )
    statements = [
        "Retailers report stronger sales growth in the latest quarter.",
        "E-commerce expands its share of total retail spending.",
    ]
    # Every >=5-char conclusion token appears in a cited statement.
    assert citation_accuracy(conclusion, statements) == 1.0

    partial = "Retailers report stronger sales growth in e-commerce markets."
    # 'markets' appears in neither cited statement -> 6/7 tokens traceable.
    assert citation_accuracy(partial, statements) == 6 / 7
    assert citation_accuracy("AI and IoT", statements) == 1.0  # tokenless conclusion


def test_support_ratio_counts_full_score_links() -> None:
    matrix = [
        SimpleNamespace(support_score="full"),
        SimpleNamespace(support_score="partial"),
        SimpleNamespace(support_score="full"),
    ]
    assert support_ratio(matrix) == 2 / 3
    assert support_ratio([]) == 0.0


def test_one_sidedness_keys_on_distinct_non_empty_domains() -> None:
    assert one_sidedness(["retail.example.com", "retail.example.com"])
    assert one_sidedness(["", ""])
    assert one_sidedness(["", "retail.example.com"])  # one real domain
    assert not one_sidedness(["retail.example.com", "retailtech.example.com"])


def test_contradiction_recall_and_precision_are_order_and_case_insensitive() -> None:
    gold = [("statement-a", "statement-b"), ("statement-c", "statement-d")]
    confirmed = [("statement-b", "statement-a")]  # reversed order still matches
    assert contradiction_recall(confirmed, gold) == 0.5
    assert contradiction_precision(confirmed, gold) == 1.0

    confirmed_two = [
        ("STATEMENT-A", "STATEMENT-B"),  # case-insensitive match to gold pair 1
        ("statement-c", "statement-d"),  # match to gold pair 2
        ("unrelated-x", "unrelated-y"),  # no gold match
    ]
    assert contradiction_recall(confirmed_two, gold) == 1.0
    assert contradiction_precision(confirmed_two, gold) == 2 / 3

    # Case-insensitive pairing.
    assert contradiction_recall([("STATEMENT-A", "statement-b")], gold) == 0.5
    assert contradiction_recall([], gold) == 0.0
    assert contradiction_precision([], gold) == 0.0


def test_traceability_counts_only_fully_resolved_chains() -> None:
    chains = {
        "s1": ("passage-1", "source-1"),
        "s2": ("passage-2", None),
        "s3": (None, None),
    }
    assert traceability(chains) == 1 / 3
    assert traceability({}) == 0.0


def test_quality_floor_constants_are_at_least_half() -> None:
    assert RECALL_FLOOR >= 0.5
    assert SUPPORT_FLOOR >= 0.5


def test_gold_sets_load_and_are_schema_valid() -> None:
    """The gold ground-truth files exist, parse, and satisfy schema/count floors."""
    import json
    from pathlib import Path

    gold_dir = Path(__file__).resolve().parent.parent / "gold"

    questions = [
        json.loads(line) for line in (gold_dir / "questions.jsonl").read_text().splitlines()
    ]
    assert len(questions) == 5
    assert any("retail" in q["question"].lower() for q in questions)

    contradictions = [
        json.loads(line)
        for line in (gold_dir / "contradictions.jsonl").read_text().splitlines()
    ]
    assert len(contradictions) >= 60
    confirmed = [c for c in contradictions if c["label"] == "confirmed"]
    assert len(confirmed) >= 40
    for c in contradictions:
        assert {"id", "statement_a", "statement_b", "label", "domain", "rationale"} <= set(c)

    citations = [
        json.loads(line) for line in (gold_dir / "citations.jsonl").read_text().splitlines()
    ]
    assert len(citations) >= 10
    for c in citations:
        assert {"id", "claim", "cited_passage", "supports", "domain"} <= set(c)
        assert isinstance(c["supports"], bool)
