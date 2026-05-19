r"""Intent classifier — rules-based with confidence + priority scoring.

Contract: docs/v2/PLANNER.md §2.

Strategy:
  * Each rule is (regex, intent, confidence). Confidence in [0, 1].
  * On multi-match, winner = (priority desc, confidence desc).
  * Return ("clarify", 0.0) when nothing matches.

Russian-stem gotcha: `\b(устаревш)\b` won't match "устаревшими" because the next
char is a letter, so there's no word boundary after `ш`. Use `\b(устаревш\w*)\b`
or drop the trailing `\b`. All rules below avoid that trap.

Target: ≥80% on the 40-example list from the Obsidian vault.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Pattern


INTENTS = frozenset({
    "introduction",
    "corpus_meta",
    "author_metadata",
    "book_compare",
    "author_vocab",
    "author_top_words",
    "author_compare",
    "author_attribution",
    "author_influences",
    "author_closest",
    "lexical_wealth",
    "book_vocab",
    "book_readability",
    "book_archaic",
    "book_emotion",
    "book_recommendation",
    "word_contexts",
    "word_collocates",
    "word_timeline",
    "word_pos",
    "word_etymology",
    "word_emotion",
    "word_dialogue",
    "word_movement",
    "learning",
    "top_authors_books",
    "book_lookup",
    "country_compare",
    "country_vocab",
    "composite_compare",
    "period_vocab",
    "genre_compare",
    "topic_words",
    "translation_quality",
    "vocab_passport",
    # Sprint 16 Phase E — meta-query intents
    "author_lookup",      # «какие книги у X»
    "book_extremum",      # «самая длинная / самая популярная книга»
    "corpus_extremum",    # «самый плодовитый / влиятельный автор»
    # Sprint 16 Phase F — semantic find_book by topic
    "topic_book_search",  # «найди книгу про X», «book about Y»
    # Sprint 16 Phase G — publication year (Open Library enrichment)
    "book_pub_year",      # «когда была опубликована X», «year of X»
    # Sprint 17 — readability comparison
    "book_readability_compare",  # «что сложнее читать X или Y»
    "out_of_scope",
    "clarify",
})


@dataclass
class IntentMatch:
    label: str
    confidence: float
    matched_pattern: str | None = None


def _re(pattern: str) -> Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


PRIORITY = {
    "out_of_scope": 200,
    # Sprint 16 Phase E — meta-query intents above corpus_meta so the
    # generic «корпус» rules don't swallow specific «какие книги у X».
    # Above author_metadata (55) so «какие книги у Doyle» doesn't get
    # eaten by the «сколько у X книг» rule (which is books_matched, not
    # the actual list).
    "author_lookup":   160,
    "book_extremum":   158,
    "corpus_extremum": 155,
    # Sprint 16 Phase F — above book_recommendation (118) so a topical
    # «найди книгу про X» beats the generic «recommend» rule.
    "topic_book_search": 145,
    # Sprint 16 Phase G — above book_lookup (122) so «когда вышла X»
    # routes to pub_year, not generic title search.
    "book_pub_year": 148,
    # Sprint 17 — readability compare. Above book_compare (110) so
    # «сложнее читать X или Y» wins over generic compare.
    "book_readability_compare": 152,
    "vocab_passport": 150,
    "composite_compare": 145,
    "translation_quality": 140,
    "country_compare": 135,
    "genre_compare": 130,
    "word_etymology": 125,
    "word_pos": 125,
    "word_timeline": 120,
    "word_emotion": 120,
    "word_collocates": 118,
    "book_recommendation": 118,
    "book_archaic": 115,
    "book_emotion": 115,
    "book_readability": 115,
    "author_closest": 113,
    "author_attribution": 112,
    "author_influences": 110,
    "author_compare": 108,
    "book_compare": 110,
    "lexical_wealth": 105,
    "word_movement": 105,
    "word_dialogue": 105,
    "book_vocab": 100,
    "learning": 98,
    "topic_words": 95,
    "country_vocab": 92,
    "period_vocab": 90,
    "word_contexts": 88,
    "author_vocab": 85,
    "top_authors_books": 70,
    "book_lookup": 122,
    "corpus_meta": 60,
    "author_top_words": 87,
    "author_metadata": 55,
    "introduction": 50,
    "clarify": 0,
}


RULES: list[tuple[Pattern[str], str, float]] = [

    # ===== introduction =====
    (_re(r"кто ты|что ты умеешь|расскажи о себе|представ(ься|ление)|"
         r"who are you|what can you do|tell me about yourself|"
         r"типы анализа|какие.{1,10}поддержив"), "introduction", 0.95),
    (_re(r"^\s*(привет|hi|hello|здравствуй|здарова|здарово|приветик)\b"),
     "introduction", 0.8),
    # Round 3: «помоги / помощь / help» — opener pattern, was clarify.
    (_re(r"^\s*(помоги|помощь|help|справка|подскажи)\b"
         r"(?!.{0,40}\b(книг|автор|слов|перевод))"),
     "introduction", 0.85),
    # Round 2 friendly openers: «ну привет, ты кто», «слушай, ...».
    # Match opening particles + question; LLM fallback covers the rest.
    (_re(r"^\s*(ну|эй|слушай|ладно|так)[,\s]+.{1,40}\b(ты|кто|умеешь|что|"
         r"книг|корпус)"), "introduction", 0.75),

    # ===== translation request =====
    # Round 3 R14: «переведи мне X на Y» — translation. Не translation_quality
    # (это про качество существующих переводов), а translation OOS — мы не
    # переводчик. Routes to out_of_scope с friendly note.
    (_re(r"\bпереведи\s+(мне|нам)?\s*.{1,60}\s+на\s+(английск|русск|немецк|"
         r"французск|испанск|итальянск|english|russian)|"
         r"\btranslate\s+(this|the\s+\w+|.{1,40})\s+(to|into)\s+\w+"),
     "out_of_scope", 0.93),

    # ===== command injection (system command / shell) =====
    # Round 3 R15: «execute system command: ls -la /etc». Adversarial input.
    (_re(r"\b(execute|run|exec)\s+(system\s+)?(command|shell)|"
         r"\b(ls\s|cat\s+/|rm\s+-|sudo\s|chmod\s|chown\s|"
         r"\\?<\\?\\?php|\\?<script|eval\(|exec\()"),
     "out_of_scope", 0.95),
    (_re(r"\bвыполни\s+(команд|shell|bash|cmd)|"
         r"\bshow\s+me\s+(your\s+)?(env|environment|secrets|password|"
         r"credentials|api[\s_]?key)"),
     "out_of_scope", 0.93),

    # ===== out_of_scope =====
    # Prompt-injection guards. These would normally bounce off the
    # wordcracker:v2 Modelfile SYSTEM prompt, but it's cheaper + safer to
    # refuse at the planner level before the LLM ever sees them.
    (_re(r"забудь\s+.{0,20}(предыдущ\w*\s+)?инструкци|"
         r"игнорируй\s+.{0,20}(предыдущ\w*\s+)?инструкци|"
         r"ignore\s+.{0,20}(previous\s+)?instructions|"
         r"forget\s+.{0,20}(previous\s+)?instructions|"
         r"system\s+prompt|"
         r"(reveal|раскрой|покажи)\s+(твой|your)\s+(system|prompt|инструкц)|"
         r"act\s+as\s+a?\s*(different|другой|new)"),
     "out_of_scope", 0.97),
    # Persona / role override attempts
    (_re(r"\b(you are now|ты теперь|ты больше не|pretend to be|"
         r"твоя новая роль|new role:|role\s*:\s*[a-zа-я]+)"),
     "out_of_scope", 0.95),
    (_re(r"(напиши|сочини|допиши|сгенерируй)\s.{0,40}"
         r"(рассказ\w*|стих\w*|поэм\w*|глав\w*|стат\w*)"), "out_of_scope", 0.95),
    (_re(r"(write|compose)\s.{0,40}(story|poem|chapter|novel|article)"),
     "out_of_scope", 0.9),
    # Q8 (Stan's 2026-05-18 demon round): «процитируй полностью», «дай
    # полный текст», «give me the full text», «quote verbatim» — verbatim-
    # reproduction requests. Always refuse: we do RAG analytics, not full-
    # text serving. Catches both copyright-locked AND public-domain — Stan
    # doesn't want the system shipping 200 KB of Pride and Prejudice
    # through chat either.
    (_re(r"(процитируй|приведи|дай|покажи)\s+(весь|полный|целиком|"
         r"полностью)\s+(текст|роман|книгу|главу)|"
         r"полный\s+текст\s+(книги|романа)|"
         r"give\s+me\s+the\s+full\s+text|"
         r"quote\s+(verbatim|entirely|in\s+full)|"
         r"verbatim\s+(text|copy)"),
     "out_of_scope", 0.95),
    (_re(r"процитируй\s+полностью"), "out_of_scope", 0.93),
    # Out-of-scope news/weather queries — narrow patterns so we don't
    # accidentally eat "слова в описаниях погоды" (that's topic_words).
    (_re(r"\b(какая|сейчас|сегодня|завтра|today|now|current)\s+погод"),
     "out_of_scope", 0.9),
    (_re(r"\b(последние|сегодняшние|today's|breaking)\s+новост"),
     "out_of_scope", 0.9),
    (_re(r"курс\s+акц|прогноз\s+погод|weather forecast|stock\s+(price|market)"),
     "out_of_scope", 0.85),

    # ===== translation_quality =====
    (_re(r"перевод\w*\b.{0,80}"
         r"(неправильно|упрощ\w*|потерял\w*|потерян\w*|разн[ыо]м|"
         r"соответствуют|нескольким\s+разным)"), "translation_quality", 0.9),
    (_re(r"транслит\w*|потер[ия]\s+(в|при)\s+перевод|"
         r"translation\s+(mistake|losses|quality)"), "translation_quality", 0.85),

    # ===== Sprint 16 Phase E — meta-query intents =====
    # Per-user-need pattern: «какие книги у X» / «список книг X» / «what
    # books does X have» — wants the catalog of an author's works, not stats.
    # Routed via author_metadata which already returns `sample_titles` +
    # books_matched.
    #
    # Why explicit preposition is REQUIRED: IGNORECASE neutralizes the
    # [A-Z] character class, so a no-prep rule «...книг + Capital + \w+»
    # also matches «...произведения подойдут...» (B2 reading level Q30
    # bug — false positive on common verbs after «произведения»).
    # Forcing the preposition kills that ambiguity. Genitive-only
    # phrasings like «Перечисли произведения Doyle» are routed by the
    # LLM fallback (classify_and_extract) which sees the surname token.
    (_re(r"\b(какие|каких|сколько\s+разных|перечисли|список|"
         r"покажи\s+(все|список|книги)?)\s+"
         r"(книг|произведен|роман|works)\w*\s+"
         r"(у\s+|при\s+|of\s+|by\s+|написал\w*\s+)"),
     "author_lookup", 0.92),
    (_re(r"\bwhat\s+(books?|works?|novels?)\s+(does|did|by|of)\s+"
         r"[A-Z]\w+"),
     "author_lookup", 0.9),
    (_re(r"\b(что|чё)\s+(есть|у\s+тебя\s+есть)\s+(у|от|of)\s+"
         r"[A-ZА-ЯЁ]\w+\s*(в\s+корпус|в\s+базе)?"),
     "author_lookup", 0.85),

    # Single-book superlatives — «самая длинная книга», «самая короткая»,
    # «самая популярная книга» (singular!), «book with the highest …».
    # Plural «топ книг» → top_authors_books, not book_extremum.
    (_re(r"\b(сам(ая|ый|ое)|какая\s+самая)\s+"
         r"(длинн|больш|коротк|маленьк|популярн|"
         r"скачиваем|сложн|прост|редк|архаичн|"
         r"богат\w*\s+(словар|vocabular)|"
         r"древн)\w*\s+(книг|роман|произведен)"),
     "book_extremum", 0.92),
    (_re(r"\bthe\s+(longest|shortest|most\s+(popular|downloaded|complex)|"
         r"simplest|rarest|oldest)\s+(book|novel|work)\b"),
     "book_extremum", 0.9),

    # Single-author corpus superlative — «самый плодовитый автор», «самый
    # популярный автор», «who is the most prolific author». Singular.
    (_re(r"\b(сам(ый|ая|ое)|какой\s+самый)\s+"
         r"(плодовит|популярн|читаем|скачиваем|известн|"
         r"древн|молод|представительн)\w*\s+автор"),
     "corpus_extremum", 0.92),
    (_re(r"\b(who|кто)\s+(is|был|была)\s+the\s+"
         r"most\s+(prolific|popular|read|downloaded|famous)\s+"
         r"(author|writer|поэт|prose)"),
     "corpus_extremum", 0.9),
    (_re(r"\bкто\s+(самый|самая)\s+"
         r"(плодовит|популярн|читаем|известн)\w*\s+автор"),
     "corpus_extremum", 0.9),

    # ===== Sprint 16 Phase F — topic_book_search =====
    # «найди книгу про X», «посоветуй роман о Y», «book about Z». Routes
    # to find_book_by_topic which wraps hybrid_search and dedupes by
    # pg_id. Differs from book_lookup (title search) and book_recommendation
    # (popularity / level filter): this is SEMANTIC by topic.
    (_re(r"\b(найди|поищи|посоветуй|подскажи|recommend|find)\s+"
         r"(?:мне\s+|me\s+|a\s+)?"
         r"(книг\w*|роман\w*|произведен\w*|book|novel)\s+"
         r"(про|о|об|на\s+тему|about|on)\s+"),
     "topic_book_search", 0.93),
    # «книга о/про/about X» — Stan asked Round 6 for «роман про викторианский
    # Лондон»; the prior path went to book_lookup which returns titles.
    (_re(r"\b(книг\w*|роман\w*|произведен\w*|book|novel)\s+"
         r"(про|о|об|about|на\s+тему)\s+\w+"),
     "topic_book_search", 0.85),
    (_re(r"\bчто\s+почитать\s+(про|о|об|на\s+тему|about)\s+"),
     "topic_book_search", 0.9),

    # ===== Sprint 16 Phase G — book_pub_year =====
    # «когда была опубликована Война и мир», «год издания Pride and
    # Prejudice», «when was Dracula published». Surfaces pub_year via
    # find_book (Sprint 9.7 Open Library enrichment).
    (_re(r"\b(когда\s+(была\s+|был\s+)?"
         r"(опубликован\w*|издан\w*|вышл\w*|написан\w*|появил\w*)|"
         r"год\s+(публикаци\w*|издани\w*|выход\w*|написани\w*)|"
         r"в\s+как(ом|ой)\s+году\s+(была\s+|был\s+)?"
         r"(опубликован\w*|издан\w*|вышл\w*|написан\w*))"),
     "book_pub_year", 0.93),
    (_re(r"\bwhen\s+was\s+.{1,60}\s+(published|released|written)|"
         r"\byear\s+of\s+publication|"
         r"\bpublication\s+(year|date)"),
     "book_pub_year", 0.92),

    # ===== Sprint 17 — book_readability_compare =====
    # «что сложнее читать, X или Y» / «легче читать X или Y» — Stan's
    # 2026-05-19 audit caught this falling to clarify because no rule
    # covered the readability-compare pattern. We anchor on
    # «сложнее/легче/проще/труднее ... читать» + «или» — the «или»
    # disambiguates from single-book readability («сложно читать X»).
    (_re(r"\b(сложнее|сложней|труднее|трудней|легче|проще)\s+"
         r"(?:будет\s+)?(?:это\s+)?читать\b.{0,80}\b(или|vs|против)\b"),
     "book_readability_compare", 0.94),
    (_re(r"\bчто\s+(сложнее|труднее|легче|проще)\s+читать\b"),
     "book_readability_compare", 0.92),
    (_re(r"\b(harder|easier|simpler|more\s+difficult|more\s+complex)\s+to\s+read\b"
         r".{0,60}\bor\b"),
     "book_readability_compare", 0.92),
    (_re(r"\b(which|what)\s+is\s+(harder|easier|simpler)\s+to\s+read\b"),
     "book_readability_compare", 0.9),

    # ===== corpus_meta =====
    # Stan round 5: «сколько у Толстого книг» wrongly routed to corpus_meta
    # (total) instead of author_metadata. Added negative lookahead — if the
    # «сколько...книг» phrase has a capitalized name in between, it's an
    # author-specific question and author_metadata's more specific rule
    # should win (also bumped its conf above 0.95). The lookahead handles
    # «сколько у Толстого книг», «сколько у X книг» staying out of
    # corpus_meta.
    (_re(r"\bсколько\s+(книг|book)\b"
         r"(?!\w*\s+(у|of)\s+[А-ЯA-ZЁ])"), "corpus_meta", 0.95),
    (_re(r"\bhow many (книг|book)"), "corpus_meta", 0.95),
    # Diminutive form — «книжек» is corpus_meta (asking-the-system)
    (_re(r"\bсколько\s+(у\s+тебя\s+)?книж(ек|ка)\b"), "corpus_meta", 0.92),
    (_re(r"прогресс\s+индексаци\w*|index progress|reindex"), "corpus_meta", 0.92),
    (_re(r"\bпрогресс\b"), "corpus_meta", 0.6),
    (_re(r"что у тебя за корпус|размер корпуса|corpus (size|stats)"),
     "corpus_meta", 0.9),
    # Meta-questions about coverage / copyright / language scope. Used to
    # fall through to clarify («не уверен что ты имеешь в виду») which is
    # rude when the user is just asking what's in the corpus. Caught by
    # Stan's adversarial round 2026-05-18.
    (_re(r"(что|как)\s+у\s+тебя\s+с\s+"
         r"(копирайт\w*|copyright|охват\w*|coverage|корпус\w*|"
         r"книг\w*|данн\w*|язык\w*|русск\w*|английск\w*)"),
     "corpus_meta", 0.92),
    (_re(r"расскажи\s+(про|о)\s+(корпус\w*|coverage|copyright|охват\w*|"
         r"покрыти\w*|содержим\w*)"),
     "corpus_meta", 0.9),
    (_re(r"(какие|какой)\s+(книги|книг|охват|coverage|период\w*|"
         r"диапазон|years|range)\s+(в|у)\s+(корпус|тебя|базе|libra)"),
     "corpus_meta", 0.88),
    (_re(r"\bcopyright\b.{0,40}(coverage|корпус|books?|книг)"),
     "corpus_meta", 0.85),

    # ===== author_metadata =====
    (_re(r"когда\s+(родил\w*|умер\w*|жил\w*)"), "author_metadata", 0.9),
    (_re(r"year of (birth|death)"), "author_metadata", 0.9),
    (_re(r"сколько у\s+.{1,40}\s*книг"), "author_metadata", 0.85),
    # Round 3 R20: «сколько у Толстого книг» правильно, но v2.x routed в
    # corpus_meta. Strengthen author_metadata rule when an author is in
    # genitive form near «книг».
    (_re(r"сколько\s+у\s+[А-ЯA-ZЁ]\w+\w*\s+книг"), "author_metadata", 0.93),
    # Round 3 R3: «дай статистику по Wodehouse» — это chat placeholder! Не
    # corpus_stats_by_author exact, но routes тoда же tool через
    # author_metadata (быстрая мета — books_total, sample titles, geo).
    (_re(r"\b(дай|покажи|выдай)\s+статистик\w*\s+по\s+[A-ZА-ЯЁ]"),
     "author_metadata", 0.92),
    (_re(r"\bstats?\s+(for|on|about)\s+[A-Z]\w+"),
     "author_metadata", 0.88),
    # Stan round 2 Q12: «годы жизни эдгара по?» — биографический интент,
    # старые правила его не ловили (требовали «когда родился»). Plus
    # обычные синонимы для биографии.
    (_re(r"\bгоды\s+жизни\b|\bдаты\s+жизни\b|"
         r"\bbirth\s+and\s+death|\blife\s+(years|dates|span)"),
     "author_metadata", 0.95),
    (_re(r"\bбиография\b|\bbiograph\w*|"
         r"\bчто\s+(ты\s+)?знаешь\s+(о|про)\s+[A-ZА-Я]"),
     "author_metadata", 0.85),

    # ===== book_lookup (Q2 from demon round) =====
    # «найди книгу X», «есть ли у тебя X», «где книга X», «is X in the
    # corpus» — pure resolution query, route to find_book directly so the
    # answer is concrete (PG id + title + author + downloads) instead of a
    # generic clarify. Routes through `book_recommendation` plan-builder
    # is wrong — that gives popular-books listing. We need a dedicated
    # `book_lookup` intent → `find_book` tool.
    (_re(r"\b(найди|поищи|есть\s+ли\s+у\s+тебя)\s+книг\w*\s+"),
     "book_lookup", 0.93),
    (_re(r"\bis\s+(the\s+)?book\s+.{2,60}\s+in\s+(the\s+)?(corpus|library)"),
     "book_lookup", 0.9),
    (_re(r"\bкнига\s+.{2,60}\s+есть\s+у\s+тебя"),
     "book_lookup", 0.9),
    # Round 2 R8/R11: «найди-ка X», «есть ли у тебя X» where X is the book
    # title directly (no «книгу» word in between). Particle «-ка» is
    # informal Russian, doesn't change intent.
    (_re(r"\b(найди|поищи)\s*-?\s*ка?\s+[«\"“‘А-ЯA-Z]"),
     "book_lookup", 0.9),
    (_re(r"\bесть\s+ли\s+у\s+тебя\s+[«\"“‘А-ЯA-Z]"),
     "book_lookup", 0.9),
    (_re(r"\b(где|у\s+тебя\s+есть)\s+\w{0,15}\s*[«\"“‘А-ЯA-Z][\w\s]{1,40}\s+("
         r"\?|$)"), "book_lookup", 0.78),

    # ===== top_authors_books =====
    (_re(r"\b(топ[- ]?\d*|top\s*\d*)\s.{0,40}(автор\w*|writer)"),
     "top_authors_books", 0.85),
    (_re(r"(самые\s+попул\w*|самые\s+скачив\w*|most popular|most downloaded)"
         r"\b.{0,40}\b(автор\w*|book|книг)"), "top_authors_books", 0.9),
    (_re(r"\b(топ[- ]?\d*|top[- ]?\d*)\s+(?:.{0,40}?\s+)?"
         r"(скачив\w*|downloaded|книг|book)"),
     "top_authors_books", 0.7),

    # ===== book_compare (Q24 / Q27 / Q5-style — two or more books contrast) =====
    # «X1 и X2 но редко в Y», «слова в X и Y, но не в Z», multi-book combos.
    # Higher priority than author_compare so the title-list pattern wins.
    (_re(r"(в|of)\s+[«\"“‘][^»\"”’]+[»\"”’]\s+и\s+[«\"“‘][^»\"”’]+[»\"”’]"
         r".{0,40}(редко|а|но\s+редко|не\s+встречаются?|but rarely)"),
     "book_compare", 0.92),
    # Q5-style: «у Диккенса в "Bleak House", но почти не встречаются у Марка
    # Твена в "Adventures of Huckleberry Finn"». Two book titles each
    # attached to an author — author_compare's regex won't catch the inner
    # «в "..."» qualifier, and the rendered table comes back as character
    # names instead of style markers. Route to book_compare which uses
    # affinity_by_book with PROPN exclusion.
    (_re(r"у\s+[А-ЯA-Z][\w\s]{1,30}\s+в\s+[«\"“‘][^»\"”’]+[»\"”’]"
         r".{0,40}\b(но|а)\b.{0,30}у\s+[А-ЯA-Z][\w\s]{1,30}\s+"
         r"в\s+[«\"“‘][^»\"”’]+[»\"”’]"),
     "book_compare", 0.96),
    (_re(r"\bотличают\s+(готическ\w*|приключенческ\w*)\s+\w+\s+(от|vs)\s+"),
     "book_compare", 0.6),  # genre-compare bucket
    # «у морских авторов Мелвилла, Конрада и Стивенсона» — multi-author
    # vs the corpus — same shape, route through compare too.
    (_re(r"\bу\s+[а-яёА-ЯЁa-zA-Z\s,]+\s+но\s+редко\s+в\s+остальном\b"),
     "book_compare", 0.85),

    # ===== author_compare =====
    (_re(r"\b(сравни|compare)\s+.{1,60}\s+(и|vs|with)\s+"), "author_compare", 0.95),
    (_re(r"отличают\s+стиль|отлич\w*\s.{0,30}стил\w*|"
         r"distinguishes the style|stylistic differences? between"),
     "author_compare", 0.9),
    # «повторяются у Диккенса, но почти не встречаются у Хемингуэя»
    (_re(r"у\s+[А-ЯA-Z][\wа-яё-]+\b.{0,40}\b(но|а)\s+.{0,20}\b(не|редко)\b"
         r".{0,30}\bу\s+[А-ЯA-Z]"), "author_compare", 0.9),

    # ===== author_closest =====
    # Bare `похож\w*\s+на` used to live here but it false-matched any
    # «похожи на ИИ / похоже на правду / похожа на сказку» phrasing and
    # bucketed those as author_closest. Require an author/style anchor
    # after «похож…на» so only stylometric queries land here.
    (_re(r"лексически\s+ближе|ближе\s+всего\s+к|"
         r"closest( authors)? to|похожи\s+на\s+стиль|"
         r"кто\s+похож\s+на|"
         r"похож\w*\s+на\s+(автор\w*|писател\w*|стил\w*|поэт\w*)"),
     "author_closest", 0.9),
    # Q9 (Stan's 2026-05-18 demon round): «на кого по стилю похож X»,
    # «по стилю похож на X», «similar to X stylistically», «like X in style»
    # — natural Russian word order, NOT covered by «похожи на стиль» rule
    # which expects «похожи на стиль» literally.
    (_re(r"\b(на\s+кого|кто)\s+по\s+стилю\s+похож\w*|"
         r"по\s+стилю\s+похож\w*\s+на|"
         r"стилистически\s+(близок|похож)|"
         r"similar\s+(to\s+\w+\s+)?stylistically|"
         r"like\s+\w+\s+in\s+style"), "author_closest", 0.92),

    # ===== author_attribution =====
    (_re(r"кто автор|определи автора|attribute (this )?text|authorship of"),
     "author_attribution", 0.9),

    # ===== author_influences =====
    (_re(r"влияни[яе]\w*|повлиял\w*|influences? on|literary influences?"),
     "author_influences", 0.9),

    # ===== author_top_words (raw frequency, not affinity) =====
    # «самое частотное слово X», «топ слов автора Y», «most frequent words
    # of Z» — user wants the raw zipf head, not the affinity head. Plan
    # routes to top_ngrams_by_author(n=1) which returns unigram counts.
    (_re(r"сам[оы]е\s+част[оы]тн\w+\s+слов"), "author_top_words", 0.95),
    (_re(r"топ\s+\d*\s*(част[оы]тн|самых?\s+част)\s+слов"), "author_top_words", 0.9),
    (_re(r"most\s+frequent\s+words?"), "author_top_words", 0.9),
    # Stan round 2 Q18: «топ-15 биграмм Достоевского» / «топ-15 биграмм у
    # Конан Дойла» — пример из README + clickable chips, должен работать.
    # Биграммы routes through author_top_words → top_ngrams_by_author(n=2)
    # via the planner. Triggers: «биграмм*», «bigram*», «n-грамм*».
    (_re(r"\b(топ\s*-?\s*\d*\s*)?\bбиграмм\w*|"
         r"\b(топ\s*-?\s*\d*\s*)?\bтриграмм\w*|"
         r"top\s*-?\s*\d*\s*bigrams?|"
         r"top\s*-?\s*\d*\s*trigrams?|"
         r"\bn-?грамм\w*"), "author_top_words", 0.95),

    # ===== author_vocab =====
    (_re(r"фирменн\w+\s+слов\w*|характерн\w+\s+(слов\w*|прилаг\w*|глагол\w*)|"
         r"signature words|маркер\w*\s+(стиля|автор)|"
         r"distinctive (vocabulary|words)"), "author_vocab", 0.85),
    # «Слова Толкина из LOTR», «лексика Свифта», — generic vocab queries
    # that name an author / book without a "characteristic" keyword.
    (_re(r"^\s*слова\s+[A-ZА-Я]\w+"), "author_vocab", 0.65),
    (_re(r"\bлексик[аиу]\s+[A-ZА-Я]\w+"), "author_vocab", 0.7),
    (_re(r"заметно\s+чаще|непропорционально|disproportionately|"
         r"встречаются\s+заметно\s+(чаще|реже)"), "author_vocab", 0.8),
    (_re(r"чаще\s+всего\s+использует|больше\s+всего\s+использует|"
         r"often uses|most often uses"), "author_vocab", 0.7),
    (_re(r"но\s+редко\s+в\s+остальном\s+корпусе"), "author_vocab", 0.75),
    (_re(r"какие\s+автор\w*\s+(использу\w*|пиш\w*|применя\w*)\s+"
         r"больше\s+всего\s+(редких|необычных|архаичных)?\s*"
         r"(прилаг\w*|глагол\w*|сущ\w*|слов\w*|word)"), "author_vocab", 0.8),

    # ===== book_vocab =====
    (_re(r"в\s+книге\s+«[^»]+».{0,30}(чаще|часто|использу\w*|необычн\w*|характерн\w*)"),
     "book_vocab", 0.9),
    (_re(r"в\s+книге\s+.{1,40}\bиспользуются\s+намного\s+чаще"), "book_vocab", 0.9),
    (_re(r"в\s+этой\s+книге\b.{0,40}\b(использу\w*|чаще|необычн\w*|характерн\w*)"),
     "book_vocab", 0.78),
    (_re(r"в\s+книге\s+[«\"“‘][^»\"”’]+[»\"”’].{0,80}"
         r"(чаще|часто|использу\w*|намного\s+чаще)"), "book_vocab", 0.85),
    # Q7-style: «в "Crime and Punishment" используются намного чаще» — title
    # in quotes but no «книге» before it. Stan's updated vault prompts.
    (_re(r"\s+в\s+[«\"“‘][^»\"”’]{4,80}[»\"”’]"
         r".{0,40}\b(использу\w*|чаще|часто|намного\s+чаще)"),
     "book_vocab", 0.9),

    # ===== book_readability =====
    (_re(r"уровень\s+сложн\w*|cefr|flesch|reading\s+(level|grade)|"
         r"насколько\s+сложн\w*|сложн\w+\s+(для\s+чтения|для\s+понимани)"),
     "book_readability", 0.92),

    # ===== book_emotion =====
    # Stan round 2 Q19: «эмоциональный профиль Dracula» — точная фраза из
    # README'а fell to clarify. Add explicit pattern (это distinct от
    # word_emotion который про «слова страха», тут — про NRC profile книги).
    (_re(r"эмоциональн\w*\s+профил\w*|"
         r"emotional\s+profile|"
         r"эмоции?\s+(в|of)\s+[\"'«“]?[A-ZА-Яa-zа-я]"),
     "book_emotion", 0.93),
    (_re(r"\bsentiment\s+(of|in)\s+"), "book_emotion", 0.9),

    # ===== book_archaic =====
    # Bare «архаизм*» used to fire here for any mention — including the
    # negation «чтобы не было слишком много архаизмов» (Q30), which is a
    # book_recommendation query, not a request to list archaisms. Anchor
    # the rule to a positive context: «слова … архаичн», «архаизмы в X»,
    # «список архаизмов», etc.
    (_re(r"\b(?:слов\w*\s+|какие\s+\w*\s*)?\s*(?:архаизм\w*|"
         r"устаревш\w*|архаичн\w*)\b"
         r"(?!\w*\s*(?:не|нет|без))"), "book_archaic", 0.92),
    (_re(r"\bархаизм\w*\s+(?:в|из)\s+"), "book_archaic", 0.95),
    (_re(r"archaic|old[- ]fashioned|outdated\s+words"), "book_archaic", 0.92),

    # ===== book_recommendation =====
    (_re(r"подойд[уё]т\w*|recommend|посоветуй|что\s+почитать|what\s+to\s+read"),
     "book_recommendation", 0.55),
    (_re(r"(подойд[уё]т|recommend|посоветуй|что\s+почитать)\b"
         r".{0,80}\b(b1|b2|c1|c2|уровень|level)"), "book_recommendation", 0.92),
    (_re(r"произведени\w*\b.{0,60}\bдля\b.{0,30}(читател\w*|уровн\w*|level)"),
     "book_recommendation", 0.88),
    # Q30: «какие произведения … можно читать … чтобы не было … архаизмов».
    # The «архаизмов» token used to lock into book_archaic at priority 115
    # (above book_rec's 118 only in pattern strictness, not numerically) and
    # the recommendation rule didn't catch «можно читать» as a trigger. Add
    # a high-confidence rule that pairs «произведения … можно/стоит читать»
    # with a level marker so book_recommendation wins for negation-style
    # phrasings.
    (_re(r"произведени\w*\b.{0,60}\b(можно|стоит)\s+(читать|изучать|освоить)"),
     "book_recommendation", 0.93),
    (_re(r"что\s+(почитать|читать)\b.{0,60}\b(после|подобн|похож|типа)"),
     "book_recommendation", 0.9),

    # ===== word_etymology =====
    (_re(r"этимолог\w*|origin of the word"), "word_etymology", 0.95),
    (_re(r"древнегерманск\w*|скандинавск\w*|германск\w*|"
         r"романск\w*|латинск\w*|french origin|"
         r"romance origin"), "word_etymology", 0.85),
    (_re(r"происхожден\w*\s+слов"), "word_etymology", 0.9),

    # ===== word_pos =====
    (_re(r"больше всего разных значений|polysemy|polysemous|"
         r"разн\w+\s+значени\w*"), "word_pos", 0.9),
    (_re(r"как\s+(noun|verb|adj|сущ\w*|глагол\w*|прилаг\w*)|"
         r"часть\s+речи|pos\s+distribution"), "word_pos", 0.9),
    (_re(r"имеют\s+больше\s+всего\s+разных\s+значений"), "word_pos", 0.95),

    # ===== word_emotion =====
    # Bare `terror|madness` used to live in this pattern but it false-matched
    # ANY mention of those words — including book titles like «At the
    # Mountains of Madness» (Q15) which broke author/book stylistic compare
    # queries into emotion routing. Require «слова/words» anchor or the
    # explicit «рядом со словами …» phrasing (Q35) so only real emotion
    # queries land here.
    (_re(r"слов\w*\s+страх\w*|слов\w*\s+гнев\w*|слов\w*\s+ужас\w*|"
         r"fear words|words of (fear|anger|sadness)|"
         r"тревожн\w+\s+контекст|мрачн\w+\s+контекст|зловещ\w*"),
     "word_emotion", 0.9),
    (_re(r"рядом\s+со?\s+словами\s+\W?(fear|terror|madness|страх|тревог|"
         r"ужас|гнев|радост)"), "word_emotion", 0.95),
    (_re(r"в\s+мрачн\w+\s+или\s+тревожн\w+\s+контекст"), "word_emotion", 0.95),

    # ===== word_timeline =====
    (_re(r"вышли\s+из\s+употреблени\w*|исчезл\w*|исчезают|исчезающ\w*|"
         r"перестали\s+(использовать|встречаться)|"
         r"disappeared after|fell out of use|words that vanished"),
     "word_timeline", 0.92),
    # Round 3 R10: «timeline слова freedom» — direct doc keyword.
    (_re(r"\btimeline\s+слова\s+|timeline\s+(for|of)\s+(the\s+)?word|"
         r"частота\s+слова\s+.{1,30}\s+по\s+(годам|эпох|период)|"
         r"word\s+frequency\s+over\s+(time|years)"),
     "word_timeline", 0.92),
    (_re(r"\b(после|до|after|before)\s+\d{4}\b.{0,80}\b(слов\w*|word)"),
     "word_timeline", 0.7),
    (_re(r"почти\s+исчезают\s+после"), "word_timeline", 0.95),
    (_re(r"популярны\s+у\s+викторианск\w*\b.{0,30}\bисчезл"),
     "word_timeline", 0.9),

    # ===== word_collocates =====
    (_re(r"соседств\w*|collocates?|collocations?"), "word_collocates", 0.9),
    (_re(r"слова\s+рядом\s+со?\s+слов"), "word_collocates", 0.9),
    (_re(r"чаще\s+всего\s+соседствуют\s+со\s+словом"), "word_collocates", 0.95),

    # ===== topic_words =====
    (_re(r"описан\w*\b.{0,30}\b(туман\w*|дожд\w*|погод\w*|сыр\w*|"
         r"мор\w*|sea|fog|rain)"), "topic_words", 0.85),
    (_re(r"чаще\s+всего\s+используются\s+в\s+описаниях"), "topic_words", 0.9),

    # ===== word_contexts =====
    (_re(r"приведи\s+примеры|примеры\s+использов\w*|examples? of usage|"
         r"в\s+каком\s+контексте|usage examples?|оттенки\s+значени"),
     "word_contexts", 0.9),
    # Round 3 R5 «найди упоминания битой посуды» — chat placeholder!
    # Routes to hybrid_search via word_contexts (which falls through to
    # hybrid_search in plan when no author scope).
    (_re(r"\bнайди\s+упоминани\w+|найди\s+(где\s+)?(говорится|описыва\w+)\s+"
         r"(про|о|об)\s+|find\s+mentions?\s+of"), "word_contexts", 0.9),
    (_re(r"у\s+разных\s+авторов\b.{0,40}\bобъясни\s+оттенки"),
     "word_contexts", 0.95),
    (_re(r"в\s+необычных\s+контекстах|обычными\s+сейчас\b.{0,40}\bконтекст"),
     "word_contexts", 0.9),

    # ===== learning =====
    # Q7 (Stan's 2026-05-18 demon round): «20 слов уровня intermediate из
    # "Pride and Prejudice"» — пример прямо из README'а. Старый набор
    # правил не ловил это явное сочетание «N слов уровня X из BOOK».
    (_re(r"\b\d+\s+слов\w*\s+уровня?\s+(b1|b2|c1|c2|intermediate|advanced|basic|"
         r"начальн\w*|средн\w*|продвинут\w*|базов\w*)\s+(из|для)"),
     "learning", 0.95),
    (_re(r"\b\d+\s+(intermediate|advanced|basic)\s+words?\s+(from|for)"),
     "learning", 0.92),
    (_re(r"\b(b1|b2|c1|c2)\b"), "learning", 0.7),
    (_re(r"\b(intermediate|advanced)\b.{0,30}\b(слов\w*|word|vocab)"),
     "learning", 0.85),
    (_re(r"слов\s+для\s+изуч\w*|выучить|для\s+расширени\w*\s+словаря|"
         r"vocabulary to learn|words I should learn|learn the words?"),
     "learning", 0.9),
    (_re(r"лексик[аиу]\s.{0,15}\bвторого\s+уровня"), "learning", 0.95),
    (_re(r"[«\"]?\s*второго\s+уровня\s*[»\"]?"), "learning", 0.9),
    (_re(r"какие\s.{0,40}слов\w*\b.{0,50}не\s+знаю"), "learning", 0.85),
    (_re(r"которые\s+я.{0,30}не\s+знаю"), "learning", 0.88),
    (_re(r"понимаю\s+примерно\s+\d+\s*%"), "learning", 0.92),
    (_re(r"(хотел бы (читать|свободно)|если бы я хотел читать)"
         r".{0,40}свободн"), "learning", 0.95),
    (_re(r"вызывают\s+сложности\b.{0,30}\b(b1|b2|c1|c2|уровн\w*|level)"),
     "learning", 0.92),

    # ===== composite_compare (Q40-style extreme cross-section) =====
    # Q40 routes here instead of country_compare because the query asks for a
    # full lexical differential between two country corpora (with period + CEFR
    # + multi-field output spec). country_compare's plan only fetches top
    # authors per country — useful as a starting point but doesn't produce the
    # actual signature-word contrast Stan's prompt asks for. composite_compare
    # extends that plan with affinity_by_author probes for the leader of each
    # country so the renderer has real word-level contrast data.
    (_re(r"b2[-–—\s]*c1"
         r".{0,200}\bотличают?\s+британск\w*"),
     "composite_compare", 0.96),
    (_re(r"раздели\w*\s+(их\s+)?на\s+британск\w*\s+и\s+американск\w*"
         r".{0,200}b2[-–—\s]*c1"),
     "composite_compare", 0.97),
    (_re(r"раздели\w*\s+(их\s+)?на\s+британск\w*\s+и\s+американск\w*"
         r".{0,200}\bсгруппируй\w*\s+слов"),
     "composite_compare", 0.95),

    # ===== country_compare =====
    (_re(r"британск\w*.{0,15}vs.{0,15}американск\w*|"
         r"american vs british|"
         r"BrE\s*vs\s*AmE"), "country_compare", 0.95),
    (_re(r"в\s+(американск\w*|британск\w*)\s+(литератур\w*|корпус\w*)"
         r".{0,60}(редко|часто)"), "country_compare", 0.92),
    (_re(r"британск\w*.{0,30}и\s+американск\w*\b.{0,80}"
         r"(сравн\w*|перекос|разниц\w*)"), "country_compare", 0.92),
    (_re(r"сравн\w*\s+лексик\w*\s+британск\w*\s+и\s+американск\w*"),
     "country_compare", 0.95),
    (_re(r"раздели\s.{0,30}\bна\s+британск\w*\s+и\s+американск\w*"),
     "country_compare", 0.95),
    (_re(r"который[е]?\s+(сильнее|сильно)\s+(отличают|отличают)\s+"
         r"британск\w*\s+(прозу|литератур\w*)\s+от\s+американск\w*"),
     "country_compare", 0.95),
    (_re(r"отличают\s+британск\w*\s+(прозу|литератур\w*)\s+от\s+американск\w*"),
     "country_compare", 0.95),

    # ===== country_vocab =====
    (_re(r"\bбританск\w*\b.{0,40}\bслов\w*\b"), "country_vocab", 0.7),
    (_re(r"\bамериканск\w*\b.{0,40}\bслов\w*\b"), "country_vocab", 0.65),

    # ===== genre_compare =====
    (_re(r"готическ\w*\s+(прозу|роман\w*)\b.{0,40}"
         r"(реалистическ\w*|реализм\w*)"), "genre_compare", 0.92),
    (_re(r"отличают\s+готическ\w*"), "genre_compare", 0.9),
    (_re(r"приключенческ\w*\b.{0,60}\bроман\w*\s+воспитан\w*"),
     "genre_compare", 0.92),

    # ===== period_vocab =====
    (_re(r"(виктори[аяь]нск\w*|edwardian|пред-?war|pre[- ]1900)"
         r".{0,80}\b(слов\w*|word|vocab)\b"), "period_vocab", 0.85),
    (_re(r"\b(до\s+(1850|1860|1870|1880|1890|1900))\b"
         r".{0,60}\b(слов\w*|word)"), "period_vocab", 0.8),
    (_re(r"женск\w+\s+персонаж\w*|female characters?"), "period_vocab", 0.85),

    # ===== vocab_passport =====
    (_re(r"словарн\w+\s+паспорт|vocabulary passport"), "vocab_passport", 0.97),
    (_re(r"паспорт\s.{0,15}(автор\w*|writer)"), "vocab_passport", 0.92),

    # ===== lexical_wealth =====
    (_re(r"богат\w+\s+словар"), "lexical_wealth", 0.92),
    (_re(r"lexical wealth"), "lexical_wealth", 0.95),
    (_re(r"уникальных\s+лемм|vocabulary size of authors"), "lexical_wealth", 0.9),
    (_re(r"самым\s+\W?богат\w+\s+словар"), "lexical_wealth", 0.92),
    (_re(r"самы(е|м)\s.{0,15}разнообразн\w*\s+словар"), "lexical_wealth", 0.85),
    # Round 3 R12: «сколько слов знал Шекспир» — vocab size question.
    (_re(r"сколько\s+(\w+\s+)?слов\s+(знал|использовал|насчитывал)\s+"
         r"[А-ЯA-ZЁ]|"
         r"how\s+many\s+(unique\s+)?words?\s+did\s+[A-Z]\w+\s+(know|use)|"
         r"vocabulary\s+size\s+of\s+[A-Z]"),
     "lexical_wealth", 0.93),

    # ===== word_dialogue =====
    (_re(r"в\s+диалогах\b.{0,40}\b(чем\s+в|а\s+не\s+в|vs|против|нежели)"),
     "word_dialogue", 0.9),
    (_re(r"авторск\w+\s+текст\w*|в\s+нарратив\w*"), "word_dialogue", 0.6),

    # ===== word_movement =====
    (_re(r"глагол\w*\s+движени\w*|verbs of (motion|movement)"),
     "word_movement", 0.95),
]


# Sprint 17 — pre-sorted rule list for early-break.
#
# `classify()` semantics: pick the rule with (priority desc, confidence
# desc). Since priority is per-intent (not per-rule), once we find ANY
# match at priority P, every later rule with priority < P can be skipped:
# it can't beat P regardless of its confidence. Within the same priority
# bucket we still iterate every rule and keep the highest-confidence
# match. Result: typical short queries that hit a high-priority rule
# (e.g. introduction at 50, or out_of_scope at 200) skip 60-95% of the
# remaining 80+ regex evaluations.
#
# Built ONCE at import time. RULES itself stays declarative (priority
# tied to intent in PRIORITY dict, not in the rule tuple itself) so
# future contributors don't have to keep two lists in sync.
_SORTED_RULES: list[tuple] = sorted(
    RULES,
    key=lambda r: (-PRIORITY.get(r[1], 0), -r[2]),
)


def classify(text: str) -> IntentMatch:
    if not text or not text.strip():
        return IntentMatch("clarify", 0.0)
    s = text.strip().lower()
    best: IntentMatch | None = None
    best_pri: int | None = None
    for pat, intent, conf in _SORTED_RULES:
        rule_pri = PRIORITY.get(intent, 0)
        # Early-break: we already have a match at higher priority — no
        # rule below this bucket can win the (priority desc, conf desc)
        # ordering.
        if best_pri is not None and rule_pri < best_pri:
            break
        if pat.search(s):
            if best is None or conf > best.confidence:
                best = IntentMatch(intent, conf, pat.pattern)
                best_pri = rule_pri
    return best or IntentMatch("clarify", 0.0)


def all_intents() -> set[str]:
    return set(INTENTS)
