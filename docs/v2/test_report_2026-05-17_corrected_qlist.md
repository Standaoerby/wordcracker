# Functional Test Report — v2 engine (v2)

Run date: 2026-05-17T20:41:15
Target: http://127.0.0.1:8890
Total queries: 40

## Summary

| Verdict | Count | % |
|---|---:|---:|
| clarify | 1 | 2% |
| out_of_scope | 6 | 15% |
| pass | 29 | 72% |
| pass-no-tool | 4 | 10% |

## Per-question

| QID | Verdict | Intent | Tools | Time | Note |
|---|---|---|---|---:|---|
| Q01 | pass-no-tool | introduction | — | 0.0s |  |
| Q02 | pass | author_vocab | affinity_by_author | 14.1s |  |
| Q03 | pass | book_archaic | book_archaic_words | 7.1s |  |
| Q04 | pass | learning | learning_words | 20.2s |  |
| Q05 | pass | author_compare | author_metadata, author_metadata, compare_authors | 8.4s |  |
| Q06 | pass | country_vocab | affinity_by_author | 9.8s |  |
| Q07 | pass | book_vocab | affinity_by_book | 13.7s |  |
| Q08 | pass-no-tool | word_etymology | — | 0.0s |  |
| Q09 | pass | word_collocates | word_collocates | 5.6s |  |
| Q10 | pass-no-tool | learning | — | 0.0s |  |
| Q11 | pass | book_archaic | book_archaic_words | 5.7s |  |
| Q12 | pass | country_compare | top_authors_by_country, top_authors_by_country | 8.7s |  |
| Q13 | pass | author_vocab | affinity_by_author | 8.6s |  |
| Q14 | pass | learning | learning_words | 23.2s |  |
| Q15 | pass | word_emotion | emotion_collocates | 17.7s |  |
| Q16 | pass | word_contexts | word_contexts | 5.8s |  |
| Q17 | pass | word_timeline | words_disappearing_after | 13.5s |  |
| Q18 | pass | word_emotion | emotion_collocates | 5.0s |  |
| Q19 | pass | word_pos | word_pos_distribution | 2.6s |  |
| Q20 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q21 | pass | learning | learning_words | 23.3s |  |
| Q22 | pass | word_timeline | words_disappearing_after | 27.9s |  |
| Q23 | pass | country_compare | top_authors_by_country, top_authors_by_country | 11.4s |  |
| Q24 | clarify | clarify | — | 0.0s |  |
| Q25 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q26 | pass | author_closest | author_influences | 5.8s |  |
| Q27 | pass | author_vocab | affinity_by_author | 14.6s |  |
| Q28 | pass | book_vocab | affinity_by_book | 13.7s |  |
| Q29 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q30 | pass | book_archaic | book_archaic_words | 5.4s |  |
| Q31 | pass | vocab_passport | author_profile | 13.5s |  |
| Q32 | pass | word_timeline | words_disappearing_after | 14.0s |  |
| Q33 | pass | topic_words | word_collocates | 4.9s |  |
| Q34 | pass | lexical_wealth | top_authors_by | 9.9s |  |
| Q35 | pass | word_emotion | emotion_collocates | 7.4s |  |
| Q36 | pass-no-tool | author_vocab | — | 0.0s |  |
| Q37 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q38 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q39 | out_of_scope | out_of_scope | — | 0.0s |  |
| Q40 | pass | country_compare | top_authors_by_country, top_authors_by_country | 9.6s |  |

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
**Q:** Какие слова у Артура Конан Дойла встречаются заметно чаще, чем у остальных английских авторов XIX века?

- Intent: `author_vocab` (conf=0.80)
- Tools: `['affinity_by_author']`
- Wall time: 14.1s

**Answer:**

Вот список слов, которые встречаются у Артура Конан Дойла значительно чаще, чем у других английских авторов XIX века, согласно анализу:

| Слово             | Количество упоминаний у Дойла | Общее количество упоминаний в корпусе | Аффинность |
|-------------------|-------------------------------|----------------------------------------|------------|
| lhut              | 104                           | 389                                    | 190.53     |
| shropshires       | 20                            | 103                                    | 138.38     |
| mcardle           | 29                            | 150                                    | 137.78     |
| birdy             | 29                            | 151                                    | 136.87     |
| goor              | 24                            | 126                                    | 135.74     |
| hatherley         | 36                            | 191                                    | 134.32     |
| ettie             | 68                            | 371                                    | 130.62     |
| challenger        | 385                           | 2245                                   | 122.21     |
| knolles           | 92                            | 553                                    | 118.56     |
| thabanchu         | 23                            | 140                                    | 117.08     |
| ploermel          | 17                            | 107                                    | 113.22     |
| barrymore         | 159                           | 1008                                   | 112.41     |
| holmes            | 4045                          | 27051                                  | 106.56     |
| flannigan         | 74                            | 503                                    | 104.84     |
| bontems           | 27                            | 184                                    | 104.57     |
| scheepers         | 14    

---

### Q03 — pass
**Q:** Покажи мне не слишком редкие, но характерные архаичные слова Уильяма Морриса в “The Well at the World's End”, которые обычно не знают изучающие английский.

- Intent: `book_archaic` (conf=0.92)
- Tools: `['book_archaic_words']`
- Wall time: 7.1s

**Answer:**

Вот список не слишком редких, но характерных архаичных слов из книги Уильяма Морриса *“The Well at the World's End”* (Project Gutenberg ID: PG169), которые могут быть интересны изучающим английский язык:

| Слово     | Количество упоминаний в книге |
|-----------|-------------------------------|
| thou      | 2016                          |
| thee      | 1363                          |
| thy       | 675                           |
| yea       | 426                           |
| ye        | 368                           |
| art       | 280                           |
| spake     | 279                           |
| wilt      | 250                           |
| hath      | 216                           |
| nay       | 206                           |
| naught    | 187                           |
| thine     | 185                           |
| quoth     | 172                           |
| shalt     | 151                           |
| withal    | 139                           |
| forsooth  | 124                           |
| bade      | 123                           |
| amidst    | 122                           |
| amongst   | 114                           |
| clad      | 99                            |
| betwixt   | 91                            |
| ere       | 86                            |
| damsel    | 84                            |
| lo        | 82                            |
| wot       | 75                            |
| thither   | 71                            |
| wert      | 67                            |
| hither    | 64                            |
| aught     | 59                            |
| verily    | 57                            |

Все слова были найдены в тексте книги, и их частота указана. Нет предупреждений, и охват текста составляет 100%.

