"""Parse PDF files using PyMuPDF (fitz)."""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF


def extract_text(pdf_path: str | Path) -> str:
    """Extract all text from a PDF file."""
    doc = fitz.open(pdf_path)
    text_parts = []
    for page in doc:
        text_parts.append(page.get_text())
    doc.close()
    return "\n".join(text_parts)


def extract_text_by_page(pdf_path: str | Path) -> list[str]:
    """Extract text page by page, returning a list of page texts."""
    doc = fitz.open(pdf_path)
    pages = [page.get_text() for page in doc]
    doc.close()
    return pages


def get_metadata(pdf_path: str | Path) -> dict[str, str]:
    """Get PDF metadata (title, author, etc.)."""
    doc = fitz.open(pdf_path)
    meta = doc.metadata
    doc.close()
    return dict(meta)


def get_page_count(pdf_path: str | Path) -> int:
    """Get the number of pages in the PDF."""
    doc = fitz.open(pdf_path)
    count = len(doc)
    doc.close()
    return count


def extract_blocks(pdf_path: str | Path, page_num: int = 0) -> list[dict]:
    """
    Extract text blocks (paragraphs) from a specific page.
    Each block has 'bbox', 'lines', 'type' etc.
    """
    doc = fitz.open(pdf_path)
    page = doc[page_num]
    blocks = page.get_text("dict")["blocks"]
    doc.close()
    return blocks


def main() -> None:
    import sys

    if len(sys.argv) < 2:
        print("Usage: python parse_pdf.py <path_to_pdf>")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}")
        sys.exit(1)

    print(f"=== Metadata ===\n{get_metadata(pdf_path)}")
    print(f"\n=== Page count: {get_page_count(pdf_path)} ===\n")
    print("=== Full text (first 2000 chars) ===\n")
    text = extract_text(pdf_path)
    print(text[:2000])


if __name__ == "__main__":
    main()
