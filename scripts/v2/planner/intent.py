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
    # R-27 WP1 (B106) — КНИГИ для изучающих язык («книги почитать для
    # уровня B2», «я учу английский, с чего начать»). Отдельно от
    # `learning` (тот — про СЛОВА по CEFR-банду, learning_words).
    "learning_books",
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
    # Sprint 17 — books semantically similar to a reference book
    "book_similar",      # «похожие на X», «продолжение X», «similar to X»
    # Sprint 18 — ambiguous similarity («в стиле X»). Plan-time
    # disambiguator routes to book_similar OR author_closest based on
    # which entity (book or author) was resolved.
    "similar_to",
    # Sprint 20 — translate-followup escape hatch. When user asks
    # «переведи эти слова» after a word-list turn, this intent surfaces
    # an honest clarify with actionable advice (list the specific words
    # explicitly — 5-10 fit in chat timeout). Real solution lives in
    # v4 LLM planner that can extract words from conversation history.
    "translate_word_list",
    # Sprint 20+ B3 — export-followup. «выгрузи в anki/csv/markdown/json».
    # Like translate_word_list, requires a prior word-list assistant turn.
    "export_word_list",
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
    # Sprint 20+ B3 — export-followup. High priority so «выгрузи в anki»
    # doesn't get swallowed by book_recommendation or generic clarify.
    # Below out_of_scope (200) but above all content intents.
    "export_word_list": 180,
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
    # Sprint 17 — «похожие на X» / «продолжение X». Above
    # book_recommendation (118) and book_compare (110) so a specific
    # similar-to-reference query wins over generic «что почитать».
    "book_similar": 146,
    # Sprint 18 — ambiguous similarity router. Below book_similar so
    # the explicit-book rules win; only kicks in when neither
    # book_similar nor author_closest matched directly. Above
    # book_recommendation 118.
    "similar_to": 130,
    "vocab_passport": 150,
    "composite_compare": 145,
    "translation_quality": 140,
    "country_compare": 135,
    "genre_compare": 130,
    "word_etymology": 125,
    "word_pos": 125,
    # R-27 WP1 (B106) — above book_lookup (122) so «есть ли у тебя книги
    # для изучающих английский» becomes a learner recommendation, and
    # above book_recommendation (118) so learner-context phrasings win
    # over the generic «подойдут/посоветуй» rules. Below similar_to
    # (130): «книги в стиле X» stays a similarity query.
    "learning_books": 124,
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
    # Sprint 18+ Round 9 N2 — vague curiosity. «расскажи что-нибудь
    # интересное», «удиви меня», «что у тебя есть интересного». Map
    # to introduction — the static intro text lists capabilities + 4
    # example queries, which is exactly what a vague-curiosity user
    # wants (concrete next steps, not «I don't understand»).
    (_re(r"\bрасскажи\s+(мне\s+)?что[\s-]+нибудь|"
         r"\bудиви\s+меня|"
         r"\bпокажи\s+что[\s-]+нибудь|"
         r"\bчто\s+(у\s+тебя\s+)?есть\s+интересн\w+|"
         r"\bчто\s+(ты\s+)?можешь\s+рассказать"
         r"(?:\s+(интересн\w*|нового|полезн\w*))?|"
         r"\bsurprise\s+me|"
         r"\btell\s+me\s+something\s+(cool|interesting)"),
     "introduction", 0.85),
    # Sprint 19+ — meta-questions about the service itself. Stan
    # 2026-05-19: naïve users ask «что за сервис», «кому ты подойдёшь»,
    # «это бесплатный?», «как работает» — all fell to clarify. Same
    # answer surface as «привет / что ты умеешь» — render the intro
    # with capabilities + value prop + 4 starter examples.
    (_re(r"\bчто\s+(это\s+|здесь\s+)?(за\s+)?сервис|"
         r"\bкак\s+(ты|это|вы|сервис)\s+(работ\w+|устрое\w+)|"
         r"\b(для\s+)?кого\s+(ты|вы|сервис)|"
         r"\bкому\s+(ты\s+)?(подойд\w+|нужен|полезен)|"
         r"\bдля\s+чего\s+(ты\s+)?(нужен|полезен|сделан)|"
         r"\bзачем\s+(ты\s+|этот\s+)?(нужен|сервис)|"
         r"\bwhat\s+(is\s+)?(this|that)\s+(service|tool|chatbot|app|bot)|"
         r"\bwho\s+(is\s+)?this\s+(for|service)|"
         r"\bhow\s+do(es)?\s+(this|you|it)\s+work|"
         r"\bwhat\s+do\s+you\s+do\b"),
     "introduction", 0.9),
    # Sprint 19+ — pricing / freeness. Treat as introduction (a single
    # honest sentence: «бесплатный исследовательский проект, поднят
    # для личного использования, доступ через Basic Auth») fits into
    # the intro text naturally. No separate pricing intent yet — keep
    # the surface small.
    (_re(r"\b(это\s+)?(бесплатн\w+|платн\w+)\s+(сервис|чат|инструмент|тул|tool|service|bot)|"
         r"\bсколько\s+стоит\s+(использование|сервис|это)|"
         r"\bplatish?\s+ли|"
         r"\bis\s+(this|it)\s+free\b|"
         r"\bdo\s+you\s+charge|"
         r"\bpricing|cost"),
     "introduction", 0.88),
    # Sprint 19+ — architecture / pipeline question. «покажи (свою)
    # схему», «как ты устроен», «что делает planner/router/renderer».
    # The intro text describes the pipeline; route here.
    (_re(r"\b(покажи\s+)?(свою\s+|твою\s+)?(схему|архитектур\w+|pipeline|"
         r"архитектура|устройств\w+)|"
         r"\bчто\s+делает\s+(planner|router|renderer|critic|пайплайн)|"
         r"\bshow\s+(me\s+)?(your\s+)?(architecture|pipeline|schema)"),
     "introduction", 0.9),
    # Sprint 18+ Round 9 N8 — «сколько страниц в КНИГЕ?». Pages не наш
    # metric (corpus stored as tokens). Route to corpus_stats_by_author
    # via book_vocab? Нет — нужен per-book token count. Route to
    # book_readability — он возвращает words + sentences + Flesch
    # которые ближе всего к «сколько страниц» (renderer пересчитает
    # tokens → ~250 words/page как стандарт).
    (_re(r"\bсколько\s+страниц\s+в\b|"
         r"\bобъём\s+(книги|романа)|"
         r"\bhow\s+(many|long)\s+pages\s+(in|is)\b|"
         r"\bword\s+count\s+(in|of|for)\b"),
     "book_readability", 0.88),

    # ===== translation request =====
    # Round 3 R14: «переведи мне X на Y» — translation. Не translation_quality
    # (это про качество существующих переводов), а translation OOS — мы не
    # переводчик. Routes to out_of_scope с friendly note.
    (_re(r"\bпереведи\s+(мне|нам)?\s*.{1,60}\s+на\s+(английск|русск|немецк|"
         r"французск|испанск|итальянск|english|russian)|"
         r"\btranslate\s+(this|the\s+\w+|.{1,40})\s+(to|into)\s+\w+"),
     "out_of_scope", 0.93),
    # Sprint 18+ Round 9 N9 — «переведи фразу / выражение / цитату X»
    # без указания целевого языка. Тоже OOS — translation не наш scope.
    (_re(r"\bпереведи\s+(мне\s+)?(эту?\s+|это\s+)?"
         r"(фраз\w*|выражени\w*|цитат\w*|строк\w*|предложени\w*)"),
     "out_of_scope", 0.92),
    (_re(r"\btranslate\s+(this|the)?\s*(phrase|line|quote|sentence|expression)"),
     "out_of_scope", 0.9),

    # E43 (2026-05-22) — Stan prod «что у тебя с копирайтом» routed to
    # corpus_meta (wrong: returned tool stats instead of policy answer).
    # Copyright / licensing / legal-status questions about the project
    # are out-of-scope of the corpus analysis tools — route to OOS.
    # NB: must NOT match «copyright coverage / share / count» — those
    # are legitimate corpus_meta enumeration questions. Discriminator:
    # POLICY questions usually phrase as «что у тебя с …», «как с …»,
    # «можно ли …», или используют license/правооблада keyword which
    # has no count-equivalent.
    (_re(r"что\s+(у\s+тебя\s+)?с\s+(копирайт\w*|лицензи\w+|авторск\w+\s+прав\w*)|"
         r"как\s+(у\s+тебя\s+)?с\s+(копирайт\w*|лицензи\w+|авторск\w+\s+прав\w*)|"
         r"\b(лицензи\w+|правооблада\w+)\b|"
         r"\bавторск\w+\s+прав\w*\b|"
         r"можно\s+ли\s+(использовать|копировать|скачивать)"),
     "out_of_scope", 0.92),

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
    # W-16 (2026-05-23, Phase 5 P2) — Russian role-play imperatives.
    # «Притворись X», «представь, что ты X», «отыграй роль», «изобрази
    # X», «play the role of X», «act as X». Hits BEFORE plan-builder
    # so the chatbot doesn't accept the role and start asking «о чём
    # будем писать?». Tight enough to not catch legitimate analytics:
    # the trigger verbs must be followed by a noun (critic, writer,
    # author, character, narrator, persona, expert, …) within ~40
    # chars. NB: `представь себе X» is NOT a role-play opener (it's
    # «imagine X»); we anchor on «что ты / что я / себя X» which IS.
    (_re(r"\b(притворис[ья]|притворитесь|прикинься|прикиньтесь|"
         r"изобрази(?:те)?|сыграй(?:те)?|отыграй(?:те)?\s+роль|"
         r"вой?ди\s+в\s+роль|войди\s+в\s+образ|"
         r"представь(?:те)?,?\s+(?:что\s+)?(?:ты|вы|себя)|"
         r"act\s+as(?:\s+a)?|"
         r"play\s+(?:the\s+)?role(?:\s+of)?|"
         r"role[-\s]?play\s+as|"
         r"be\s+(?:a|an|the)\s+(?:critic|writer|author|poet|narrator|"
         r"character|persona|expert|reviewer|editor|professor))\b"),
     "out_of_scope", 0.95),
    (_re(r"(напиши|сочини|допиши|сгенерируй|составь)\s.{0,40}"
         r"(рассказ\w*|стих\w*|поэм\w*|глав\w*|стат\w*|"
         # W-16 — essays / reviews / fiction continuations are the
         # canonical role-play artifacts. «напиши эссе» fell through
         # because «эссе» wasn't in the artifact list.
         r"эссе|сочинени\w*|реценз\w*|обзор\w*|критическ\w+\s+(?:разбор|"
         r"анал\w*|текст)|"
         r"пародию|пастиш|пастиче|"
         r"стилизаци\w*|"
         # «продолжение текста / истории / романа»
         r"продолжени\w*\s+(?:текста|истории|романа|главы|повести))"),
     "out_of_scope", 0.95),
    (_re(r"(write|compose|draft|generate)\s.{0,40}"
         r"(story|poem|chapter|novel|article|essay|review|"
         r"pastiche|critique|continuation|fan[-\s]?fic\w*)"),
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
    # B-R17-1 stage3.2 v5 — admit filler words between «книги» and «у»:
    # «какие книги ЕСТЬ у Wells?», «какие книги имеются у X», «what books
    # are by X». Without the filler tolerance, queries like Stan's
    # «какие книги есть у Wells?» fell through to v4 LLM-planner which
    # built a top_books_by_downloads plan, bypassing the rules-path
    # ambiguous-author clarify.
    (_re(r"\b(какие|каких|сколько\s+разных|перечисли|список|"
         r"покажи\s+(все|список|книги)?)\s+"
         r"(книг|произведен|роман|works)\w*\s+"
         r"(?:(?:есть|имеются|имеется|существуют|написаны|написал\w*|"
            r"are|exist)\s+)?"
         r"(у\s+|при\s+|of\s+|by\s+|написал\w*\s+)"),
     "author_lookup", 0.92),
    (_re(r"\bwhat\s+(books?|works?|novels?)\s+(does|did|by|of)\s+"
         r"[A-Z]\w+"),
     "author_lookup", 0.9),
    (_re(r"\b(что|чё)\s+(есть|у\s+тебя\s+есть)\s+(у|от|of)\s+"
         r"[A-ZА-ЯЁ]\w+\s*(в\s+корпус|в\s+базе)?"),
     "author_lookup", 0.85),
    # E1b (S-R5, 2026-05-31) — bare-genitive author catalog, NO preposition:
    # «книги Диккенса», «произведения Толстого», «works Dickens». Stan: these
    # fell to clarify (no rule matched) → v4 LLM-planner → ~16s flake, while
    # the prepositional form «какие книги у Диккенса» resolved deterministically
    # in 2-4s. The Phase E rules above REQUIRE a preposition («у|при|of|by»)
    # precisely because a naive bare «книг + Capital» false-positives under
    # IGNORECASE on Q30-style «произведения подойдут…» (see comment above).
    #
    # What makes the no-prep form safe now is the (?-i:[A-ZА-ЯЁ][a-zа-яё])
    # proper-noun guard — our classify-time author-presence proxy, the same
    # idiom as the E30 book_readability and book_emotion guards. The real
    # author resolution still happens downstream in extract() → author_regex;
    # this rule only steers the query onto the deterministic author_lookup
    # path instead of the LLM-flake clarify. The guard discriminates:
    #   · capital-THEN-lowercase = a real surname (Диккенса / Толстого /
    #     Dickens), NOT a roman-numeral century — «книги XIX века» is X then
    #     UPPERCASE I, excluded → stays book_extremum (W11BookRanking) — and
    #     NOT a CEFR level «книги B2» (capital-then-digit, excluded).
    #   · IGNORECASE-disabled capital = the token must START uppercase, so
    #     lowercase topic/verb phrasings stay out: «книги про войну», «книги о
    #     космосе» (preposition lowercase → topic_book_search), «произведения
    #     подойдут…» (Q30 verb lowercase → book_recommendation).
    # Plural noun only (книги/произведения/works, NOT singular «книга») so a
    # specific-title phrasing «книга Война и мир» stays book_lookup. Confidence
    # 0.83 < the prepositional rules so they still win when both match (same
    # label either way).
    (_re(r"\b(книги|произведения|works)\s+"
         r"[«\"'“‘]?(?-i:[A-ZА-ЯЁ][a-zа-яё])"),
     "author_lookup", 0.83),

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
    # W-11 (Phase 5 P2, 2026-05-23) — plural ranking. «какие книги XIX
    # века самые сложные», «какие самые простые романы у викторианцев»,
    # «the hardest books of the 1800s». No single tool ranks books by
    # readability/archaic-density across a period (BookProfile.archaic_
    # density isn't built yet, see book_recommendation B9 note). Plan
    # builder for book_extremum returns a fast smart-clarify recipe
    # instead of LLM-fallback 50-60s parse-fail.
    #
    # IMPORTANT: leading marker MUST be a real ranking word («самые|
    # наиболее|the most|hardest»). The earlier draft accepted bare
    # «какие/какой» as leading, which mis-classified Q30 («Какие
    # произведения подойдут для читателя уровня B2…») — that's a
    # book_recommendation, not a ranking query.
    #
    # Three orderings covered (each ranking marker appears between BOOK
    # and DIFFICULTY, or before both):
    #   RANK.{0,80}BOOK.{0,80}DIFF     — «самые ... книги ... сложные»
    #   RANK.{0,80}DIFF.{0,80}BOOK     — «самые сложные книги»
    #   BOOK.{0,80}RANK\s+DIFF         — «<question> книги <period> самые сложные»
    (_re(r"\b(самые|наиболее|the\s+most|hardest|simplest|easiest|"
         r"most\s+(?:complex|difficult|archaic|simple|popular))\b.{0,80}"
         r"\b(книг\w+|роман\w+|произведен\w+|повест\w+|"
         r"books?|novels?|works?)\b.{0,80}"
         r"\b(сложн\w+|трудн\w+|архаичн\w+|устаревш\w+|"
         r"прост\w+|лёгк\w+|легк\w+|"
         r"complex|difficult|hard|archaic|simple)\b"),
     "book_extremum", 0.88),
    (_re(r"\b(самые|наиболее|the\s+most|hardest|simplest|easiest|"
         r"most\s+(?:complex|difficult|archaic|simple|popular))\b.{0,80}"
         r"\b(сложн\w+|трудн\w+|архаичн\w+|устаревш\w+|"
         r"прост\w+|лёгк\w+|легк\w+|"
         r"complex|difficult|hard|archaic|simple)\b.{0,80}"
         r"\b(книг\w+|роман\w+|произведен\w+|повест\w+|"
         r"books?|novels?|works?)\b"),
     "book_extremum", 0.88),
    # «<question> книги <period> самые сложные» — BOOK comes first,
    # ranking marker + difficulty trail at the end.
    (_re(r"\b(книг\w+|роман\w+|произведен\w+|повест\w+|"
         r"books?|novels?|works?)\b.{0,80}"
         r"\b(самые|наиболее|the\s+most|hardest|simplest|easiest|"
         r"most\s+(?:complex|difficult|archaic|simple|popular))\s+"
         r"(?:\w+\s+){0,2}"
         r"\b(сложн\w+|трудн\w+|архаичн\w+|устаревш\w+|"
         r"прост\w+|лёгк\w+|легк\w+|"
         r"complex|difficult|hard|archaic|simple)\b"),
     "book_extremum", 0.88),
    # English compact form — leading adjective IS the difficulty.
    (_re(r"\b(hardest|simplest|easiest|"
         r"most\s+(?:complex|difficult|archaic|simple|popular))\s+"
         r"(?:\w+\s+){0,2}"
         r"\b(books?|novels?|works?)\b"),
     "book_extremum", 0.88),

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
    # R-27 WP1 meta (§10 scope) — обратный порядок: «какой автор самый
    # популярный?» / «какая книга самая длинная?» (существительное ДО
    # «самый»). Старые правила требовали «самый … автор/книга» и эти
    # формулировки падали в clarify → v4 LLM ~40s.
    (_re(r"\bкакой\s+автор\s+сам(?:ый|ая)\s+"
         r"(плодовит|популярн|читаем|скачиваем|известн)"),
     "corpus_extremum", 0.92),
    (_re(r"\bкакая\s+книга\s+(?:в\s+корпусе\s+)?сам(?:ая|ый)\s+"
         r"(длинн|больш|коротк|маленьк|популярн|скачиваем|сложн|прост|древн)"),
     "book_extremum", 0.92),

    # ===== Sprint 16 Phase F — topic_book_search =====
    # «найди книгу про X», «посоветуй роман о Y», «book about Z». Routes
    # to find_book_by_topic which wraps hybrid_search and dedupes by
    # pg_id. Differs from book_lookup (title search) and book_recommendation
    # (popularity / level filter): this is SEMANTIC by topic.
    (_re(r"\b(найди|поищи|посоветуй|подскажи|recommend|find)\s+"
         r"(?:мне\s+|me\s+|a\s+)?"
         r"(книг\w*|роман\w*|произведен\w*|рассказ\w*|повест\w*|"
         r"book|novel|story|short\s+story|tale)\s+"
         r"(про|о|об|на\s+тему|about|on)\s+"),
     "topic_book_search", 0.93),
    # «книга о/про/about X» — Stan asked Round 6 for «роман про викторианский
    # Лондон»; the prior path went to book_lookup which returns titles.
    (_re(r"\b(книг\w*|роман\w*|произведен\w*|рассказ\w*|повест\w*|"
         r"book|novel|story|tale)\s+"
         r"(про|о|об|about|на\s+тему)\s+\w+"),
     "topic_book_search", 0.85),
    (_re(r"\bчто\s+почитать\s+(про|о|об|на\s+тему|about)\s+"),
     "topic_book_search", 0.9),

    # ===== Sprint 17 — book_similar =====
    # «книги похожие на X», «продолжение X», «similar to X», «like X».
    # Distinct from author_closest («кто похож на Doyle» — author probe)
    # — book_similar rules REQUIRE explicit book context («книг/роман/
    # произведен» word, OR «по жанру/стилю» qualifier, OR a quoted title,
    # OR an explicit «similar to + capitalized X» where X is later
    # resolved to a book entity). Without that constraint, «похож на» is
    # too broad and steals author_closest queries.
    #
    # Critical: catches renderer's own follow-up suggestion «хочу по
    # жанру похожему на X» — Stan 2026-05-19 was clicking on it only to
    # land in clarify (UX trap: system suggesting a phrasing it can't
    # itself classify).
    (_re(r"\b(книг\w*|роман\w*|произведен\w*)\s+(похож\w*|подобн\w*)\s+на\s+"),
     "book_similar", 0.93),
    (_re(r"\bпохож\w*\s+по\s+(жанру|стилю|тематике)"),
     "book_similar", 0.92),
    (_re(r"\b(похож\w*|подобн\w*)\s+на\s+[«\"„]"),    # quoted target
     "book_similar", 0.92),
    (_re(r"\b(хочу|посоветуй|подскажи)\s+"
         r"(?:книг\w*|роман\w*|произведен\w*)\s+"
         r"(?:по\s+(жанру|стилю|тематике)\s+)?"
         r"(похож\w*|подобн\w*)"),
     "book_similar", 0.9),
    # «продолжение X» — bare title accepted. The «продолжение» trigger
    # is a strong book signal (never used for authors). Requires only a
    # capitalized token after.
    (_re(r"\bпродолжени\w+\s+(?:[«\"„])?[A-ZА-ЯЁ]"),
     "book_similar", 0.88),
    # English: «(recommend|find|suggest) books similar to/like X». The
    # «books/novels» token is required to avoid stealing «authors similar
    # to» phrasings.
    (_re(r"\b(recommend|suggest|find)\s+(?:me\s+|a\s+)?"
         r"(?:books?|novels?)\s+(?:similar|like)\s+(?:to\s+)?"),
     "book_similar", 0.92),
    (_re(r"\b(books?|novels?)\s+(similar\s+to|like|in\s+the\s+style\s+of)\s+"
         r"[\"«]?[A-Z]"),
     "book_similar", 0.9),
    # Sprint 18 — ambiguous similarity. «в стиле X» / «подобное на X» /
    # «типа X» without explicit book/author marker. Routes to similar_to;
    # plan disambiguates by extracted entity (book → book_similar,
    # author → author_closest). Avoids regex IGNORECASE neutralizing
    # the capital-letter check.
    (_re(r"\b(в\s+стиле|in\s+the\s+style\s+of|типа\s+как)\s+\w"),
     "similar_to", 0.83),
    # Sprint 20+ B8 — «sequel to X» / «в продолжение X» / «что почитать
    # после X / what to read after X» — post-reading-recommendation
    # queries. Without an explicit «похож на» these fell to clarify or
    # find_book_by_topic. Now route to book_similar so the plan uses
    # the reference book.
    #
    # The bare Russian «после X» (без verb) is intentionally NOT here —
    # it's too ambiguous and steals book_recommendation phrasings like
    # «какие книги читать после Шерлока Холмса для B2 без архаизмов».
    # Where the verb makes the intent explicit («что почитать после X»),
    # there's a separate rule at line ~834 («что почитать ... после»).
    (_re(r"\bв\s+продолжени\w+\s+(?:[«\"„])?[A-ZА-ЯЁ]"),
     "book_similar", 0.88),
    (_re(r"\bsequel\s+to\s+[\"«]?[A-Z]"),
     "book_similar", 0.92),
    # «what to read after X» / «what to read next after X» / «what to
    # read next, X». The negative lookahead guards against B2/no archaic
    # recommendation framings. Accept comma after «next»: «what to read
    # next, Sherlock Holmes».
    (_re(r"\bwhat\s+to\s+read\s+(?:after|next)[\s,]+"
         r"(?!.*\b([A-C][12]\s+level|no\s+archaic))"),
     "book_similar", 0.85),
    (_re(r"\bafter\s+reading\s+(?:[\"«]?[A-Z])"
         r"(?!.*\b([A-C][12]\s+level|no\s+archaic))"),
     "book_similar", 0.82),

    # ===== Sprint 16 Phase G — book_pub_year =====
    # «когда была опубликована Война и мир», «год издания Pride and
    # Prejudice», «when was Dracula published». Surfaces pub_year via
    # find_book (Sprint 9.7 Open Library enrichment).
    # Sprint 18: dropped «появил\w*» from the rule — too generic, was
    # eating «когда появилось слово radio» (word_timeline) and «когда
    # появились telephone, automobile» (multi-word timeline). The
    # remaining verbs (опубликован/издан/вышл/написан) are book-specific.
    (_re(r"\b(когда\s+(была\s+|был\s+)?"
         r"(опубликован\w*|издан\w*|вышл\w*|написан\w*)|"
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
    # E25 (2026-05-22) — persona-beginner Q6: «что сложнее для чтения —
    # "Дракула" или "Франкенштейн"» matched book_readability (single)
    # instead of compare because patterns above require literal «читать».
    # Pattern «для чтения … или …» mirrors the «читать … или …» rule
    # but for the «для чтения»/«для понимания» phrasing.
    (_re(r"\b(сложнее|сложней|труднее|трудней|легче|проще)\s+"
         r"для\s+(чтения|понимани\w*)\b.{0,80}\b(или|vs|против)\b"),
     "book_readability_compare", 0.94),
    (_re(r"\bчто\s+(сложнее|труднее|легче|проще)\s+"
         r"для\s+(чтения|понимани\w*)\b"),
     "book_readability_compare", 0.92),
    (_re(r"\b(harder|easier|simpler|more\s+difficult|more\s+complex)\s+to\s+read\b"
         r".{0,60}\bor\b"),
     "book_readability_compare", 0.92),
    (_re(r"\b(which|what)\s+is\s+(harder|easier|simpler)\s+to\s+read\b"),
     "book_readability_compare", 0.9),
    # Phase 4 W-5 (2026-05-23) — «что сложнее — X или Y» БЕЗ «читать» /
    # «для чтения». Stan test bench: «что сложнее — Дракула или
    # Франкенштейн» — фраза без явного «читать», но это readability-
    # compare по смыслу. Anchor on «сложнее/легче/проще/труднее ... или»
    # (присутствие «или» отделяет от single-book «X сложнее всех») плюс
    # отсутствие явного слово-фокуса («слов/word»). Confidence 0.86 —
    # ниже явных «читать»-rules чтобы они выигрывали когда есть.
    (_re(r"\b(что|какая|какой|кто|which|what)\s+(?:is\s+)?"
         r"(сложнее|сложней|труднее|трудней|легче|проще|harder|easier|simpler)\b"
         r"(?!\s+(?:слов|word))"
         r".{1,120}\b(или|or|vs|versus|против)\b"),
     "book_readability_compare", 0.86),

    # ===== corpus_meta =====
    # Stan round 5: «сколько у Толстого книг» wrongly routed to corpus_meta
    # (total) instead of author_metadata. Added negative lookahead — if the
    # «сколько...книг» phrase has a capitalized name in between, it's an
    # author-specific question and author_metadata's more specific rule
    # should win (also bumped its conf above 0.95). The lookahead handles
    # «сколько у Толстого книг», «сколько у X книг» staying out of
    # corpus_meta.
    # R15 Q88 extension: also exclude когда после «сколько книг» идёт
    # «написал/писал/wrote/published» с capitalized именем автора —
    # это author_metadata, не corpus_meta. Без этого fix:
    # «сколько книг написал Marlowe» → corpus_meta → renderer фабрикует
    # PG id'ы (PG1342 = Pride and Prejudice как «Doctor Faustus»).
    (_re(r"\bсколько\s+(книг|book)\b"
         r"(?!\w*\s+(у|of)\s+[А-ЯA-ZЁ])"
         r"(?!\s+(написал\w*|писал\w*|создал\w*|опубликовал\w*|"
         r"wrote|written|published|created|authored)\s+[A-ZА-ЯЁ])"),
     "corpus_meta", 0.95),
    (_re(r"\bhow many (книг|books?)\b"
         r"(?!\w*\s+(?:did|has|have)\s+[A-Z]\w+\s+"
         r"(?:writ\w+|publish\w+|create\w+|author\w*))"),
     "corpus_meta", 0.95),
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
    # R15 Q88: «сколько книг написал Christopher Marlowe» — иной word order
    # (без «у», глагол «написал/писал»). Без этого правила routes в
    # corpus_meta, рендерер фабрикует PG id'ы (PG1342 = Pride and Prejudice
    # выдан за Doctor Faustus Marlowe). Высокий приоритет — author всегда
    # explicit в этой формулировке. Покрывает: «написал/писал/создал»,
    # русский + английский варианты, «произведений» вместо «книг».
    (_re(r"сколько\s+(?:книг|произведений|работ|стих\w*|драм\w*)\s+"
         r"(?:написал\w*|писал\w*|создал\w*|опубликовал\w*)\s+"
         r"[A-ZА-ЯЁ]\w+"),
     "author_metadata", 0.95),
    (_re(r"how\s+many\s+(?:books?|works?|plays?|poems?|novels?)\s+"
         r"(?:did|has|have)\s+[A-Z]\w+\s+"
         r"(?:writ\w+|publish\w+|produce\w+|author\w*)"),
     "author_metadata", 0.95),
    # Defensive — «сколько X у автора Y» / «количество книг автора Y»
    (_re(r"количество\s+(?:книг|произведений)\s+(?:у\s+)?[A-ZА-ЯЁ]\w+"),
     "author_metadata", 0.9),
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
    # Sprint 18+ Round 9 N10 — «кто такой X» / «who is X». Bibliographic
    # author probe. IGNORECASE neutralizes the capital-letter check, so
    # we just match the trigger phrase and let the entity extractor's
    # alias dict do the proper-noun work. If no author resolves, plan
    # bounces to _need_author with the curated suggestion list.
    (_re(r"\bкто\s+так(ой|ая|ие)\s+\w"),
     "author_metadata", 0.92),
    # Sprint 19+ — case-sensitive proper-noun guard, plus negative
    # lookahead for service-meta words («this / that / it / here /
    # the service / this for / this bot») which shouldn't trigger
    # author_metadata (caught by introduction's meta-question rules).
    (_re(r"\bwho\s+(is|was)\s+"
         r"(?!(?:this|that|it|here|the|free|a)\b)"
         r"(?-i:[A-Z])\w+"),
     "author_metadata", 0.88),

    # ===== book_lookup (Q2 from demon round) =====
    # «найди книгу X», «есть ли у тебя X», «где книга X», «is X in the
    # corpus» — pure resolution query, route to find_book directly so the
    # answer is concrete (PG id + title + author + downloads) instead of a
    # generic clarify. Routes through `book_recommendation` plan-builder
    # is wrong — that gives popular-books listing. We need a dedicated
    # `book_lookup` intent → `find_book` tool.
    (_re(r"\b(найди|поищи|есть\s+ли\s+у\s+тебя)\s+книг\w*\s+"),
     "book_lookup", 0.93),
    # Sprint 19+ — «метаданные книги PG1342» / «metadata of <book>» /
    # «инфа по книге X». Entity extractor pulls the PG id; book_lookup
    # tool surfaces title/author/downloads/year — exactly what
    # «метаданные» asks for.
    (_re(r"\b(метаданн\w+|метадат\w+|инфа|информаци\w+)\s+(книги|по\s+книге|book)"),
     "book_lookup", 0.92),
    (_re(r"\bmetadata\s+(of|for|about)\s+(the\s+)?book"),
     "book_lookup", 0.9),
    (_re(r"\b(что\s+ты\s+знаешь|info|инфо)\s+(о|про|about)\s+(книге?\s+)?"
         r"PG\d+"),
     "book_lookup", 0.9),
    # Sprint 18+ Round 9 N6 — «у тебя есть X?» / «есть ли у тебя X?»
    # availability check with X as a known book title. Triggers when
    # entity extractor resolved book_id/book_title — _plan_book_lookup
    # surfaces metadata if PG-indexed, or copyright OOS message if
    # known-but-not-indexed (HP / LOTR / etc).
    (_re(r"\b(у\s+тебя\s+есть|есть\s+ли\s+у\s+тебя|у\s+вас\s+есть|"
         r"do\s+you\s+have|is\s+there)\s+(книг\w*\s+|book\s+)?[«\"„]?"),
     "book_lookup", 0.85),
    (_re(r"\bis\s+(the\s+)?book\s+.{2,60}\s+in\s+(the\s+)?(corpus|library)"),
     "book_lookup", 0.9),
    (_re(r"\bкнига\s+.{2,60}\s+есть\s+у\s+тебя"),
     "book_lookup", 0.9),
    # R-27 WP1 meta (§10 scope) — bare presence probe: «Шекспир есть?»,
    # «есть Диккенс?». Proper-noun guard (capital first letter) +
    # wh-word stop-list so «что есть?» / «кто есть?» don't fire.
    # _plan_book_lookup disambiguates at plan time: resolved AUTHOR →
    # author_metadata (да/нет + счётчики), resolved book → find_book.
    (_re(r"^\s*(?!(?:что|кто|где|как|какие|сколько)\b)"
         r"(?-i:[А-ЯЁA-Z])[\w.'-]{2,}\s+(?:есть|имеется)\s*\?|"
         r"^\s*есть\s+(?:ли\s+)?(?-i:[А-ЯЁA-Z])[\w.'-]{2,}\s*\?"),
     "book_lookup", 0.8),
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
    # Phase 4 W-5 (2026-05-23) — generic «compare X and Y» / «сравни X и
    # Y». Originally tagged author_compare only — but the same phrasing
    # is used for books, so the plan builder redirects to book_compare
    # when ≥2 book entities surface and no authors. Confidence stays at
    # 0.95 since the intent classifier doesn't see entities.
    (_re(r"\b(сравни|compare)\s+.{1,60}\s+(и|and|vs|with)\s+"), "author_compare", 0.95),
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
    # Sprint 18 — bibliographic «who wrote X» / «кто написал X». Plan
    # dispatches via _plan_author_attribution which now routes to
    # book_lookup if a book entity is present. Pure stylometry queries
    # («angle: passage») still hit the «кто автор» / «определи автора»
    # path above.
    (_re(r"\b(who\s+wrote|кто\s+написал)\s+[«\"„]?[A-ZА-ЯЁ]"),
     "author_attribution", 0.92),
    (_re(r"\bwho\s+is\s+the\s+author\s+of\s+[«\"„]?[A-ZА-ЯЁ]"),
     "author_attribution", 0.91),
    # Sprint 17 Round 8 C7: «угадай автора отрывка X» / «whose passage
    # is this» — natural phrasings the existing «кто автор» rule misses.
    (_re(r"\b(угадай|отгада[йт]|определи|пойми|identify|guess|determine)\s+"
         r"(?:кто\s+)?(?:the\s+)?(?:автор\w*|автора|author)\s+"
         r"(?:of\s+(?:this|the))?\s*"
         r"(?:этого\s+|этот\s+)?"
         r"(?:отрывк\w*|текста|строк|цитат\w*|"
         r"passage|excerpt|quote|line|text|prose)"),
     "author_attribution", 0.93),
    # «чей (это|этот) отрывок» / «whose passage» — bare interrogative.
    # Allow либо «это» (neuter, demonstrative pronoun «this is»),
    # либо «этот/эта/эти» (adjective forms), либо ничего.
    (_re(r"\b(чей|чья|чьи|whose)\s+"
         r"(?:это\s+|этот\s+|эта\s+|эти\s+)?"
         r"(?:is\s+)?"
         r"(?:this\s+)?"
         r"(?:отрывок|отрывк\w*|пассаж|стиль|"
         r"passage|excerpt|text|prose|line|quote)"),
     "author_attribution", 0.9),
    (_re(r"\b(угадай|отгада[йт]|guess|identify)\s+автора\s+[«\"„]"),
     "author_attribution", 0.92),
    # EN: «who is the author of this/the (passage|text|excerpt|prose)»
    # — bibliographic «who wrote BookTitle» intentionally NOT matched
    # here (that's book_lookup territory).
    (_re(r"\bwho\s+(is|wrote|made)\s+(the\s+)?author\s+of\s+"
         r"(?:this|the|that)\s+"
         r"(passage|text|excerpt|quote|prose|line|paragraph)"),
     "author_attribution", 0.93),

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
         r"distinctive (vocabulary|words)|"
         # Sprint 20 — Stan 2026-05-19 «сто любимых слов Дойла». Living-
         # language phrasings: «любимые слова», «любимая лексика»,
         # «favourite words», «his go-to vocabulary».
         r"любим\w+\s+(слов\w*|лексик\w*|выражен\w*)|"
         r"favou?rite\s+(words|vocabulary|phrases)|"
         r"go-?to\s+(vocabulary|words)|"
         # Sprint 20 — Stan 2026-05-19 «топ 100 аффинных слов Агаты
         # Кристи». Technical term «аффинн*» / «affinity words» —
         # power users skip «фирменные» / «характерные» and call them
         # what the underlying metric is named.
         r"аффинн\w+\s+слов\w*|"
         r"\baffinity\s+words?\b|"
         r"high-affinity\s+(words|vocabulary)"), "author_vocab", 0.85),
    # «Слова Толкина из LOTR», «лексика Свифта», — generic vocab queries
    # that name an author / book without a "characteristic" keyword.
    (_re(r"^\s*слова\s+[A-ZА-Я]\w+"), "author_vocab", 0.65),
    (_re(r"\bлексик[аиу]\s+[A-ZА-Я]\w+"), "author_vocab", 0.7),
    # R15 Q79 — «общие слова Мелвилла, Конрада и Стивенсона» fell through to
    # generic clarify (0 tool_calls) because line 791 requires «^слова» at
    # start. _plan_author_vocab already parallels affinity_by_author × N via
    # multi_author_regex (Sprint 11.3), so we just need intent classification
    # to pick this up. Same for «common words across X, Y, Z», «пересечение
    # слов», «что общего у». Renderer computes the intersection downstream.
    (_re(r"\b(общие|общая|пересечен\w+)\s+(фирменн\w+\s+)?"
         r"(слов|лексик|вокабуляр)"),
     "author_vocab", 0.85),
    # Phase 5 W-5 (2026-05-24) — «у X и Y какие слова / какая лексика» and
    # «какие слова у X и Y» — natural Russian genitive «у автор/-ов»
    # framings the explicit-trigger rules above miss. Anchor on «слова»
    # / «лексик» / «вокабуляр» near «у <NAME>» which the entity
    # extractor already pulls multi_author_regex from. Routes to
    # author_vocab — fan_out invariant then clones the affinity step.
    # Confidence 0.78 — below the explicit «фирменные / характерные»
    # rules (0.85) but above noisy clarify fall-through.
    (_re(r"\bу\s+[A-ZА-ЯЁ]\w+\s+(и|и\s+|,\s*)\s*"
         r"[A-ZА-ЯЁ]\w+.{0,30}\b(слов\w*|лексик\w+|вокабуляр\w*)\b"),
     "author_vocab", 0.78),
    (_re(r"\b(какие|чьи|какая|whose)\s+(слов\w*|лексик\w+|вокабуляр\w*)"
         r".{0,30}\bу\s+[A-ZА-ЯЁ]\w+\s+(и|,\s*)\s*[A-ZА-ЯЁ]"),
     "author_vocab", 0.78),
    # Phase 5 W-5 — «у X и Y что страшнее / что мрачнее / что эмоциональнее»
    # multi-author emotional contrast. Routes to word_emotion which fan-outs
    # by author. Specific emotion («страшн / тревожн / мрачн») biases
    # toward fear-bucket but the renderer surfaces whichever bucket
    # emotion_collocates returns.
    (_re(r"\bу\s+[A-ZА-ЯЁ]\w+\s+и\s+[A-ZА-ЯЁ]\w+"
         r".{0,30}\b(что|у\s+кого)\s+"
         r"(страшн\w+|мрачн\w+|тревожн\w+|зловещ\w*|эмоцион\w+)"),
     "word_emotion", 0.85),
    (_re(r"\b(что|у\s+кого)\s+(страшн\w+|мрачн\w+|тревожн\w+|зловещ\w*)"
         r".{0,30}\bу\s+[A-ZА-ЯЁ]\w+\s+(и|,\s*)\s*[A-ZА-ЯЁ]"),
     "word_emotion", 0.85),
    # Phase 5 W-5 — «кто пишет проще / сложнее: X или Y» natural multi-author
    # readability question. Routes to author_compare which surfaces
    # author_metadata + compare_authors (no per-author readability tool
    # exists yet; renderer can compare lex_div as a proxy). Distinct from
    # book_readability_compare (which needs books, not authors).
    (_re(r"\bкто\s+(пиш\w+|пиш\w+\s+\w+|читается)\s+"
         r"(проще|сложнее|труднее|легче|архаичнее|современнее|"
         r"богаче|разнообразнее|проще\s+всего|сложнее\s+всего)\b"
         r".{0,30}[:?]?\s*[A-ZА-ЯЁ]\w+\s+(или|и|vs)\s+[A-ZА-ЯЁ]"),
     "author_compare", 0.85),
    (_re(r"\bу\s+кого\s+\w*\s*(больше|меньше|сложнее|проще|архаичнее)"
         r".{0,30}\bу\s+[A-ZА-ЯЁ]\w+\s+(или|и)\s+[A-ZА-ЯЁ]"),
     "author_compare", 0.82),
    (_re(r"\bcommon\s+(signature\s+)?words?\b"), "author_vocab", 0.85),
    (_re(r"\bчто\s+общего\s+у\s+[A-ZА-Я]"), "author_vocab", 0.8),
    (_re(r"\b(intersection|shared)\s+of\s+(signature\s+)?(words?|vocabulary)"),
     "author_vocab", 0.8),
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
    # E17 (2026-05-22) — Stan prod: «характерные прилагательные в "The
    # Picture of Dorian Gray"». No «книге» word, trigger word
    # («характерные») BEFORE the «в», not after the quoted title.
    # author_vocab line 781 matched first («характерн\w+\s+прилаг\w*»),
    # but author_vocab priority is 85 vs book_vocab 100 — so book_vocab
    # WINS once we have a matching rule. Add the rule with the same
    # trigger words as author_vocab but require a quoted title (Latin
    # quotes OR Russian guillemets) after «в» / «in» — preposition is
    # OPTIONAL so «top-30 affinity words "Heart of Darkness"» also lands
    # here. Title regex is loose to accept multi-word English titles
    # (≥4 chars, ≤80) like "The Picture of Dorian Gray", "Crime and
    # Punishment", "Heart of Darkness". Same vocabulary tier as
    # author_vocab rule.
    (_re(r"\b(фирменн\w+|характерн\w+|любим\w+|аффинн\w+|"
         r"characteristic|distinctive|signature|favou?rite|affinity|high-affinity)\s+"
         r"(слов\w*|прилаг\w*|глагол\w*|сущ\w*|лексик\w*|"
         r"adjectives?|verbs?|nouns?|vocabulary|words?)\s+"
         r"(?:(?:в|in|of)\s+)?[«\"“‘][^»\"”’]{4,80}[»\"”’]"),
     "book_vocab", 0.92),
    # E17 — mirror for «слова|лексика [BOOK_QUOTED]» without modifier
    (_re(r"\b(слов\w+|лексик\w+|vocabulary|words?)\s+"
         r"(?:в|in|of)\s+[«\"“‘][^»\"”’]{4,80}[»\"”’]"),
     "book_vocab", 0.82),

    # ===== book_readability =====
    (_re(r"уровень\s+сложн\w*|cefr|flesch|reading\s+(level|grade)|"
         r"насколько\s+сложн\w*|сложн\w+\s+(для\s+чтения|для\s+понимани)"),
     "book_readability", 0.92),
    # E30 (S-R4, 2026-05-29) — bare «сложность <BOOK>» WITHOUT «уровень» /
    # «для чтения». Stan Q7: «сложность Франкенштейна» fell to clarify
    # because the rule above required «уровень сложн*» or «сложн* для
    # чтения». Route the noun «сложность/сложности <ProperNoun>» to
    # single-book readability. The capital-letter guard (?-i:[A-ZА-ЯЁ]) is
    # essential: _re() applies IGNORECASE globally, so a plain [A-ZА-Я]
    # would also match lowercase and wrongly catch the negative case
    # «сложность викторианской прозы» (abstract/period, lowercase «в» →
    # must stay clarify, NOT book_readability). The capital guard fires
    # only on a proper-noun book title (Франкенштейн / Dracula / Pride).
    # The trailing letter class (not just the capital) is required so a
    # CEFR level — «сложности B2 при чтении Лавкрафта» (learning intent) —
    # does NOT match: «B2» is capital-then-DIGIT, a book title is
    # capital-then-LETTER. Without it the new rule stole the B2/C1 learning
    # queries (regression caught by test_plan::test_learning_b2_lovecraft).
    (_re(r"\bсложност[ьи]\s+[«\"'“‘]?(?-i:[A-ZА-ЯЁ])[A-Za-zА-Яа-яЁё]"),
     "book_readability", 0.85),

    # ===== book_emotion =====
    # Stan round 2 Q19: «эмоциональный профиль Dracula» — точная фраза из
    # README'а fell to clarify. Add explicit pattern (это distinct от
    # word_emotion который про «слова страха», тут — про NRC profile книги).
    (_re(r"эмоциональн\w*\s+профил\w*|"
         r"emotional\s+profile|"
         r"эмоции?\s+(в|of)\s+[\"'«“]?[A-ZА-Яa-zа-я]"),
     "book_emotion", 0.93),
    (_re(r"\bsentiment\s+(of|in)\s+"), "book_emotion", 0.9),
    # Sprint 19+ — «эмоции и настроение в X», «настроение в X», «mood
    # in X», «тон / тональность книги». «эмоции» с inserted conjunction
    # («эмоции и настроение в», «эмоции, тон и атмосфера в») was missed
    # by the strict «эмоции\s+в» rule. Allow up to ~40 chars of fill
    # between the emotion-keyword and «в/of».
    (_re(r"\b(эмоции?|настроени\w*|тон|тональност\w*|атмосфер\w*)\b"
         r"[\w\s,]{0,40}\b(в|of|in)\s+[\"'«“]?[A-Za-zА-Яа-яЁё]"),
     "book_emotion", 0.91),
    (_re(r"\bmood\s+(in|of)\s+[\"'«“]?[A-Z]"),
     "book_emotion", 0.9),
    # Bare genitive: «тональность Frankenstein» / «атмосфера Hamlet»
    # — Russian doesn't require a preposition for «mood of X» construct.
    # Require capital letter after (proper noun) to avoid catching «тон
    # героя» / «атмосфера комнаты» general-noun phrases.
    # `(?-i:...)` disables IGNORECASE for the capital-letter check
    # — _re() applies IGNORECASE globally, which would otherwise
    # match «к» (lowercase) too and false-positive «атмосфера комнаты».
    (_re(r"\b(тональност\w+|атмосфер\w+|настроени\w+)\s+"
         r"[\"'«“]?(?-i:[A-ZА-ЯЁ])"),
     "book_emotion", 0.85),

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
    # Sprint 17 fix: «что почитать после/подобное/похожее/типа X» —
    # was wrongly classified as book_recommendation (generic popularity)
    # ignoring the resolved book entity. These are all similarity-to-
    # reference queries → book_similar with X as the semantic reference.
    # Stan 2026-05-19: «что почитать после преступления и наказания»
    # used to return top_books_by_downloads (Hemingway/Carroll/Christie
    # — generic top), now routes to find_book_by_topic(Crime and
    # Punishment) which returns thematically similar books.
    (_re(r"что\s+(почитать|читать)\b.{0,60}\b(после|подобн|похож|типа)"),
     "book_similar", 0.93),
    # E44 (2026-05-22) — Stan prod «что почитать, если нравится Толстой?»
    # matched `book_recommendation` generic → top_books_by_downloads
    # (Gatsby, Blue Castle, JFK Commission report — zero relation к
    # Tolstoy). Existing rule above only catches «после|подобн|похож|типа»
    # — «если нравится X» phrasing slipped through. Same structural class
    # as the Sprint 17 fix; add the «taste» / «fan» variants.
    # Route to `similar_to` (NOT `book_similar`) because the entity may
    # be an AUTHOR (Tolstoy) OR a BOOK (Pride & Prejudice). `_plan_similar_to`
    # dispatches to author_closest (Burrows Delta neighbours) or
    # book_similar (find_book_by_topic) depending on which entity slot
    # filled — see plan.py:1683-1707.
    (_re(r"что\s+(почитать|читать)\b.{0,80}\bесли\s+нравится\b|"
         r"если\s+нравится\b.{0,30}\bчто\s+(почитать|читать)\b|"
         r"if\s+you\s+like\b.{0,40}\bwhat\s+to\s+read\b|"
         r"\bfans?\s+of\s+[A-ZА-Я]\w+.{0,30}\b(read|enjoy|recommend)|"
         r"я\s+люблю\s+\w+\s*,?\s*что\s+(почитать|посоветуешь)"),
     "similar_to", 0.93),

    # ===== word_etymology =====
    (_re(r"этимолог\w*|origin of the word"), "word_etymology", 0.95),
    (_re(r"древнегерманск\w*|скандинавск\w*|германск\w*|"
         r"романск\w*|латинск\w*|french origin|"
         r"romance origin"), "word_etymology", 0.85),
    (_re(r"происхожден\w*\s+слов"), "word_etymology", 0.9),
    # Sprint 18+ Round 9 N5 — «что значит слово X», «определи слово X»,
    # «what does X mean». Definition probe. word_etymology tool surfaces
    # cached enrich_word output (translation_ru + definition_en + CEFR
    # + family), which is exactly what the user wants. Distinct rule so
    # the renderer can label appropriately.
    (_re(r"\bчто\s+(значит|означа\w+)\s+слов\w*\s+\w"),
     "word_etymology", 0.92),
    (_re(r"\bопредели\s+слово\s+\w"),
     "word_etymology", 0.92),
    (_re(r"\bwhat\s+does\s+[\"'«]?\w{3,30}[\"'»]?\s+mean"),
     "word_etymology", 0.92),
    (_re(r"\bопредели(?:те)?\s+значени\w+\s+слова\s+\w"),
     "word_etymology", 0.9),

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
    (_re(r"вышл\w+\s+из\s+употреблени\w*|"
         r"вышедш\w+\s+из\s+употреблени\w*|"
         r"исчезл\w*|исчезают|исчезающ\w*|"
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
    # Sprint 18 Round 8 C5 — multi-word timeline. «timeline telephone+
    # automobile+aeroplane» / «частота radio+television по эпохам» /
    # «когда появились telephone, automobile, aeroplane». Bare lowercase
    # English tokens chained by + or , inside a timeline-trigger context.
    (_re(r"\b(timeline|частота|когда\s+появил\w*)\b.{0,40}"
         r"\b[a-z]{3,}\s*[+,]\s*[a-z]{3,}"),
     "word_timeline", 0.92),
    (_re(r"\b[a-z]{3,}\s*[+,]\s*[a-z]{3,}\b.{0,40}\b"
         r"(timeline|по\s+эпох|over\s+(time|years|decades))"),
     "word_timeline", 0.9),
    # Sprint 18 — single-word origin queries. «когда появилось слово
    # radio» / «когда появилось radio в текстах». Distinct from
    # book_pub_year (which is «когда опубликована/издана КНИГА»).
    # Trigger «появил» + «слов» or bare lowercase token.
    (_re(r"\bкогда\s+появил\w*\s+слов\w*"),
     "word_timeline", 0.93),
    (_re(r"\bкогда\s+появил\w*\s+[a-z]{3,30}"),
     "word_timeline", 0.88),
    # Phase 4 W-12 (2026-05-23) — rise direction. «слова, ставшие чаще»
    # / «новые слова после 1900» / «слова, появившиеся в XX веке» /
    # «trending words» / «emerging vocabulary after WWI». Symmetric pair
    # to the existing «вышли из употребления» rule above. Plan builder
    # picks `words_appearing_after` vs `words_disappearing_after` by the
    # same direction markers; intent stays unified at `word_timeline`.
    (_re(r"\b(слов\w*|words?)\b.{0,40}\b(ставш\w*\s+чаще|"
         r"появивш\w*|стали\s+чаще|появились|"
         r"started\s+(?:appearing|trending)|emerging|"
         r"rising|trending\s+up|became\s+(?:common|popular)|"
         r"more\s+frequent)"),
     "word_timeline", 0.92),
    (_re(r"\b(новые|new|emerging)\s+слов\w*\s+"
         r"(?:после|after|в|in)\s+(?:\d{3,4}|XX|XIX|20)"),
     "word_timeline", 0.92),
    (_re(r"\bemerging\s+vocabulary|trending\s+words|rising\s+words?\b"),
     "word_timeline", 0.92),
    (_re(r"\bкакие\s+слов\w*\s+(?:появились|стали\s+чаще|"
         r"вошли\s+в\s+употреблени\w*)"),
     "word_timeline", 0.93),

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
    # S-R5 / E9 (2026-05-30) — bare «примеры <english-word> у <author>» /
    # «examples <word>». The entity extractor's _BARE_WORD_AFTER_EXAMPLES
    # (entities.py) already lifts the Latin token out of this form, but no
    # INTENT rule matched it: the rule above needs a Russian filler
    # («использования») or a leading «приведи», so bare
    # «примеры heart у Дойла» fell through to clarify(0.0) — probe E9.
    # The Latin-word guard ([a-z] under IGNORECASE excludes Cyrillic)
    # mirrors the extractor so Russian genitive fillers («примеры авторов /
    # примеры слов») don't bind. _plan_word_contexts backstops any
    # over-match: no extractable word → clarify, so this can only help.
    (_re(r"\b(?:пример(?:ы|ов|ами)?|examples?)\s+"
         r"(?:of\s+|слова\s+|word\s+)?[\"'«“]?[a-z][a-z-]{2,}\b"),
     "word_contexts", 0.88),
    # Round 3 R5 «найди упоминания битой посуды» — chat placeholder!
    # Routes to hybrid_search via word_contexts (which falls through to
    # hybrid_search in plan when no author scope).
    # Sprint 20 — broadened to cover bare «упоминания X», «вхождения X»,
    # «occurrences of X», «mentions of X» without the «найди» trigger.
    (_re(r"\bнайди\s+упоминани\w+|найди\s+(где\s+)?(говорится|описыва\w+)\s+"
         r"(про|о|об)\s+|find\s+mentions?\s+of|"
         r"\bупоминани\w+\s+\w+|\bвхождени\w+\s+(?:слова\s+)?\w+|"
         r"\bвстречаемост\w+\s+\w+|"
         r"\b(?:mentions?|occurrences?)\s+of\s+\w+"),
     "word_contexts", 0.9),
    (_re(r"у\s+разных\s+авторов\b.{0,40}\bобъясни\s+оттенки"),
     "word_contexts", 0.95),
    (_re(r"в\s+необычных\s+контекстах|обычными\s+сейчас\b.{0,40}\bконтекст"),
     "word_contexts", 0.9),
    # Phase 4 W-10 (2026-05-23) — «что значит X» / «meaning of X» /
    # «define X». Stan test bench: «что значит ajar» bounced to clarify
    # because no rule fired. Route to word_contexts which already
    # bundles enrich_word (translation + IPA + POS + definition +
    # etymology via Wiktionary) AND hybrid_search (corpus snippets with
    # titles). That gives the full W-10 bundle in one plan.
    #
    # Two-flavor rules to keep precision high without dropping coverage:
    #   (A) strong triggers with explicit «слова / word» anchor — accept
    #       any single token after them.
    #   (B) weak triggers like «what is X» / «define X» — require the
    #       bare token to NOT be a comparator/connector that would
    #       indicate a book-compare query («what is harder, X or Y»).
    (_re(r"\b(?:что\s+значит|что\s+такое|значени\w*\s+слова|смысл\s+слова|"
         r"определени\w*\s+слова|объясни\s+слово|"
         r"meaning\s+of(?:\s+the\s+word)?|definition\s+of(?:\s+the\s+word)?|"
         r"what\s+(?:is|does)\s+the\s+word)\s+"
         r"[\"'«“]?[a-zA-Zа-яё-]{2,30}[\"'»”]?"),
     "word_contexts", 0.92),
    (_re(r"\b(?:define|what\s+(?:is|does))\s+"
         r"[\"'«“]?(?!harder\b|easier\b|simpler\b|more\b|less\b|"
         r"this\b|that\b|the\b|a\b|an\b|your\b|that\s+|the\s+book)"
         r"[a-zA-Zа-яё-]{2,30}[\"'»”]?\s*$"),
     "word_contexts", 0.88),
    # S-R2 (R-27, 2026-06-10) — «расскажи (мне) про слово X» / «tell me
    # about the word X». Live nit 2026-06-02 (E-routing, backlog п.3):
    # «расскажи про слово ajar» missed every deterministic rule
    # (clarify 0.0) and the v4 LLM clarify-rescue picked
    # word_pos_distribution — a POS histogram instead of the W-10 word
    # bundle (translation + corpus contexts + etymology). word_contexts'
    # no-author plan IS that bundle. The literal «слов(о/е)» anchor keeps
    # «расскажи о себе» (introduction) and «расскажи про корпус»
    # (corpus_meta) untouched — negative cases in
    # tests/v2/test_r27_honest_renderer.py.
    (_re(r"\bрасскажи(?:\s+мне)?\s+(?:про|о|об)\s+слов[ое]\s+\S|"
         r"\btell\s+me\s+about\s+the\s+word\s+\S"),
     "word_contexts", 0.92),

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

    # ===== learning_books (R-27 WP1 / B106) =====
    # Прод /feedback 01.06 + 10.06 (уже на 2.6.47): «какие книги
    # почитать, если у меня уровень B2» падало в learning 0.7 (голое
    # `(b1|b2|c1|c2)` правило) → _plan_learning unscoped clarify →
    # v4 LLM planner ~40s → canned «не получилось разобрать». Книги
    # по уровню не имели интента вовсе — только слова (learning_words).
    #
    # Якоря: «книг» + читать/для-изучающих/уровень. Голое «какие книги
    # почитать» (без learning-контекста) НЕ матчится — остаётся на
    # обычном book-поиске (негативный тест в
    # tests/v2/test_r27_learning_intents.py). «слова уровня B2 из
    # книги X» тоже не матчится (нет «книг* читать» / «книг* для»).
    (_re(r"\bкниг\w*\s+(?:по)?читать\b.{0,80}"
         r"\b(?:уров(?:ень|ня|не)\b|(?:уч[уи]\w*|изуча\w+)\s+(?:английск|язык))|"
         r"\bуров(?:ень|ня|не)\s+[abc][12]\b.{0,60}\bкниг\w*\s+(?:по)?читать"),
     "learning_books", 0.95),
    # «книги для уровня B2» / «книги для изучающих английский» /
    # «лёгкие книги для начинающих»
    (_re(r"\bкниг\w*\s+для\s+(?:уровн\w+|изуча\w+|начинающ\w+|новичк\w+)"),
     "learning_books", 0.92),
    # «я учу английский, с чего начать?» (нота юзера: «надо советовать
    # книгу с минимальным порогом вхождения») / «с чего начать изучать
    # английский» / «учу английский — что почитать»
    (_re(r"\b(?:уч[уи]\w*|изуча\w+|выучить)\s+(?:английск\w*|язык\w*)"
         r".{0,60}\b(?:с\s+чего\s+начать|что\s+(?:по)?читать|какие\s+книг)|"
         r"\bс\s+чего\s+начать\s+(?:учить|изучать)\s+(?:английск\w*|язык)"),
     "learning_books", 0.93),
    # EN: «books to read at B2 level», «easy books for (English)
    # learners», «where do I start learning English»
    (_re(r"\bbooks?\s+(?:to\s+read\s+)?(?:for|at)\s+(?:level\s+)?[abc][12]\b|"
         r"\b(?:easy|simple|beginner)\s+books?\s+for\s+(?:english\s+)?learners|"
         r"\bbooks?\s+for\s+(?:english\s+)?learners\b|"
         r"\bwhere\s+(?:do\s+i|to)\s+start\s+(?:learning|reading)\s+english"),
     "learning_books", 0.92),

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
    # Phase 4 W-5 (2026-05-23) — generic «сравни <country1> и <country2>
    # авторов / литературу / корпус». Without this rule «сравни
    # британских и американских авторов» bucketed into author_compare
    # (which produced an empty plan because no author_regex), and the
    # user saw a clarify-bounce. The pattern requires both adjective
    # stems + a compare verb («сравни / compare») + a nominal target
    # («автор / литератур / корпус / писател / книг»).
    #
    # Phase 5 W-5 (2026-05-24) — symmetric country alternation. Previous
    # version of country2 lacked plain English `american\w*`, so a fully
    # English compare query («compare British and American authors»)
    # OR a Russian-verb + English-adjective query («сравни British и
    # American авторов») dropped into author_compare with 0 steps. Also
    # added bare country codes (GB/US/UK) for codes-only phrasings
    # («compare GB and US writers»), and `или / or` as a connector
    # alongside `и / and`.
    (_re(r"\b(сравни\w*|compare)\b.{1,40}"
         r"\b(британск\w*|american\w*|brit\w*|british\w*|"
         r"русск\w*|русских|french|французск\w*|"
         r"немецк\w*|german|gb|uk|us|usa)\w*\b.{1,40}"
         r"\b(и|или|and|or|with|против|vs|versus)\b.{1,40}"
         r"\b(американск\w*|american\w*|brit\w*|british\w*|британск\w*|"
         r"русск\w*|french|французск\w*|"
         r"немецк\w*|german|gb|uk|us|usa)\w*\b.{0,60}"
         r"\b(автор\w*|author\w*|writer\w*|писател\w*|"
         r"литератур\w*|literature|корпус\w*|corpus|"
         r"книг\w*|book|произведен\w*|works?|novels?|poets?|поэт\w*)"),
     "country_compare", 0.93),

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
    # W-11 (Phase 5 P2, 2026-05-23) — extended period stem `виктори[аяь]н\w*`
    # (was `виктори[аяь]нск\w*` — required «ск», missed «викторианцев»
    # / «викторианки» / plural-genitive forms that `_VICTORIAN` in
    # entities.py already recognized as a period marker). Without the
    # match, queries like «слова викторианцев 1837-1901» went to LLM
    # fallback (45-60s) instead of period_vocab plan (<1s).
    # Also: accept POS-noun triggers («существительные/прилагательные/
    # глаголы») as anchors equivalent to «слова», so «какие
    # существительные чаще у викторианцев» routes here too. Both orders
    # work — anchor BEFORE the period or AFTER it.
    (_re(r"(виктори[аяь]н\w*|victorian|edwardian|пред-?war|pre[- ]1900|"
         r"эдвард(?:иан|овск)\w*)"
         r".{0,80}\b(слов\w*|word|vocab|"
         r"существительн\w+|nouns?|"
         r"прилагательн\w+|adjectives?|"
         r"глагол\w+|verbs?|"
         r"наречи\w+|adverbs?)\b"),
     "period_vocab", 0.85),
    (_re(r"\b(слов\w*|word|vocab|"
         r"существительн\w+|nouns?|"
         r"прилагательн\w+|adjectives?|"
         r"глагол\w+|verbs?|"
         r"наречи\w+|adverbs?)\b"
         r".{0,40}\b(виктори[аяь]н\w*|victorian|edwardian|"
         r"эдвард(?:иан|овск)\w*)"),
     "period_vocab", 0.85),
    (_re(r"\b(до\s+(1850|1860|1870|1880|1890|1900))\b"
         r".{0,60}\b(слов\w*|word)"), "period_vocab", 0.8),
    # W-11 — explicit year-range «1837-1901» / «1800-1899» paired with
    # «слова / words / nouns / …» is a period_vocab. Without this,
    # «слова викторианцев 1837-1901» fell to author_vocab (which
    # requires capitalized first letter) or to LLM fallback.
    (_re(r"\b(слов\w*|word|vocab|"
         r"существительн\w+|nouns?|"
         r"прилагательн\w+|adjectives?|"
         r"глагол\w+|verbs?)\b"
         r".{0,40}\b(1[5-9]\d{2}|20\d{2})\s*[–—\-]\s*(1[5-9]\d{2}|20\d{2})\b"),
     "period_vocab", 0.88),
    (_re(r"\b(1[5-9]\d{2}|20\d{2})\s*[–—\-]\s*(1[5-9]\d{2}|20\d{2})\b"
         r".{0,40}\b(слов\w*|word|vocab|"
         r"существительн\w+|nouns?|"
         r"прилагательн\w+|adjectives?|"
         r"глагол\w+|verbs?)\b"),
     "period_vocab", 0.86),
    # W-11 follow-up (Phase 5 P2, 2026-05-24) — POS-anchored generic-period
    # queries. «топ существительных XIX века», «топ существительных эпохи»,
    # «существительные периода», «топ-100 прилагательных столетия». The
    # earlier W-11 rules require either an era stem (виктори*/edwardian) OR
    # an explicit year range like «1837-1901». Phrasings with bare period
    # words («эпохи», «периода», «века», «столети», «XIX»/«XX») fell to
    # clarify → LLM-fallback 50s parse-fail.
    #
    # The two orderings cover both POS-first («топ существительных XIX
    # века») and period-first («XIX века существительные»). Pattern
    # tightened to avoid colliding with author_vocab — POS noun + era word
    # is the strong signal; we don't fire on bare «эпохи» alone.
    (_re(r"\b(топ\s*-?\s*\d*\s*)?"
         r"(существительн\w+|nouns?|"
         r"прилагательн\w+|adjectives?|"
         r"глагол\w+|verbs?|"
         r"наречи\w+|adverbs?|"
         r"слов\w*|words?|vocab\w*)\b"
         r".{0,40}\b(эпох\w+|период\w+|века|столети\w+|"
         r"XIX|XX|XVIII|XVII|"
         r"19th[\s-]centur\w*|20th[\s-]centur\w*|18th[\s-]centur\w*|"
         r"18\d\d-?х|19\d\d-?х|1[5-9]\d\d-х)\b"),
     "period_vocab", 0.82),
    (_re(r"\b(эпох\w+|период\w+|столети\w+|"
         r"XIX|XX|XVIII|XVII|"
         r"19th[\s-]centur\w*|20th[\s-]centur\w*|18th[\s-]centur\w*)\b"
         r".{0,40}\b(существительн\w+|nouns?|"
         r"прилагательн\w+|adjectives?|"
         r"глагол\w+|verbs?|наречи\w+|adverbs?|"
         r"слов\w*|words?|vocab\w*)\b"),
     "period_vocab", 0.82),
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

    # ===== export_word_list (Sprint 20+ B3) =====
    # Recognized formats: anki / csv / markdown / json / tsv / excel /
    # spreadsheet / obsidian / notion. Always paired with a verb
    # («выгрузи», «дай в», «export to», «save as»). We classify standalone
    # «выгрузи в anki» here; follow-up-context detection (where the prior
    # turn must be a word-list intent) happens in history.py before the
    # plan is built.
    (_re(r"\b(выгрузи|выгрузить|выгрузь|сохрани|конвертируй|"
         r"дай(те)?|"
         r"export|save|convert|dump|format)\b"
         r".{0,40}\b(anki|csv|json|markdown|\.md\b|tsv|"
         r"excel|spreadsheet|таблиц|obsidian|обсидиан|notion)"),
     "export_word_list", 0.92),
    # Bare «в Anki» / «as csv» without a verb when very close to
    # the start — naïve users sometimes write just «csv pls» or «anki».
    (_re(r"^\s*(в\s+|to\s+|as\s+|in\s+)?"
         r"(anki|csv|json|markdown|tsv)\b"
         r"(\s+(pls|please|пж|пжл))?\s*[?.!]?\s*$"),
     "export_word_list", 0.85),
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
    # Sprint 19+ — preserve original case. Rules carry re.IGNORECASE
    # by default via _re() helper, so case-insensitive matching still
    # works. Lowercase-first broke inline `(?-i:[A-Z])` guards added
    # for proper-noun discrimination («тональность Frankenstein» book
    # vs «тональность голоса» general noun).
    s = text.strip()
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
