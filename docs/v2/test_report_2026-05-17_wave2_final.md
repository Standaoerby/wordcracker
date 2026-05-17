# Functional Test Report — v2 engine (v2)

Run date: 2026-05-17T19:16:58
Target: http://127.0.0.1:8890
Total queries: 40

## Summary

| Verdict | Count | % |
|---|---:|---:|
| out_of_scope | 7 | 18% |
| pass | 24 | 60% |
| pass-no-tool | 9 | 22% |

## Per-question

| QID | Verdict | Intent | Tools | Time | Note |
|---|---|---|---|---:|---|
| Q01 | pass-no-tool | introduction | — | 0.0s |  |
| Q02 | pass | author_vocab | affinity_by_author | 15.0s |  |
| Q03 | pass | author_vocab | affinity_by_author | 14.3s |  |
| Q04 | pass | learning | learning_words | 25.8s |  |
| Q05 | pass | author_compare | author_metadata, author_metadata, compare_authors | 3.3s |  |
| Q06 | pass | country_vocab | affinity_by_author | 10.7s |  |
| Q07 | pass | book_vocab | affinity_by_book | 14.4s |  |
| Q08 | pass | word_etymology | find_words_by_etymology | 11.9s |  |
| Q09 | pass | word_collocates | word_collocates | 38.1s |  |
| Q10 | pass-no-tool | learning | — | 0.0s |  |
| Q11 | pass | book_archaic | book_archaic_words | 5.8s |  |
| Q12 | pass | country_compare | top_authors_by_country, top_authors_by_country | 3.7s |  |
| Q13 | pass | author_vocab | affinity_by_author | 12.8s |  |
| Q14 | pass-no-tool | learning | — | 0.0s |  |
| Q15 | pass | author_compare | author_metadata, author_metadata, compare_authors | 15.6s |  |
| Q16 | pass | word_contexts | word_contexts_global | 27.4s |  |
| Q17 | pass | word_timeline | words_disappearing_after | 33.6s |  |
| Q18 | pass-no-tool | word_emotion | — | 0.0s |  |
| Q19 | pass-no-tool | word_pos | — | 0.0s |  |
| Q20 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q21 | pass | learning | learning_words | 27.7s |  |
| Q22 | pass | word_timeline | words_disappearing_after | 37.0s |  |
| Q23 | pass | country_compare | top_authors_by_country, top_authors_by_country | 9.4s |  |
| Q24 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q25 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q26 | pass | author_closest | author_influences | 4.8s |  |
| Q27 | pass | author_vocab | affinity_by_author | 14.4s |  |
| Q28 | pass-no-tool | word_contexts | — | 0.0s |  |
| Q29 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q30 | pass | book_recommendation | top_books_by_downloads | 6.1s |  |
| Q31 | pass-no-tool | vocab_passport | — | 0.0s |  |
| Q32 | pass | word_timeline | words_disappearing_after | 34.1s |  |
| Q33 | pass | topic_words | word_collocates | 40.0s |  |
| Q34 | pass | lexical_wealth | top_authors_by | 62.1s |  |
| Q35 | pass-no-tool | word_emotion | — | 0.0s |  |
| Q36 | pass-no-tool | author_vocab | — | 0.0s |  |
| Q37 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q38 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q39 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q40 | pass | country_compare | top_authors_by_country, top_authors_by_country | 7.8s |  |

## Full Answers

### Q01 — pass-no-tool
**Q:** Напиши, что ты умеешь, какие типы анализа поддерживаешь, и приведи пример сложного исследовательского запроса.

- Intent: `introduction` (conf=0.95)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Меня зовут Словоёб. Я аналитик корпуса Project Gutenberg (~55 тыс. книг). Умею:

**📊 Стилометрия:** фирменные слова автора (`affinity_by_author`), сравнение авторов, биграммы, лексическая разнообразность, Burrows Delta attribution и influences.

**📚 Книги:** уровень сложности (Flesch+CEFR), архаизмы, эмоциональный профиль, фирменные слова книги.

**🔤 Слова:** контексты, collocates, timeline по эпохам, polysemy, этимология через Wiktionary, emotion collocates.

**🎓 Изучение:** vocab B1/B2/C1/rare, enrichment с переводом, Anki/Markdown/JSON export.

**🌐 Корпус:** прогресс индексации, топ-авторы, топ-книги.

**Пример сложного запроса:** «характерные прилагательные Уайльда, которые встречаются у него как минимум в 5 произведениях, с CEFR ≥ B2 и этимологией latin/french».

Спрашивай как поставить вопрос правильно — подскажу.

---

### Q02 — pass
**Q:** Какие слова у Конан Дойла встречаются заметно чаще, чем у остальных английских авторов XIX века?

- Intent: `author_vocab` (conf=0.80)
- Tools: `['affinity_by_author']`
- Wall time: 15.0s

**Answer:**

Вот список слов, которые у Артура Конан Дойла встречаются заметно чаще, чем у остальных английских авторов XIX века:

| Слово             | Количество упоминаний у Дойла | Общее количество упоминаний в корпусе | Аффинность (отношение к другим авторам) |
|-------------------|-------------------------------|----------------------------------------|----------------------------------------|
| lhut              | 104                           | 389                                    | 190.53                                 |
| shropshires       | 20                            | 103                                    | 138.38                                 |
| mcardle           | 29                            | 150                                    | 137.78                                 |
| birdy             | 29                            | 151                                    | 136.87                                 |
| goor              | 24                            | 126                                    | 135.74                                 |
| hatherley         | 36                            | 191                                    | 134.32                                 |
| ettie             | 68                            | 371                                    | 130.62                                 |
| challenger        | 385                           | 2245                                   | 122.21                                 |
| knolles           | 92                            | 553                                    | 118.56                                 |
| thabanchu         | 23                            | 140                                    | 117.08                                 |
| ploermel          | 17                            | 107                                    | 113.22                                 |
| barrymore         | 159                           | 1008                                   | 112.41             

---

### Q03 — pass
**Q:** Покажи мне не слишком редкие, но характерные слова Толкина, которые обычно не знают изучающие английский.

- Intent: `author_vocab` (conf=0.85)
- Tools: `['affinity_by_author']`
- Wall time: 14.3s