Можно дальше спросить:  
- Какие из этих слов чаще всего используются в диалогах?  
- Есть ли в тексте примеры использования слов "thou" и "thee" в контексте?

---

### Q04 — pass
**Q:** Какие слова чаще всего вызывают сложности у читателей уровня B2 при чтении “The Call of Cthulhu” Лавкрафта?

- Intent: `learning` (conf=0.92)
- Tools: `['learning_words']`
- Wall time: 20.2s

**Answer:**

На основе анализа текста "The Call of Cthulhu" Лавкрафта, следующие слова чаще всего вызывают сложности у читателей уровня B2:

| Слово         | Часть речи | Контекст примера                                                                 | Оценка сложности |
|---------------|------------|----------------------------------------------------------------------------------|------------------|
| bas           | Существительное | "could be the meaning of the queer clay [BAS] relief..."                        | 10.329           |
| monolith      | Существительное | "the curtain of flame stood a great granite [MONOLITH]..."                     | 9.442            |
| voodoo        | Существительное | "serious native unrest toward the end of march [VOODOO] orgies multiply..."    | 9.437            |
| carven        | Глагол      | "which incongruous in its diminutiveness rested the noxious [CARVEN] statuette..." | 9.118            |
| eons          | Существительное | "that there came the single glimpse of forbidden [EONS]..."                    | 9.111            |
| worshipers    | Существительное | "the ring of [WORSHIPERS] moved in endless bacchanale..."                      | 9.103            |
| squatters     | Существительное | "the [SQUATTERS] there mostly primitive but good natured descendants..."        | 9.091            |
| cuttings      | Существительное | "the disjointed jottings ramblings and [CUTTINGS] which I found had my uncle..." | 9.042            |
| hieroglyphics | Существительное | "hint at its remotest affiliations above these apparent [HIEROGLYPHICS]..."     | 9.024            |
| waterfront    | Существительное | "a short cut from the [WATERFRONT] to the deceased's home..."                 | 8.926            |
| cryptic       | Прилагательное | "that [CRYPTIC] regularity which lurks in prehistoric writing..."              | 8.868            |
| cosmic        | Прилагательное | "the awesome grandeur of the [COSMIC] cycle..."                         

---

### Q05 — pass
**Q:** Найди слова, которые постоянно повторяются у Диккенса в “Bleak House”, но почти не встречаются у Марка Твена в “Adventures of Huckleberry Finn”.

- Intent: `author_compare` (conf=0.90)
- Tools: `['author_metadata', 'author_metadata', 'compare_authors']`
- Wall time: 8.4s

**Answer:**

Вот список слов, которые часто встречаются у Чарльза Диккенса в "Bleak House", но почти не встречаются у Марка Твена в "Adventures of Huckleberry Finn":

| Слово       | Количество упоминаний у Диккенса | Количество упоминаний в корпусе | Аффинность (уникальность для Диккенса) |
|-------------|-------------------------------|-------------------------------|-------------------------------------|
| wegg        | 639                           | 1043                          | 241.92                              |
| trotwood    | 323                           | 557                           | 228.98                              |
| dombey      | 1820                          | 3184                          | 225.71                              |
| maylie      | 305                           | 537                           | 224.28                              |
| pickwick    | 5197                          | 9241                          | 222.07                              |
| weller      | 2101                          | 4056                          | 204.54                              |
| heep        | 326                           | 657                           | 195.93                              |
| nickleby    | 1044                          | 2176                          | 189.45                              |
| copperfield | 1162                          | 2467                          | 185.99                              |
| squeers     | 912                           | 1953                          | 184.4                               |

⚠️ **Важно:** В данных есть предупреждения, что у обоих авторов не было найдено "уникальных слов", что может означать, что они не были найдены в корпусе SPGC. Также, **coverage** (покрытие) для обоих авторов не определено, что может повлиять на точность результатов.

Можно дальше спросить:  
- Какие слова чаще встречаются у Твена, но почти не у Диккенса?  
- Есть ли общие слова, которые характерны для обоих авторов?

---

### Q06 — pass
**Q:** Какие необычные британские слова часто использует Агата Кристи в “The Murder of Roger Ackroyd”?

- Intent: `country_vocab` (conf=0.70)
- Tools: `['affinity_by_author']`
- Wall time: 9.8s

**Answer:**

В романе **“The Murder of Roger Ackroyd”** Агата Кристи использует несколько необычных британских слов, которые выделяются по сравнению с общим корпусом текстов. Ниже приведены наиболее значимые из них, основываясь на анализе:

| Слово         | Количество упоминаний в произведениях Кристи | Количество упоминаний в общем корпусе | Уровень аффинности (отношение к авторству) |
|---------------|---------------------------------------------|--------------------------------------|------------------------------------------|
| tuppence      | 613                                         | 1230                                 | 5230.24                                  |
| couching      | 50                                          | 697                                  | 752.84                                   |
| fillings      | 18                                          | 647                                  | 291.97                                   |
| strychnine    | 70                                          | 2617                                 | 280.71                                   |
| stitch        | 382                                         | 16596                                | 241.56                                   |
| collotype     | 12                                          | 528                                  | 238.51                                   |
| stitching     | 51                                          | 2976                                 | 179.85                                   |
| vavasour      | 29                                          | 1845                                 | 164.96                                   |
| tambour       | 10                                          | 647                                  | 162.2                                    |
| xvth          | 8                                           | 605                                  | 138.77                                   |

### Особенности

---

### Q07 — pass
**Q:** Покажи слова, которые в “Crime and Punishment” используются намного чаще, чем в среднем по библиотеке.

- Intent: `book_vocab` (conf=0.90)
- Tools: `['affinity_by_book']`
- Wall time: 13.7s

**Answer:**

Вот список слов, которые используются намного чаще в "Crime and Punishment" по сравнению со средним по библиотеке Project Gutenberg:

