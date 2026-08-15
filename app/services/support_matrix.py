"""Deterministic lexical support matrix (task_007, build-plan Step 7).

The first, $0 stage of the verify-first gate: a pure function that scores how
much of a statement's vocabulary is present in its supporting passage.

- ``score_support(statement_text, passage_text) -> (EvidenceScore, ratio)``
  computes *containment*: ratio = |statement tokens ∩ passage tokens| /
  |statement tokens| — how much of the claim the passage actually covers.
- Thresholds (module constants, documented here as the contract):
  - ``ratio >= FULL_THRESHOLD (0.5)``    -> ``EvidenceScore.FULL``
  - ``ratio >= PARTIAL_THRESHOLD (0.25)`` -> ``EvidenceScore.PARTIAL``
  - otherwise                            -> ``EvidenceScore.NONE``

Deterministic by construction: no LLM, no network, no I/O. The judge LLM is
only invoked after this matrix confirms at least ``PARTIAL`` alignment, so a
totally unsupported statement costs zero tokens (G-03).
"""

from __future__ import annotations

import re

from app.db.enums import EvidenceScore

FULL_THRESHOLD = 0.5
PARTIAL_THRESHOLD = 0.25

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Casefolded alphanumeric word set (punctuation and hyphens ignored)."""
    return set(_TOKEN_PATTERN.findall(text.casefold()))


def score_support(statement_text: str, passage_text: str) -> tuple[EvidenceScore, float]:
    """Score how much of ``statement_text`` is covered by ``passage_text``.

    Returns ``(EvidenceScore, ratio)`` where ``ratio`` is the fraction of the
    statement's unique words present in the passage (always within [0.0, 1.0]).
    An empty statement raises :class:`ValueError`; an empty passage yields
    ``(EvidenceScore.NONE, 0.0)`` so a missing source can never promote a claim.
    """
    if not statement_text.strip():
        raise ValueError("statement text must be non-empty")
    statement_tokens = _tokens(statement_text)
    if not statement_tokens:
        raise ValueError("statement text must contain at least one word")
    if not passage_text.strip():
        return EvidenceScore.NONE, 0.0
    passage_tokens = _tokens(passage_text)
    ratio = len(statement_tokens & passage_tokens) / len(statement_tokens)
    if ratio >= FULL_THRESHOLD:
        return EvidenceScore.FULL, ratio
    if ratio >= PARTIAL_THRESHOLD:
        return EvidenceScore.PARTIAL, ratio
    return EvidenceScore.NONE, ratio
