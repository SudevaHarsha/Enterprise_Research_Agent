"""Content safety filter (build-plan Step 14, guardrail G-04).

Heuristic unsafe-category detection applied to fetched text before it is
blob-stored. Pure deterministic substring matching on casefolded text — no LLM
call, no dependencies. Categories intentionally narrow (explicitly dangerous
instruction sets); the default is ``is_unsafe(...) == False`` so benign
research content is never blocked.

Exported for tests (guardrail G-04 / G-13): ``find_unsafe_categories``,
``is_unsafe``, ``unsafe_reason``.
"""

from __future__ import annotations

# Casefolded marker phrases per category. Deliberately exact phrases (not bare
# words like "bomb") so ordinary content about safety research stays clean.
_TERMS: dict[str, frozenset[str]] = {
    "violence": frozenset({"bomb-making", "pipe bomb", "explosive device", "how to build a bomb"}),
    "hate": frozenset({"ethnic cleansing", "hate speech", "white supremacist"}),
    "sexual": frozenset({"child sexual abuse", "sexual exploitation of minors"}),
    "illicit": frozenset({"darknet market", "illegal drug manufacture", "credit card fraud"}),
}


def find_unsafe_categories(text: str) -> tuple[str, ...]:
    """Return the ordered, deduplicated categories matched in ``text``."""
    haystack = text.casefold()
    return tuple(
        category
        for category, phrases in _TERMS.items()
        if any(phrase in haystack for phrase in phrases)
    )


def is_unsafe(text: str) -> bool:
    """True when any unsafe category matches (default-safe)."""
    return bool(find_unsafe_categories(text))


def unsafe_reason(text: str) -> str:
    """Human-readable quarantine reason, e.g. ``content flagged as unsafe: hate, sexual``."""
    categories = find_unsafe_categories(text)
    if not categories:
        return "content flagged as unsafe: no categories"
    return f"content flagged as unsafe: {', '.join(categories)}"
