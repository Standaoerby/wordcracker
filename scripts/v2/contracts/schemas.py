"""Declared output schemas for every v1 function called by a v2 wrapper.

Each `V1<Name>` class is a TypedDict-like declaration:
  * Class body lists every key v1 actually returns on the SUCCESS branch.
  * `__required__` is the subset that MUST be present (everything else is
    optional / conditional).
  * `__row_keys__` is a frozenset of keys read from list-of-dict ROW items
    inside the top-level result (e.g. `top` rows have `word`, `count`,
    `affinity` — wrappers iterate these freely).
  * `__defaults__` provides representative defaults for `mock_from_schema`.

Adding a key to a v1 function = add it here = bump the schema. The contract
sweep then revalidates every wrapper against the new declaration.
"""
from __future__ import annotations

from typing import Any, TypedDict


class V1Schema(TypedDict, total=False):
    """Marker base for all declared schemas. Subclasses set __required__,
    __row_keys__, __defaults__ as class-level attributes."""


# ============================================================
# scripts/rag_tools.py
# ============================================================


class V1CorpusOverview(V1Schema):
    raw_books_available: int | None
    raw_books_pg: int
    raw_books_user_uploads: int
    rsync_mirror_files: int | None
    rsync_running: bool | None
    chromadb_chunks: Any
    chromadb_error: str
    reindex_running: bool
    last_index_log: str
    last_index_log_mtime: str
    reindex_progress: dict
    index_gap_approx: int
    spgc_baseline: dict
    sources: list[str]


V1CorpusOverview.__required__ = frozenset({"sources"})
V1CorpusOverview.__row_keys__ = frozenset()
V1CorpusOverview.__defaults__ = {
    "raw_books_pg": 0, "raw_books_user_uploads": 0,
    "reindex_running": False, "sources": ["SPGC-2018-07-18"],
}


class V1SemanticSearch(V1Schema):
    query: str
    retrieval_query: str
    author_filter: str | None
    results: list[dict]


V1SemanticSearch.__required__ = frozenset({"query", "results"})
V1SemanticSearch.__row_keys__ = frozenset({
    "author", "title", "pg_id", "chunk", "distance", "snippet",
    "metadata", "text", "id",
})
V1SemanticSearch.__defaults__ = {
    "query": "", "results": [],
}


class V1CorpusStatsByAuthor(V1Schema):
    author_regex: str
    books_matched: int
    books_with_counts: int
    titles: list[str]
    total_tokens: int
    unique_words: int
    avg_book_length_words: int
    longest_book: dict
    shortest_book: dict
    languages: list[str]


V1CorpusStatsByAuthor.__required__ = frozenset({
    "author_regex", "books_matched",
})
V1CorpusStatsByAuthor.__row_keys__ = frozenset({"pg_id", "title", "tokens"})
V1CorpusStatsByAuthor.__defaults__ = {
    "author_regex": "^Author,", "books_matched": 0, "books_with_counts": 0,
    "titles": [], "total_tokens": 0, "unique_words": 0,
    "avg_book_length_words": 0, "languages": ["en"],
    "longest_book": {}, "shortest_book": {},
}


class V1TopNgramsByAuthor(V1Schema):
    author_regex: str
    n: int
    pos_filter: list[str] | None
    books_used: int
    total_ngrams: int
    top: list[dict]


V1TopNgramsByAuthor.__required__ = frozenset({"author_regex", "top"})
V1TopNgramsByAuthor.__row_keys__ = frozenset({"ngram", "count"})
V1TopNgramsByAuthor.__defaults__ = {
    "author_regex": "^Author,", "n": 2, "books_used": 0,
    "total_ngrams": 0, "top": [],
}


# R-29 S1 / bug A — book-scoped raw-frequency counterpart to
# top_ngrams_by_author. Same row shape (ngram, count) so book/author
# top-words render through the identical path; scoped to ONE book.
class V1TopNgramsByBook(V1Schema):
    pg_id: str
    title: str
    n: int
    pos_filter: list[str] | None
    book_tokens: int
    total_ngrams: int
    top: list[dict]