**Answer:**

Вот список не слишком редких, но характерных слов, связанных с Толкином, которые обычно не знают изучающие английский:

| Слово       | Количество упоминаний у Толкина | Количество упоминаний в корпусе | Аффинность |
|-------------|-------------------------------|-------------------------------|------------|
| prec        | 157                           | 399                           | 21092.69   |
| allit       | 20                            | 120                           | 8934.17    |
| oe          | 2074                          | 24012                         | 4630.05    |
| habbe       | 15                            | 218                           | 3688.42    |
| ȝ           | 6                             | 107                           | 3005.89    |
| pleonastic  | 9                             | 166                           | 2906.29    |
| sonder      | 6                             | 117                           | 2748.97    |
| doþ         | 5                             | 109                           | 2458.94    |
| wk          | 6                             | 135                           | 2382.44    |
| þare        | 5                             | 117                           | 2290.81    |
| kyn         | 10                            | 267                           | 2007.68    |
| noȝt        | 6                             | 167                           | 1925.93    |
| þar         | 16                            | 471                           | 1820.98    |
| nth         | 10                            | 301                           | 1780.9     |
| leue        | 18                            | 547                           | 1763.97    |
| pouer       | 5                             | 171                           | 1567.4     |
| bodi        | 6                             | 219                           | 1468.63    |
| ȝet         | 6                             | 222                           | 1448.78    |
| wors        | 

---

### Q04 — pass
**Q:** Какие слова чаще всего вызывают сложности у читателей уровня B2 при чтении Лавкрафта?

- Intent: `learning` (conf=0.92)
- Tools: `['learning_words']`
- Wall time: 25.8s

**Answer:**

Вот список слов, которые чаще всего вызывают сложности у читателей уровня B2 при чтении произведений Х. П. Лавкрафта:

| Слово         | Часть речи | Количество в тексте | Количество в корпусе | Пример из текста |
|---------------|------------|---------------------|-----------------------|------------------|
| whateley      | существительное | 73                 | 415                   | "a half from any other dwelling that wilbur [WHATELEY] was born at on sunday the second of" |
| renschaw      | существительное | 55                 | 2041                  | "central national bank columbus ohio lovecraft angell providence [RENSHAW] second coffeeville miss william dowdell secretary east cleveland" |
| journalism    | существительное | 69                 | 7501                  | "convenience united amateur press association exponent of amateur [JOURNALISM] the united amateur january department of public criticism" |
| fritter       | существительное | 40                 | 909                   | "give details and samples of amateur papers leo [FRITTER] president central national bank columbus ohio lovecraft angell" |
| dunwich       | глагол      | 38                 | 528                   | "[DUNWICH] horror by lovecraft gorgons and hydras and chimeras" |
| haughton      | существительное | 35                 | 1625                  | "based on the temple of solomon edna mitchell [HAUGHTON] character sketch the family doctor is just and" |
| kleiner       | существительное | 44                 | 245                   | "the work of none but titled authors rheinhart [KLEINER] contributes the single piece of verse a smooth" |
| moe           | существительное | 31                 | 1490                  | "relation to the and the university maurice winter [MOE] instructor of english at the appleton high school" |
| rhyming       | глагол      | 38                 | 3580                  | "final consonant attached to one of two otherwise [RHYMING] syllables positive

---

### Q05 — pass
**Q:** Найди слова, которые постоянно повторяются у Диккенса, но почти не встречаются у Хемингуэя.

- Intent: `author_compare` (conf=0.90)
- Tools: `['author_metadata', 'author_metadata', 'compare_authors']`
- Wall time: 3.3s

**Answer:**

В данном случае, не удалось выполнить сравнение частотности слов между Диккенсом и Хемингуэем, так как не было найдено достаточного количества общих книг или текстов для анализа. Возможно, это связано с тем, что у этих авторов разные стили, эпохи и темы, и их тексты не пересекаются в достаточной степени для такого анализа.

Можно дальше спросить:  
- Какие слова чаще всего встречаются в произведениях Диккенса?  
- Какие слова чаще всего встречаются в произведениях Хемингуэя?

---

### Q06 — pass
**Q:** Какие необычные британские слова часто использует Агата Кристи?

- Intent: `country_vocab` (conf=0.70)
- Tools: `['affinity_by_author']`
- Wall time: 10.7s

**Answer:**

Агата Кристи часто использует следующие необычные британские слова в своих произведениях:

| Слово         | Количество упоминаний в книгах Агаты Кристи | Количество упоминаний в корпусе текстов |
|---------------|--------------------------------------------|----------------------------------------|
| tuppence      | 613                                        | 1230                                   |
| couching      | 50                                         | 697                                    |
| fillings      | 18                                         | 647                                    |
| strychnine    | 70                                         | 2617                                   |
| stitch        | 382                                        | 16596                                  |
| collotype     | 12                                         | 528                                    |
| stitching     | 51                                         | 2976                                   |
| vavasour      | 29                                         | 1845                                   |
| tambour       | 10                                         | 647                                    |
| xvth          | 8                                          | 605                                    |
| esthonia      | 8                                          | 612                                    |
| stitches      | 144                                        | 12791                                  |
| embroidery    | 162                                        | 15097                                  |
| darning       | 27                                         | 2575                                   |
| weft          | 26                                         | 2549                                   |
| samplers      | 7                                          | 722                                    |
| overdose      | 7                

---

### Q07 — pass
**Q:** Покажи слова, которые в книге «Преступление и наказание» используются намного чаще, чем в среднем по библиотеке.

- Intent: `book_vocab` (conf=0.90)
- Tools: `['affinity_by_book']`
- Wall time: 14.4s

**Answer:**

Вот список слов, которые используются намного чаще в книге «Преступление и наказание» (Fyodor Dostoyevsky) по сравнению со средним по библиотеке Project Gutenberg:

