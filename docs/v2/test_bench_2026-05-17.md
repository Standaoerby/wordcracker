# v2 performance benchmark — 2026-05-17T22:08:37

- runs: 1
- questions per run: 40
- total calls: 40
- cache hit rate: 0% (0/36)
- critic: 4 clean / 28 flagged

## Per-intent wall-clock latency

| Intent | n | p50 (s) | p95 (s) | max (s) | mean (s) |
|---|---:|---:|---:|---:|---:|
| author_closest | 1 | 8.64 | 8.64 | 8.64 | 8.64 |
| author_compare | 1 | 12.24 | 12.24 | 12.24 | 12.24 |
| author_vocab | 4 | 17.84 | 17.84 | 26.14 | 14.66 |
| book_archaic | 3 | 9.15 | 9.15 | 9.77 | 9.03 |
| book_compare | 1 | 17.50 | 17.50 | 17.50 | 17.50 |
| book_vocab | 2 | 17.66 | 17.29 | 17.66 | 17.47 |
| country_compare | 3 | 17.92 | 17.92 | 21.93 | 16.14 |
| country_vocab | 1 | 17.80 | 17.80 | 17.80 | 17.80 |
| introduction | 1 | 0.02 | 0.02 | 0.02 | 0.02 |
| learning | 4 | 18.74 | 18.74 | 22.93 | 14.35 |
| lexical_wealth | 1 | 9.96 | 9.96 | 9.96 | 9.96 |
| out_of_scope | 6 | 0.00 | 0.00 | 0.00 | 0.00 |
| topic_words | 1 | 9.05 | 9.05 | 9.05 | 9.05 |
| vocab_passport | 1 | 23.35 | 23.35 | 23.35 | 23.35 |
| word_collocates | 1 | 7.85 | 7.85 | 7.85 | 7.85 |
| word_contexts | 1 | 6.12 | 6.12 | 6.12 | 6.12 |
| word_emotion | 3 | 9.10 | 9.10 | 11.46 | 9.08 |
| word_etymology | 1 | 14.77 | 14.77 | 14.77 | 14.77 |
| word_pos | 1 | 3.18 | 3.18 | 3.18 | 3.18 |
| word_timeline | 3 | 17.24 | 17.24 | 26.96 | 20.29 |

## Per-tool internal runtime

| Tool | n | p50 (ms) | p95 (ms) | mean (ms) |
|---|---:|---:|---:|---:|
| affinity_by_author | 4 | 458 | 458 | 480 |
| affinity_by_book | 3 | 9034 | 9034 | 6719 |
| author_influences | 1 | 28 | 28 | 28 |
| author_metadata | 2 | 191 | 183 | 187 |
| author_profile | 1 | 4020 | 4020 | 4020 |
| book_archaic_words | 3 | 6 | 6 | 6 |
| compare_authors | 1 | 1404 | 1404 | 1404 |
| emotion_collocates | 3 | 100 | 100 | 99 |
| find_words_by_etymology | 1 | 39340 | 39340 | 39340 |
| learning_words | 3 | 6457 | 6457 | 6047 |
| top_authors_by | 1 | 59742 | 59742 | 59742 |
| top_authors_by_country | 6 | 68 | 68 | 46 |
| word_collocates | 2 | 37033 | 878 | 18955 |
| word_contexts | 1 | 210 | 210 | 210 |
| word_pos_distribution | 1 | 510 | 510 | 510 |
| words_disappearing_after | 3 | 25857 | 25857 | 25181 |