V1TopNgramsByBook.__required__ = frozenset({"pg_id", "top"})
V1TopNgramsByBook.__row_keys__ = frozenset({"ngram", "count"})
V1TopNgramsByBook.__defaults__ = {
    "pg_id": "PG345", "title": "", "n": 1, "book_tokens": 0,
    "total_ngrams": 0, "top": [],
}


class V1AffinityByAuthor(V1Schema):
    author_regex: str
    slug: str
    pos_filter: list[str] | None
    effective_min_corpus_count: int
    total_unique_words: int
    top: list[dict]
    cached: bool
    proper_noun_filter: str
    # Tolerated meta-keys present on author_profile composite path
    n_books: int


V1AffinityByAuthor.__required__ = frozenset({"author_regex", "top"})
# T2 (Phase 2): row_keys mirror v1's actual emission (rag_tools.py:735).
# `token` was a phantom alias the wrapper used to fall back to; v1
# never sets it. Dropped together with the wrapper-side fallback chain.
V1AffinityByAuthor.__row_keys__ = frozenset({
    "word", "author_count", "corpus_count", "affinity",
})
V1AffinityByAuthor.__defaults__ = {
    "author_regex": "^Author,", "slug": "author", "top": [],
    "cached": False, "proper_noun_filter": "",
    "effective_min_corpus_count": 0, "total_unique_words": 0,
}


class V1WordContexts(V1Schema):
    author_regex: str
    word: str
    total_occurrences: int
    samples: list[dict]


V1WordContexts.__required__ = frozenset({"word", "samples"})
V1WordContexts.__row_keys__ = frozenset({
    "pg_id", "title", "context", "snippet", "text", "id", "author",
})
V1WordContexts.__defaults__ = {
    "author_regex": "^Author,", "word": "", "total_occurrences": 0,
    "samples": [],
}


class V1CompareAuthors(V1Schema):
    author1: dict
    author2: dict
    shared_high_affinity: list[dict]
    cosine_similarity: float
    cosine_note: str
    min_corpus_count: int


V1CompareAuthors.__required__ = frozenset({"author1", "author2"})
V1CompareAuthors.__row_keys__ = frozenset({
    "regex", "slug", "top_unique", "top", "word",
    "affinity_1", "affinity_2", "affinity",
})
V1CompareAuthors.__defaults__ = {
    "author1": {"regex": "^A,", "slug": "a", "top_unique": []},
    "author2": {"regex": "^B,", "slug": "b", "top_unique": []},
    "shared_high_affinity": [], "cosine_similarity": 0.0,
    "cosine_note": "", "min_corpus_count": 500,
}


class V1LexicalDiversity(V1Schema):
    scope: str
    tokens: int
    types: int
    ttr: float
    ttr_aggregate: float
    ttr_avg_per_book: float
    books_used: int
    top_5_most_varied: list[dict]
    bottom_5_least_varied: list[dict]
    note: str


V1LexicalDiversity.__required__ = frozenset({"scope"})
V1LexicalDiversity.__row_keys__ = frozenset({
    "pg_id", "tokens", "types", "ttr",
})
V1LexicalDiversity.__defaults__ = {
    "scope": "all_corpus", "tokens": 0, "types": 0, "ttr": 0.0,
    "books_used": 0,
}


class V1WordCollocates(V1Schema):
    scope: str
    word: str
    window: int
    total_occurrences: int
    books_with_hits: int
    top_collocates: list[dict]


V1WordCollocates.__required__ = frozenset({"word", "top_collocates"})
V1WordCollocates.__row_keys__ = frozenset({"word", "count"})
V1WordCollocates.__defaults__ = {
    "word": "", "window": 4, "total_occurrences": 0,
    "books_with_hits": 0, "top_collocates": [], "scope": "",
}


class V1BookReadability(V1Schema):
    id: str
    pg_id: str
    title: str
    author: str
    user_uploaded: bool
    sampled_chars: int
    sentences: int
    words: int
    avg_sentence_length_words: float
    avg_syllables_per_word: float
    flesch_reading_ease: float
    flesch_kincaid_grade: float
    cefr_heuristic: str


