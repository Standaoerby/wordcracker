#!/usr/bin/env python3
"""
RAG over the wordcracker ChromaDB index.

Retrieves top-K chunks via multilingual MiniLM, hands them to Ollama
(default: qwen3:14b) with an anti-hallucination prompt template,
returns the answer plus structured source citations.

Usage as module:
    from rag_query import rag_query
    r = rag_query("найди упоминания битой посуды в книгах")
    print(r["answer"])
    for s in r["sources"]:
        print(s["author"], s["title"], s["pg_id"])

Usage as CLI:
    python rag_query.py "вопрос или question" [--k 8] [--model qwen3:14b] [--stream]
"""
import argparse
import json
import re
import sys
import time
from typing import Iterable

import requests

DEFAULT_CHROMA_PATH    = "/workspace/chroma_db"
DEFAULT_COLLECTION     = "gutenberg-index"
DEFAULT_EMBEDDER       = "paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_OLLAMA_HOST    = "http://ollama:11434"
DEFAULT_MODEL          = "qwen3:14b"
DEFAULT_K              = 8
DEFAULT_TEMPERATURE    = 0.5
DEFAULT_SNIPPET_CHARS  = 900

# keep_alive=0 -> unload model immediately after the response so the ~9 GB
# of VRAM frees up for embedding/indexing work running in parallel.
DEFAULT_KEEP_ALIVE     = 0

PROMPT_TEMPLATE = """Ты — литературный аналитик. Отвечаешь на вопрос на основе фрагментов из книг Project Gutenberg.

Правила:
1. Если в фрагментах есть ХОТЯ БЫ ЧТО-ТО релевантное вопросу — построй ответ на этом. Цитируй фрагменты прямо.
2. Только если фрагменты СОВСЕМ не пересекаются с темой — скажи «В найденных фрагментах упоминаний не обнаружено».
3. Не сочиняй фактов и цитат за пределами контекста. PG-id ссылки бери ТОЛЬКО из предоставленных фрагментов.
4. Каждое утверждение подкрепляй ссылкой [Автор, "Название", PG12345].
5. Отвечай на том же языке, на котором задан вопрос. Имена персонажей оставляй как в оригинале.

Структура ответа:
• Краткое summary (1–2 предложения)
• Конкретные упоминания: короткая цитата + источник
• Замеченный паттерн (если виден)

КОНТЕКСТ:
{context}

ВОПРОС: {question}

ОТВЕТ:"""


CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


def _has_cyrillic(text: str) -> bool:
    return bool(CYRILLIC_RE.search(text))


def _translate_to_english(question: str, model: str, ollama_host: str) -> str:
    """Use Ollama to translate a non-English query into English for retrieval.

    Multilingual MiniLM does not transliterate proper nouns (Дживс != Jeeves),
    so when the corpus is English we get better recall by embedding an English
    paraphrase of the query rather than the original Russian. The answer is
    still generated in the original language because rag_query passes the
    original question into the LLM prompt.
    """
    prompt = (
        "Translate the following question to English. "
        "Use canonical English forms for proper nouns and characters "
        "(e.g. Дживс → Jeeves, Берти Вустер → Bertie Wooster, "
        "Шерлок Холмс → Sherlock Holmes). "
        "Output ONLY the translation, no commentary.\n\n"
        f"Question: {question}\n\nEnglish:"
    )
    payload = {
        "model": model, "prompt": prompt, "stream": False, "keep_alive": 0,
        "options": {"temperature": 0}, "think": False,
    }
    try:
        resp = requests.post(f"{ollama_host}/api/generate", json=payload, timeout=60)
        resp.raise_for_status()
        return resp.json().get("response", "").strip().strip('"').strip()
    except Exception as e:
        print(f"[rag] translation failed, using original query: {e}", file=sys.stderr)
        return question


