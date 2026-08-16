"""CheckpointStore unit tests (task_011 — durable, resumable checkpoints).

Hermetic: the store is exercised against the in-memory FakeSessionFactory
(no Prefect, no DB). Covers save/load roundtrip, completed-stage discovery,
idempotent (run_id, stage) upsert, JSON-serializability validation, and G-05
redaction of checkpoint state before persistence.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.db.models import Checkpoint
from app.pipeline.checkpoint import CheckpointStore
from tests.conftest import FakeSessionFactory, rows_of

SECRET = "sk-fake-test-1234567890"  # noqa: S105 - fake fixture value; must be redacted


@pytest.fixture
def factory() -> FakeSessionFactory:
    return FakeSessionFactory()


async def test_save_load_roundtrip(factory: FakeSessionFactory) -> None:
    store = CheckpointStore(factory)
    run_id = uuid4()
    cp = await store.save(run_id, "search", {"urls": ["https://retail.example.com/a"]})
    assert isinstance(cp, Checkpoint)
    assert cp.stage == "search"
    assert await store.load(run_id, "search") == {"urls": ["https://retail.example.com/a"]}


async def test_load_missing_stage_returns_none(factory: FakeSessionFactory) -> None:
    store = CheckpointStore(factory)
    assert await store.load(uuid4(), "define") is None


async def test_completed_stages_returns_set(factory: FakeSessionFactory) -> None:
    store = CheckpointStore(factory)
    run_id = uuid4()
    assert await store.completed_stages(run_id) == set()
    await store.save(run_id, "define", None)
    await store.save(run_id, "search", {"urls": []})
    completed = await store.completed_stages(run_id)
    assert completed == {"define", "search"}
    # other runs are isolated
    assert await store.completed_stages(uuid4()) == set()


async def test_upsert_same_run_stage_does_not_duplicate(factory: FakeSessionFactory) -> None:
    store = CheckpointStore(factory)
    run_id = uuid4()
    await store.save(run_id, "collect", {"count": 1})
    await store.save(run_id, "collect", {"count": 2})
    rows = rows_of(factory.storage, Checkpoint)
    assert len(rows) == 1
    assert await store.load(run_id, "collect") == {"count": 2}
    assert await store.completed_stages(run_id) == {"collect"}


async def test_state_must_be_json_serializable(factory: FakeSessionFactory) -> None:
    store = CheckpointStore(factory)
    with pytest.raises(ValueError, match="JSON"):
        await store.save(uuid4(), "define", {"bad": object()})


async def test_save_redacts_secrets_from_state(factory: FakeSessionFactory) -> None:
    store = CheckpointStore(factory)
    run_id = uuid4()
    await store.save(run_id, "define", {"topic": f"use {SECRET} to authenticate"})
    state = await store.load(run_id, "define")
    assert state is not None
    assert SECRET not in json.dumps(state)
    row = rows_of(factory.storage, Checkpoint)[0]
    assert SECRET not in json.dumps(row.state)