V1BookReadability.__required__ = frozenset({
    "pg_id", "flesch_reading_ease", "flesch_kincaid_grade", "cefr_heuristic",
})
V1BookReadability.__row_keys__ = frozenset()
V1BookReadability.__defaults__ = {
    "id": "PG0", "pg_id": "PG0", "title": "", "author": "",
    "user_uploaded": False, "sampled_chars": 0,
    "sentences": 0, "words": 0,
    "avg_sentence_length_words": 0.0, "avg_syllables_per_word": 0.0,
    "flesch_reading_ease": 0.0, "flesch_kincaid_grade": 0.0,
    "cefr_heuristic": "B2",
}


class V1WordFreqTimeline(V1Schema):
    word: str
    bucket_years: int
    basis: str
    axis_basis: str
    timeline: list[dict]


V1WordFreqTimeline.__required__ = frozenset({"word", "timeline"})
V1WordFreqTimeline.__row_keys__ = frozenset({
    "period", "books", "total_tokens", "occurrences", "per_million",
})
V1WordFreqTimeline.__defaults__ = {
    "word": "", "bucket_years": 25, "basis": "auto", "axis_basis": "",
    "timeline": [],
}


class V1WordsDisappearingAfter(V1Schema):
    year_cutoff: int
    basis: str
    pre_bucket: dict
    post_bucket: dict
    min_pre_per_million: float
    top: list[dict]
    _elapsed_s: float


V1WordsDisappearingAfter.__required__ = frozenset({"year_cutoff", "top"})
V1WordsDisappearingAfter.__row_keys__ = frozenset({
    "word", "pre_per_million", "post_per_million",
    "drop_ratio", "pre_count", "post_count",
    "books", "total_tokens",
})
V1WordsDisappearingAfter.__defaults__ = {
    "year_cutoff": 1920, "basis": "auto", "top": [],
    "pre_bucket": {"books": 0, "total_tokens": 0},
    "post_bucket": {"books": 0, "total_tokens": 0},
    "min_pre_per_million": 50.0, "_elapsed_s": 0.0,
}


class V1WordsAppearingAfter(V1Schema):
    """W-12 (2026-05-23) — mirror of V1WordsDisappearingAfter for the
    rise-direction tool. Rows carry `rise_ratio` instead of
    `drop_ratio`; everything else (pre/post buckets, per_million,
    counts) matches one-to-one."""
    year_cutoff: int
    basis: str
    pre_bucket: dict
    post_bucket: dict
    min_post_per_million: float
    top: list[dict]
    _elapsed_s: float


V1WordsAppearingAfter.__required__ = frozenset({"year_cutoff", "top"})
V1WordsAppearingAfter.__row_keys__ = frozenset({
    "word", "pre_per_million", "post_per_million",
    "rise_ratio", "pre_count", "post_count",
    "books", "total_tokens",
})
V1WordsAppearingAfter.__defaults__ = {
    "year_cutoff": 1920, "basis": "auto", "top": [],
    "pre_bucket": {"books": 0, "total_tokens": 0},
    "post_bucket": {"books": 0, "total_tokens": 0},
    "min_post_per_million": 50.0, "_elapsed_s": 0.0,
}


class V1WordContextsGlobal(V1Schema):
    word: str
    k: int
    samples: list[dict]
    unique_authors: int
    filter_stats: dict


V1WordContextsGlobal.__required__ = frozenset({"word", "samples"})
V1WordContextsGlobal.__row_keys__ = frozenset({
    "author", "title", "pg_id", "distance", "snippet",
    "context", "text", "id",
})
V1WordContextsGlobal.__defaults__ = {
    "word": "", "k": 12, "samples": [], "unique_authors": 0,
    "filter_stats": {"dropped_lang": 0, "dropped_metalinguistic": 0,
                     "lang": "en"},
}


class V1WordPosDistribution(V1Schema):
    scope: str
    word: str
    total_occurrences: int
    max_occurrences: int
    pos_distribution: list[dict]
    _elapsed_s: float
    warning: str
    books_scanned: int


V1WordPosDistribution.__required__ = frozenset({"word"})
V1WordPosDistribution.__row_keys__ = frozenset({
    "pos", "count", "share", "samples", "pg_id", "sentence",
})
V1WordPosDistribution.__defaults__ = {
    "word": "", "scope": "", "total_occurrences": 0,
    "max_occurrences": 200, "pos_distribution": [], "_elapsed_s": 0.0,
}


class V1AuthorAttribution(V1Schema):
    tokens_in_text: int
    words_in_vector: int
    authors_in_index: int
    top: list[dict]