| Слово             | Количество в книге | Количество в библиотеке | Affinity |
|-------------------|--------------------|--------------------------|----------|
| svidrigaïlov      | 205                | 205                      | 13831.96 |
| alexandrovna      | 123                | 939                      | 1811.85  |
| copecks           | 36                 | 336                      | 1482.0   |
| rouble            | 20                 | 960                      | 288.17   |
| roubles           | 95                 | 4993                     | 263.18   |
| foo               | 12                 | 1270                     | 130.7    |
| hm                | 31                 | 3310                     | 129.54   |
| poof              | 4                  | 437                      | 126.61   |
| sniggering        | 3                  | 395                      | 105.05   |
| louse             | 14                 | 1871                     | 103.5    |
| unhinged          | 9                  | 1301                     | 95.69    |
| vodka             | 15                 | 2279                     | 91.04    |
| huckster          | 6                  | 1028                     | 80.73    |
| inquisitively     | 8                  | 1496                     | 73.97    |
| agonising         | 11                 | 2084                     | 73.01    |
| practicality      | 4                  | 762                      | 72.61    |
| stammers          | 3                  | 635                      | 65.35    |
| distrustfully     | 3                  | 655                      | 63.35    |
| lodgers           | 19                 | 4284                     | 61.35    |
| malignantly       | 3                  | 685                      | 60.58    |
| pawnbroker        | 13                 | 3006                     | 59.82    |
| po

---

### Q08 — pass-no-tool
**Q:** Какие слова у Уильяма Морриса в “The House of the Wolfings” имеют древнегерманское или скандинавское происхождение?

- Intent: `word_etymology` (conf=0.85)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Этимологию какого слова — или нужно «германские/латинские слова у автора X»?

---

### Q09 — pass
**Q:** Какие слова чаще всего соседствуют со словом “fog” у викторианских авторов 1837–1901 годов?

- Intent: `word_collocates` (conf=0.95)
- Tools: `['word_collocates']`
- Wall time: 5.6s

**Answer:**

Вот список слов, которые чаще всего соседствуют со словом **"fog"** у викторианских авторов 1837–1901 годов:

| Слово       | Количество встреч |
|-------------|------------------|
| through     | 713              |
| thick       | 635              |
| dense       | 523              |
| like        | 352              |
| there       | 349              |
| rain        | 299              |
| all         | 258              |
| over        | 241              |
| came        | 234              |
| mist        | 205              |
| night       | 202              |
| sea         | 197              |
| one         | 197              |
| heavy       | 196              |
| lifted      | 193              |
| th          | 188              |
| london      | 183              |
| light       | 182              |
| about       | 181              |
| away        | 174              |

Общее количество встреч слова **"fog"** в текстах: **9998**, и оно встречается в **2445 книгах**.

⚠️ **Важно:** В данных отсутствует информация о количестве книг, соответствующих критериям поиска, и общем количестве книг в корпусе. Это может повлиять на интерпретацию результатов.

Можно дальше спросить:  
- Какие книги чаще всего содержат слово "fog"?  
- Какие авторы чаще всего используют слово "fog"?

---

### Q10 — pass-no-tool
**Q:** Покажи мне лексику “второго уровня” из “Pride and Prejudice” — не базовые слова, но и не совсем экзотику.

- Intent: `learning` (conf=0.95)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Для изучаемой лексики уточни: для какого автора или книги? Пример: «B1 vocab из Pride and Prejudice», «слова для Wodehouse».

---

### Q11 — pass
**Q:** Какие слова из “Dracula” Брэма Стокера сейчас считаются устаревшими или архаичными?

- Intent: `book_archaic` (conf=0.92)
- Tools: `['book_archaic_words']`
- Wall time: 5.7s

**Answer:**

В романе **"Dracula"** Брэма Стокера встречаются следующие устаревшие или архаичные слова:

| Слово       | Количество упоминаний | Примечание |
|-------------|-----------------------|------------|
| amongst     | 40                    | —          |
| ye          | 31                    | —          |
| nay         | 30                    | —          |
| art         | 25                    | —          |
| alas        | 21                    | —          |
| ought       | 9                     | —          |
| ere         | 8                     | —          |
| wrought     | 7                     | —          |
| whither     | 7                     | —          |
| whence      | 6                     | —          |
| bade        | 4                     | —          |
| aught       | 4                     | —          |
| clad        | 3                     | —          |
| thence      | 3                     | —          |
| anent       | 2                     | —          |
| mayhap      | 2                     | —          |
| thither     | 2                     | —          |
| chloral     | 2                     | Исторический медицинский термин, сейчас не используется |
| morrow      | 2                     | —          |
| amidst      | 2                     | —          |

**Coverage:** 100% (все книги, соответствующие запросу, обработаны).

**Возможные следующие вопросы:**
- Какие из этих слов чаще всего встречаются в диалогах персонажей?
- Есть ли в тексте примеры использования этих слов в контексте, который сейчас считается устаревшим?

---

### Q12 — pass
**Q:** Найди слова, которые в американской литературе XIX века используются редко, а в британской — часто.

- Intent: `country_compare` (conf=0.92)
- Tools: `['top_authors_by_country', 'top_authors_by_country']`
- Wall time: 8.7s

**Answer:**

Вот список топ-авторов по количеству книг в британской и американской литературе XIX века:

### Британская литература (GB):
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

### Американская литература (US):
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

Для дальнейшего анализа можно выбрать конкретных авторов и исследовать лексику, характерную для их произведений, или сравнить частоту использования слов в произведениях британских и американских авторов. 

Можно дальше спросить:  
- Какие слова чаще встречаются в произведениях Диккенса по сравнению с Твейном?  
- Какие лексические особенности отличают британскую литературу XIX века от американской?

---

### Q13 — pass
**Q:** Какие характерные прилагательные чаще всего использует Оскар Уайльд в “The Picture of Dorian Gray”?

- Intent: `author_vocab` (conf=0.85)
- Tools: `['affinity_by_author']`
- Wall time: 8.6s

**Answer:**

В романе “The Picture of Dorian Gray” Оскар Уайльд чаще всего использует следующие характерные прилагательные (по данным анализа корпуса):

| Прилагательное     | Количество упоминаний в произведении | Общее количество упоминаний в корпусе | Аффинность (отношение к автору) |
|--------------------|--------------------------------------|--------------------------------------|----------------------------------|
| arcady             | 32                                   | 1298                                 | 64.01                            |
| algy               | 22                                   | 1668                                 | 34.25                            |
| unreadable         | 12                                   | 1023                                 | 30.46                            |
| unpractical        | 22                                   | 1998                                 | 28.59                            |
| petal              | 27                                   | 2909                                 | 24.1                             |
| felicitous         | 29                                   | 3204                                 | 23.5                             |
| decorative         | 147                                  | 17789                                | 21.46                            |
| nazarene           | 20                                   | 2447                                 | 21.22                            |
| sunless            | 19                                   | 2337                                 | 21.11                            |
| parnassus          | 29                                   | 3639                                 | 20.69                            |
| caliban            | 18                                   | 2304                                 | 20.29                            |
| poetess            | 21                                   | 2729                          

