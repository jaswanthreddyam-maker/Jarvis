from __future__ import annotations

import argparse
from pathlib import Path

from pypdf import PdfReader


def extract_pdf(source: Path, destination: Path) -> None:
    reader = PdfReader(str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)

    chunks: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        chunks.append(f"--- Page {index} ---\n{text.strip()}\n")

    destination.write_text("\n".join(chunks), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract text from a PDF into a plain text file."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()

    extract_pdf(args.source, args.destination)


if __name__ == "__main__":
    main()