V1AuthorAttribution.__required__ = frozenset({"top"})
V1AuthorAttribution.__row_keys__ = frozenset({
    "author", "delta", "books_in_training", "name", "score",
})
V1AuthorAttribution.__defaults__ = {
    "tokens_in_text": 0, "words_in_vector": 0,
    "authors_in_index": 0, "top": [],
}


class V1AuthorInfluences(V1Schema):
    pivot_author: str
    pivot_books_in_training: int
    top: list[dict]


V1AuthorInfluences.__required__ = frozenset({"pivot_author", "top"})
V1AuthorInfluences.__row_keys__ = frozenset({
    "author", "delta", "books_in_training", "name", "score", "distance",
})
V1AuthorInfluences.__defaults__ = {
    "pivot_author": "", "pivot_books_in_training": 0, "top": [],
}


class V1BookEmotionProfile(V1Schema):
    id: str
    title: str
    author: str
    total_tokens: int
    emotion_bearing_tokens: int
    emotion_coverage_pct: float
    per_million: dict
    share_among_primary_emotions: dict
    sample_anchor_words: dict


V1BookEmotionProfile.__required__ = frozenset({
    "id", "share_among_primary_emotions",
})
V1BookEmotionProfile.__row_keys__ = frozenset({"emotion", "name"})
V1BookEmotionProfile.__defaults__ = {
    "id": "PG0", "title": "", "author": "",
    "total_tokens": 0, "emotion_bearing_tokens": 0,
    "emotion_coverage_pct": 0.0,
    "per_million": {}, "share_among_primary_emotions": {},
    "sample_anchor_words": {},
}


class V1EmotionCollocates(V1Schema):
    scope: str
    emotion: str
    anchor_pool_in_lexicon: int
    anchors_in_scope: list[dict]
    top_collocates: list[dict]
    total_anchor_hits: int
    warning: str
    anchor_pool_size: int


V1EmotionCollocates.__required__ = frozenset({"emotion"})
V1EmotionCollocates.__row_keys__ = frozenset({
    "word", "count", "token", "npmi", "score",
})
V1EmotionCollocates.__defaults__ = {
    "scope": "", "emotion": "fear",
    "anchor_pool_in_lexicon": 0, "anchors_in_scope": [],
    "top_collocates": [], "total_anchor_hits": 0,
}


class V1FindBook(V1Schema):
    title_query: str
    author_filter: str | None
    total_matches: int
    matches: list[dict]


V1FindBook.__required__ = frozenset({"matches"})
V1FindBook.__row_keys__ = frozenset({
    "id", "title", "author", "downloads", "authoryearofbirth",
    "language", "pub_year", "pg_id",
})
V1FindBook.__defaults__ = {
    "title_query": "", "total_matches": 0, "matches": [],
}


class V1WordEtymology(V1Schema):
    word: str
    raw_codes: list[str]
    family_chain: list[str]
    primary_family: str
    wiktionary_url: str
    from_cache: bool


V1WordEtymology.__required__ = frozenset({"word"})
V1WordEtymology.__row_keys__ = frozenset()
V1WordEtymology.__defaults__ = {
    "word": "", "raw_codes": [], "family_chain": [],
    "primary_family": "", "wiktionary_url": "", "from_cache": False,
}


class V1FindWordsByEtymology(V1Schema):
    scope: dict
    family: str
    candidates_examined: int
    cold_wiktionary_lookups: int
    matched: list[dict]


V1FindWordsByEtymology.__required__ = frozenset({"family", "matched"})
V1FindWordsByEtymology.__row_keys__ = frozenset({
    "word", "affinity", "occurrences", "corpus_count",
    "family_chain", "raw_codes",
})
V1FindWordsByEtymology.__defaults__ = {
    "scope": {}, "family": "", "candidates_examined": 0,
    "cold_wiktionary_lookups": 0, "matched": [],
}


class V1AuthorProfile(V1Schema):
    author_regex: str
    metadata: dict
    stats: dict
    signature: dict
    top_bigrams: dict
    diversity: dict
    influences: dict
    dominant_emotions: dict
    _elapsed_s: float


