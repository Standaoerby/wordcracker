# build_index.py
# 🌐 Строим поисковый индекс с метаданными для ChromaDB

from pathlib import Path
from tqdm import tqdm
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
import hashlib
import re

CORPUS_PATH = Path("/workspace/data")
DB_PATH = "/workspace/chroma_db"

# 📁 Инициализация ChromaDB
chroma_client = chromadb.PersistentClient(path=DB_PATH)
embedding_fn = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

collection = chroma_client.get_or_create_collection(
    name="gutenberg-index",
    embedding_function=embedding_fn,
    metadata={"creator": "wordcracker"}
)

def extract_metadata(path: Path):
    """Выделить автора, название, язык и ID книги по имени файла"""
    parts = path.parts[-2:]
    author = parts[0]
    title = path.stem
    file_hash = hashlib.md5(path.read_bytes()).hexdigest()[:12]
    return {
        "author": author,
        "title": title,
        "hash": file_hash,
        "filepath": str(path)
    }

def chunk_text(text: str, max_words=200):
    words = text.split()
    for i in range(0, len(words), max_words):
        chunk = words[i:i + max_words]
        yield " ".join(chunk)

print("♻️ Индексация файлов...")

for txt_file in tqdm(list(CORPUS_PATH.rglob("*.txt"))):
    text = txt_file.read_text(encoding="utf-8", errors="ignore")
    metadata = extract_metadata(txt_file)

    for i, chunk in enumerate(chunk_text(text)):
        uid = f"{metadata['hash']}_{i}"
        collection.add(
            documents=[chunk],
            ids=[uid],
            metadatas=[{
                "author": metadata["author"],
                "title": metadata["title"],
                "filepath": metadata["filepath"],
                "chunk": i
            }]
        )

print(f"✅ Индексация завершена. Всего документов: {collection.count()}")
