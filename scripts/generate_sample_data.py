"""Generate ``sample_data/ecrke_seed_report.pdf`` (build-plan Step 14).

Builds a minimal valid PDF from scratch — no third-party PDF library — with
proper ``/ToUnicode`` CMaps on every font so text extractors can recover the
real UTF-8 text. Reproducible: the output is deterministic byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

PAGE_WIDTH = 612
PAGE_HEIGHT = 792
MARGIN = 72

SAMPLE_DATA_DIR = Path(__file__).resolve().parent.parent / "sample_data"
OUTPUT = SAMPLE_DATA_DIR / "ecrke_seed_report.pdf"

TITLE = "ECRKE Seed Research Report"
SUBTITLE = "Hermetic seed run - all content is fictional research material."

BODY = (
    "Retailers report stronger same-store sales growth as e-commerce expands "
    "its share of total retail spending. AI demand forecasting lowers retail "
    "inventory carrying costs by improving demand prediction accuracy. "
    "Personalized product recommendations boost retail conversion rates "
    "without degrading customer trust. The seed run generates this report "
    "from two allowlisted sources with a deterministic pipeline."
)

TABLE_HEADER = ("Metric", "Value", "Source domain")
TABLE_ROWS = (
    ("Decomposition coverage", "1.0", "retail.example.com"),
    ("Support ratio", "1.0", "retail.example.com"),
    ("Traceability", "1.0", "retail.example.com"),
    ("Contradiction recall", "1.0", "gold set"),
)


def _cmap_object(font_num: int, first_code: int = 0x20, last_code: int = 0x7E) -> list[str]:
    """Return the PDF objects for a font's /ToUnicode CMap."""
    entries = [f"<{code:02X}> <{code:04X}>" for code in range(first_code, last_code + 1)]
    stream_lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<00> <FF>",
        "endcodespacerange",
        f"{len(entries)} beginbfchar",
        *entries,
        "endbfchar",
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    stream = ("\n".join(stream_lines) + "\n").encode("ascii")
    return [
        f"{font_num} 0 obj",
        f"<< /Length {len(stream)} >>",
        "stream",
        stream.decode("ascii"),
        "endstream",
        "endobj",
    ]


def _font_object(font_num: int, cmap_num: int, bold: bool) -> list[str]:
    """Return the PDF objects for a Helvetica font with its ToUnicode CMap."""
    name = "Helvetica-Bold" if bold else "Helvetica"
    return [
        f"{font_num} 0 obj",
        f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} "
        f"/Encoding /WinAnsiEncoding /ToUnicode {cmap_num} 0 R >>",
        "endobj",
    ]


def _text_line(text: str, size: int, x: int, y: int, font: str = "F1") -> str:
    return f"BT /{font} {size} Tf {x} {y} Td ({text}) Tj ET"


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, width_chars: int = 86) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _page_object(
    page_num: int, pages_num: int, content_num: int, fonts: dict[str, int]
) -> list[str]:
    font_resources = " ".join(f"/{name} {num} 0 R" for name, num in sorted(fonts.items()))
    return [
        f"{page_num} 0 obj",
        f"<< /Type /Page /Parent {pages_num} 0 R "
        f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
        f"/Resources << /Font << {font_resources} >> >> "
        f"/Contents {content_num} 0 R >>",
        "endobj",
    ]


def _content_stream_object(content_num: int, operators: list[str]) -> list[str]:
    data = "\n".join(operators).encode("ascii")
    return [
        f"{content_num} 0 obj",
        f"<< /Length {len(data)} >>",
        "stream",
        data.decode("ascii"),
        "endstream",
        "endobj",
    ]


