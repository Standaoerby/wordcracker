"""v2 tools, grouped by category. Import this package to register all tools."""
from scripts.v2.tools.corpus_meta import overview, stats_by_author  # noqa: F401
from scripts.v2.tools.books import (  # noqa: F401
    find_book, affinity_book, readability, top_books,
)
from scripts.v2.tools.authors import (  # noqa: F401
    author_metadata, top_authors, affinity, author_profile, top_ngrams,
)
from scripts.v2.tools.words import (  # noqa: F401
    contexts, collocates, timeline, emotion, pos, etymology, lemma_profile,
)
from scripts.v2.tools.learning import learning_words, enrich  # noqa: F401
from scripts.v2.tools.search import lexical, hybrid, semantic  # noqa: F401