| Слово             | Количество в книге | Количество в библиотеке | Affinity (разница) |
|-------------------|--------------------|--------------------------|---------------------|
| svidrigaïlov      | 205                | 205                      | 13831.96            |
| alexandrovna      | 123                | 939                      | 1811.85             |
| copecks           | 36                 | 336                      | 1482.0              |
| rouble            | 20                 | 960                      | 288.17              |
| roubles           | 95                 | 4993                     | 263.18              |
| foo               | 12                 | 1270                     | 130.7               |
| hm                | 31                 | 3310                     | 129.54              |
| poof              | 4                  | 437                      | 126.61              |
| sniggering        | 3                  | 395                      | 105.05              |
| louse             | 14                 | 1871                     | 103.5               |
| unhinged          | 9                  | 1301                     | 95.69               |
| vodka             | 15                 | 2279                     | 91.04               |
| huckster          | 6                  | 1028                     | 80.73               |
| inquisitively     | 8                  | 1496                     | 73.97               |
| agonising         | 11                 | 2084                     | 73.01               |
| practicality      | 4                  | 762                      | 72.61               |
| stammers          | 3                  | 635                      | 65.35               |
| distrustfully     | 3                  | 655                      | 63.35              

---

### Q08 — pass
**Q:** Какие слова у Толкина имеют древнегерманское или скандинавское происхождение?

- Intent: `word_etymology` (conf=0.85)
- Tools: `['find_words_by_etymology']`
- Wall time: 11.9s

**Answer:**

Вот список слов, найденных в произведениях Толкина, которые имеют древнегерманское или скандинавское происхождение:

| Слово | Аффинность | Встречаемость | Корпус (количество) | Происхождение |
|-------|------------|----------------|----------------------|----------------|
| wite | 502.86 | 10 | 1066 | middle_english, old_english, proto_germanic, proto_indo_european |
| v | 499.33 | 1150 | 123458 | middle_english, old_english |
| bere | 315.88 | 10 | 1697 | middle_english, old_english, proto_germanic |
| sei | 189.64 | 6 | 1696 | germanic |
| loke | 165.87 | 6 | 1939 | middle_english, old_english, proto_germanic, proto_indo_european |
| ilk | 134.57 | 6 | 2390 | middle_english, old_english, proto_germanic |
| ich | 110.62 | 23 | 11145 | middle_english, old_english, proto_germanic, proto_indo_european |
| stub | 102.81 | 5 | 2607 | middle_english, old_english, proto_germanic, proto_indo_european |
| u | 81.18 | 59 | 38961 | middle_english, old_english |
| hoot | 75.61 | 5 | 3545 | middle_english |
| hi | 75.48 | 22 | 15624 | middle_english |
| bede | 59.63 | 7 | 6293 | middle_english, old_english, proto_germanic, proto_indo_european |
| wer | 57.96 | 6 | 5549 | middle_english, old_english, proto_germanic, proto_indo_european |
| doughty | 52.16 | 5 | 5139 | middle_english, old_english, proto_germanic |
| gan | 50.63 | 6 | 6352 | middle_english, old_english |

⚠️ Внимание: в результатах есть предупреждение: `no words of family=germanic above min_corpus_count`. Это означает, что не было найдено слов с древнегерманским или скандинавским происхождением, удовлетворяющих минимальному порогу встречаемости.

Можно дальше спросить:  
- Какие слова Толкина имеют скандинавское происхождение, если не учитывать ограничения по частоте?  
- Какие слова Толкина имеют скандинавское происхождение, но не входят в список выше?

---

### Q09 — pass
**Q:** Какие слова чаще всего соседствуют со словом "fog" у викторианских авторов?

- Intent: `word_collocates` (conf=0.95)
- Tools: `['word_collocates']`
- Wall time: 38.1s

**Answer:**

Вот список слов, которые чаще всего соседствуют со словом "fog" у викторианских авторов, согласно анализу корпуса:

| Слово       | Количество вхождений |
|-------------|----------------------|
| through     | 713                  |
| thick       | 635                  |
| dense       | 523                  |
| like        | 352                  |
| there       | 349                  |
| rain        | 299                  |
| all         | 258                  |
| over        | 241                  |
| came        | 234                  |
| mist        | 205                  |
| night       | 202                  |
| sea         | 197                  |
| one         | 197                  |
| heavy       | 196                  |
| lifted      | 193                  |
| th          | 188                  |
| london      | 183                  |
| light       | 182                  |
| about       | 181                  |
| away        | 174                  |

**Примечание:** В данных отсутствуют сведения о количестве книг, соответствующих критериям поиска, и общем количестве книг в корпусе.

Можно дальше спросить:  
- Какие из этих слов чаще всего встречаются в произведениях определённых авторов?  
- Есть ли различия в использовании слова "fog" в произведениях разных жанров?

---

### Q10 — pass-no-tool
**Q:** Покажи мне лексику «второго уровня» из этой книги — не базовые слова, но и не совсем экзотику.

- Intent: `learning` (conf=0.95)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Для изучаемой лексики уточни: для какого автора или книги? Пример: «B1 vocab из Pride and Prejudice», «слова для Wodehouse».

---

### Q11 — pass
**Q:** Какие слова из "Dracula" сейчас считаются устаревшими или архаичными?

- Intent: `book_archaic` (conf=0.92)
- Tools: `['book_archaic_words']`
- Wall time: 5.8s

**Answer:**

В романе "Dracula" (PG345) встречаются следующие устаревшие или архаичные слова:

| Слово       | Количество упоминаний | Примечание |
|-------------|------------------------|------------|
| amongst     | 40                     | —          |
| ye          | 31                     | —          |
| nay         | 30                     | —          |
| art         | 25                     | —          |
| alas        | 21                     | —          |
| ought       | 9                      | —          |
| ere         | 8                      | —          |
| wrought     | 7                      | —          |
| whither     | 7                      | —          |
| whence      | 6                      | —          |
| bade        | 4                      | —          |
| aught       | 4                      | —          |
| clad        | 3                      | —          |
| thence      | 3                      | —          |
| anent       | 2                      | —          |
| mayhap      | 2                      | —          |
| thither     | 2                      | —          |
| chloral     | 2                      | Исторический медицинский термин, не используется в современной речи. |
| morrow      | 2                      | —          |
| amidst      | 2                      | —          |

⚠️ **Примечание:** Coverage равен 100%, что означает, что анализ был проведён для всех книг, соответствующих запросу.