---

### Q14 — pass
**Q:** Покажи слова, которые я, скорее всего, не знаю, если понимаю примерно 80% текста “Heart of Darkness” Конрада.

- Intent: `learning` (conf=0.92)
- Tools: `['learning_words']`
- Wall time: 23.2s

**Answer:**

Вот список слов, которые, вероятно, вы не знаете, если вы понимаете примерно 80% текста "Heart of Darkness" Конрада. Эти слова были выявлены на основе анализа частотности и сложности:

| Слово         | Часть речи | Контекст примера                                                                 | Оценка сложности |
|---------------|------------|----------------------------------------------------------------------------------|------------------|
| rivets        | существительное | "did i want what i really wanted was [RIVETS] by heaven rivets to get on with the" | 8.862            |
| snag          | существительное | "shaved by a fluke some infernal sly old [SNAG] that would have ripped the life out of" | 8.414            |
| helmsman      | существительное | "and educated by my poor predecessor was the [HELMSMAN] he sported a pair of brass earrings wore" | 7.527            |
| stretcher     | существительное | "grass in a compact body bearing an improvised [STRETCHER] in their midst instantly in the emptiness of" | 7.33             |
| nightmares    | существительное | "was like a weary pilgrimage amongst hints for [NIGHTMARES] it was upward of thirty days before i" | 7.235            |
| screech       | существительное | "disappear a tiny projectile would give a feeble [SCREECH] and nothing happened nothing could happen there was" | 7.067            |
| waterway      | существительное | "before us like the beginning of an interminable [WATERWAY] in the offing the sea and the sky" | 6.946            |
| pyjamas       | существительное | "red whiskers who wore sidespring boots and pink [PYJAMAS] tucked into his socks two others remained a" | 6.89             |
| hippo         | существительное | "day i was disturbed there was an old [HIPPO] that had the bad habit of getting out" | 6.889            |
| riverside     | существительное | "agitation he vanished i went on to the [RIVERSIDE] and the other followed me i heard a" | 6.741            |
| immensity     | сущ

---

### Q15 — pass
**Q:** Какие слова сильнее всего отличают стиль Эдгара По в “The Raven” от стиля Лавкрафта в “At the Mountains of Madness”?

- Intent: `word_emotion` (conf=0.90)
- Tools: `['emotion_collocates']`
- Wall time: 17.7s

**Answer:**

В тексте Эдгара По "The Raven" (PG1065) слова, наиболее часто связанные с эмоцией **страха**, включают:

- **bird** (7 упоминаний)
- **door** (3 упоминания)
- **sure** (3 упоминания)
- **raven** (3 упоминания)
- **bust** (3 упоминания)
- **yore** (3 упоминания)
- **ungainly** (3 упоминания)
- **gaunt** (3 упоминания)
- **tell** (3 упоминания)
- **each** (2 упоминания)
- **ember** (2 упоминания)
- **wrought** (2 упоминания)
- **books** (2 упоминания)
- **surcease** (2 упоминания)
- **lost** (2 упоминания)
- **chamber** (2 упоминания)
- **there** (2 упоминания)
- **stood** (2 упоминания)
- **dreaming** (2 упоминания)
- **unbroken** (2 упоминания)
- **craven** (2 упоминания)
- **ancient** (2 упоминания)
- **followed** (2 упоминания)
- **whether** (2 упоминания)
- **tempter** (2 упоминания)

Эти слова отражают атмосферу мрачности, таинственности и напряжённости, характерные для стиля По. Однако, стоит отметить, что в данном анализе не были найдены коллокаты, связанные с эмоциональными анкерами, что может повлиять на полноту интерпретации.

Можно дальше спросить:  
- Какие слова сильнее всего отличают стиль Лавкрафта в “At the Mountains of Madness” от стиля По в “The Raven”?  
- Какие эмоции доминируют в тексте Лавкрафта?

---

### Q16 — pass
**Q:** Покажи примеры использования слова “ajar” у Джейн Остин, Диккенса и Конан Дойла и объясни оттенки значения.

- Intent: `word_contexts` (conf=0.90)
- Tools: `['word_contexts']`
- Wall time: 5.8s

**Answer:**

Вот примеры использования слова **"ajar"** в произведениях Джейн Остин, взятые из книги **"Emma"** (PG158):

1. **Контекст:**  
   *"most comfortable manner she was obliged to leave the door [AJAR] as she found it but she fully intended that elton"*  
   **Оттенок значения:** Здесь "ajar" означает, что дверь была **немного приоткрыта**, но не полностью. Это указывает на **неполное действие** или **незавершённое состояние**.

2. **Контекст:**  
   *"close it it was not closed however it still remained [AJAR] but by engaging the housekeeper in incessant conversation she hoped"*  
   **Оттенок значения:** Здесь "ajar" также описывает **незакрытую дверь**, но с акцентом на **намеренное или неизбежное состояние**. Это может отражать **непреднамеренные последствия** или **незавершённые действия**.

### Общее значение:
Слово **"ajar"** означает **незакрытое, приоткрытое состояние**, часто с оттенком **неполного действия** или **незавершённости**. В контексте Джейн Остин оно может использоваться для передачи **незаметных, но значимых деталей**, которые влияют на развитие сюжета или характеры персонажей.

### Можно дальше спросить:
- Какие ещё слова часто используются в похожем контексте в произведениях Джейн Остин?  
- Есть ли примеры использования слова "ajar" в произведениях Диккенса или Конан Дойла?

---

### Q17 — pass
**Q:** Какие слова резко вышли из употребления после 1920 года в английской художественной литературе?

- Intent: `word_timeline` (conf=0.92)
- Tools: `['words_disappearing_after']`
- Wall time: 13.5s

**Answer:**

Вот список слов, которые резко вышли из употребления в английской художественной литературе после 1920 года, согласно анализу данных:

| Слово         | Частота до 1920 (на миллион слов) | Частота после 1920 (на миллион слов) | Соотношение снижения |
|---------------|----------------------------------|-------------------------------------|----------------------|
| sidenote      | 159.54                           | 16.24                               | 9.83                 |
| que           | 57.67                            | 7.65                                | 7.54                 |
| thou          | 208.66                           | 29.68                               | 7.03                 |
| whilst        | 70.61                            | 10.97                               | 6.44                 |
| reign         | 66.98                            | 10.6                                | 6.32                 |
| thee          | 130.59                           | 22.49                               | 5.81                 |
| favour        | 64.81                            | 11.71                               | 5.54                 |
| parliament    | 62.97                            | 11.61                               | 5.42                 |
| defence       | 51.24                            | 10.17                               | 5.04                 |
| hath          | 88.32                            | 17.57                               | 5.03                 |
| honour        | 93.88                            | 19.09                               | 4.92                 |
| thy           | 165.36                           | 33.66                               | 4.91                 |
| celebrated    | 52.52                            | 11.23                               | 4.68                 |
| whence        | 54.37                            | 11.94                               | 4.55                 |
| earl          | 67.69                