def _retrieve(question: str, k: int, chroma_path: str, collection_name: str,
              embedder_name: str) -> dict:
    """Run a ChromaDB query and return the raw response."""
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    client = chromadb.PersistentClient(path=chroma_path)
    embed_fn = SentenceTransformerEmbeddingFunction(model_name=embedder_name, device="cuda")
    col = client.get_collection(collection_name, embedding_function=embed_fn)
    return col.query(query_texts=[question], n_results=k)


def _format_context(query_result: dict, snippet_chars: int) -> tuple[str, list[dict]]:
    """Build the prompt context block and a parallel list of structured sources."""
    docs  = query_result["documents"][0]
    metas = query_result["metadatas"][0]
    dists = query_result["distances"][0]

    blocks, sources = [], []
    for i, (doc, md, dist) in enumerate(zip(docs, metas, dists), 1):
        snippet = doc[:snippet_chars].replace("\n", " ").strip()
        author = md.get("author") or "Unknown"
        title  = md.get("title")  or "Untitled"
        pg_id  = md.get("pg_id")  or ""
        blocks.append(f"[{i}] {author} — \"{title}\" [{pg_id}]\n{snippet}")
        sources.append({
            "author": author, "title": title, "pg_id": pg_id,
            "chunk": md.get("chunk"), "distance": round(float(dist), 4),
            "snippet": snippet,
        })
    return "\n\n".join(blocks), sources