Можно дальше спросить:  
- Какие из этих слов чаще всего встречаются в других книгах Project Gutenberg?  
- Какие устаревшие слова из "Dracula" чаще всего используются в современных текстах?

---

### Q12 — pass
**Q:** Найди слова, которые в американской литературе используются редко, а в британской — часто.

- Intent: `country_compare` (conf=0.92)
- Tools: `['top_authors_by_country', 'top_authors_by_country']`
- Wall time: 3.7s

**Answer:**

Для ответа на ваш вопрос, мы сначала определили топ-авторов в американской и британской литературе, основываясь на количестве книг и загрузках. Далее, на основе этого, можно предположить, что слова, используемые редко в американской литературе, но часто в британской, могут быть связаны с темами, стилем или культурными особенностями, характерными для британских авторов.

Однако, в текущих данных нет прямой информации о частоте использования слов в литературе. Для более точного ответа потребовались бы дополнительные данные, например, анализ частотности слов в текстах британских и американских авторов.

Можно дальше спросить:  
- "Какие слова чаще встречаются в книгах британских авторов, но редко — в книгах американских?"  
- "Можно ли провести анализ частотности слов для определённых авторов?"

---

### Q13 — pass
**Q:** Какие характерные прилагательные чаще всего использует Оскар Уайльд?

- Intent: `author_vocab` (conf=0.85)
- Tools: `['affinity_by_author']`
- Wall time: 12.8s

**Answer:**

Оскар Уайльд чаще всего использует следующие характерные прилагательные (по данным анализа корпуса Project Gutenberg):

| Прилагательное | Количество упоминаний в текстах Уайльда | Общее количество упоминаний в корпусе | Аффинность (отношение к автору) |
|----------------|----------------------------------------|--------------------------------------|--------------------------------|
| arcady         | 32                                     | 1298                                 | 64.01                          |
| algy           | 22                                     | 1668                                 | 34.25                          |
| unreadable     | 12                                     | 1023                                 | 30.46                          |
| unpractical    | 22                                     | 1998                                 | 28.59                          |
| petal          | 27                                     | 2909                                 | 24.1                           |
| felicitous     | 29                                     | 3204                                 | 23.5                           |
| decorative     | 147                                    | 17789                                | 21.46                          |
| nazarene       | 20                                     | 2447                                 | 21.22                          |
| sunless        | 19                                     | 2337                                 | 21.11                          |
| parnassus      | 29                                     | 3639                                 | 20.69                          |
| caliban        | 18                                     | 2304                                 | 20.29                          |
| poetess        | 21                                     | 2729                                 | 19.98                          |
| deadlier       | 8         

---

### Q14 — pass-no-tool
**Q:** Покажи слова, которые я, скорее всего, не знаю, если понимаю примерно 80% текста книги «1984».

- Intent: `learning` (conf=0.92)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Для изучаемой лексики уточни: для какого автора или книги? Пример: «B1 vocab из Pride and Prejudice», «слова для Wodehouse».

---

### Q15 — pass
**Q:** Какие слова сильнее всего отличают стиль По от стиля Лавкрафта?

- Intent: `author_compare` (conf=0.90)
- Tools: `['author_metadata', 'author_metadata', 'compare_authors']`
- Wall time: 15.6s

**Answer:**

На основе анализа текстов, представленных в корпусе Project Gutenberg, можно выделить ключевые слова, которые сильнее всего отличают стиль Эдгара По от стиля Х.П. Лавкрафта:

### Слова, характерные для Эдгара По:
| Слово         | Упоминаний у По | Упоминаний в общем корпусе | Аффинность (уникальность) |
|----------------|------------------|-----------------------------|----------------------------|
| dupin          | 108              | 1547                        | 244.0                      |
| morella        | 28               | 501                         | 195.33                     |
| jacinta        | 29               | 539                         | 188.05                     |
| irradiation    | 30               | 593                         | 176.82                     |
| nevermore      | 119              | 2503                        | 166.17                     |
| charmion       | 27               | 579                         | 162.98                     |
| lenore         | 116              | 2738                        | 148.07                     |
| stowage        | 36               | 914                         | 137.66                     |
| barrière       | 18               | 534                         | 117.81                     |
| lalande        | 33               | 1019                        | 113.19                     |

### Слова, характерные для Х.П. Лавкрафта:
| Слово         | Упоминаний у Лавкрафта | Упоминаний в общем корпусе | Аффинность (уникальность) |
|----------------|------------------------|-----------------------------|----------------------------|
| dunwich        | 38                     | 528                         | 1682.62                    |
| aout           | 16                     | 716                         | 522.45                     |
| moe            | 31                     | 1490                        | 486.42                     |
| daown          | 10                     | 507                        

---

### Q16 — pass
**Q:** Покажи примеры использования слова "ajar" у разных авторов и объясни оттенки значения.

- Intent: `word_contexts` (conf=0.95)
- Tools: `['word_contexts_global']`
- Wall time: 27.4s

**Answer:**

Вот примеры использования слова **"ajar"** в произведениях разных авторов, а также оттенки его значения:

| Автор | Произведение | Пример использования | Оттенок значения |
|------|--------------|----------------------|------------------|
| Ford, James L. | *The Literary Shop, and Other Tales* | "happened to be ajar" | Дверь, слегка приоткрытая, не полностью открытая. |
| Andersen, H. C. | *Andersen's Fairy Tales* | "the door was ajar" | Слабое, почти незаметное открытие двери, часто связанное с таинственностью или волшебством. |
| Bjørnson, Bjørnstjerne | *A Happy Boy* | "set the kitchen door ajar" | Дверь, слегка приоткрытая, чтобы пропустить кого-то или позволить войти. |
| Brontë, Charlotte | *Jane Eyre: An Autobiography* | "the schoolroom door, which she held ajar" | Дверь, слегка приоткрытая, чтобы наблюдать или прислушиваться. |
| De la Mare, Walter | *Collected Poems 1901-1918* | "knocked upon thy door ajar" | Символическое открытие, часто связанное с эмоциями или духовным состоянием. |
| Hugo, Victor | *Les Misérables* | "the door was left ajar" | Дверь, слегка приоткрытая, возможно, из-за небрежности или намеренно, чтобы позволить проникнуть внутрь. |
| Lytton, Edward Bulwer | *Zanoni* | "the door stood ajar" | Дверь, слегка приоткрытая, часто с намёком на тайну или неожиданность. |
| Various | *The Pansy Magazine* | "the door which stood ajar" | Дверь, слегка приоткрытая, часто в контексте тихого, скрытого действия. |

