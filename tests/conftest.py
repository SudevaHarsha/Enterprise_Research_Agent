"""Shared pytest fixtures for the ECRKE test suite.

Hermetic service-test helpers: LiteLLM-shaped fake responses/providers and an
in-memory async-session stand-in so the gateway / cost-meter / kv-cache unit
tests never touch a real database, network, or LLM provider (no real LLM API
calls in tests — task_004 constraint).
"""

from __future__ import annotations

import contextlib
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.db.models import KVEntry, Passage, Run, Source


class SampleOutput(BaseModel):
    """Small Pydantic model used to exercise structured-output gateway paths."""

    topic: str
    confidence: float = Field(ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)


class FakeUsage:
    """LiteLLM-shaped usage metadata."""

    def __init__(self, prompt_tokens: int = 10, completion_tokens: int = 5) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeMessage:
    """LiteLLM-shaped assistant message."""

    def __init__(self, content: str) -> None:
        self.content = content


class FakeChoice:
    """LiteLLM-shaped choice wrapper."""

    def __init__(self, content: str) -> None:
        self.message = FakeMessage(content)


class FakeResponse:
    """LiteLLM-shaped completion response (object attribute access)."""

    def __init__(
        self,
        content: str,
        prompt_tokens: int = 10,
        completion_tokens: int = 5,
    ) -> None:
        self.choices = [FakeChoice(content)]
        self.usage = FakeUsage(prompt_tokens, completion_tokens)


class FakeProvider:
    """Async callable mimicking ``litellm.acompletion``; records every call."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self._responses = list(responses or [])
        self._error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse("hello from fake provider")

    def queue(self, response: Any) -> None:
        """Queue a response to be returned on the next provider call."""
        self._responses.append(response)

    def set_error(self, error: Exception) -> None:
        """Make the provider raise ``error`` on every subsequent call."""
        self._error = error


class FakeClock:
    """Controllable clock for expiry tests."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Advance the clock by ``seconds``."""
        self._now += timedelta(seconds=seconds)


class FakeSession:
    """In-memory stand-in for ``AsyncSession`` covering the service surface used."""

    def __init__(self, storage: dict[Any, Any]) -> None:
        self._storage = storage
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def get(self, model: type[Any], key: Any) -> Any | None:
        if isinstance(key, str):
            with contextlib.suppress(ValueError):
                key = UUID(key)
        return self._storage.get(key)

    def add(self, obj: Any) -> None:
        if isinstance(obj, KVEntry):
            self._storage[obj.key] = obj
        elif isinstance(obj, (Run, Source, Passage)):
            self._storage[obj.id] = obj
        else:
            raise TypeError(f"FakeSession.add does not support {type(obj).__name__}")

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def delete(self, obj: Any) -> None:
        if isinstance(obj, KVEntry):
            self._storage.pop(obj.key, None)
        elif isinstance(obj, (Run, Source, Passage)):
            self._storage.pop(obj.id, None)
        else:
            raise TypeError(f"FakeSession.delete does not support {type(obj).__name__}")

    async def scalar(self, statement: Any) -> Any | None:
        """Minimal translator for ``select(Entity).where(Entity.col == value)``.

        Covers the dedupe lookups used by the collectors; anything else raises
        ``NotImplementedError`` so a real SQLAlchemy expression is never silently
        mis-evaluated in tests.
        """
        descriptions = getattr(statement, "column_descriptions", None)
        whereclause = getattr(statement, "whereclause", None)
        if not descriptions or whereclause is None:
            raise NotImplementedError(
                "FakeSession.scalar only supports select(Entity).where(Entity.col == value)"
            )
        entity = descriptions[0].get("entity")
        if entity is None:
            raise NotImplementedError("FakeSession.scalar: unsupported select entity")
        column = getattr(whereclause, "left", None)
        column_key = getattr(column, "key", None)
        if column_key is None:
            raise NotImplementedError("FakeSession.scalar: unsupported WHERE column")
        expected = getattr(whereclause.right, "value", whereclause.right)
        for obj in self._storage.values():
            if isinstance(obj, entity) and getattr(obj, column_key) == expected:
                return obj
        return None


class FakeSessionFactory:
    """Callable that hands out ``FakeSession`` objects over one shared storage dict."""

    def __init__(self, storage: dict[Any, Any] | None = None) -> None:
        self.storage: dict[Any, Any] = storage if storage is not None else {}

    def __call__(self) -> FakeSession:
        return FakeSession(self.storage)


@pytest.fixture
def fake_session_factory() -> FakeSessionFactory:
    """Fixture: fresh in-memory session factory per test."""
    return FakeSessionFactory()


@pytest.fixture
def fake_provider() -> FakeProvider:
    """Fixture: provider that records calls and returns canned responses."""
    return FakeProvider()


def make_run_row(cost_spent_usd: Decimal | float = Decimal("0.0000")) -> Run:
    """Build a ``Run`` ORM instance without a database (pre-insert object)."""
    return Run(
        id=uuid4(),
        tenant_id=uuid4(),
        question="test question",
        cost_spent_usd=cost_spent_usd,
    )


