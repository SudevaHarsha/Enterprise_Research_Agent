"""Gold-set loaders for the ECRKE eval suite (build-plan Step 14).

The gold files live in ``tests/gold/``: one JSONL of seed questions with gold
claims, one JSONL of contradiction pairs labeled confirmed | rejected |
flagged. Every loader is a pure function of the files; tests never mutate the
gold data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GOLD_DIR = Path(__file__).resolve().parent.parent / "gold"
QUESTIONS_FILE = GOLD_DIR / "questions.jsonl"
CONTRADICTIONS_FILE = GOLD_DIR / "contradictions.jsonl"

VALID_LABELS = frozenset({"confirmed", "rejected", "flagged"})
VALID_DOMAINS = frozenset({"retail-operations", "climate-transport", "education", "work", "health"})


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load a JSONL file as a list of dicts (raises a clear error when missing)."""
    if not path.exists():
        raise FileNotFoundError(f"gold file missing: {path}")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{line_number} is not valid JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path.name}:{line_number} is not a JSON object")
            rows.append(row)
    return rows


def load_questions() -> list[dict[str, Any]]:
    """Load the seed questions (each with an id, domain, question, gold_claims)."""
    return load_jsonl(QUESTIONS_FILE)


def load_contradictions() -> list[dict[str, Any]]:
    """Load the gold contradiction pairs (id, label, domain, statement_a, statement_b)."""
    return load_jsonl(CONTRADICTIONS_FILE)


def contradictions_by_label(label: str) -> list[dict[str, Any]]:
    """Return gold pairs with exactly ``label`` (validated against VALID_LABELS)."""
    if label not in VALID_LABELS:
        raise ValueError(f"unknown gold label {label!r}; expected one of {sorted(VALID_LABELS)}")
    return [row for row in load_contradictions() if row.get("label") == label]


def confirmed_pairs() -> list[tuple[str, str]]:
    """Gold confirmed pairs as ``(statement_a, statement_b)`` text tuples."""
    return [
        (row["statement_a"], row["statement_b"]) for row in contradictions_by_label("confirmed")
    ]


def flagged_pairs() -> list[tuple[str, str]]:
    """Gold flagged pairs as ``(statement_a, statement_b)`` text tuples."""
    return [(row["statement_a"], row["statement_b"]) for row in contradictions_by_label("flagged")]


def rejected_pairs() -> list[tuple[str, str]]:
    """Gold rejected pairs as ``(statement_a, statement_b)`` text tuples."""
    return [(row["statement_a"], row["statement_b"]) for row in contradictions_by_label("rejected")]
