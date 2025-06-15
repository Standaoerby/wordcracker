import os
from pathlib import Path
from ebooklib import epub
from bs4 import BeautifulSoup
from tqdm import tqdm

# Входные и выходные папки внутри контейнера (монтируются из хоста)
EPUB_DIR = Path("/books")
OUTPUT_DIR = Path("/clean_books")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def extract_text_from_epub(epub_path: Path) -> str:
    try:
        book = epub.read_epub(str(epub_path))
        text = []
        for item in book.get_items():
            if item.get_type() == epub.ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), 'html.parser')
                text.append(soup.get_text())
        return '\n'.join(text)
    except Exception as e:
        print(f"❌ Ошибка чтения {epub_path.name}: {e}")
        return ""

def clean_text(raw: str) -> str:
    lines = raw.splitlines()
    cleaned = [line.strip() for line in lines if line.strip() and not line.lower().startswith("project gutenberg")]
    return '\n'.join(cleaned)

def convert_all():
    for epub_file in tqdm(EPUB_DIR.glob("*.epub"), desc="🧼 Очистка EPUB"):
        try:
            text = extract_text_from_epub(epub_file)
            if not text:
                continue

            clean = clean_text(text)
            out_name = epub_file.stem + ".txt"
            out_path = OUTPUT_DIR / out_name
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(clean)
            print(f"✅ Сохранили: {out_name}")
        except Exception as e:
            print(f"❌ Ошибка с файлом {epub_file.name}: {e}")

if __name__ == "__main__":
    convert_all()