def _build_pdf() -> bytes:
    # Object numbering plan.
    catalog_num = 1
    pages_num = 2
    page_nums = [3, 4, 5]
    font_f1_num = 6  # Helvetica (body, table)
    font_f2_num = 7  # Helvetica-Bold (title, table header)
    cmap_f1_num = 8
    cmap_f2_num = 9
    content_nums = [10, 11, 12]

    fonts = {"F1": font_f1_num, "F2": font_f2_num}

    # Page 1 — title page.
    page1: list[str] = [
        _text_line(_escape_pdf_text(TITLE), 24, MARGIN, 700, "F2"),
        _text_line(_escape_pdf_text(SUBTITLE), 11, MARGIN, 676, "F1"),
        _text_line("Contents", 14, MARGIN, 620, "F2"),
        _text_line("1. Summary findings", 12, MARGIN + 20, 596, "F1"),
        _text_line("2. Evidence table", 12, MARGIN + 20, 578, "F1"),
    ]

    # Page 2 — summary findings paragraph.
    body_lines = _wrap(BODY)
    page2: list[str] = [_text_line("1. Summary findings", 16, MARGIN, 700, "F2")]
    y = 668
    for line in body_lines:
        page2.append(_text_line(_escape_pdf_text(line), 12, MARGIN, y, "F1"))
        y -= 18

    # Page 3 — evidence table.
    page3: list[str] = [
        _text_line("2. Evidence table", 16, MARGIN, 700, "F2"),
        f"1 0 0 1 {MARGIN} 640 cm",
    ]
    col_widths = [220, 90, 158]
    row_height = 22
    x_offsets = [0, col_widths[0], col_widths[0] + col_widths[1]]
    table_y = 0
    # Header row (bold, shaded background).
    for col, header in enumerate(TABLE_HEADER):
        page3.append(
            f"0.90 0.92 0.95 rg {x_offsets[col]} {table_y} {col_widths[col]} {row_height} re f"
        )
        page3.append(
            _text_line(_escape_pdf_text(header), 11, x_offsets[col] + 6, table_y + 7, "F2")
        )
    table_y -= row_height
    for row in TABLE_ROWS:
        for col, value in enumerate(row):
            page3.append(
                _text_line(_escape_pdf_text(value), 11, x_offsets[col] + 6, table_y + 7, "F1")
            )
        table_y -= row_height
    # Grid lines.
    grid_top = 0
    grid_bottom = table_y + row_height
    for x in x_offsets + [sum(col_widths)]:
        page3.append(f"0.6 0.6 0.6 RG {x} {grid_bottom} m {x} {grid_top} l S")
    for yy in range(grid_bottom, grid_top + 1, row_height):
        page3.append(f"0.6 0.6 0.6 RG 0 {yy} m {sum(col_widths)} {yy} l S")

    objects: list[list[str]] = [
        # 1: catalog
        [
            f"{catalog_num} 0 obj",
            f"<< /Type /Catalog /Pages {pages_num} 0 R >>",
            "endobj",
        ],
        # 2: pages
        [
            f"{pages_num} 0 obj",
            "<< /Type /Pages /Kids ["
            + " ".join(f"{n} 0 R" for n in page_nums)
            + f"] /Count {len(page_nums)} >>",
            "endobj",
        ],
    ]
    for page_num in page_nums:
        content_num = content_nums[page_nums.index(page_num)]
        objects.append(_page_object(page_num, pages_num, content_num, fonts))
    objects.append(_font_object(font_f1_num, cmap_f1_num, bold=False))
    objects.append(_font_object(font_f2_num, cmap_f2_num, bold=True))
    objects.append(_cmap_object(cmap_f1_num))
    objects.append(_cmap_object(cmap_f2_num))
    objects.append(_content_stream_object(content_nums[0], page1))
    objects.append(_content_stream_object(content_nums[1], page2))
    objects.append(_content_stream_object(content_nums[2], page3))

    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets: list[int] = [0]
    body: list[bytes] = [header]
    for obj in objects:
        offsets.append(len(b"".join(body)))
        body.append(("\n".join(obj) + "\n").encode("ascii"))
    xref_offset = len(b"".join(body))
    xref = [f"xref\n0 {len(objects) + 1}\n", "0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref.append(f"{offset:010d} 00000 n \n")
    trailer = [
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_num} 0 R >>",
        "startxref",
        str(xref_offset),
        "%%EOF",
    ]
    return (
        b"".join(body) + "".join(xref).encode("ascii") + "\n".join(trailer).encode("ascii") + b"\n"
    )


def main() -> None:
    data = _build_pdf()
    SAMPLE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(data)
    print(f"wrote {OUTPUT} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