class FakeTransport(httpx.AsyncBaseTransport):
    """In-process httpx transport: canned per-URL responses, no real network.

    Records every request as ``(unix_timestamp, url_string)`` so rate-limit
    behavior can be asserted with a ``FakeClock``.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._responses: dict[str, tuple[bytes, str, int]] = {}
        self.calls: list[tuple[float, str]] = []

    def respond(
        self,
        url: str,
        content: bytes,
        content_type: str,
        status_code: int = 200,
    ) -> None:
        """Register a canned response for an exact URL."""
        self._responses[url] = (content, content_type, status_code)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((self._clock().timestamp(), str(request.url)))
        canned = self._responses.get(str(request.url))
        if canned is None:
            return httpx.Response(
                404,
                request=request,
                content=b"not found",
                headers={"content-type": "text/plain"},
            )
        content, content_type, status_code = canned
        return httpx.Response(
            status_code,
            request=request,
            content=content,
            headers={"content-type": content_type},
        )


class AdvancingSleep:
    """Async sleep that advances a ``FakeClock`` instead of waiting for real time."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    async def __call__(self, seconds: float) -> None:
        self._clock.advance(seconds)


def sample_html_bytes() -> bytes:
    """Small HTML document with a title and two short paragraphs."""
    return b"""<!DOCTYPE html>
<html><head><title>Retail Report</title></head>
<body>
<h1>Retail Report</h1>
<p>Retailers report stronger same-store sales growth in the latest quarter.</p>
<p>E-commerce continues to expand its share of total retail spending.</p>
</body></html>"""


def sample_long_html_bytes(paragraph_count: int = 8) -> bytes:
    """HTML with N clearly distinct paragraphs (default 8) for multi-chunk tests."""
    paragraphs = "\n\n".join(
        f"<p>Paragraph {i}. "
        + ("Retail analytics and supply chain visibility improve planning accuracy. " * 3)
        + "</p>"
        for i in range(1, paragraph_count + 1)
    )
    return f"<!DOCTYPE html><html><body>{paragraphs}</body></html>".encode()


def sample_pdf_bytes() -> bytes:
    """Build a real, extractable PDF in memory (pypdf, no files, no network).

    A ``/ToUnicode`` CMap maps the drawn byte codes to Unicode so pypdf's
    ``extract_text`` returns readable text.
    """
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    cmap = b"""\
/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
15 beginbfchar
<20> <0020>
<43> <0043>
<44> <0044>
<45> <0045>
<46> <0046>
<48> <0048>
<4B> <004B>
<4C> <004C>
<4F> <004F>
<50> <0050>
<52> <0052>
<53> <0053>
<65> <0065>
<6C> <006C>
<6F> <006F>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""
    to_unicode = DecodedStreamObject()
    to_unicode.set_data(cmap)
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 72 720 Td (Hello ECRKE PDF) Tj ET")
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            NameObject("/ToUnicode"): writer._add_object(to_unicode),
        }
    )
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.add_metadata({"/Title": "ECRKE sample"})
    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    extracted = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if "Hello ECRKE PDF" not in extracted:
        raise AssertionError("sample_pdf_bytes produced a PDF without extractable text")
    return pdf_bytes


def sample_docx_bytes() -> bytes:
    """Build a real .docx in memory (python-docx, no files, no network)."""
    import docx

    document = docx.Document()
    document.add_paragraph("Hello ECRKE DOCX")
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def sample_rtf_bytes() -> bytes:
    """Small RTF document that striprtf can parse."""
    return b"{\\rtf1\\ansi\\deff0 {\\fonttbl {\\f0 Times New Roman;}} \\f0\\fs24 Hello ECRKE RTF.}"


def sample_rss_xml() -> bytes:
    """RSS 2.0 feed with one item pointing at an allowlisted domain."""
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Retail News</title>
    <link>https://retail.example.com/rss</link>
    <description>Retail industry news</description>
    <item>
      <title>Retail margins rise</title>
      <link>https://retail.example.com/news/margins</link>
      <description>Margins improved this quarter across the retail sector.</description>
      <pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""


def sample_atom_xml() -> bytes:
    """Atom feed with one entry pointing at an allowlisted domain."""
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Retail Tech Blog</title>
  <link href="https://retailtech.example.com/feed"/>
  <entry>
    <title>New POS features</title>
    <link href="https://retailtech.example.com/posts/pos"/>
    <content type="html">Point of sale upgrades were announced today.</content>
    <updated>2026-01-02T00:00:00Z</updated>
  </entry>
</feed>"""


def rows_of(storage: dict[Any, Any], model: type[Any]) -> list[Any]:
    """Return every row of ``model`` stored in a FakeSession storage dict."""
    return [obj for obj in storage.values() if isinstance(obj, model)]


@pytest.fixture
def fake_settings() -> Settings:
    """Hermetic settings for collection tests (no real credentials or network)."""
    return Settings(
        app_env="test",
        allowed_domains="retail.example.com,retailtech.example.com",
        search_provider="mock",
        search_results_limit=5,
        fetch_min_interval_seconds=0.0,
        fetch_timeout_seconds=5.0,
        blob_store_backend="local",
        blob_store_dir=".blobs",
    )
