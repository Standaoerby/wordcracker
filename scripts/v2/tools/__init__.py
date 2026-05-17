"""v2 tools, grouped by category. Import this package to register all tools."""
from scripts.v2.tools.corpus_meta import overview  # noqa: F401
from scripts.v2.tools.books import find_book, affinity_book, readability  # noqa: F401
from scripts.v2.tools.authors import (  # noqa: F401
    author_metadata, top_authors, affinity, author_profile,
)
from scripts.v2.tools.words import (  # noqa: F401
    contexts, collocates, timeline, emotion, pos, etymology,
)
from scripts.v2.tools.learning import learning_words  # noqa: F401
from scripts.v2.tools.search import lexical, hybrid  # noqa: F401
