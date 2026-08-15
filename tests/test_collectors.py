"""Collector tests — URLConnector pipeline, dedupe, quarantine, redaction;
SearchConnector provider-agnostic dispatch; RSS/Atom candidate parsing.

Hermetic by construction: in-memory session factory, in-process httpx
transport, local blob store on tmp_path. No network, DB, Docker, or S3.
"""

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.config import Settings
from app.db.models import Passage, Source
from app.services.allowlist import Allowlist
from app.services.blob_store import LocalBlobStore
from app.services.collectors.rss import RSSConnector, RSSParseError
from app.services.collectors.search import SearchConnector, SearchProviderError
from app.services.collectors.url import URLConnector
from app.services.fetcher import Fetcher
from app.services.normalizer import Normalizer
from tests.conftest import (
    AdvancingSleep,
    FakeClock,
    FakeSessionFactory,
    FakeTransport,
    rows_of,
    sample_atom_xml,
    sample_html_bytes,
    sample_long_html_bytes,
    sample_pdf_bytes,
    sample_rss_xml,
)


def make_url_collector(
    tmp_path: Path, settings: Settings
) -> tuple[URLConnector, FakeSessionFactory, FakeTransport, LocalBlobStore]:
    """Wire a hermetic URLConnector: fake clock/transport/session + local blobs."""
    clock = FakeClock()
    transport = FakeTransport(clock)
    client = httpx.AsyncClient(transport=transport, timeout=settings.fetch_timeout_seconds)
    fetcher = Fetcher(
        allowlist=Allowlist.from_settings(settings),
        client=client,
        clock=clock,
        sleep_fn=AdvancingSleep(clock),
        min_interval_seconds=settings.fetch_min_interval_seconds,
        timeout_seconds=settings.fetch_timeout_seconds,
    )
    blob_store = LocalBlobStore(tmp_path / "blobs")
    factory = FakeSessionFactory()
    connector = URLConnector(
        session_factory=factory,
        fetcher=fetcher,
        normalizer=Normalizer(),
        blob_store=blob_store,
    )
    return connector, factory, transport, blob_store


# --- URL pipeline -----------------------------------------------------------


async def test_collect_writes_source_row_with_metadata_and_passages(
    tmp_path,
    fake_settings,
) -> None:
    """Test 12: source metadata + unique (source_id, seq) passage rows."""
    connector, factory, transport, _ = make_url_collector(tmp_path, fake_settings)
    transport.respond(
        "https://retail.example.com/report",
        sample_long_html_bytes(),
        "text/html; charset=utf-8",
    )
    run_id = uuid4()
    source = await connector.collect(
        "https://retail.example.com/report",
        run_id=run_id,
        title="Retail Report",
    )
    assert source is not None
    assert source.uri == "https://retail.example.com/report"
    assert source.title == "Retail Report"
    assert source.source_type == "web"
    assert source.fetched_at is not None
    assert source.allowlisted_uri is True
    assert len(source.content_hash) == 64
    assert source.raw_ref == source.content_hash
    assert source.status == "normalized"

    passages = sorted(rows_of(factory.storage, Passage), key=lambda p: p.seq)
    assert len(passages) >= 2
    assert [p.seq for p in passages] == list(range(len(passages)))
    assert len({(p.source_id, p.seq) for p in passages}) == len(passages)
    assert all(p.source_id == source.id for p in passages)


async def test_same_url_twice_is_idempotent_one_source_row(tmp_path, fake_settings) -> None:
    """Test 3: re-collection dedupes on content_hash — one Source row."""
    connector, factory, transport, _ = make_url_collector(tmp_path, fake_settings)
    transport.respond(
        "https://retail.example.com/report",
        sample_html_bytes(),
        "text/html; charset=utf-8",
    )
    run_id = uuid4()
    first = await connector.collect("https://retail.example.com/report", run_id=run_id)
    second = await connector.collect("https://retail.example.com/report", run_id=run_id)
    assert first is not None and second is not None
    assert first.id == second.id
    assert len(rows_of(factory.storage, Source)) == 1
    assert len(rows_of(factory.storage, Passage)) == 1


