"""Normalizer tests — HTML/PDF/DOCX/RTF → text, chunking, hashing, G-04/G-05 hooks.

Fixtures are generated at test time (pypdf / python-docx / striprtf) so the
suite stays hermetic: no files, no network, no Docker.
"""

import pytest

from app.services.normalizer import (
    chunk_passages,
    classify_source,
    contains_unsafe_content,
    content_hash,
    normalize,
    redact_secrets,
)
from tests.conftest import (
    sample_docx_bytes,
    sample_html_bytes,
    sample_long_html_bytes,
    sample_pdf_bytes,
    sample_rtf_bytes,
)


def test_normalize_html_strips_tags() -> None:
    text = normalize("web", sample_html_bytes())
    assert "Retail Report" in text
    assert "same-store sales growth" in text
    assert "<h1>" not in text
    assert "<p>" not in text
    assert "<html" not in text.lower()


def test_normalize_pdf_extracts_text() -> None:
    text = normalize("pdf", sample_pdf_bytes())
    assert "Hello ECRKE PDF" in text


def test_normalize_docx_extracts_text() -> None:
    text = normalize("docx", sample_docx_bytes())
    assert "Hello ECRKE DOCX" in text


def test_normalize_rtf_extracts_text() -> None:
    text = normalize("rtf", sample_rtf_bytes())
    assert "Hello ECRKE RTF" in text


def test_normalize_unknown_source_type_raises() -> None:
    with pytest.raises(ValueError, match="source_type"):
        normalize("banana", b"x")


def test_chunk_passages_cover_text_and_respect_bounds() -> None:
    """Paragraph-aware chunking: contiguous, exact substrings, 500–1200 chars.

    Middle chunks respect the minimum; only a trailing short chunk is allowed
    (the source text simply runs out).
    """
    text = normalize("web", sample_long_html_bytes())
    chunks = chunk_passages(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert text[chunk.start_char : chunk.end_char] == chunk.text
        assert len(chunk.text) <= 1200
    for chunk in chunks[:-1]:
        assert len(chunk.text) >= 500
    assert len(chunks[-1].text) > 0
    assert chunks[0].start_char == 0
    assert chunks[-1].end_char == len(text)
    for previous, next_chunk in zip(chunks, chunks[1:], strict=False):
        assert previous.end_char == next_chunk.start_char


def test_chunk_passages_single_short_paragraph_is_one_chunk() -> None:
    text = normalize("web", sample_html_bytes())
    chunks = chunk_passages(text)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].start_char == 0
    assert chunks[0].end_char == len(text)


def test_content_hash_is_sha256_hex() -> None:
    digest = content_hash(b"payload")
    assert len(digest) == 64
    assert digest == content_hash(b"payload")
    assert digest != content_hash(b"payload2")


def test_classify_source_by_content_type() -> None:
    assert (
        classify_source("text/html; charset=utf-8", "https://retail.example.com/x").value == "web"
    )
    assert classify_source("application/pdf", "https://retail.example.com/x").value == "pdf"
    assert classify_source("application/rtf", "https://retail.example.com/x").value == "rtf"
    assert (
        classify_source(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "https://retail.example.com/x",
        ).value
        == "docx"
    )


def test_classify_source_falls_back_to_uri_suffix() -> None:
    assert (
        classify_source("application/octet-stream", "https://retail.example.com/a.pdf").value
        == "pdf"
    )
    assert (
        classify_source("application/octet-stream", "https://retail.example.com/a.docx").value
        == "docx"
    )
    assert (
        classify_source("application/octet-stream", "https://retail.example.com/a.rtf").value
        == "rtf"
    )
    assert (
        classify_source("application/octet-stream", "https://retail.example.com/a.atom").value
        == "rss"
    )


def test_classify_source_unknown_is_other() -> None:
    assert classify_source("", "https://retail.example.com/a").value == "other"


def test_contains_unsafe_content_flags_unsafe_terms_case_insensitively() -> None:
    assert contains_unsafe_content("retail update with bomb-making instructions inside")
    assert contains_unsafe_content("BOMB-MAKING instructions are not allowed")
    assert not contains_unsafe_content("retail margins are improving this quarter")


def test_redact_secrets_replaces_fake_api_key() -> None:
    leaked_value = "sk-live-abcdefghijklmnop"
    redacted = redact_secrets(f"the key is {leaked_value} inside text")
    assert leaked_value not in redacted
    assert "[REDACTED" in redacted


def test_redact_secrets_leaves_plain_text_unchanged() -> None:
    text = "Retailers report stronger same-store sales growth."
    assert redact_secrets(text) == text