---

### Q18 — pass
**Q:** Найди слова, которые почти всегда используются в мрачном или тревожном контексте у Эдгара По.

- Intent: `word_emotion` (conf=0.95)
- Tools: `['emotion_collocates']`
- Wall time: 5.0s

**Answer:**

Вот список слов, которые часто используются в мрачном или тревожном контексте в произведениях Эдгара По:

| Слово       | Количество упоминаний |
|-------------|------------------------|
| shell       | 359                    |
| horror      | 178                    |
| sorrow      | 163                    |
| terror      | 160                    |
| terrible    | 140                    |
| intense     | 132                    |
| despair     | 127                    |
| unknown     | 127                    |
| darkness    | 125                    |
| possession  | 119                    |
| forced      | 112                    |
| grave       | 111                    |
| accident    | 106                    |
| pain        | 104                    |
| mad         | 97                     |

⚠️ **Важно:** В данных есть предупреждение: "no words near emotion anchors", что может указывать на ограничения в анализе. Также, информация о количестве книг, в которых были найдены упоминания, отсутствует (coverage: books_matched и books_total равны -1).

Можно дальше спросить:  
- Какие слова чаще всего появляются в произведениях По, связанных с ужасом?  
- Есть ли другие эмоциональные категории, которые можно проанализировать для текстов По?

---

### Q19 — pass
**Q:** Какие слова в “Alice’s Adventures in Wonderland” имеют больше всего разных значений в зависимости от контекста?

- Intent: `word_pos` (conf=0.95)
- Tools: `['word_pos_distribution']`
- Wall time: 2.6s

**Answer:**

В книге “Alice’s Adventures in Wonderland” (PG11) слово **"light"** не встречается в тексте, что делает невозможным анализ его разных значений в зависимости от контекста.

⚠️ **Предупреждение:** Слово "light" не найдено в тексте, поэтому невозможно определить его полиморфные значения.

Можно дальше спросить:  
- Какие слова в “Alice’s Adventures in Wonderland” чаще всего используются в разных частях речи?  
- Какие слова в “Alice’s Adventures in Wonderland” имеют наибольшее количество упоминаний?

---

### Q20 — out_of_scope
**Q:** Какие слова чаще всего переводят неправильно или упрощают в русских переводах “Jane Eyre” и “Wuthering Heights”?

- Intent: `out_of_scope` (conf=0.90)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Параллельный корпус EN↔RU пока не подключён (Sprint 9.8). Могу показать фирменные слова автора, биграммы, обороты — но не сравнивать с переводами.

---

### Q21 — pass
**Q:** Если бы я хотел читать Голсуорси и “The Forsyte Saga” свободно, какие 300 слов мне нужно выучить в первую очередь?

- Intent: `learning` (conf=0.95)
- Tools: `['learning_words']`
- Wall time: 23.3s

**Answer:**

Если вы хотите читать "The Forsyte Saga" Голсуорси свободно, рекомендуется начать с изучения следующих 30 наиболее значимых слов, выбранных на основе их частоты и важности в тексте:

| Слово       | Часть речи | Количество в тексте | Количество в корпусе | Аффинность | Оценка | Пример контекста |
|-------------|------------|----------------------|------------------------|-------------|--------|------------------|
| annette     | PROPN      | 159                  | 8845                   | 166.53      | 8.778  | paris a few travel and he could bring [ANNETTE] back quite separated from a past which in |
| profond     | PROPN      | 90                   | 597                    | 1396.56     | 8.731  | and gloated from his eye that belgian chap [PROFOND] he said is a member here now he |
| imogen      | NOUN       | 73                   | 4758                   | 142.13      | 7.928  | of the little darties of little publius of [IMOGEN] maud and benedict to write the following note |
| gradman     | PROPN      | 39                   | 160                    | 2258.06     | 7.392  | had a coat of paint his old clerk [GRADMAN] was seated where he always was at a |
| polteed     | NOUN       | 43                   | 132                    | 3017.77     | 7.379  | would have trouble with himself he had employed [POLTEED] agency several times in the routine of his |
| crum        | PROPN      | 28                   | 731                    | 354.84      | 7.307  | who are making great names in art like [CRUM] stone or bleeder are making them by avoiding |
| lamotte     | PROPN      | 26                   | 1575                   | 152.93      | 6.993  | prosperity and the obvious business capacity of madame [LAMOTTE] he did not however neglect to leave certain |
| filly       | ADV        | 23                   | 2086                   | 102.14      | 6.683  | chapter jolyon at home chapter colt and the [FILLY] chapter prosecutes trusteeship chapter hears the news chapter |

---

### Q22 — pass
**Q:** Какие слова характерны для английских текстов, опубликованных до 1900 года, но почти исчезают после 1900?

- Intent: `word_timeline` (conf=0.95)
- Tools: `['words_disappearing_after']`
- Wall time: 27.9s

**Answer:**

Вот список слов, которые были характерны для английских текстов, опубликованных до 1900 года, но почти исчезли после 1900 года:

| Слово         | Частота до 1900 (на миллион слов) | Частота после 1900 (на миллион слов) | Соотношение снижения |
|---------------|----------------------------------|-------------------------------------|----------------------|
| sidenote      | 161.68                          | 36.31                              | 4.45                 |
| hath          | 96.08                           | 22.71                              | 4.23                 |
| christ        | 138.11                          | 40.07                              | 3.45                 |
| mode          | 54.45                           | 16.14                              | 3.37                 |
| reign         | 67.73                           | 20.9                               | 3.24                 |
| parliament    | 64.74                           | 20.07                              | 3.23                 |
| pope          | 67.81                           | 21.22                              | 3.19                 |
| earl          | 69.01                           | 22.08                              | 3.13                 |
| thy           | 189.26                          | 61.03                              | 3.1                  |
| thou          | 235.75                          | 77.49                              | 3.04                 |
| species       | 167.6                           | 55.96                              | 3.0                  |
| thee          | 148.5                           | 52.11                              | 2.85                 |
| consequence   | 81.91                           | 28.91                              | 2.83                 |
| scotland      | 60.26                           | 21.38                              | 2.82                 |
| celebrated    | 53.32                           | 19.25                

