"""Content-addressed blob storage for raw source bytes.

Refs are sha256 hex digests of the content (``raw_ref == content_hash``), so
the store is dedupe-friendly and immutable by construction. ``LocalBlobStore``
is the default backend and is fully hermetic-tested; ``S3BlobStore`` is an
R2/S3-compatible option behind the optional ``[s3]`` extra (aiobotocore,
lazily imported) and is excluded from hermetic tests.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.core.config import Settings


class BlobNotFoundError(KeyError):
    """Raised when a blob ref does not exist in the store."""


@runtime_checkable
class BlobStore(Protocol):
    """Async, content-addressed blob storage protocol."""

    async def put(self, ref: str, content: bytes) -> None: ...

    async def get(self, ref: str) -> bytes: ...


class LocalBlobStore:
    """Filesystem blob store rooted at ``root``; one file per content ref."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, ref: str) -> Path:
        return self.root / ref

    async def put(self, ref: str, content: bytes) -> None:
        """Write ``content`` at ``ref`` atomically; no-op when already present."""
        target = self._path_for(ref)
        if target.exists():
            return  # content-addressed: same ref => same bytes, already stored
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(content)
        os.replace(tmp, target)

    async def get(self, ref: str) -> bytes:
        """Return the bytes stored at ``ref``; raise :class:`BlobNotFoundError`."""
        target = self._path_for(ref)
        if not target.is_file():
            raise BlobNotFoundError(f"blob {ref!r} not found in {self.root}")
        return target.read_bytes()


def make_blob_store(settings: Settings) -> BlobStore:
    """Build the configured blob store backend (from ``BLOB_STORE_BACKEND``)."""
    backend = (settings.blob_store_backend or "local").strip().lower()
    if backend == "local":
        return LocalBlobStore(Path(settings.blob_store_dir))
    if backend == "s3":
        return S3BlobStore(
            endpoint=settings.blob_endpoint,
            bucket=settings.blob_bucket,
            access_key=str(settings.blob_access_key) if settings.blob_access_key else None,
            secret_key=str(settings.blob_secret_key) if settings.blob_secret_key else None,
        )
    raise ValueError(f"unsupported BLOB_STORE_BACKEND {backend!r} (supported: local, s3)")


class S3BlobStore:
    """R2/S3-compatible blob store (optional ``[s3]`` extra, lazy aiobotocore import).

    Never imported by hermetic tests; instantiating it without the optional
    dependency fails fast with a clear install hint.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str | None,
        secret_key: str | None,
    ) -> None:
        try:
            import aiobotocore  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "S3BlobStore requires the optional [s3] extra (aiobotocore); "
                "install with 'pip install -e .[s3]' or set BLOB_STORE_BACKEND=local"
            ) from exc
        self._endpoint = endpoint
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key

    async def put(self, ref: str, content: bytes) -> None:
        import aiobotocore

        session = aiobotocore.get_session()
        async with session.create_client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        ) as client:
            await client.put_object(Bucket=self._bucket, Key=ref, Body=content)

    async def get(self, ref: str) -> bytes:
        import aiobotocore
        from botocore.exceptions import ClientError

        session = aiobotocore.get_session()
        async with session.create_client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        ) as client:
            try:
                response = await client.get_object(Bucket=self._bucket, Key=ref)
            except ClientError as exc:
                raise BlobNotFoundError(f"blob {ref!r} not found in bucket {self._bucket}") from exc
            body = await response["Body"].read()
            return bytes(body)