async def test_same_content_different_urls_dedupes_to_one_source_row(
    tmp_path,
    fake_settings,
) -> None:
    """Test 4: content-hash dedupe across URLs."""
    connector, factory, transport, _ = make_url_collector(tmp_path, fake_settings)
    payload = sample_html_bytes()
    transport.respond("https://retail.example.com/a", payload, "text/html; charset=utf-8")
    transport.respond("https://retail.example.com/b", payload, "text/html; charset=utf-8")
    run_id = uuid4()
    await connector.collect("https://retail.example.com/a", run_id=run_id)
    await connector.collect("https://retail.example.com/b", run_id=run_id)
    sources = rows_of(factory.storage, Source)
    assert len(sources) == 1
    assert len(rows_of(factory.storage, Passage)) == 1


async def test_pdf_fixture_produces_normalized_passage_rows(tmp_path, fake_settings) -> None:
    """Test 5: sample PDF -> normalized text -> passages with seq/char/hash."""
    connector, factory, transport, _ = make_url_collector(tmp_path, fake_settings)
    transport.respond(
        "https://retail.example.com/report.pdf",
        sample_pdf_bytes(),
        "application/pdf",
    )
    run_id = uuid4()
    source = await connector.collect(
        "https://retail.example.com/report.pdf",
        run_id=run_id,
        title="PDF Report",
        source_type="pdf",
    )
    assert source is not None
    assert source.source_type == "pdf"
    assert source.status == "normalized"
    passages = sorted(rows_of(factory.storage, Passage), key=lambda p: p.seq)
    assert len(passages) >= 1
    assert passages[0].seq == 0
    assert passages[0].source_id == source.id
    assert passages[0].start_char == 0
    assert passages[0].end_char is not None and passages[0].end_char > 0
    assert len(passages[0].hash) == 64
    assert "Hello ECRKE PDF" in passages[0].text


async def test_non_allowlisted_uri_refused_end_to_end(tmp_path, fake_settings) -> None:
    connector, factory, transport, _ = make_url_collector(tmp_path, fake_settings)
    with pytest.raises(Exception) as exc_info:
        await connector.collect("https://evil.example.net/phish", run_id=uuid4())
    assert type(exc_info.value).__name__ == "AllowlistDeniedError"
    assert transport.calls == []
    assert rows_of(factory.storage, Source) == []


# --- G-04 / G-05 guardrails -------------------------------------------------


async def test_unsafe_content_is_quarantined_and_not_amplified(tmp_path, fake_settings) -> None:
    """Test 9: G-04 unsafe content -> Source.status='quarantined', no passages/blobs."""
    connector, factory, transport, blob_store = make_url_collector(tmp_path, fake_settings)
    unsafe_html = (
        b"<html><body><p>Retail update that contains bomb-making instructions "
        b"must never be amplified.</p></body></html>"
    )
    transport.respond(
        "https://retail.example.com/unsafe",
        unsafe_html,
        "text/html; charset=utf-8",
    )
    run_id = uuid4()
    source = await connector.collect("https://retail.example.com/unsafe", run_id=run_id)
    assert source is not None
    assert source.status == "quarantined"
    assert rows_of(factory.storage, Passage) == []
    assert list(blob_store.root.iterdir()) == []