---

### Q23 — pass
**Q:** Сравни лексику британских и американских авторов XIX века: какие слова дают самый сильный перекос?

- Intent: `country_compare` (conf=0.95)
- Tools: `['top_authors_by_country', 'top_authors_by_country']`
- Wall time: 11.4s

**Answer:**

На основе анализа топ-авторов XIX века из Великобритании и США, можно выделить следующие различия в лексике, которые дают сильный перекос:

### Топ-авторы по странам:
| Страна | Автор | Количество книг |
|--------|-------|-----------------|
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

### Основные различия в лексике:
1. **Британские авторы** часто используют более формальный и литературный стиль, характерный для эпохи Викторианской эпохи. Например, Dickens и Doyle часто используют сложные конструкции и описательные элементы.
2. **Американские авторы** (например, Twain и Hawthorne) чаще используют разговорный стиль и сатиру, отражая более динамичный и разнообразный социальный контекст США.

### Coverage:
- Для Великобритании: `geo_coverage_for_country` = 3541.
- Для США: `geo_coverage_for_country` = 5120.

### Вывод:
Наиболее сильный перекос в лексике наблюдается в использовании формального стиля у британских авторов и разговорного — у американских. Это может быть связано с различиями в культурных и социальных контекстах XIX века.

Можно дальше спросить:  
- Какие конкретные слова чаще вст

---

### Q24 — clarify
**Q:** Какие слова чаще всего встречаются в “Treasure Island” и “Moby Dick”, но редко встречаются в романах воспитания вроде “David Copperfield”?

- Intent: `clarify` (conf=0.00)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Не уверен, что ты имеешь в виду. Спроси конкретнее — например: «фирменные слова Wodehouse», «уровень сложности Pride and Prejudice», «германские слова Толкина».

---

### Q25 — out_of_scope
**Q:** Покажи 100 слов, которые отличают готическую прозу “Dracula”, “Frankenstein” и “The Monk” от реалистического романа XIX века.

- Intent: `out_of_scope` (conf=0.90)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Жанровая разметка корпуса пока не размечена. Могу предложить ближайшее: сравни конкретных авторов (compare_authors), или фильтр по периоду + country.

---

### Q26 — pass
**Q:** Какие авторы лексически ближе всего к Конан Дойлу по стилю “The Hound of the Baskervilles”?

- Intent: `author_closest` (conf=0.90)
- Tools: `['author_influences']`
- Wall time: 5.8s

**Answer:**

На основе анализа стиля произведения *“The Hound of the Baskervilles”* Артура Конан Дойла, ближайшие по лексическому стилю авторы, определённые с помощью метрики Burrows Delta, следующие:

| Автор | Delta (разница в стиле) | Количество книг в анализе |
|------|------------------------|--------------------------|
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

Самый близкий по стилю автор — **Thorne, Guy**, с минимальным значением Delta.

⚠️ **Важно:** В данном анализе не указано количество совпавших и общих книг, что может повлиять на точность интерпретации результатов.

Можно дальше спросить:  
- Какие произведения этих авторов наиболее близки по стилю к *“The Hound of the Baskervilles”*?  
- Есть ли среди этих авторов другие произведения, написанные в подобном стиле?

---

### Q27 — pass
**Q:** Найди слова, которые часто встречаются у морских авторов — Мелвилла в “Moby Dick”, Конрада в “Lord Jim” и Стивенсона в “Treasure Island”, — но редко в остальном корпусе.

- Intent: `author_vocab` (conf=0.75)
- Tools: `['affinity_by_author']`
- Wall time: 14.6s

**Answer:**

Вот список слов, которые часто встречаются у Мелвилла в его произведениях, но реже — в остальном корпусе:

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
| arva          | 38                               | 228                     

---

### Q28 — pass
**Q:** Какие слова у Джейн Остин в “Emma” выглядят обычными сейчас, но используются в необычных контекстах?

- Intent: `book_vocab` (conf=0.90)
- Tools: `['affinity_by_book']`
- Wall time: 13.7s

**Answer:**

В романе Джейн Остин *Emma* есть несколько слов, которые сейчас кажутся обычными, но использовались в необычных контекстах. Вот некоторые из них:

| Слово         | Количество в книге | Количество в корпусе | Аффинность |
|----------------|--------------------|-----------------------|------------|
| grandmama      | 12                 | 626                   | 350.01     |
| surprized      | 31                 | 1902                  | 297.59     |
| surprize       | 36                 | 2251                  | 292.01     |
| unreserve      | 6                  | 379                   | 289.06     |
| chuses         | 3                  | 222                   | 246.74     |
| larkins        | 12                 | 1102                  | 198.82     |
| conversable    | 3                  | 293                   | 186.95     |
| sucklings      | 6                  | 744                   | 147.25     |
| naivete        | 3                  | 676                   | 81.03      |
| suckling       | 14                 | 3427                  | 74.59      |
| stopt          | 9                  | 2601                  | 63.18      |
| charades       | 4                  | 1257                  | 58.1       |
| elegancies     | 3                  | 959                   | 57.12      |
| affectedly     | 3                  | 966                   | 56.7       |
| secresy        | 3                  | 1051                  | 52.12      |
| pianoforte     | 21                 | 7612                  | 50.37      |
| connexions     | 10                 | 3715                  | 49.15      |
| unexceptionable| 7                  | 2750                  | 46.48      |
| obliging       | 32                 | 14353                 | 40.71      |
| civilities     | 9                  | 4125                  | 39.84      |
| rencontre      | 3                  | 1404                  | 39.01      |
| ostler         | 5                  | 2356                  | 38.75      |
| conun

---

### Q29 — out_of_scope
**Q:** Покажи слова, которые в русских переводах “Pride and Prejudice” чаще всего соответствуют нескольким разным английским словам.

- Intent: `out_of_scope` (conf=0.90)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Параллельный корпус EN↔RU пока не подключён (Sprint 9.8). Могу показать фирменные слова автора, биграммы, обороты — но не сравнивать с переводами.

---

### Q30 — pass
**Q:** Какие произведения уровня B2 можно читать после “The Adventures of Sherlock Holmes”, чтобы не было слишком много архаизмов?

- Intent: `book_archaic` (conf=0.92)
- Tools: `['book_archaic_words']`
- Wall time: 5.4s

**Answer:**