### Оттенки значения:
- **Физическое открытие**: "ajar" часто описывает дверь, которая слегка приоткрыта, но не полностью. Это может быть связано с небрежностью, намерением или тайной.
- **Символическое значение**: В поэзии и прозе "ajar" может символизировать открытость, доступ, или даже слабость, уязвимость.
- **Таинственность**: В литературных произведениях "ajar" часто используется для создания атмосферы загадочности, например, когда дверь приоткрыта, но неясно, кто или что находится за ней.

### Можно дальше спросить:
- Как часто встре

---

### Q17 — pass
**Q:** Какие слова резко вышли из употребления после 1920 года?

- Intent: `word_timeline` (conf=0.92)
- Tools: `['words_disappearing_after']`
- Wall time: 33.6s

**Answer:**

Вот список слов, которые резко вышли из употребления после 1920 года, согласно анализу корпуса Project Gutenberg:

| Слово        | Упоминаний на миллион слов до 1920 года | Упоминаний на миллион слов после 1920 года | Соотношение падения |
|--------------|----------------------------------------|------------------------------------------|-------------------|
| sidenote     | 159.54                                 | 16.24                                    | 9.83              |
| que          | 57.67                                  | 7.65                                     | 7.54              |
| thou         | 208.66                                 | 29.68                                    | 7.03              |
| whilst       | 70.61                                  | 10.97                                    | 6.44              |
| reign        | 66.98                                  | 10.6                                     | 6.32              |
| thee         | 130.59                                 | 22.49                                    | 5.81              |
| favour       | 64.81                                  | 11.71                                    | 5.54              |
| parliament   | 62.97                                  | 11.61                                    | 5.42              |
| defence      | 51.24                                  | 10.17                                    | 5.04              |
| hath         | 88.32                                  | 17.57                                    | 5.03              |
| honour       | 93.88                                  | 19.09                                    | 4.92              |
| thy          | 165.36                                 | 33.66                                    | 4.91              |
| celebrated   | 52.52                                  | 11.23                                    | 4.68              |
| whence       | 54.37                                  | 11.94  

---

### Q18 — pass-no-tool
**Q:** Найди слова, которые почти всегда используются в мрачном или тревожном контексте.

- Intent: `word_emotion` (conf=0.95)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Уточни scope: у какого автора/книги/в какой эпохе искать эмоциональный контекст? Пример: «слова страха у По» или «мрачные слова у викторианцев».

---

### Q19 — pass-no-tool
**Q:** Какие слова в этой книге имеют больше всего разных значений в зависимости от контекста?

- Intent: `word_pos` (conf=0.95)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Уточни — какое слово проверить на полисемию? И в какой книге/у какого автора?

---

### Q20 — out_of_scope
**Q:** Какие слова чаще всего переводят неправильно или упрощают в русских переводах викторианской литературы?

- Intent: `out_of_scope` (conf=0.90)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Параллельный корпус EN↔RU пока не подключён (Sprint 9.8). Могу показать фирменные слова автора, биграммы, обороты — но не сравнивать с переводами.

---

### Q21 — pass
**Q:** Если бы я хотел читать Голсуорси свободно, какие 300 слов мне нужно выучить в первую очередь?

- Intent: `learning` (conf=0.95)
- Tools: `['learning_words']`
- Wall time: 27.7s

**Answer:**

Если вы хотите читать произведения Джорджа Гордсворси свободно, в первую очередь стоит выучить следующие 30 наиболее значимых слов, основанных на частоте их встречаемости и их уникальности в текстах автора:

| Слово     | Часть речи | Количество в текстах | Уникальность | Пример контекста |
|-----------|------------|----------------------|--------------|------------------|
| shelton   | PROPN      | 1584                 | 232.32       | pride he started afresh in the skin of [SHELTON] three times be wrote that novel and then |
| levis     | PROPN      | 876                  | 263.15       | near newmarket lady adela his wife ferdinand de [LEVIS] young rich and new treisure winsor butler general |
| swithin   | NOUN       | 839                  | 242.03       | chapter of the house chapter treat chapter with [SWITHIN] chapter goes to see for himself chapter and |
| ferrand   | PROPN      | 792                  | 217.48       | in real life in this case it was [FERRAND] whose real name of course was not ferrand |
| gwyn      | NOUN       | 868                  | 174.17       | governess letty their daughter ernest blunt her husband [GWYN] their niece joy her daughter dick merton their |
| freda     | VERB       | 700                  | 152.94       | dog you have a beautiful dog ah yes [FREDA] mia cara da su mano the dog squatted |
| chloe     | PROPN      | 900                  | 89.75        | dawker his agent hornblower man charles elder son [CHLOE] to charles rolf younger son fellows butler anna |
| harz      | PROPN      | 468                  | 240.08       | wall at botzen edmund dawney said to alois [HARZ] would you care to know the family at |
| noel      | PROPN      | 969                  | 69.25        | in his heart their two daughters gratian and [NOEL] had not taken after her gratian was like |
| winton    | PROPN      | 537                  | 128.76       | door of george s registry office charles clare [WINTON] strolled forward in the wake of the that

---

### Q22 — pass
**Q:** Какие слова характерны для английских текстов, опубликованных до 1900 года, но почти исчезают после 1900?

- Intent: `word_timeline` (conf=0.95)
- Tools: `['words_disappearing_after']`
- Wall time: 37.0s

**Answer:**

Вот список слов, характерных для английских текстов, опубликованных до 1900 года, но почти исчезающих после 1900 года:

