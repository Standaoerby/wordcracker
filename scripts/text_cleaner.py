# scripts/text_cleaner.py

import os
import re
import unicodedata
from pathlib import Path
from bs4 import BeautifulSoup
from ebooklib import epub
import ftfy
import argparse


def clean_text(text: str) -> str:
    text = ftfy.fix_text(text)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace('\r', '').replace('\xa0', ' ')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def extract_epub_text(epub_path: Path) -> str:
    book = epub.read_epub(str(epub_path))
    text = ""
    for item in book.get_items():
        if item.get_type() == epub.EpubHtml:
            soup = BeautifulSoup(item.get_content(), 'html.parser')
            text += soup.get_text(separator=' ', strip=True) + "\n"
    return text


def process_file(input_path: Path, output_path: Path):
    ext = input_path.suffix.lower()
    if ext == ".txt":
        with open(input_path, encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    elif ext == ".epub":
        raw = extract_epub_text(input_path)
    else:
        print(f"❌ Unsupported file format: {input_path.name}")
        return

    cleaned = clean_text(raw)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(cleaned)

    try:
        rel = output_path.relative_to(Path.cwd())
    except ValueError:
        rel = output_path
    print(f"✅ Cleaned: {input_path.name} -> {rel}")


def batch_process(raw_dir: Path, out_dir: Path):
    for author_dir in raw_dir.iterdir():
        if author_dir.is_dir():
            for book_file in author_dir.glob("*.*"):
                rel_path = book_file.relative_to(raw_dir)
                out_path = out_dir / rel_path.with_suffix(".txt")
                process_file(book_file, out_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean and normalize raw books")
    parser.add_argument("--input", type=str, default="raw_books", help="Path to raw input books")
    parser.add_argument("--output", type=str, default="data", help="Output path for cleaned books")
    args = parser.parse_args()

    batch_process(Path(args.input), Path(args.output))