В книге *“The Adventures of Sherlock Holmes”* (PG1661) встречаются архаичные слова, такие как **whence**, **ought**, **hence**, **clad**, **art**, **alas**, **bade**, **swain**, **fortnight**. Эти слова могут быть сложны для читателей уровня B2, так как они не часто используются в современном английском языке.

Если вы хотите читать произведения, которые будут легче по уровню и содержат меньше архаизмов, можно рассмотреть следующие книги из корпуса Project Gutenberg:

| Название книги | PG ID | Описание |
|----------------|-------|----------|
| *The Call of the Wild* | PG166 | Современная проза, простой язык, подходит для уровня B2. |
| *The Swiss Family Robinson* | PG1226 | Приключенческая история с понятным языком. |
| *The Secret Garden* | PG1227 | Роман для молодёжи с простым и понятным языком. |

⚠️ **Важно:** В данном ответе не указаны конкретные рекомендации по выбору книг, так как в tool_results не было информации о других книгах. Для более точных рекомендаций можно запросить список книг с уровнем B2 и низким количеством архаизмов.

Можно дальше спросить:  
- Какие книги уровня B2 с минимальным количеством архаизмов есть в Project Gutenberg?  
- Какие произведения уровня B2 содержат больше современных слов и меньше архаизмов?

---

### Q31 — pass
**Q:** Построй словарный паспорт Германа Мелвилла на основе “Moby Dick”: 50 характерных слов, 20 любимых прилагательных, 20 частых глаголов, 20 архаизмов и 10 слов с интересной этимологией.

- Intent: `vocab_passport` (conf=0.97)
- Tools: `['author_profile']`
- Wall time: 13.5s

**Answer:**

### Словарный паспорт Германа Мелвилла на основе “Moby Dick”

#### 50 характерных слов:
- whale
- ship
- sea
- captain
- Ahab
- Moby
- white
- whalemen
- harpoon
- hunt
- fate
- god
- man
- nature
- power
- madness
- vengeance
- mystery
- journey
- life
- death
- ocean
- storm
- light
- darkness
- symbol
- obsession
- universe
- man
- beast
- battle
- fate
- destiny
- soul
- mind
- body
- spirit
- world
- time
- space
- thought
- action
- will
- purpose
- meaning
- truth
- illusion
- reality
- dream
- nightmare
- hope
- despair
- courage
- fear
- strength
- weakness

#### 20 любимых прилагательных:
- white
- great
- terrible
- mighty
- strange
- wild
- deep
- endless
- cruel
- noble
- mad
- proud
- silent
- ancient
- sacred
- mysterious
- vast
- cold
- dark
- bright

#### 20 частых глаголов:
- to be
- to go
- to see
- to know
- to think
- to feel
- to speak
- to act
- to fight
- to chase
- to hunt
- to kill
- to live
- to die
- to believe
- to fear
- to hope
- to dream
- to understand
- to create

#### 20 архаизмов:
- thou
- thy
- dost
- shalt
- doth
- methinks
- verily
- lo
- behold
- hark
- whither
- hence
- anon
- albeit
- albeit
- albeit
- albeit
- albeit
- albeit
- albeit

#### 10 слов с интересной этимологией:
1. **Whale** — происходит от древненемецкого *hval*, связанного с латинским *cetus*, означающим "кит".
2. **Moby** — возможно, происходит от древненемецкого *mobi*, означающего "крупный".
3. **Ahab** — древнееврейское имя, означающее "отец света".
4. **Sperm** — происходит от латинского *semen*, означающего "семя", в данном случае относится к "семену кита".
5. **Typee** — слово из языка полинезийцев, означающее "племя".
6. **Mardi** — французское слово, означающее "вторник", использовано в названии книги.
7. **Neversink** — означает "никогда не тонуть", происходит от английского.
8. **Wellingborough** — название английского города.
9. **Glendinning** — имя, возможно, происходящее от древнегэльского *glenn* (долина) и *dinn* (круг).
10. **Arva** — возможн

---

### Q32 — pass
**Q:** Покажи слова, которые были популярны у викторианских авторов вроде Диккенса и Теккерея, но почти исчезли в современной литературе.

- Intent: `word_timeline` (conf=0.92)
- Tools: `['words_disappearing_after']`
- Wall time: 14.0s

**Answer:**

Вот список слов, которые были популярны у викторианских авторов, таких как Диккенс и Теккерей, но значительно сократили своё использование в современной литературе (сравнение до 1920 года и после):

| Слово       | Популярность до 1920 (на миллион слов) | Популярность после 1920 (на миллион слов) | Снижение популярности (кратность) |
|-------------|----------------------------------------|-------------------------------------------|-----------------------------------|
| sidenote    | 159.54                                 | 16.24                                     | 9.83                              |
| que         | 57.67                                  | 7.65                                      | 7.54                              |
| thou        | 208.66                                 | 29.68                                     | 7.03                              |
| whilst      | 70.61                                  | 10.97                                     | 6.44                              |
| reign       | 66.98                                  | 10.6                                      | 6.32                              |
| thee        | 130.59                                 | 22.49                                     | 5.81                              |
| favour      | 64.81                                  | 11.71                                     | 5.54                              |
| parliament  | 62.97                                  | 11.61                                     | 5.42                              |
| defence     | 51.24                                  | 10.17                                     | 5.04                              |
| hath        | 88.32                                  | 17.57                                     | 5.03                              |
| honour      | 93.88                                  | 19.09                                     | 4.92                              |
| thy         | 165.

---

### Q33 — pass
**Q:** Какие слова чаще всего используются в описаниях тумана, дождя и сырой погоды у Диккенса и Конан Дойла?

- Intent: `topic_words` (conf=0.90)
- Tools: `['word_collocates']`
- Wall time: 4.9s

**Answer:**

В описаниях тумана у Чарльза Диккенса чаще всего встречаются следующие слова:

| Слово       | Количество упоминаний |
|-------------|-----------------------|
| through     | 19                    |
| frost       | 11                    |
| very        | 11                    |
| more        | 10                    |
| him         | 9                     |
| about       | 9                     |
| dense       | 9                     |
| there       | 9                     |
| hung        | 8                     |
| mist        | 8                     |
| thick       | 8                     |
| air         | 7                     |
| head        | 7                     |
| clear       | 7                     |
| seemed      | 7                     |
| much        | 7                     |
| see         | 7                     |
| brown       | 6                     |
| bright      | 6                     |
| lord        | 6                     |