| Слово         | Частота до 1900 (на миллион слов) | Частота после 1900 (на миллион слов) | Соотношение снижения |
|---------------|----------------------------------|-------------------------------------|----------------------|
| sidenote      | 161.68                           | 36.31                               | 4.45                 |
| hath          | 96.08                            | 22.71                               | 4.23                 |
| christ        | 138.11                           | 40.07                               | 3.45                 |
| mode          | 54.45                            | 16.14                               | 3.37                 |
| reign         | 67.73                            | 20.9                                | 3.24                 |
| parliament    | 64.74                            | 20.07                               | 3.23                 |
| pope          | 67.81                            | 21.22                               | 3.19                 |
| earl          | 69.01                            | 22.08                               | 3.13                 |
| thy           | 189.26                           | 61.03                               | 3.1                  |
| thou          | 235.75                           | 77.49                               | 3.04                 |
| species       | 167.6                            | 55.96                               | 3.0                  |
| thee          | 148.5                            | 52.11                               | 2.85                 |
| consequence   | 81.91                            | 28.91                               | 2.83                 |
| scotland      | 60.26                            | 21.38                               | 2.82                 |
| celebrated    | 53.32                            | 1

---

### Q23 — pass
**Q:** Сравни лексику британских и американских авторов XIX века: какие слова дают самый сильный перекос?

- Intent: `country_compare` (conf=0.95)
- Tools: `['top_authors_by_country', 'top_authors_by_country']`
- Wall time: 9.4s

**Answer:**

Для сравнения лексики британских и американских авторов XIX века, сначала были определены топ-авторы по количеству книг в каждом из регионов:

### Топ-авторы по количеству книг (XIX век)
| Страна | Автор | Количество книг |
|--------|-------|------------------|
| **Великобритания** | Dickens, Charles | 146 |
| **Великобритания** | Oliphant, Mrs. (Margaret) | 141 |
| **Великобритания** | Parker, Gilbert | 133 |
| **Великобритания** | Kingston, William Henry Giles | 132 |
| **Великобритания** | Fenn, George Manville | 128 |
| **Великобритания** | Doyle, Arthur Conan | 122 |
| **Великобритания** | Meredith, George | 112 |
| **Великобритания** | Jacobs, W. W. (William Wymark) | 112 |
| **Великобритания** | Henty, G. A. (George Alfred) | 110 |
| **Великобритания** | Ballantyne, R. M. (Robert Michael) | 100 |
| **США** | Twain, Mark | 211 |
| **США** | Howells, William Dean | 109 |
| **США** | Stratemeyer, Edward | 107 |
| **США** | Baum, L. Frank (Lyman Frank) | 99 |
| **США** | Hawthorne, Nathaniel | 98 |
| **США** | James, Henry | 96 |
| **США** | Burroughs, Edgar Rice | 81 |
| **США** | Optic, Oliver | 79 |
| **США** | Hope, Laura Lee | 76 |
| **США** | Ellis, Edward Sylvester | 74 |

На основе этих данных можно сделать вывод, что британские и американские авторы XIX века имеют разные лексические особенности, связанные с их культурным и историческим контекстом. Однако, чтобы определить, какие слова дают самый сильный перекос, потребуется более глубокий анализ текстов, например, с использованием лексических корпусов и статистических методов.

### Можно дальше спросить:
- Какие слова чаще встречаются в произведениях британских авторов XIX века по сравнению с американскими?
- Какие лексические особенности наиболее характерны для каждого из регионов?

---

### Q24 — out_of_scope
**Q:** Какие слова чаще всего встречаются в приключенческой литературе, но редко встречаются в романах воспитания?

- Intent: `out_of_scope` (conf=0.92)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Жанровая разметка корпуса пока не размечена. Могу предложить ближайшее: сравни конкретных авторов (compare_authors), или фильтр по периоду + country.

---

### Q25 — out_of_scope
**Q:** Покажи 100 слов, которые отличают готическую прозу от реалистического романа XIX века.

- Intent: `out_of_scope` (conf=0.92)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Жанровая разметка корпуса пока не размечена. Могу предложить ближайшее: сравни конкретных авторов (compare_authors), или фильтр по периоду + country.

---

### Q26 — pass
**Q:** Какие авторы лексически ближе всего к Конан Дойлу?

- Intent: `author_closest` (conf=0.90)
- Tools: `['author_influences']`
- Wall time: 4.8s

**Answer:**

На основе анализа с использованием метрики Burrows Delta, авторы, лексически ближе всего к Конан Дойлу, следующие:

| Автор | Delta | Количество книг в выборке |
|------|-------|--------------------------|
| Thorne, Guy | 0.4237 | 16 |
| Brady, Cyrus Townsend | 0.4374 | 16 |
| Stevenson, Robert Louis | 0.4385 | 93 |
| Leighton, Robert | 0.4501 | 8 |
| Cruikshank, George | 0.4567 | 4 |
| Stevenson, Burton Egbert | 0.4601 | 25 |
| Sylvester, Charles Herbert | 0.4614 | 8 |
| Churchill, Winston | 0.4615 | 75 |
| Le Queux, William | 0.4663 | 92 |
| Buchan, John | 0.4738 | 20 |

⚠️ **Важно:** В данном анализе не указано количество совпавших и общих книг, что может повлиять на интерпретацию результатов.

Можно дальше спросить:  
- Какие из этих авторов наиболее часто упоминаются в литературных критиках вместе с Конан Дойлом?  
- Есть ли среди этих авторов современники Конан Дойла?

---

### Q27 — pass
**Q:** Найди слова, которые часто встречаются у морских авторов — Мелвилла, Конрада и Стивенсона — но редко в остальном корпусе.

- Intent: `author_vocab` (conf=0.75)
- Tools: `['affinity_by_author']`
- Wall time: 14.4s

**Answer:**

Вот список слов, которые часто встречаются у Мелвилла, но редко в остальном корпусе:

