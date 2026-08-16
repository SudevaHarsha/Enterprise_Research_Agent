"""Pure, hand-verifiable trust metrics for the ECRKE eval suite.

Every function here is deterministic and free of LLM/DB/network dependencies so
a human (or a fresh agent) can recompute each value by hand. Inputs are duck
typed: plain strings, ``SimpleNamespace`` objects, or the report-renderer
Pydantic models all work for the attribute accessors.

Floor constants are the minimum acceptable values the seed run must meet;
they are deliberately modest (>= 0.5) because the seed is a smoke-quality
fixture, not a research deliverable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

RECALL_FLOOR = 0.5
SUPPORT_FLOOR = 0.5

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute access that works on dicts, Pydantic models, and namespaces."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def content_tokens(text: str, min_len: int = 4) -> set[str]:
    """Casefolded alphanumeric word set with short tokens filtered out."""
    return {
        token for token in _TOKEN_PATTERN.findall(str(text).casefold()) if len(token) >= min_len
    }


def claim_covered(claim: str, passages: Iterable[str], threshold: float = 0.6) -> bool:
    """True when >= ``threshold`` of the claim's content tokens appear in passages.

    A claim with no content tokens is vacuously covered (nothing to verify).
    """
    claim_tokens = content_tokens(claim)
    if not claim_tokens:
        return True
    passage_tokens: set[str] = set()
    for passage in passages:
        passage_tokens |= content_tokens(passage)
    return len(claim_tokens & passage_tokens) / len(claim_tokens) >= threshold


def statement_decomposition_coverage(
    statements: Iterable[Any],
    passages: Iterable[str],
    threshold: float = 0.6,
) -> float:
    """Fraction of statements whose claim tokens are covered by some passage."""
    passage_texts = [str(p) for p in passages]
    covered = 0
    total = 0
    for statement in statements:
        total += 1
        if claim_covered(_attr(statement, "text", str(statement)), passage_texts, threshold):
            covered += 1
    return covered / total if total else 1.0


def citation_accuracy(
    conclusion_text: str,
    statement_texts: Iterable[str],
    min_len: int = 5,
) -> float:
    """Fraction of a conclusion's content tokens traceable to cited statements.

    A conclusion with no content tokens is vacuously accurate. Uses longer
    tokens (>= 5 chars) so common function words do not inflate the score.
    """
    conclusion_tokens = content_tokens(conclusion_text, min_len)
    if not conclusion_tokens:
        return 1.0
    statement_tokens: set[str] = set()
    for text in statement_texts:
        statement_tokens |= content_tokens(text, min_len)
    return len(conclusion_tokens & statement_tokens) / len(conclusion_tokens)


def support_ratio(support_matrix: Iterable[Any], full_score: str = "full") -> float:
    """Fraction of evidence links in a support matrix rated ``full``."""
    entries = list(support_matrix)
    if not entries:
        return 0.0
    full = sum(1 for entry in entries if _attr(entry, "support_score") == full_score)
    return full / len(entries)


def one_sidedness(distinct_domains: Iterable[str], min_domains: int = 2) -> bool:
    """True when fewer than ``min_domains`` distinct non-empty domains back a claim."""
    domains = {domain for domain in distinct_domains if str(domain).strip()}
    return len(domains) < min_domains


def _pair_key(statement_a_id: Any, statement_b_id: Any) -> frozenset[str]:
    """Order/case-insensitive key for a contradiction pair."""
    return frozenset({str(statement_a_id).casefold(), str(statement_b_id).casefold()})


def contradiction_recall(
    confirmed_pairs: Iterable[tuple[Any, Any]],
    gold_pairs: Iterable[tuple[Any, Any]],
) -> float:
    """Fraction of gold-confirmed pairs that the detector actually confirmed."""
    confirmed = {_pair_key(a, b) for a, b in confirmed_pairs}
    gold = {_pair_key(a, b) for a, b in gold_pairs}
    if not gold:
        return 0.0
    return len(confirmed & gold) / len(gold)


def contradiction_precision(
    confirmed_pairs: Iterable[tuple[Any, Any]],
    gold_pairs: Iterable[tuple[Any, Any]],
) -> float:
    """Fraction of the detector's confirmed pairs that are gold-confirmed."""
    confirmed = {_pair_key(a, b) for a, b in confirmed_pairs}
    gold = {_pair_key(a, b) for a, b in gold_pairs}
    if not confirmed:
        return 0.0
    return len(confirmed & gold) / len(confirmed)


def traceability(chain_map: Mapping[str, Sequence[str | None]]) -> float:
    """Fraction of statements with a fully resolved provenance chain.

    ``chain_map`` maps statement id -> ``(passage_id, source_id)``. A chain is
    complete when both the passage and the source resolve (statement ->
    passage -> source, 2 hops within the default max of 3).
    """
    if not chain_map:
        return 0.0
    complete = 0
    for statement_id, chain in chain_map.items():
        if not str(statement_id):
            continue
        nodes = [node for node in chain if node is not None]
        if len(nodes) >= 2:
            complete += 1
    return complete / len(chain_map)
