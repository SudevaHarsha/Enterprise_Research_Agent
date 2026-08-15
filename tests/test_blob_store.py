"""Blob store tests — content-addressed local store (default backend)."""

import hashlib

import pytest

from app.services.blob_store import BlobNotFoundError, LocalBlobStore


async def test_put_get_roundtrip(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    ref = "a" * 64
    await store.put(ref, b"payload")
    assert await store.get(ref) == b"payload"


async def test_blob_file_is_content_addressed(tmp_path) -> None:
    """raw_ref is the sha256 hex of the raw bytes — the file lives at that name."""
    store = LocalBlobStore(tmp_path)
    payload = b"retail blob payload"
    ref = hashlib.sha256(payload).hexdigest()
    await store.put(ref, payload)
    assert (tmp_path / ref).is_file()
    assert (tmp_path / ref).read_bytes() == payload


async def test_missing_blob_raises_not_found(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    with pytest.raises(BlobNotFoundError):
        await store.get("f" * 64)


async def test_put_is_idempotent(tmp_path) -> None:
    store = LocalBlobStore(tmp_path)
    ref = "b" * 64
    await store.put(ref, b"first")
    await store.put(ref, b"first")
    assert await store.get(ref) == b"first"
    assert len(list(tmp_path.iterdir())) == 1
