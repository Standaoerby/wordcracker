# scripts/make_corpus.py
# 📦 Универсальный пайплайн: загрузка + очистка + статистика

import subprocess
import argparse
from pathlib import Path
import shutil
from collections import Counter


def run_script(script, *args):
    cmd = ["python", script] + list(args)
    print(f"\n🚀 Запуск: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def count_tokens(text_path: Path):
    with open(text_path, encoding='utf-8') as f:
        text = f.read()
    words = text.lower().split()
    total = len(words)
    freqs = Counter(words)
    rare = [w for w, c in freqs.items() if c == 1]
    midfreq = [w for w, c in freqs.items() if 3 <= c <= 10]
    print(f"\n📊 Статистика: {text_path.relative_to(Path.cwd())}")
    print(f"  👉 Всего слов: {total:,}")
    print(f"  🔹 Уникальных слов: {len(freqs):,}")
    print(f"  🧩 Редких (1 раз): {len(rare):,}")
    print(f"  ⚖️  Частота 3-10: {len(midfreq):,}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--author", type=str, required=True, help="Имя автора (пример: wodehouse)")
    args = parser.parse_args()

    author = args.author.lower().replace(" ", "_")
    raw_path = Path("raw_books") / author
    data_path = Path("data") / author

    # 1. Скачиваем книги
    run_script("scripts/gutenberg_import.py")

    # 2. Чистим их
    run_script("scripts/text_cleaner.py", "--input", "raw_books", "--output", "data")

    # 3. Показываем аналитику по каждой книге
    for book in data_path.glob("*.txt"):
        count_tokens(book)


if __name__ == "__main__":
    main()
