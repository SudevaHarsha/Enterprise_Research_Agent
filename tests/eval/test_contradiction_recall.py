"""Gold-set contradiction recall harness (build-plan Step 14).

Runs the REAL :class:`ContradictionDetector` over all 124 gold statement texts
(62 pairs x 2) with a cooperative fake judge, then measures recall and
precision against the gold-confirmed pairs with the pure metrics. The judge
queue mirrors the detector's deterministic candidate order exactly, so
``provider.calls == len(queue)`` proves no hidden or missing LLM calls.
"""

from __future__ import annotations

from typing import Any

from app.services.contradiction_detector import candidate_pairs, negation_signal
from app.services.normalizer import redact_secrets
from tests.eval.conftest import confirm_json, flag_json, make_detector_harness, make_statement
from tests.eval.eval_metrics import RECALL_FLOOR, contradiction_precision, contradiction_recall
from tests.eval.gold import load_contradictions


def _text_key(text: str) -> str:
    return text.casefold()


def _pair_key(a: str, b: str) -> frozenset[str]:
    return frozenset({_text_key(a), _text_key(b)})


def _build_gold_index(
    rows: list[dict[str, Any]],
) -> tuple[set[frozenset[str]], set[frozenset[str]], dict[str, str]]:
    """Return (confirmed keys, flagged keys, statement-text -> id map)."""
    confirmed: set[frozenset[str]] = set()
    flagged: set[frozenset[str]] = set()
    ids: dict[str, str] = {}
    for row in rows:
        a, b = row["statement_a"], row["statement_b"]
        if row["label"] == "confirmed":
            confirmed.add(_pair_key(a, b))
        elif row["label"] == "flagged":
            flagged.add(_pair_key(a, b))
        ids.setdefault(_text_key(a), row["id"] + ":a")
        ids.setdefault(_text_key(b), row["id"] + ":b")
    return confirmed, flagged, ids


def _cooperative_queue(
    statements: list[Any],
    confirmed_gold: set[frozenset[str]],
    flagged_gold: set[frozenset[str]],
) -> list[Any]:
    """Queue judge responses in the detector's candidate order (mirrors detect)."""
    queue: list[Any] = []
    for a, b in candidate_pairs(statements):
        key = _pair_key(a.text, b.text)
        a_text = redact_secrets(a.text)
        b_text = redact_secrets(b.text)
        if key in confirmed_gold:
            queue.append(flag_json(flag="flag", contradictory=True, reason="gold conflict"))
            if not negation_signal(a_text, b_text):
                queue.append(confirm_json(contradictory=True, reason="gold conflict"))
        elif key in flagged_gold:
            queue.append(flag_json(flag="flag", contradictory=True, reason="surface conflict"))
            queue.append(confirm_json(contradictory=False, reason="compatible in context"))
        else:
            queue.append(flag_json(flag="no_flag", contradictory=False, reason="compatible"))
    return queue


async def test_gold_recall_meets_floor_with_perfect_precision() -> None:
    harness = make_detector_harness()
    gold_rows = load_contradictions()
    confirmed_gold, flagged_gold, _ = _build_gold_index(gold_rows)

    statements_by_text: dict[str, Any] = {}
    for row in gold_rows:
        for text in (row["statement_a"], row["statement_b"]):
            statements_by_text.setdefault(_text_key(text), make_statement(text, harness.run.id))
    statements = list(statements_by_text.values())

    queue = _cooperative_queue(statements, confirmed_gold, flagged_gold)
    for response in queue:
        harness.provider.queue(response)

    confirmed_rows = await harness.detector.detect(statements, harness.run.id)

    # The cooperative judge was consumed exactly in candidate order.
    assert len(harness.provider.calls) == len(queue)

    id_by_text = {text: statement.id for text, statement in statements_by_text.items()}
    confirmed_pairs = [(row.statement_a_id, row.statement_b_id) for row in confirmed_rows]
    gold_pairs = [
        (id_by_text[_text_key(row["statement_a"])], id_by_text[_text_key(row["statement_b"])])
        for row in gold_rows
        if row["label"] == "confirmed"
    ]
    recall = contradiction_recall(confirmed_pairs, gold_pairs)
    precision = contradiction_precision(confirmed_pairs, gold_pairs)
    assert recall >= RECALL_FLOOR
    assert precision == 1.0
