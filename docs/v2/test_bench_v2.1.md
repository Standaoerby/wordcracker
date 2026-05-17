# v2 performance benchmark — 2026-05-18T00:04:33

- runs: 1
- questions per run: 40
- total calls: 40
- cache hit rate: 0% (0/36)
- critic: 23 clean / 9 flagged

## Per-intent wall-clock latency

| Intent | n | p50 (s) | p95 (s) | max (s) | mean (s) |
|---|---:|---:|---:|---:|---:|
| author_closest | 1 | 7.88 | 7.88 | 7.88 | 7.88 |
| author_vocab | 4 | 15.53 | 15.53 | 16.78 | 11.63 |
| book_archaic | 3 | 8.15 | 8.15 | 8.83 | 7.55 |
| book_compare | 2 | 15.44 | 8.96 | 15.44 | 12.20 |
| book_vocab | 2 | 16.12 | 15.39 | 16.12 | 15.76 |
| country_compare | 3 | 13.49 | 13.49 | 19.74 | 14.49 |
| country_vocab | 1 | 9.68 | 9.68 | 9.68 | 9.68 |
| introduction | 1 | 0.02 | 0.02 | 0.02 | 0.02 |
| learning | 4 | 18.98 | 18.98 | 19.05 | 13.94 |
| lexical_wealth | 1 | 8.45 | 8.45 | 8.45 | 8.45 |
| out_of_scope | 6 | 0.00 | 0.00 | 0.00 | 0.00 |
| topic_words | 1 | 6.82 | 6.82 | 6.82 | 6.82 |
| vocab_passport | 1 | 15.35 | 15.35 | 15.35 | 15.35 |
| word_collocates | 1 | 8.34 | 8.34 | 8.34 | 8.34 |
| word_contexts | 1 | 6.80 | 6.80 | 6.80 | 6.80 |
| word_emotion | 3 | 6.29 | 6.29 | 10.59 | 7.66 |
| word_etymology | 1 | 14.31 | 14.31 | 14.31 | 14.31 |
| word_pos | 1 | 3.69 | 3.69 | 3.69 | 3.69 |
| word_timeline | 3 | 15.75 | 15.75 | 15.90 | 15.70 |

## Per-tool internal runtime

| Tool | n | p50 (ms) | p95 (ms) | mean (ms) |
|---|---:|---:|---:|---:|
| affinity_by_author | 6 | 458 | 769 | 856 |
| affinity_by_book | 4 | 10662 | 10662 | 7797 |
| author_influences | 1 | 28 | 28 | 28 |
| author_profile | 1 | 4020 | 4020 | 4020 |
| book_archaic_words | 3 | 6 | 6 | 6 |
| emotion_collocates | 3 | 100 | 100 | 99 |
| find_words_by_etymology | 1 | 39340 | 39340 | 39340 |
| learning_words | 3 | 6457 | 6457 | 6047 |
| top_authors_by | 1 | 59742 | 59742 | 59742 |
| top_authors_by_country | 6 | 68 | 68 | 46 |
| word_collocates | 2 | 37033 | 878 | 18955 |
| word_contexts | 1 | 210 | 210 | 210 |
| word_pos_distribution | 1 | 510 | 510 | 510 |
| words_disappearing_after | 3 | 25857 | 25857 | 25181 |