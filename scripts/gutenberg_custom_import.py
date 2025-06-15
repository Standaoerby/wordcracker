# scripts/gutenberg_import.py
# ⚙️ Скрипт для скачивания и сортировки книг из Project Gutenberg

import os
import requests
from pathlib import Path
from bs4 import BeautifulSoup
import time
import re

# Загрузим только английские книги
BASE_URL = "https://www.gutenberg.org"
BOOKSHELF_URL = BASE_URL + "/wiki/Category:Bookshelf"
OUTPUT_DIR = Path("raw_books")
TARGET_AUTHORS = ["wodehouse"]  # lowercase

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; GutenbergBot/1.0)'
}

def fetch_books_by_author(author_name):
    search_url = f"https://www.gutenberg.org/ebooks/search/?query={author_name.replace(' ', '+')}"
    r = requests.get(search_url, headers=HEADERS)
    soup = BeautifulSoup(r.text, 'html.parser')
    books = []
    for link in soup.select("li.booklink a.link"):
        href = link.get("href")
        if href and href.startswith("/ebooks/"):
            book_id = href.split("/")[-1]
            books.append(book_id)
    return books

def download_book(book_id, author_folder):
    book_url = f"https://www.gutenberg.org/ebooks/{book_id}"
    r = requests.get(book_url, headers=HEADERS)
    soup = BeautifulSoup(r.text, "html.parser")

    links = soup.find_all("a", href=True)
    for link in links:
        href = link["href"]
        if href.endswith(".epub.noimages") or href.endswith(".epub.images") or href.endswith(".epub"):
            if href.startswith("/"):
                href = BASE_URL + href
            filename = f"{book_id}.epub"
            out_path = author_folder / filename
            if out_path.exists():
                return
            print(f"📥 {filename}")
            file = requests.get(href, headers=HEADERS)
            with open(out_path, "wb") as f:
                f.write(file.content)
            time.sleep(1)
            return

    print(f"⚠️  No EPUB found for book {book_id}")

def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    for author in TARGET_AUTHORS:
        author_folder = OUTPUT_DIR / author.lower().replace(" ", "_")
        author_folder.mkdir(parents=True, exist_ok=True)
        books = fetch_books_by_author(author)
        print(f"🔍 Found {len(books)} books for {author}")
        for book_id in books:
            try:
                download_book(book_id, author_folder)
            except Exception as e:
                print(f"❌ Error downloading {book_id}: {e}")

if __name__ == "__main__":
    main()