V1AuthorProfile.__required__ = frozenset({"author_regex"})
# author_profile is a composite of sub-tools — each sub-result's keys
# (from author_metadata, affinity_by_author, top_ngrams_by_author,
# lexical_diversity, author_influences, book_emotion_profile) can be
# read off the corresponding nested dict.
V1AuthorProfile.__row_keys__ = frozenset({
    "emotion", "avg_per_million", "books_sampled", "top",
    "books_matched", "books_with_counts", "total_tokens",
    "year_of_birth_min", "year_of_death_max", "authors_matched",
    "sample_titles", "ttr", "ttr_aggregate", "ttr_avg_per_book",
    "n", "books_used", "total_ngrams",
})
V1AuthorProfile.__defaults__ = {
    "author_regex": "^Author,", "metadata": {}, "stats": {},
    "signature": {}, "top_bigrams": {}, "diversity": {},
    "influences": {}, "dominant_emotions": {}, "_elapsed_s": 0.0,
}


class V1TopAuthorsByCountry(V1Schema):
    country: str
    metric: str
    top_n: int
    geo_coverage_for_country: int
    top: list[dict]


V1TopAuthorsByCountry.__required__ = frozenset({"country", "top"})
V1TopAuthorsByCountry.__row_keys__ = frozenset({
    "author", "books", "downloads", "country_code", "tokens",
})
V1TopAuthorsByCountry.__defaults__ = {
    "country": "GB", "metric": "books", "top_n": 20,
    "geo_coverage_for_country": 0, "top": [],
}


class V1TopAuthorsBy(V1Schema):
    metric: str
    top_n: int
    lang: str
    top: list[dict]


V1TopAuthorsBy.__required__ = frozenset({"metric", "top"})
V1TopAuthorsBy.__row_keys__ = frozenset({
    "author", "books", "downloads", "tokens", "books_with_counts",
})
V1TopAuthorsBy.__defaults__ = {
    "metric": "books", "top_n": 10, "lang": "en", "top": [],
}


class V1TopBooksByDownloads(V1Schema):
    top_n: int
    lang: str
    author_regex: str | None
    top: list[dict]


V1TopBooksByDownloads.__required__ = frozenset({"top"})
# R-28 B120 — `pg_id` removed: a phantom row key. Real v1 rows carry
# `id` only (golden fixture); declaring pg_id here let router._inject
# read it «по контракту» and silently deliver {} to every dependent
# book_readability step since 2.7.6.
V1TopBooksByDownloads.__row_keys__ = frozenset({
    "id", "title", "author", "downloads",
})
V1TopBooksByDownloads.__defaults__ = {
    "top_n": 20, "lang": "en", "author_regex": None, "top": [],
}


class V1TopBooksByRecency(V1Schema):
    top_n: int
    lang: str
    author_regex: str | None
    metric: str
    sort: str
    top: list[dict]
    note: str


V1TopBooksByRecency.__required__ = frozenset({"top"})
# R-28 B120 — `pg_id` removed: phantom row key, same as Downloads above
# (fixture rows: id, title, author, author_birth, pub_year, downloads).
V1TopBooksByRecency.__row_keys__ = frozenset({
    "id", "title", "author", "author_birth", "pub_year", "downloads",
})
V1TopBooksByRecency.__defaults__ = {
    "top_n": 20, "lang": "en", "author_regex": None,
    "metric": "pg_id", "sort": "id desc", "top": [],
}


class V1AuthorMetadata(V1Schema):
    author_regex: str
    books_matched: int
    authors_matched: list[str]
    year_of_birth_min: int | None
    year_of_death_max: int | None
    total_downloads: int
    languages: list[str]
    sample_titles: list[str]


V1AuthorMetadata.__required__ = frozenset({"author_regex"})
V1AuthorMetadata.__row_keys__ = frozenset()
V1AuthorMetadata.__defaults__ = {
    "author_regex": "^Author,", "books_matched": 0,
    "authors_matched": [], "year_of_birth_min": None,
    "year_of_death_max": None, "total_downloads": 0,
    "languages": ["en"], "sample_titles": [],
}


# ============================================================
# scripts/learning_tools.py
# ============================================================


class V1AffinityByBook(V1Schema):
    pg_id: str
    title: str
    author: str
    book_tokens: int
    book_vocab: int
    pos_filter: list[str] | None
    effective_min_corpus_count: int
    top: list[dict]


