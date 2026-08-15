"""Deterministic content normalization, chunking, hashing, and guardrail hooks.

task_005 normalization layer:

- ``normalize`` — HTML/PDF/DOCX/RTF bytes -> clean text (bs4 + lxml, pypdf,
  python-docx, striprtf; all already declared in pyproject.toml).
- ``chunk_passages`` — paragraph-aware 500-1200 char chunks that are exact
  substrings of the normalized text (retrieval units for later stages).
- ``content_hash`` — sha256 hex of raw bytes (dedupe + blob addressing).
- ``classify_source`` — content-type first, URI suffix fallback.
- ``contains_unsafe_content`` — G-04 deterministic unsafe-content filter.
- ``redact_secrets`` / ``redact_bytes_for_storage`` — G-05 redaction hook.

G-05 note: text-like bytes are redacted *before* hashing/blob persistence;
binary formats (PDF/DOCX/RTF) pass through the blob layer intact and are
redacted on the normalized text layer instead, so binary artifacts are
preserved for downstream extraction.
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Callable
from dataclasses import dataclass

from app.db.enums import SourceType

_CHUNK_MIN_CHARS = 500
_CHUNK_MAX_CHARS = 1200

_UNSAFE_TERMS: tuple[str, ...] = ("bomb-making",)

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bgh[pousr]_[0-9A-Za-z]{20,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_API_KEY]"),
    (
        re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*[^\s,;]+"),
        "[REDACTED]",
    ),
)


@dataclass(frozen=True)
class Chunk:
    """One retrieval unit — an exact substring of the normalized text."""

    text: str
    start_char: int
    end_char: int


def _collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace; cap newlines at paragraph breaks."""
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _normalize_html(content: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    blocks: list[str] = []
    for element in soup.find_all(["p", "h1", "h2", "h3", "h4", "li", "blockquote", "pre"]):
        text = _collapse_whitespace(element.get_text(" ", strip=True))
        if text:
            blocks.append(text)
    if not blocks:
        text = _collapse_whitespace(soup.get_text(" ", strip=True))
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)


