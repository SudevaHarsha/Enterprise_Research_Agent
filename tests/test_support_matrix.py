"""Unit tests for the deterministic support matrix (task_007).

``score_support`` is a pure, $0 lexical scorer: it maps statement<->passage
token overlap to ``EvidenceScore`` full|partial|none with a numeric ratio in
[0, 1]. No gateway, no LLM, no database — deterministic by construction.
"""

from __future__ import annotations

import pytest

from app.db.enums import EvidenceScore
from app.services.support_matrix import (
    FULL_THRESHOLD,
    PARTIAL_THRESHOLD,
    score_support,
)

STATEMENT_SUPPORTED = "Retailers reported stronger same-store sales growth."
PASSAGE_SUPPORTED = "Retailers reported stronger same-store sales growth in the latest quarter."


def test_strong_token_overlap_is_full() -> None:
    """Statement words fully contained in the passage -> FULL, ratio 1.0."""
    score, ratio = score_support(STATEMENT_SUPPORTED, PASSAGE_SUPPORTED)
    assert score is EvidenceScore.FULL
    assert ratio == pytest.approx(1.0)


def test_moderate_token_overlap_is_partial() -> None:
    """A statement ~29% supported by the passage -> PARTIAL."""
    statement = (
        "The Acme-Beta merger was approved by regulators subject to "
        "divesting the logistics unit entirely."
    )
    passage = "Regulators approved the merger."
    score, ratio = score_support(statement, passage)
    assert score is EvidenceScore.PARTIAL
    assert ratio == pytest.approx(4 / 14)


def test_unrelated_text_is_none() -> None:
    """No shared words -> NONE, ratio 0.0."""
    score, ratio = score_support("Berlin weather was sunny all week.", PASSAGE_SUPPORTED)
    assert score is EvidenceScore.NONE
    assert ratio == pytest.approx(0.0)


def test_ratio_stays_within_bounds() -> None:
    """The returned ratio is always within [0, 1] for any input pair."""
    cases = [
        (STATEMENT_SUPPORTED, PASSAGE_SUPPORTED),
        (
            "The Acme-Beta merger was approved by regulators subject to "
            "divesting the logistics unit entirely.",
            "Regulators approved the merger.",
        ),
        ("Berlin weather was sunny all week.", PASSAGE_SUPPORTED),
        ("alpha beta gamma delta", "alpha omega"),
    ]
    for statement, passage in cases:
        _, ratio = score_support(statement, passage)
        assert 0.0 <= ratio <= 1.0


def test_threshold_exactly_full_is_full() -> None:
    """Ratio == FULL_THRESHOLD (0.5) counts as FULL."""
    score, ratio = score_support("alpha beta", "alpha omega")
    assert ratio == pytest.approx(0.5)
    assert ratio >= FULL_THRESHOLD
    assert score is EvidenceScore.FULL


def test_threshold_exactly_partial_is_partial() -> None:
    """Ratio == PARTIAL_THRESHOLD (0.25) counts as PARTIAL."""
    score, ratio = score_support("alpha beta gamma delta", "alpha omega")
    assert ratio == pytest.approx(0.25)
    assert PARTIAL_THRESHOLD <= ratio < FULL_THRESHOLD
    assert score is EvidenceScore.PARTIAL


def test_deterministic_same_input_same_output() -> None:
    """Pure function: identical inputs yield identical (score, ratio)."""
    assert score_support(STATEMENT_SUPPORTED, PASSAGE_SUPPORTED) == score_support(
        STATEMENT_SUPPORTED, PASSAGE_SUPPORTED
    )


def test_case_insensitive_matching() -> None:
    """Matching is case-insensitive."""
    score, ratio = score_support(
        "RETAILERS REPORTED STRONGER", "retailers reported stronger same-store sales"
    )
    assert score is EvidenceScore.FULL
    assert ratio == pytest.approx(1.0)


def test_punctuation_and_hyphens_split_into_words() -> None:
    """Punctuation and hyphens are stripped; 'same-store' -> {'same', 'store'}."""
    score, ratio = score_support("same-store sales growth.", "same store sales growth")
    assert score is EvidenceScore.FULL
    assert ratio == pytest.approx(1.0)


def test_empty_passage_is_none_with_zero_ratio() -> None:
    """A missing/blank passage yields (NONE, 0.0) — no crash, no LLM."""
    assert score_support(STATEMENT_SUPPORTED, "") == (EvidenceScore.NONE, 0.0)
    assert score_support(STATEMENT_SUPPORTED, "   ") == (EvidenceScore.NONE, 0.0)


def test_empty_statement_raises_value_error() -> None:
    """An empty statement is a degenerate input, rejected up front."""
    with pytest.raises(ValueError, match="statement text must be non-empty"):
        score_support("", PASSAGE_SUPPORTED)
    with pytest.raises(ValueError, match="statement text must be non-empty"):
        score_support("   ", PASSAGE_SUPPORTED)


def test_statement_without_words_raises_value_error() -> None:
    """A statement with no alphanumeric words would divide by zero — rejected."""
    with pytest.raises(ValueError, match="statement text must contain"):
        score_support("!!!", PASSAGE_SUPPORTED)


def test_ratio_anchored_to_statement_not_passage() -> None:
    """Ratio is containment of the statement in the passage (1/5, not 1/2)."""
    score, ratio = score_support("alpha beta gamma delta zeta", "alpha omega")
    assert score is EvidenceScore.NONE
    assert ratio == pytest.approx(0.2)