V1AffinityByBook.__required__ = frozenset({"pg_id", "top"})
V1AffinityByBook.__row_keys__ = frozenset({
    "word", "book_count", "corpus_count", "affinity",
})
V1AffinityByBook.__defaults__ = {
    "pg_id": "PG0", "title": "", "author": "",
    "book_tokens": 0, "book_vocab": 0,
    "effective_min_corpus_count": 200, "top": [],
}


class V1LearningWords(V1Schema):
    scope: str
    level: str
    band_min: int
    band_max: int
    top_n: int
    candidates: int
    results: list[dict]


V1LearningWords.__required__ = frozenset({"results"})
V1LearningWords.__row_keys__ = frozenset({
    "word", "scope_count", "corpus_count", "affinity",
    "score", "lemma", "pos",
})
V1LearningWords.__defaults__ = {
    "scope": "", "level": "intermediate",
    "band_min": 100, "band_max": 10_000,
    "top_n": 50, "candidates": 0, "results": [],
}


class V1EnrichWord(V1Schema):
    word: str
    translation_ru: str
    translation_en: str
    translation: str
    definition_en: str
    definition: str
    pos: str
    pos_tag: str
    cefr_estimate: str
    lemma: str
    example_sentence: str
    etymology: str
    proper_noun: bool
    archaic: bool
    archaic_note: str
    # Etymology lineage: canonical key is `family_chain` (the normalized
    # family taxonomy from word_etymology composite). `etymology_chain`
    # was a phantom alias the wrapper used to fall back to; dropped in
    # T2 along with the `.get() or .get()` chain.
    primary_family: str
    family_chain: list[str]
    ipa: str
    related_forms: list
    cognates: list
    derived_from: list
    _cached: bool
    _lookup_ms: float


V1EnrichWord.__required__ = frozenset({"word"})
V1EnrichWord.__row_keys__ = frozenset()
V1EnrichWord.__defaults__ = {
    "word": "", "translation_ru": "", "definition_en": "",
    "pos": "NOUN", "cefr_estimate": "B2", "lemma": "",
    "example_sentence": "", "etymology": "",
    "proper_noun": False, "archaic": False, "_cached": False,
    "_lookup_ms": 0.0, "family_chain": [], "primary_family": "",
}


class V1BookArchaicWords(V1Schema):
    id: str
    checked_book_vocab: int
    seed_or_cache_hits: int
    enriched_now: int
    top: list[dict]
    _elapsed_s: float


V1BookArchaicWords.__required__ = frozenset({"id", "top"})
V1BookArchaicWords.__row_keys__ = frozenset({
    "word", "book_count", "source", "note", "lemma",
})
V1BookArchaicWords.__defaults__ = {
    "id": "PG0", "checked_book_vocab": 0,
    "seed_or_cache_hits": 0, "enriched_now": 0,
    "top": [], "_elapsed_s": 0.0,
}


class V1ExportWordList(V1Schema):
    out_path: str
    format: str
    entries: int
    skipped_proper_nouns: int
    content: str
    text: str
    filename: str


V1ExportWordList.__required__ = frozenset({"format"})
V1ExportWordList.__row_keys__ = frozenset()
V1ExportWordList.__defaults__ = {
    "out_path": "/tmp/out.csv", "format": "anki_csv",
    "entries": 0, "skipped_proper_nouns": 0,
    "content": "", "filename": "out.csv",
}


# ============================================================
# scripts/v2/entity_resolver_v6 (used by tools/meta/resolve_entity.py)
# These are v2-internal resolvers, not legacy v1 — but they have the
# same contract pattern (declared output → wrapper reads only declared
# keys). We schema-bind them to lock the v6 resolver shape.
# ============================================================


class V6ResolveAuthor(V1Schema):
    decision: str
    normalization_trace: list[dict]
    confidence_reason: str
    candidates: list[dict]
    confidence: str
    resolved: dict


V6ResolveAuthor.__required__ = frozenset({"decision"})
V6ResolveAuthor.__row_keys__ = frozenset({
    "author_regex", "display", "source", "prominence",
    "books_in_corpus", "name", "score",
})
V6ResolveAuthor.__defaults__ = {
    "decision": "ok", "normalization_trace": [],
    "confidence_reason": "", "candidates": [],
    "confidence": "high",
    "resolved": {"author_regex": "^Author,", "display": "Author",
                 "source": "metadata", "prominence": 1,
                 "books_in_corpus": 1},
}


