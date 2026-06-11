"""B115 (R-27 WP3, 2026-06-11) — render-instruction leak guard.

Q7 live repro (prod 2.7.3): the visible answer contained «requested
top=30 … renderer must say 27» — the `under_filled` ToolWarning message
and the `_render_note` «ACTUAL COUNT…» instruction, echoed verbatim by
the renderer LLM. Both travel to the model inside the render payload
(`render_instructions`, `warnings`, `_render_note`) BY DESIGN — they are
instructions for the model, never content for the user. The model
occasionally parrots them; prompt rules alone cannot guarantee it won't.

This module closes the channel deterministically on every path:

  * `strip_service_lines(text)` — post-render scrub of the final answer
    (both `ask()` and `ask_stream()` call it right after the renderer).
  * `StreamLineScrubber` — line-buffered filter for the `stream_render`
    delta path, so a leaked service line never reaches the client even
    transiently (docs/webapp.md: the `done` envelope replaces streamed
    text, but the user has already SEEN the stream — scrub in flight).

Markers are service-only vocabulary that legit answers never contain;
each one is tied to a known instruction emitter (see _SERVICE_MARKERS).
Whole-line removal is intentional: a service instruction spliced into a
prose line makes the whole line untrustworthy.
"""
from __future__ import annotations

import re
from typing import Callable

# Service-instruction vocabulary → emitter:
#   «renderer must say»      — under_filled / top_capped ToolWarning
#   «ACTUAL COUNT»           — affinity/learning_words _render_note
#   «render_note / render note / _render_columns / render_instructions»
#                            — payload plumbing keys
#   «requested top=»         — under_filled warning body
#   «top_requested/_returned»— count-honesty fields echoed as text
#   «COLUMNS: this tool emits» — learning_words column contract note
#   «ЗАПРЕЩЕНО изобретать»   — compare_authors empty-side note
_SERVICE_MARKERS = (
    "renderer must say",
    "actual count",
    "_render_note",
    "render_note",
    "render note",
    "_render_columns",
    "render_instructions",
    "requested top=",
    "top_requested",
    "top_returned",
    "columns: this tool emits",
    "запрещено изобретать",
)

# Tool-tagged instruction lines: «[affinity_by_author] …» / «[plan] …» —
# the `f"[{r.tool}] {note}"` shape _collect_render_instructions builds.
# A markdown link `[text](url)` is NOT matched (the `(?!\()` guard).
_TAGGED_NOTE_LINE_RE = re.compile(r"^\s*\[[a-z_][a-z0-9_]{2,}\]\s(?!\()")


def _is_service_line(line: str) -> bool:
    low = line.lower()
    if any(m in low for m in _SERVICE_MARKERS):
        return True
    return bool(_TAGGED_NOTE_LINE_RE.match(line))


def strip_service_lines(text: str) -> str:
    """Remove lines carrying render-service instructions from `text`.

    Collapses the blank-line runs left behind. Idempotent; returns the
    input unchanged when nothing matches (the overwhelmingly common
    case — one lowercase pass over the text)."""
    if not text:
        return text
    low = text.lower()
    if (not any(m in low for m in _SERVICE_MARKERS)
            and not _TAGGED_NOTE_LINE_RE.search(text)):
        return text
    kept = [ln for ln in text.split("\n") if not _is_service_line(ln)]
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip("\n") if out.strip() else text


class StreamLineScrubber:
    """Line-buffered scrub for streamed render deltas (B115, stream path).

    Wraps an `emit(piece: str)` consumer. Deltas are buffered until a
    newline completes the line; service lines are dropped, clean lines
    forwarded. `flush()` releases the unterminated tail at end of stream
    (scrubbed the same way). Worst-case latency cost: one line.
    """

    def __init__(self, emit: Callable[[str], None]):
        self._emit = emit
        self._buf = ""

    def feed(self, piece: str) -> None:
        if not piece:
            return
        self._buf += piece
        while True:
            idx = self._buf.find("\n")
            if idx == -1:
                return
            line, self._buf = self._buf[:idx], self._buf[idx + 1:]
            if not _is_service_line(line):
                self._emit(line + "\n")

    def flush(self) -> None:
        tail, self._buf = self._buf, ""
        if tail and not _is_service_line(tail):
            self._emit(tail)


__all__ = ["strip_service_lines", "StreamLineScrubber"]
