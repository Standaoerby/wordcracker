"""Profile caches — pre-computed author/book/lemma rollups.

Each profile is a thin wrapper around v1 analytics calls, persisted in a
SQLite under /data/v2_profiles/ tagged with corpus_version. Re-asks hit the
cache instead of re-running the underlying scan.
"""
