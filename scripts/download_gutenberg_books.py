import os
import requests
from tqdm import tqdm
import re
import time
import random
from pathlib import Path

# Куда сохраняем книги
SAVE_PATH = "/media/standa/books"
LOG_PATH = os.path.join(SAVE_PATH, "download_log.txt")
os.makedirs(SAVE_PATH, exist_ok=True)

# Базовый URL API Gutendex (англоязычные книги)
BASE_API = "https://gutendex.com/books?languages=en"

def sanitize_filename(title: str):
    """Удаляет запрещённые символы из названия файла"""
    return re.sub(r'[\\/*?:"<>|]', "_", title)

def get_epub_url(formats: dict) -> str | None:
    """Ищет .epub ссылку (сначала без изображений, потом любую)"""
    for k, v in formats.items():
        if k == "application/epub+zip" and "images" not in k:
            return v
    for k, v in formats.items():
        if k == "application/epub+zip":
            return v
    return None

def was_downloaded(book_id: int) -> bool:
    """Проверяет, был ли уже скачан файл по логу"""
    if not os.path.exists(LOG_PATH):
        return False
    with open(LOG_PATH, "r", encoding="utf-8") as log:
        return str(book_id) in log.read()

def log_download(book_id: int, title: str):
    with open(LOG_PATH, "a", encoding="utf-8") as log:
        log.write(f"{book_id}\t{title}\n")

def download_book(book_id: int, title: str, url: str):
    """Скачивает и сохраняет файл, если он epub"""
    filename = f"{book_id} - {sanitize_filename(title)}.epub"
    full_path = os.path.join(SAVE_PATH, filename)
    print(f"\n⏳ Пытаемся сохранить: {full_path}")

    if os.path.exists(full_path) or was_downloaded(book_id):
        print(f"⏭ Пропущено (уже существует или было скачано)")
        return

    try:
        r = requests.get(url, timeout=20)
        print(f"📡 HTTP {r.status_code} — {url}")

        if r.status_code == 200 and r.content[:4] == b"PK\x03\x04":
            with open(full_path, "wb") as f:
                f.write(r.content)
            log_download(book_id, title)
            print(f"✅ Сохранили: {full_path}")
        else:
            print(f"⚠️ {book_id}: не EPUB-файл или плохой ответ")
    except Exception as e:
        print(f"❌ {book_id}: ошибка — {e}")

def main():
    print(f"📥 Скачиваем английские EPUB книги в: {SAVE_PATH}")
    next_url = BASE_API
    total = 0
    session = requests.Session()

    while next_url:
        try:
            r = session.get(next_url, timeout=30)
            r.raise_for_status()
            data = r.json()
            books = data.get("results", [])

            for book in tqdm(books, desc="⬇️ Загрузка книг"):
                book_id = book.get("id")
                title = book.get("title")
                formats = book.get("formats")
                if not book_id or not title or not formats:
                    continue

                print(f"\n📘 {book_id} — {title}")
                print("🔍 Форматы:", list(formats.keys()))

                url = get_epub_url(formats)
                if url:
                    print(f"✅ Найден .epub: {url}")
                    download_book(book_id, title, url)
                    total += 1
                    time.sleep(random.uniform(0.3, 1.5))
                else:
                    print("❌ EPUB-ссылка не найдена.")

            next_url = data.get("next")
            if not next_url:
                print("\n🏁 Достигнут конец каталога.")
        except Exception as e:
            print(f"❌ Ошибка при загрузке страницы: {e}")
            break

    print(f"\n✅ Готово. Загружено книг: {total}")

if __name__ == "__main__":
    main()