async def test_fake_secret_redacted_before_persist(tmp_path, fake_settings, caplog) -> None:
    """Test 10: G-05 + Rule 01 — no secret value in any row, blob, or log."""
    leaked_value = "sk-live-abcdefghijklmnop"
    html = (
        f"<html><body><p>Retail news with leaked key {leaked_value} inside "
        f"the report body.</p></body></html>"
    ).encode()
    connector, factory, transport, blob_store = make_url_collector(tmp_path, fake_settings)
    transport.respond(
        "https://retail.example.com/leak",
        html,
        "text/html; charset=utf-8",
    )
    run_id = uuid4()
    source = await connector.collect("https://retail.example.com/leak", run_id=run_id)
    assert source is not None
    assert source.status == "normalized"

    for obj in factory.storage.values():
        dumped = "\n".join(str(value) for value in vars(obj).values() if isinstance(value, str))
        assert leaked_value not in dumped

    for blob_path in blob_store.root.iterdir():
        raw = blob_path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.hex()
        assert leaked_value not in text

    assert leaked_value not in caplog.text


# --- Search connector -------------------------------------------------------


async def test_search_connector_mock_provider_returns_candidates(fake_settings) -> None:
    """Test 7: provider-agnostic — an injected search backend drives results."""

    async def fake_search(query: str, limit: int) -> list[str]:
        return [f"https://retail.example.com/result-{i}" for i in range(limit)]

    connector = SearchConnector.from_settings(fake_settings, search_fn=fake_search)
    uris = await connector.search("retail margins")
    assert uris == [
        "https://retail.example.com/result-0",
        "https://retail.example.com/result-1",
        "https://retail.example.com/result-2",
        "https://retail.example.com/result-3",
        "https://retail.example.com/result-4",
    ]


def test_search_connector_unknown_provider_is_clear_config_error() -> None:
    settings = Settings(search_provider="not-a-real-provider", search_results_limit=5)
    with pytest.raises(SearchProviderError, match="SEARCH_PROVIDER"):
        SearchConnector.from_settings(settings)


def test_search_connector_missing_provider_is_clear_config_error() -> None:
    settings = Settings(search_provider="", search_results_limit=5)
    with pytest.raises(SearchProviderError, match="SEARCH_PROVIDER"):
        SearchConnector.from_settings(settings)


# --- RSS connector ----------------------------------------------------------


def test_rss_connector_parses_rss_candidates() -> None:
    """Test 8: RSS 2.0 item links become candidate URLs."""
    connector = RSSConnector()
    uris = connector.fetch_candidates(
        sample_rss_xml(),
        "https://retail.example.com/rss",
    )
    assert uris == ["https://retail.example.com/news/margins"]


def test_rss_connector_parses_atom_candidates() -> None:
    connector = RSSConnector()
    uris = connector.fetch_candidates(
        sample_atom_xml(),
        "https://retailtech.example.com/feed",
    )
    assert uris == ["https://retailtech.example.com/posts/pos"]


def test_rss_connector_rejects_malformed_xml() -> None:
    connector = RSSConnector()
    with pytest.raises(RSSParseError):
        connector.fetch_candidates(b"<not xml", "https://retail.example.com/rss")


def test_rss_connector_rejects_oversized_feed_before_parsing() -> None:
    """Length-cap guard: an oversized feed is refused before XML parsing."""
    connector = RSSConnector()
    oversized = b"<rss>" + b"x" * (5 * 1024 * 1024) + b"</rss>"
    with pytest.raises(RSSParseError, match="cap"):
        connector.fetch_candidates(oversized, "https://retail.example.com/rss")


# --- Rule 01: env var NAMES only --------------------------------------------


def test_app_source_references_env_names_not_values() -> None:
    """Test 13: app code references env var NAMES only — never values."""
    leaked_value = "sk-live-abcdefghijklmnop"
    repo_root = Path(__file__).resolve().parent.parent
    for path in sorted((repo_root / "app").rglob("*.py")):
        content = path.read_text(encoding="utf-8")
        assert leaked_value not in content, f"secret value leaked into {path}"

    env_text = (repo_root / ".env.example").read_text(encoding="utf-8")
    for name in (
        "ALLOWED_DOMAINS",
        "SEARCH_API_KEY",
        "SEARCH_PROVIDER",
        "BLOB_STORE_BACKEND",
        "BLOB_STORE_DIR",
    ):
        assert name in env_text, f"{name} missing from .env.example"