def _normalize_pdf(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    pages = [page.extract_text() or "" for page in reader.pages]
    return _collapse_whitespace("\n\n".join(pages))


def _normalize_docx(content: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(content))
    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    return _collapse_whitespace("\n\n".join(paragraphs))


def _normalize_rtf(content: bytes) -> str:
    from striprtf.striprtf import rtf_to_text

    try:
        text = rtf_to_text(content.decode("ascii", errors="ignore"))
    except Exception as exc:
        raise ValueError(f"could not parse RTF content: {exc}") from exc
    return _collapse_whitespace(text)


_NORMALIZERS: dict[str, Callable[[bytes], str]] = {
    "web": _normalize_html,
    "pdf": _normalize_pdf,
    "docx": _normalize_docx,
    "rtf": _normalize_rtf,
}


def normalize(source_type: str | SourceType, content: bytes) -> str:
    """Normalize raw source bytes to clean, redacted text (G-05 applied)."""
    key = source_type.value if isinstance(source_type, SourceType) else source_type
    handler = _NORMALIZERS.get(key)
    if handler is None:
        raise ValueError(
            f"unsupported source_type {key!r} (supported: {', '.join(sorted(_NORMALIZERS))})"
        )
    return redact_secrets(handler(content))


class Normalizer:
    """Facade over the deterministic normalization functions used by collectors."""

    def normalize(self, source_type: str | SourceType, content: bytes) -> str:
        return normalize(source_type, content)

    def chunk_passages(
        self,
        text: str,
        min_chars: int = _CHUNK_MIN_CHARS,
        max_chars: int = _CHUNK_MAX_CHARS,
    ) -> list[Chunk]:
        return chunk_passages(text, min_chars, max_chars)


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """Return contiguous (start, end) spans covering ``text`` at ``\\n\\n`` boundaries.

    Every span except the last includes its trailing ``\\n\\n`` separator so the
    spans tile the text exactly (substring invariant for chunks).
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in re.finditer("\n\n", text):
        spans.append((cursor, match.end()))
        cursor = match.end()
    if cursor < len(text):
        spans.append((cursor, len(text)))
    return spans


def _spans_length(spans: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in spans)


def _flush_spans(spans: list[tuple[int, int]], text: str) -> Chunk:
    start = spans[0][0]
    end = spans[-1][1]
    return Chunk(text=text[start:end], start_char=start, end_char=end)


def _hard_split(text: str, start: int, end: int, max_chars: int) -> list[Chunk]:
    """Split an oversized paragraph at word boundaries; pieces are exact substrings."""
    pieces: list[Chunk] = []
    cursor = start
    while end - cursor > max_chars:
        candidate = cursor + max_chars
        cut = text.rfind(" ", cursor + 1, candidate + 1)
        boundary = cut if cut != -1 else candidate
        pieces.append(Chunk(text=text[cursor:boundary], start_char=cursor, end_char=boundary))
        cursor = boundary
    pieces.append(Chunk(text=text[cursor:end], start_char=cursor, end_char=end))
    return pieces


def chunk_passages(
    text: str,
    min_chars: int = _CHUNK_MIN_CHARS,
    max_chars: int = _CHUNK_MAX_CHARS,
) -> list[Chunk]:
    """Split normalized text into paragraph-aware chunks within [min_chars, max_chars].

    Chunks are exact substrings of ``text`` (``text[start:end] == chunk.text``)
    and cover it contiguously, so downstream stages can trace each passage back
    to its source byte range.
    """
    chunks: list[Chunk] = []
    current: list[tuple[int, int]] = []
    for span_start, span_end in _paragraph_spans(text):
        paragraph_len = span_end - span_start
        if paragraph_len > max_chars:
            if current:
                chunks.append(_flush_spans(current, text))
                current = []
            chunks.extend(_hard_split(text, span_start, span_end, max_chars))
            continue
        if current and _spans_length(current) + paragraph_len > max_chars:
            chunks.append(_flush_spans(current, text))
            current = []
        current.append((span_start, span_end))
        if _spans_length(current) >= min_chars:
            chunks.append(_flush_spans(current, text))
            current = []
    if current:
        chunks.append(_flush_spans(current, text))
    return chunks


def content_hash(content: bytes) -> str:
    """sha256 hex digest used for dedupe and content-addressed blob refs."""
    return hashlib.sha256(content).hexdigest()


_CONTENT_TYPE_MAP: tuple[tuple[str, SourceType], ...] = (
    ("text/html", SourceType.WEB),
    ("application/xhtml", SourceType.WEB),
    ("application/pdf", SourceType.PDF),
    ("application/rtf", SourceType.RTF),
    ("text/rtf", SourceType.RTF),
    (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        SourceType.DOCX,
    ),
)


def classify_source(content_type: str, uri: str) -> SourceType:
    """Map a response content-type (or URI suffix) to a :class:`SourceType`."""
    lowered = (content_type or "").lower()
    for needle, source_type in _CONTENT_TYPE_MAP:
        if needle in lowered:
            return source_type
    path = uri.split("?", 1)[0].rsplit("#", 1)[0].lower()
    if path.endswith(".pdf"):
        return SourceType.PDF
    if path.endswith(".docx"):
        return SourceType.DOCX
    if path.endswith(".rtf"):
        return SourceType.RTF
    if path.endswith((".rss", ".atom", ".xml")):
        return SourceType.RSS
    return SourceType.OTHER


def contains_unsafe_content(text: str) -> bool:
    """G-04 deterministic filter: unsafe terms flag a source for quarantine."""
    lowered = text.lower()
    return any(term in lowered for term in _UNSAFE_TERMS)


def redact_secrets(text: str) -> str:
    """G-05: replace secret-looking substrings (API keys, tokens, passwords)."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_bytes_for_storage(content: bytes) -> bytes:
    """Redact text-like bytes before persistence; binary formats pass through.

    Binary (PDF/DOCX/RTF) artifacts keep their original bytes in the blob store;
    their text layer is redacted by :func:`normalize` instead.
    """
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    return redact_secrets(decoded).encode("utf-8")