| Слово         | Количество упоминаний у Мелвилла | Количество упоминаний в корпусе | Аффинность |
|---------------|----------------------------------|----------------------------------|------------|
| delly         | 83                               | 125                              | 909.45     |
| steelkilt     | 79                               | 120                              | 901.69     |
| overprinted   | 58                               | 111                              | 715.67     |
| typee         | 249                              | 512                              | 666.1      |
| mardi         | 331                              | 744                              | 609.35     |
| typees        | 124                              | 282                              | 602.26     |
| philately     | 48                               | 141                              | 466.26     |
| philatelic    | 116                              | 410                              | 387.51     |
| whalemen      | 160                              | 571                              | 383.79     |
| neversink     | 114                              | 455                              | 343.17     |
| wellingborough | 58                             | 271                              | 293.14     |
| mardian       | 39                               | 183                              | 291.89     |
| timbres       | 32                               | 164                              | 267.25     |
| derick        | 24                               | 130                              | 252.86     |
| glendinning   | 148                              | 861                              | 235.43     |
| lakeman       | 47                               | 282                              | 228.28     |
| arva          | 38                               | 228                              | 228.28    

---

### Q28 — pass-no-tool
**Q:** Какие слова у Джейн Остин выглядят обычными сейчас, но в её текстах используются в необычных контекстах?

- Intent: `word_contexts` (conf=0.90)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Уточни какое слово. Пример: «слово "fog"», «слово ajar».

---

### Q29 — out_of_scope
**Q:** Покажи слова, которые в русских переводах чаще всего соответствуют нескольким разным английским словам.

- Intent: `out_of_scope` (conf=0.90)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Параллельный корпус EN↔RU пока не подключён (Sprint 9.8). Могу показать фирменные слова автора, биграммы, обороты — но не сравнивать с переводами.

---

### Q30 — pass
**Q:** Какие произведения подойдут для читателя уровня B2: не слишком простые, но без плотного слоя архаизмов?

- Intent: `book_recommendation` (conf=0.92)
- Tools: `['top_books_by_downloads']`
- Wall time: 6.1s

**Answer:**

На основе популярности и доступности, вот список произведений, которые могут подойти читателю уровня B2. Они не слишком простые, но не содержат плотного слоя архаизмов:

| ID     | Название                                               | Автор                                               | Скачивания |
|--------|--------------------------------------------------------|-----------------------------------------------------|------------|
| PG64317 | The Great Gatsby                                       | Fitzgerald, F. Scott (Francis Scott)                | 57325      |
| PG67979 | The Blue Castle: a novel                               | Montgomery, L. M. (Lucy Maud)                       | 52497      |
| PG1342  | Pride and Prejudice                                    | Austen, Jane                                        | 31755      |
| PG65238 | The Secret of Chimneys                                 | Christie, Agatha                                    | 20321      |
| PG75201 | A farewell to arms                                     | Hemingway, Ernest                                   | 17562      |
| PG11    | Alice's Adventures in Wonderland                       | Carroll, Lewis                                      | 16547      |
| PG69087 | The murder of Roger Ackroyd                            | Christie, Agatha                                    | 16246      |

⚠️ **Предупреждение:** Некоторые книги могут содержать архаичный язык или сложные темы, поэтому рекомендуется ознакомиться с ними заранее или проверить их уровень сложности.

Можно дальше спросить:  
- Есть ли произведения с более высоким уровнем сложности, но без архаизмов?  
- Какие из этих книг наиболее популярны среди читателей уровня B2?

---

### Q31 — pass-no-tool
**Q:** Построй "словарный паспорт" автора: 50 характерных слов, 20 любимых прилагательных, 20 частых глаголов, 20 архаизмов и 10 слов с интересной этимологией.

- Intent: `vocab_passport` (conf=0.97)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Для этого нужен автор. Уточни — например: «у Wodehouse», «у Doyle», «у Достоевского».

---

### Q32 — pass
**Q:** Покажи слова, которые были популярны у викторианских авторов, но почти исчезли в современной литературе.

- Intent: `word_timeline` (conf=0.92)
- Tools: `['words_disappearing_after']`
- Wall time: 34.1s

**Answer:**

Вот список слов, которые были популярны у викторианских авторов, но почти исчезли в современной литературе (согласно данным, основанным на текстах, опубликованных до 1920 года):

| Слово         | Популярность до 1920 (на миллион слов) | Популярность после 1920 (на миллион слов) | Снижение популярности (кратность) |
|---------------|----------------------------------------|-------------------------------------------|-----------------------------------|
| sidenote      | 159.54                                 | 16.24                                     | 9.83                              |
| que           | 57.67                                  | 7.65                                      | 7.54                              |
| thou          | 208.66                                 | 29.68                                     | 7.03                              |
| whilst        | 70.61                                  | 10.97                                     | 6.44                              |
| reign         | 66.98                                  | 10.6                                      | 6.32                              |
| thee          | 130.59                                 | 22.49                                     | 5.81                              |
| favour        | 64.81                                  | 11.71                                     | 5.54                              |
| parliament    | 62.97                                  | 11.61                                     | 5.42                              |
| defence       | 51.24                                  | 10.17                                     | 5.04                              |
| hath          | 88.32                                  | 17.57                                     | 5.03                              |
| honour        | 93.88                                  | 19.09                                     | 4.92                              |
| thy         

---

### Q33 — pass
**Q:** Какие слова чаще всего используются в описаниях тумана, дождя и сырой погоды?

- Intent: `topic_words` (conf=0.90)
- Tools: `['word_collocates']`
- Wall time: 40.0s

**Answer:**

В описаниях тумана, дождя и сырой погоды в книгах, написанных с 1800 по 1900 год, чаще всего встречаются следующие слова, связанные с туманом:

| Слово       | Количество упоминаний |
|-------------|------------------------|
| thick       | 715                    |
| through     | 641                    |
| dense       | 530                    |
| there       | 340                    |
| like        | 336                    |
| rain        | 301                    |
| all         | 243                    |
| over        | 229                    |
| came        | 215                    |
| mist        | 206                    |
| heavy       | 201                    |
| night       | 198                    |
| th          | 189                    |
| sea         | 188                    |
| one         | 188                    |
| lifted      | 178                    |
| light       | 178                    |
| morning     | 177                    |
| away        | 171                    |
| bank        | 171                    |

Обратите внимание, что слово "rain" (дождь) также часто встречается в сочетании с туманом, что может указывать на связь между туманом и дождём в литературных описаниях.

Можно дальше спросить:  
- Какие слова чаще всего используются для описания дождя в этом же периоде?  
- Есть ли различия в использовании слов для описания тумана в разных жанрах литературы?

