# wordcracker v2.5.1 — onboarding overlay + recovery UX + faster footer

Three small polishes that round out the v2.5 demo pass.

## Onboarding overlay

First-visit splash with a quick «вот что я умею» tour:

> **Привет, я Словоёб**
>
> Литературный аналитик корпуса Project Gutenberg — ~55 000 английских книг.
>
> Что умею:
> ✓ Стилометрия: фирменные слова автора, сравнение, кто на кого похож
> ✓ Книги: уровень сложности, архаизмы, эмоциональный профиль
> ✓ Слова: контексты, collocates, этимология (Wiktionary), эпохи
> ✓ Лексика для изучения: B1/B2/C1, экспорт в Anki
> ✓ Топ-листы: по странам, скачиваниям, токенам
>
> Спрашивай по-русски или по-английски. Можно начать с подсказок ниже.
>
> [поехали]

Dismissal sets a localStorage flag (`wordcracker_onboarded_v1`); returning users skip it. Users with chat history skip it too — they already know.

## Recovery UX

Error states (both `event:error` from the SSE stream and client-side stream-errors) now render with a **«↻ повторить»** button that resubmits the original query. Without it, the user saw a red banner and had to retype.

## Footer live update

Stan's demon round noticed counters frozen during a 12-question session. v2.5.1:

- Poll interval **30 s → 10 s**
- Explicit `refreshStats()` call after each user submit completes — count bumps **immediately**, not on next tick
- `cache: 'no-store'` on the fetch (belt-and-braces; server already sends the header)

## Tests

207/207 unit, all behavioural code unchanged — pure UI/HTML/JS polish.

Co-developed with Claude Opus 4.7 (1M context).
