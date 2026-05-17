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
    "country_compare",
    "country_vocab",
    "composite_compare",
    "period_vocab",
    "genre_compare",
    "topic_words",
    "translation_quality",
    "vocab_passport",
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
    (_re(r"^\s*(привет|hi|hello)\b"), "introduction", 0.7),

    # ===== out_of_scope =====
    (_re(r"(напиши|сочини|допиши|сгенерируй)\s.{0,40}"
         r"(рассказ\w*|стих\w*|поэм\w*|глав\w*|стат\w*)"), "out_of_scope", 0.95),
    (_re(r"(write|compose)\s.{0,40}(story|poem|chapter|novel|article)"),
     "out_of_scope", 0.9),
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

    # ===== corpus_meta =====
    (_re(r"\b(сколько|how many)\s.{0,20}(книг|book)"), "corpus_meta", 0.95),
    (_re(r"прогресс\s+индексаци\w*|index progress|reindex"), "corpus_meta", 0.92),
    (_re(r"\bпрогресс\b"), "corpus_meta", 0.6),
    (_re(r"что у тебя за корпус|размер корпуса|corpus (size|stats)"),
     "corpus_meta", 0.9),

    # ===== author_metadata =====
    (_re(r"когда\s+(родил\w*|умер\w*|жил\w*)"), "author_metadata", 0.9),
    (_re(r"year of (birth|death)"), "author_metadata", 0.9),
    (_re(r"сколько у\s+.{1,40}\s*книг"), "author_metadata", 0.85),

    # ===== top_authors_books =====
    (_re(r"\b(топ[- ]?\d*|top\s*\d*)\s.{0,40}(автор\w*|writer)"),
     "top_authors_books", 0.85),
    (_re(r"(самые\s+попул\w*|самые\s+скачив\w*|most popular|most downloaded)"
         r"\b.{0,40}\b(автор\w*|book|книг)"), "top_authors_books", 0.9),
    (_re(r"\b(топ|top)\s*\d*\s*(скачив\w*|downloaded|книг|book)"),
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

    # ===== book_archaic =====
    (_re(r"архаизм\w*|устаревш\w*|архаичн\w*|"
         r"archaic|old[- ]fashioned|outdated\s+words"), "book_archaic", 0.92),

    # ===== book_recommendation =====
    (_re(r"подойд[уё]т\w*|recommend|посоветуй|что\s+почитать|what\s+to\s+read"),
     "book_recommendation", 0.55),
    (_re(r"(подойд[уё]т|recommend|посоветуй|что\s+почитать)\b"
         r".{0,80}\b(b1|b2|c1|c2|уровень|level)"), "book_recommendation", 0.92),
    (_re(r"произведени\w*\b.{0,60}\bдля\b.{0,30}(читател\w*|уровн\w*|level)"),
     "book_recommendation", 0.88),

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
    (_re(r"слова\s+страх\w*|слова\s+гнев\w*|слова\s+ужас\w*|"
         r"fear words|words of (fear|anger|sadness)|"
         r"тревожн\w+\s+контекст|мрачн\w+\s+контекст|зловещ\w*|"
         r"terror|madness"), "word_emotion", 0.9),
    (_re(r"рядом\s+со?\s+словами\s+\W?(fear|terror|madness|страх|тревог|"
         r"ужас|гнев|радост)"), "word_emotion", 0.95),
    (_re(r"в\s+мрачн\w+\s+или\s+тревожн\w+\s+контекст"), "word_emotion", 0.95),

    # ===== word_timeline =====
    (_re(r"вышли\s+из\s+употреблени\w*|исчезл\w*|исчезают|исчезающ\w*|"
         r"перестали\s+(использовать|встречаться)|"
         r"disappeared after|fell out of use|words that vanished"),
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
    (_re(r"у\s+разных\s+авторов\b.{0,40}\bобъясни\s+оттенки"),
     "word_contexts", 0.95),
    (_re(r"в\s+необычных\s+контекстах|обычными\s+сейчас\b.{0,40}\bконтекст"),
     "word_contexts", 0.9),

    # ===== learning =====
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

    # ===== word_dialogue =====
    (_re(r"в\s+диалогах\b.{0,40}\b(чем\s+в|а\s+не\s+в|vs|против|нежели)"),
     "word_dialogue", 0.9),
    (_re(r"авторск\w+\s+текст\w*|в\s+нарратив\w*"), "word_dialogue", 0.6),

    # ===== word_movement =====
    (_re(r"глагол\w*\s+движени\w*|verbs of (motion|movement)"),
     "word_movement", 0.95),
]


def classify(text: str) -> IntentMatch:
    if not text or not text.strip():
        return IntentMatch("clarify", 0.0)
    s = text.strip().lower()
    matches: list[IntentMatch] = []
    for pat, intent, conf in RULES:
        if pat.search(s):
            matches.append(IntentMatch(intent, conf, pat.pattern))
    if not matches:
        return IntentMatch("clarify", 0.0)
    matches.sort(
        key=lambda m: (PRIORITY.get(m.label, 0), m.confidence),
        reverse=True,
    )
    return matches[0]


def all_intents() -> set[str]:
    return set(INTENTS)