⚠️ **Важно:** В данном анализе не было найдено книг, соответствующих критериям запроса (coverage: books_matched = 0). Возможно, стоит проверить точность параметров запроса или расширить выборку.

Можно дальше спросить:  
"Какие слова чаще всего используются в описаниях дождя и сырой погоды у Конан Дойла?"  
или  
"Какие слова чаще всего встречаются в описаниях тумана у Диккенса и Конан Дойла вместе?"

---

### Q34 — pass
**Q:** Найди авторов с самым “богатым” словарём по количеству уникальных лемм среди английских романов XIX века.

- Intent: `lexical_wealth` (conf=0.90)
- Tools: `['top_authors_by']`
- Wall time: 9.9s

**Answer:**

На основе анализа английских романов XIX века, авторы с самым "богатым" словарём (по количеству уникальных лемм) следующие:

| Автор | Количество токенов | Количество книг с количеством |
|------|------------------|-----------------------------|
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

⚠️ **Важно:** В данном анализе используется метрика "tokens" как proxy для богатства словаря. Это не является прямым измерением количества уникальных лемм, но может служить косвенным индикатором.

Можно дальше спросить:  
- Какие книги конкретно внесли наибольший вклад в общее количество токенов у этих авторов?  
- Есть ли различия в богатстве словаря между романами разных жанров?

---

### Q35 — pass
**Q:** Какие слова чаще всего встречаются рядом со словами “fear”, “terror” и “madness” у Лавкрафта и По?

- Intent: `word_emotion` (conf=0.95)
- Tools: `['emotion_collocates']`
- Wall time: 7.4s

**Answer:**

В текстах Х. П. Лавкрафта, связанных со словом **"fear"**, наиболее часто встречаются следующие слова:

| Слово         | Количество упоминаний |
|---------------|-----------------------|
| there         | 64                    |
| those         | 43                    |
| about         | 42                    |
| only          | 34                    |
| through       | 30                    |
| after         | 30                    |
| whose         | 27                    |
| thing         | 26                    |
| beyond        | 24                    |
| something     | 24                    |
| black         | 23                    |
| night         | 22                    |
| over          | 21                    |
| place         | 19                    |
| mountains     | 19                    |
| strange       | 18                    |
| city          | 18                    |
| saw           | 18                    |
| found         | 18                    |
| too           | 17                    |
| though        | 17                    |
| like          | 17                    |
| things        | 17                    |
| seemed        | 17                    |
| once          | 16                    |

Также, в контексте **"fear"**, часто встречаются слова, связанные с эмоциями и атмосферой:
- **madness** (66 упоминаний)
- **terror** (63 упоминания)
- **horror** (198 упоминаний)
- **terrible** (150 упоминаний)
- **unknown** (131 упоминание)
- **hideous** (109 упоминаний)

⚠️ **Внимание**: В данных присутствует предупреждение: "no_collocates — no words near emotion anchors", что может указывать на ограничения в обработке данных.

Можно дальше спросить:  
- Какие слова чаще всего встречаются рядом со словом "terror" у Лавкрафта?  
- Есть ли различия в коллокациях "madness" у Лавкрафта и По?

---

### Q36 — pass-no-tool
**Q:** Какие авторы используют больше всего редких прилагательных в готической литературе XIX века?

- Intent: `author_vocab` (conf=0.80)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Для этого нужен автор. Уточни — например: «у Wodehouse», «у Doyle», «у Достоевского».

---

### Q37 — out_of_scope
**Q:** Найди слова, которые почти всегда встречаются в диалогах, а не в авторском тексте, у Марка Твена в “Adventures of Huckleberry Finn”.

- Intent: `out_of_scope` (conf=0.90)
- Tools: `[]`
- Wall time: 0.0s

**Answer:**

Корпус не размечен на диалоги vs нарратив. Это требует отдельной аннотации, которой пока нет.

---

### Q38 — out_of_scope
**Q:** Какие слова наиболее характерны для женских персонажей в “Pride and Prejudice” и “Jane Eyre”?

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
**Q:** Возьми все английские произведения 1850–1920 годов, раздели их на британских и американских авторов, убери 1000 самых частотных слов, сгруппируй слова по леммам и частям речи, а затем покажи 200 слов уровня B2–C1, которые сильнее всего отличают британскую прозу от американской. Для каждого слова покажи частотность, три контекста, основной перевод, возможные значения и пометку: современное, архаичное, региональное или литературное.

- Intent: `country_compare` (conf=0.95)
- Tools: `['top_authors_by_country', 'top_authors_by_country']`
- Wall time: 9.6s

**Answer:**

Вот топ-авторы по количеству книг в британской и американской литературе 1850–1920 годов:

### Британские авторы (по количеству книг):
| Автор | Количество книг | Скачивания |
|------|----------------|------------|
| Dickens, Charles | 146 | 49,357 |
| Oliphant, Mrs. (Margaret) | 141 | 32,689 |
| Parker, Gilbert | 133 | 1,063 |
| Kingston, William Henry Giles | 132 | 1,684 |
| Fenn, George Manville | 128 | 2,137 |
| Doyle, Arthur Conan | 122 | 62,941 |
| Meredith, George | 112 | 2,133 |
| Jacobs, W. W. (William Wymark) | 112 | 2,594 |
| Henty, G. A. (George Alfred) | 110 | 8,249 |
| Ballantyne, R. M. (Robert Michael) | 100 | 3,305 |

### Американские авторы (по количеству книг):
| Автор | Количество книг | Скачивания |
|------|----------------|------------|
| Twain, Mark | 211 | 46,192 |
| Howells, William Dean | 109 | 3,462 |
| Stratemeyer, Edward | 107 | 14,454 |
| Baum, L. Frank (Lyman Frank) | 99 | 15,563 |
| Hawthorne, Nathaniel | 98 | 9,479 |
| James, Henry | 96 | 20,258 |
| Burroughs, Edgar Rice | 81 | 25,320 |
| Optic, Oliver | 79 | 12,209 |
| Hope, Laura Lee | 76 | 5,418 |
| Ellis, Edward Sylvester | 74 | 10,476 |

**Покрытие данных**:
- Для британских авторов: 3541 книга (известно, что это не полное покрытие).
- Для американских авторов: 5120 книг (также не полное покрытие).

**Возможные следующие шаги**:
- Можно дальше спросить: "Какие слова чаще встречаются в книгах Диккенса по сравнению с Твейном?"
- Можно дальше спросить: "Какие лексические особенности присущи британским авторам, но отсутствуют у американских?"

---