class V6ResolveBook(V1Schema):
    decision: str
    normalization_trace: list[dict]
    candidates: list[dict]
    confidence: str
    resolved: dict


V6ResolveBook.__required__ = frozenset({"decision"})
V6ResolveBook.__row_keys__ = frozenset({
    "pg_id", "title", "author", "source", "id",
})
V6ResolveBook.__defaults__ = {
    "decision": "ok", "normalization_trace": [], "candidates": [],
    "confidence": "high",
    "resolved": {"pg_id": "PG0", "title": "", "author": "",
                 "source": "metadata"},
}


# ============================================================
# Schema → declared-keys index
# ============================================================


def _collect_keys(cls: type[V1Schema]) -> frozenset[str]:
    """Combine TypedDict annotations + parents' annotations."""
    out: set[str] = set()
    for klass in cls.__mro__:
        anns = getattr(klass, "__annotations__", {}) or {}
        out.update(k for k in anns if not k.startswith("_"))
    return frozenset(out)


_ALL_SCHEMAS: tuple[type[V1Schema], ...] = (
    V1CorpusOverview, V1SemanticSearch, V1CorpusStatsByAuthor,
    V1TopNgramsByAuthor, V1TopNgramsByBook, V1AffinityByAuthor,
    V1WordContexts,
    V1CompareAuthors, V1LexicalDiversity, V1WordCollocates,
    V1BookReadability, V1WordFreqTimeline, V1WordsDisappearingAfter,
    V1WordsAppearingAfter,
    V1WordContextsGlobal, V1WordPosDistribution, V1AuthorAttribution,
    V1AuthorInfluences, V1BookEmotionProfile, V1EmotionCollocates,
    V1FindBook, V1WordEtymology, V1FindWordsByEtymology, V1AuthorProfile,
    V1TopAuthorsByCountry, V1TopAuthorsBy, V1TopBooksByDownloads,
    V1TopBooksByRecency, V1AuthorMetadata,
    V1AffinityByBook, V1LearningWords, V1EnrichWord,
    V1BookArchaicWords, V1ExportWordList,
    V6ResolveAuthor, V6ResolveBook,
)

SCHEMA_KEYS: dict[type[V1Schema], frozenset[str]] = {
    cls: _collect_keys(cls) for cls in _ALL_SCHEMAS
}

# Allowed top-level keys = SUCCESS keys + universal `error`/`details`.
# Used by contract sweep when validating recorded golden fixtures.
SUCCESS_ERROR_KEYS: dict[type[V1Schema], frozenset[str]] = {
    cls: SCHEMA_KEYS[cls] | frozenset({"error", "details", "hint"})
    for cls in _ALL_SCHEMAS
}


__all__ = [
    "V1Schema",
    "SCHEMA_KEYS",
    "SUCCESS_ERROR_KEYS",
    # rag_tools
    "V1CorpusOverview", "V1SemanticSearch", "V1CorpusStatsByAuthor",
    "V1TopNgramsByAuthor", "V1TopNgramsByBook", "V1AffinityByAuthor",
    "V1WordContexts",
    "V1CompareAuthors", "V1LexicalDiversity", "V1WordCollocates",
    "V1BookReadability", "V1WordFreqTimeline", "V1WordsDisappearingAfter",
    "V1WordsAppearingAfter",
    "V1WordContextsGlobal", "V1WordPosDistribution", "V1AuthorAttribution",
    "V1AuthorInfluences", "V1BookEmotionProfile", "V1EmotionCollocates",
    "V1FindBook", "V1WordEtymology", "V1FindWordsByEtymology",
    "V1AuthorProfile", "V1TopAuthorsByCountry", "V1TopAuthorsBy",
    "V1TopBooksByDownloads", "V1TopBooksByRecency", "V1AuthorMetadata",
    # learning_tools
    "V1AffinityByBook", "V1LearningWords", "V1EnrichWord",
    "V1BookArchaicWords", "V1ExportWordList",
    # v6 resolvers
    "V6ResolveAuthor", "V6ResolveBook",
]
