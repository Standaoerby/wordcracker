# wordcracker v3.1 — Sprint 19: admin library + user-upload disclosure

Minor release on top of v3.0.2. Stan's session-end request: «3 часа на
админку — загрузка / чистка / fix mangled markup / статистика /
удаление / скачать сырой текст / полная имплементация в инструменты.
Чат должен знать когда отвечает не из канонического корпуса». Done.

**Suite:** 529 unit tests, 0 failures (was 513 at v3.0.2).

## What's new for the admin

### Library inventory + inline actions (`admin.slovoeb.net/library/page`)

New dark-theme dashboard listing every uploaded book with:
- **Health classification** per book: `ok` / `no_tokens` / `no_counts`
  / `no_raw` / `truncated` (<5KB raw). One glance, anything red.
- **Inline actions** per row:
  - `stats` — overlay shows tokens / vocab / TTR / top-20 words /
    3 sample paragraphs / Gutenberg-header detection
  - `raw` — download text/plain with proper Content-Disposition
  - `reprocess` — re-strip Gutenberg header/footer + re-tokenize
    (run `tokenize_user_books.py --id U<N>`). Idempotent; useful
    when initial upload kept the boilerplate or had mangled markup.
  - `delete` — irreversible, confirm dialog. Removes raw / tokens /
    counts / metadata row. Chroma chunks reclaimed on next reindex.
- **↻ reindex Chroma** button in the header — kicks off
  `build_index_raw.py` in background after delete / reprocess
  sessions, without bouncing through the upload tab.

### API endpoints (no UI required)

```
GET  /api/library                      — full inventory JSON
GET  /api/audit                        — bulk health summary
GET  /book/U<N>/stats                  — per-book JSON stats
GET  /book/U<N>/raw                    — text/plain download
POST /book/U<N>/delete                 — remove raw/tokens/counts/meta
POST /book/U<N>/reprocess              — re-strip + re-tokenize
POST /reindex                          — trigger Chroma reindex
```

### CLI audit (`scripts/v2/audit_user_uploads.py`)

```
python /workspace/scripts/v2/audit_user_uploads.py               # markdown
python /workspace/scripts/v2/audit_user_uploads.py --json
python /workspace/scripts/v2/audit_user_uploads.py --broken-only
```

Same data the live `/api/audit` returns, formatted for piping into
Obsidian / notebooks / cron health checks.

## What's new in chat

### Source disclosure (RENDER_PROMPT rule 12)

The deterministic pipeline now detects U-prefixed book ids anywhere
in tool results (`_detect_user_uploads(results)` — recursive walk
matching `\bU\d+\b`). When found:

1. `summary_payload` to renderer gets `user_uploads_used: true` +
   count + sample of 5 ids
2. New RENDER_PROMPT **rule 12** instructs the LLM:
   > *Если в payload есть `user_uploads_used: true` — добавь
   > короткое примечание в конце ответа: «В ответе использованы
   > загруженные вами книги (U<N>) — это не часть канонического
   > корпуса SPGC». Tool behaviour не изменяется — это исключительно
   > прозрачность для пользователя.*
3. `obs_mod.log_request` emits `user_uploads_used` /
   `user_upload_count` for the admin dashboard.

Tool behaviour unchanged — all 36 v2 tools already handled U-prefix
ids transparently via `_counts_path` / `_tokens_path` / `raw_text/u<N>
.txt` dispatch. This commit just makes the **source visible** to the
user when relevant.

## How user uploads flow through the stack

```
1. Upload via /admin/page (.epub / .txt / .zip / .tar.gz)
2. admin_server._link_into_raw assigns U<N> id (next_user_id), writes:
   - /workspace/raw_text/u<N>.txt        — header-stripped text
   - /workspace/spgc/derived/user_uploads_metadata.csv  — DC row
3. Optional reindex tokenizes:
   - /workspace/spgc/user_tokens/U<N>_tokens.txt
   - /workspace/spgc/user_counts/U<N>_counts.txt
4. _metadata_df merges user CSV with SPGC baseline →
   find_book / corpus_stats_by_author / lexical_diversity etc.
   all see the user's book by title or U-id transparently.
5. When chat answer cites any U-prefix id →
   rag_v2._detect_user_uploads flags it → renderer adds disclosure.
6. Admin library page surfaces health / stats / actions for QC.
```

## Tests (tests/v2/test_sprint19_admin.py, 16 cases)

- `UidNormalization × 2` — case-insensitive «U7»/«u7»/«U042»
  canonicalization
- `LibraryWithTempDir × 9` — tempdir fixture for the full lifecycle:
  list / health classification / audit summary / per-book stats /
  raw-path resolution / delete
- `DetectUserUploadsInResults × 5` — no-uploads / single / multi /
  dedup / mixed-PG-U / skips-failed-results

Combined Sprint 17+18+19: **529 unit tests / 0 failures** (was 422
at v3.0).

## Deploy

```bash
# On SOW:
sudo -u claude git -C /home/claude/wordcracker pull
sudo systemctl restart wordcracker-admin   # for library endpoints
sudo systemctl restart wordcracker-chat    # for source disclosure
```

Browser: `Ctrl+Shift+R` on admin.slovoeb.net to pick up new library
page (otherwise cached old admin HTML may not show the «→ library»
nav link).

## What's NOT done

- Bulk delete (deletes go one-by-one — fine for a single-admin setup)
- Automatic re-strip on upload — admins can run reprocess after the
  fact instead; not all uploads need it (clean EPUBs are fine)
- Chroma chunk-level delete (still requires full reindex) — adding
  a per-pg-id Chroma `.delete(where={"pg_id": "U7"})` call would
  invalidate the reindex incremental optimization, deferred
- User-facing «show me my uploaded books» chat intent — admin task,
  not a user-facing query
- Migration tool to renumber user uploads (U1, U2, U3 ... if you
  delete U2, slot U2 stays empty) — single-admin scenario doesn't
  need it; multi-user setup would

Co-developed with Claude Opus 4.7 (1M context).