def _ollama_generate(prompt: str, model: str, ollama_host: str, temperature: float,
                     keep_alive: int, stream: bool) -> Iterable[str] | str:
    """Call Ollama generate. Stream mode yields chunks; non-stream returns whole string."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": stream,
        "keep_alive": keep_alive,
        "options": {"temperature": temperature},
        "think": False,  # qwen3 reasoning mode off — answer-only
    }
    url = f"{ollama_host}/api/generate"

    if not stream:
        try:
            resp = requests.post(url, json=payload, timeout=300)
            resp.raise_for_status()
        except requests.exceptions.ReadTimeout:
            return "[ERROR] Ollama timeout (>300s) — модель не отвечает"
        except requests.exceptions.ConnectionError as e:
            return f"[ERROR] Ollama unreachable at {ollama_host}: {e}"
        data = resp.json()
        return data.get("response", ""), data.get("eval_count")

    def _gen():
        with requests.post(url, json=payload, stream=True, timeout=300) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "response" in obj:
                    yield obj["response"]
                if obj.get("done"):
                    break
    return _gen()


def _validate_citations(answer: str, sources: list[dict]) -> list[str]:
    """Find PG-ids cited in the answer that are NOT in the retrieved sources."""
    cited = set(re.findall(r"PG\d+", answer))
    retrieved = {s["pg_id"] for s in sources}
    return sorted(cited - retrieved)


def rag_query(
    question: str,
    k: int = DEFAULT_K,
    model: str = DEFAULT_MODEL,
    ollama_host: str = DEFAULT_OLLAMA_HOST,
    chroma_path: str = DEFAULT_CHROMA_PATH,
    collection_name: str = DEFAULT_COLLECTION,
    embedder_name: str = DEFAULT_EMBEDDER,
    temperature: float = DEFAULT_TEMPERATURE,
    snippet_chars: int = DEFAULT_SNIPPET_CHARS,
    keep_alive: int = DEFAULT_KEEP_ALIVE,
) -> dict:
    """One-shot RAG. Returns {"answer", "sources", "question", "model", "tokens", "warnings", "retrieval_query"}."""
    retrieval_query = question
    if _has_cyrillic(question):
        t_tr = time.time()
        retrieval_query = _translate_to_english(question, model, ollama_host)
        print(f"[rag] translated for retrieval ({time.time()-t_tr:.2f}s): {retrieval_query!r}",
              file=sys.stderr)

    t0 = time.time()
    r = _retrieve(retrieval_query, k, chroma_path, collection_name, embedder_name)
    t_retr = time.time() - t0
    context, sources = _format_context(r, snippet_chars)
    print(f"[rag] retrieval: {t_retr:.2f}s, k={k}", file=sys.stderr)

    t1 = time.time()
    prompt = PROMPT_TEMPLATE.format(context=context, question=question)
    answer, tokens = _ollama_generate(prompt, model, ollama_host, temperature, keep_alive, stream=False)
    t_gen = time.time() - t1
    print(f"[rag] generation: {t_gen:.2f}s, tokens={tokens}", file=sys.stderr)

    warnings = []
    bad = _validate_citations(answer, sources)
    if bad:
        warnings.append(f"hallucinated PG ids in answer: {bad}")
        print(f"[rag] WARNING: {warnings[-1]}", file=sys.stderr)

    return {
        "answer":           answer,
        "sources":          sources,
        "question":         question,
        "retrieval_query":  retrieval_query,
        "model":            model,
        "tokens":           tokens,
        "timing":           {"retrieval_s": round(t_retr, 2), "generation_s": round(t_gen, 2)},
        "warnings":         warnings,
    }


def rag_query_stream(question: str, **kwargs):
    """Same as rag_query but yields the answer in chunks. Sources/warnings printed at end."""
    k             = kwargs.get("k", DEFAULT_K)
    model         = kwargs.get("model", DEFAULT_MODEL)
    ollama_host   = kwargs.get("ollama_host",  DEFAULT_OLLAMA_HOST)
    chroma_path   = kwargs.get("chroma_path",  DEFAULT_CHROMA_PATH)
    collection    = kwargs.get("collection_name", DEFAULT_COLLECTION)
    embedder      = kwargs.get("embedder_name",   DEFAULT_EMBEDDER)
    temperature   = kwargs.get("temperature",  DEFAULT_TEMPERATURE)
    snippet_chars = kwargs.get("snippet_chars", DEFAULT_SNIPPET_CHARS)
    keep_alive    = kwargs.get("keep_alive",   DEFAULT_KEEP_ALIVE)

    t0 = time.time()
    r = _retrieve(question, k, chroma_path, collection, embedder)
    print(f"[rag] retrieval: {time.time()-t0:.2f}s, k={k}", file=sys.stderr)
    context, sources = _format_context(r, snippet_chars)
    prompt = PROMPT_TEMPLATE.format(context=context, question=question)

    answer_parts = []
    for piece in _ollama_generate(prompt, model, ollama_host, temperature, keep_alive, stream=True):
        answer_parts.append(piece)
        yield piece
    answer = "".join(answer_parts)
    bad = _validate_citations(answer, sources)
    yield {"_done": True, "sources": sources, "warnings":
           [f"hallucinated PG ids: {bad}"] if bad else []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--k",           type=int,   default=DEFAULT_K)
    ap.add_argument("--model",       default=DEFAULT_MODEL)
    ap.add_argument("--ollama-host", default=DEFAULT_OLLAMA_HOST)
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--stream",      action="store_true", help="stream tokens as they come")
    ap.add_argument("--json",        action="store_true", help="print full structured result as JSON")
    args = ap.parse_args()

    if args.stream:
        for piece in rag_query_stream(args.question, k=args.k, model=args.model,
                                      ollama_host=args.ollama_host, temperature=args.temperature):
            if isinstance(piece, dict):
                print("\n\n--- Sources ---", file=sys.stderr)
                for s in piece["sources"]:
                    print(f"  [{s['pg_id']}] {s['author']} — {s['title']}  (dist {s['distance']})",
                          file=sys.stderr)
                if piece["warnings"]:
                    for w in piece["warnings"]:
                        print(f"  ⚠ {w}", file=sys.stderr)
            else:
                sys.stdout.write(piece)
                sys.stdout.flush()
        sys.stdout.write("\n")
        return

    res = rag_query(args.question, k=args.k, model=args.model,
                    ollama_host=args.ollama_host, temperature=args.temperature)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return
    print(res["answer"])
    print("\n--- Sources ---")
    for s in res["sources"]:
        print(f"  [{s['pg_id']}] {s['author']} — {s['title']}  (dist {s['distance']})")
    if res["warnings"]:
        for w in res["warnings"]:
            print(f"  WARN {w}")


if __name__ == "__main__":
    main()