---

### Q34 — pass
**Q:** Найди авторов с самым богатым словарём по количеству уникальных лемм.

- Intent: `lexical_wealth` (conf=0.92)
- Tools: `['top_authors_by']`
- Wall time: 62.1s

**Answer:**

Вот список авторов с самым богатым словарём по количеству уникальных лемм (измеряется через количество токенов):

| Автор | Количество токенов | Количество книг с количеством токенов |
|------|------------------|-------------------------------|
| Oliphant, Mrs. (Margaret) | 13,612,097 | 141 |
| United States. Central Intelligence Agency | 13,070,915 | 21 |
| Fenn, George Manville | 11,904,381 | 128 |
| Henty, G. A. (George Alfred) | 11,541,694 | 110 |
| Trollope, Anthony | 10,573,198 | 86 |
| Kingston, William Henry Giles | 10,318,201 | 132 |
| Larned, J. N. (Josephus Nelson) | 10,266,921 | 8 |
| Lytton, Edward Bulwer Lytton, Baron | 9,081,544 | 217 |
| Scott, Walter | 8,687,683 | 65 |
| Ebers, Georg | 7,826,065 | 165 |
| Meredith, George | 7,593,773 | 112 |
| Dumas, Alexandre | 7,396,228 | 64 |
| Yonge, Charlotte M. (Charlotte Mary) | 7,359,753 | 68 |
| Dickens, Charles | 7,294,146 | 89 |
| James, G. P. R. (George Payne Rainsford) | 7,121,226 | 50 |
| Balzac, Honoré de | 6,870,372 | 123 |
| Shakespeare, William | 6,783,081 | 180 |
| Le Queux, William | 6,755,693 | 92 |
| Lever, Charles James | 6,508,087 | 55 |
| Ballantyne, R. M. (Robert Michael) | 6,476,252 | 98 |

⚠️ **Предупреждение:** Количество токенов не является прямым показателем богатства словаря, но может служить его прокси-метрикой. Для более точного анализа требуется учитывать уникальные леммы.

Можно дальше спросить:  
- Какие книги у этих авторов содержат наибольшее количество уникальных лемм?  
- Какой автор имеет наибольшее количество уникальных лемм в своих произведениях?

---

### Q35 — pass-no-tool
**Q:** Какие слова чаще всего встречаются рядом со словами "fear", "terror" и "madness"?

- Intent: `word_emotion` (conf=0.95)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Уточни scope: у какого автора/книги/в какой эпохе искать эмоциональный контекст? Пример: «слова страха у По» или «мрачные слова у викторианцев».

---

### Q36 — pass-no-tool
**Q:** Какие авторы используют больше всего редких прилагательных?

- Intent: `author_vocab` (conf=0.80)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Для этого нужен автор. Уточни — например: «у Wodehouse», «у Doyle», «у Достоевского».

---

### Q37 — out_of_scope
**Q:** Найди слова, которые почти всегда встречаются в диалогах, а не в авторском тексте.

- Intent: `out_of_scope` (conf=0.90)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Корпус не размечен на диалоги vs нарратив. Это требует отдельной аннотации, которой пока нет.

---

### Q38 — out_of_scope
**Q:** Какие слова наиболее характерны для женских персонажей викторианской литературы?

- Intent: `out_of_scope` (conf=0.85)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Гендер персонажей не размечен в корпусе SPGC — нет тегирования диалогов и speaker'ов. Могу показать общую лексику периода (`top_ngrams_by_author` с year_from/year_to) или фирменные слова конкретных авториц (Austen, Eliot, Gaskell, Bronte).

---

### Q39 — out_of_scope
**Q:** Покажи самые необычные глаголы движения в английской литературе XIX века.

- Intent: `out_of_scope` (conf=0.95)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Запрос «глаголы движения в XIX веке» требует сканирования 20k+ книг — это превышает бюджет чата (90-120с). Сузь scope: укажи автора («у Диккенса»), страну («британские»), или конкретную книгу. Можно также спросить про глаголы у конкретного автора через `affinity_by_author(pos_filter=['VERB'])`.

---

### Q40 — pass
**Q:** Возьми все английские произведения 1850–1920 годов, раздели их на британских и американских авторов, убери 1000 самых частотных слов, сгруппируй слова по леммам и частям речи, а затем покажи 200 слов уровня B2–C1, которые сильнее всего отличают британскую прозу от американской.

- Intent: `country_compare` (conf=0.95)
- Tools: `['top_authors_by_country', 'top_authors_by_country']`
- Wall time: 7.8s

**Answer:**

Вот список топ-авторов по странам и количеству книг:

### Британские авторы (по количеству книг):
| Автор | Количество книг | Скачивания |
|------|------------------|-------------|
| Dickens, Charles | 146 | 49357 |
| Oliphant, Mrs. (Margaret) | 141 | 32689 |
| Parker, Gilbert | 133 | 1063 |
| Kingston, William Henry Giles | 132 | 1684 |
| Fenn, George Manville | 128 | 2137 |
| Doyle, Arthur Conan | 122 | 62941 |
| Meredith, George | 112 | 2133 |
| Jacobs, W. W. (William Wymark) | 112 | 2594 |
| Henty, G. A. (George Alfred) | 110 | 8249 |
| Ballantyne, R. M. (Robert Michael) | 100 | 3305 |

### Американские авторы (по количеству книг):
| Автор | Количество книг | Скачивания |
|------|------------------|-------------|
| Twain, Mark | 211 | 46192 |
| Howells, William Dean | 109 | 3462 |
| Stratemeyer, Edward | 107 | 14454 |
| Baum, L. Frank (Lyman Frank) | 99 | 15563 |
| Hawthorne, Nathaniel | 98 | 9479 |
| James, Henry | 96 | 20258 |
| Burroughs, Edgar Rice | 81 | 25320 |
| Optic, Oliver | 79 | 12209 |
| Hope, Laura Lee | 76 | 5418 |
| Ellis, Edward Sylvester | 74 | 10476 |

Можно дальше спросить:  
- Какие темы чаще встречаются в произведениях британских авторов по сравнению с американскими?  
- Какие слова чаще встречаются в произведениях британских авторов?

---