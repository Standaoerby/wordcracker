#!/usr/bin/env python3
"""Sprint 19 — library audit CLI for /admin/library health surface.

Runs the same heuristics as the live /api/audit endpoint but produces
a Markdown report suitable for piping into a notebook / Obsidian.

Usage (inside gutenberg-lab container):
    python /workspace/scripts/v2/audit_user_uploads.py
    python /workspace/scripts/v2/audit_user_uploads.py --json
    python /workspace/scripts/v2/audit_user_uploads.py --broken-only

Health categories:
    ok           — all four files present, raw >5 KB
    no_raw       — listed in metadata but raw_text/u<N>.txt missing
    no_tokens    — raw exists but tokens dump missing
    no_counts    — tokens but no counts (rare)
    truncated    — raw exists but <5 KB (almost certainly bad upload)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# admin_library lives at scripts/admin_library.py; sys.path so it imports.
_REPO_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_REPO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REPO_SCRIPTS))


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of Markdown")
    ap.add_argument("--broken-only", action="store_true",
                    help="skip ok rows; show only books needing attention")
    args = ap.parse_args()

    try:
        from admin_library import audit_library, list_user_books
    except ImportError as e:
        print(f"ERROR: cannot import admin_library — {e}", file=sys.stderr)
        print("Run inside gutenberg-lab container or set "
              "PYTHONPATH=/workspace/scripts.", file=sys.stderr)
        return 2

    summary = audit_library()
    all_books = list_user_books()

    if args.json:
        out = {
            "summary": summary,
            "books":   all_books if not args.broken_only
                       else [b for b in all_books if b["health"] != "ok"],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    # Markdown report
    lines = [
        "# wordcracker · user-uploads audit",
        "",
        f"- **Total books**: {summary['total']}",
        f"- **Total raw bytes**: {_fmt_bytes(summary['total_bytes'])}",
        "- **By health**:",
    ]
    for health, count in sorted(summary["by_health"].items(),
                                 key=lambda x: -x[1]):
        marker = "🟢" if health == "ok" else "🟠" if health.startswith("no_") else "🔴"
        lines.append(f"  - {marker} `{health}`: **{count}**")
    lines.append("")

    target_rows = (
        [b for b in all_books if b["health"] != "ok"]
        if args.broken_only else all_books
    )
    if not target_rows:
        lines.append("_All clean._")
    else:
        lines.append(f"## Books ({len(target_rows)})")
        lines.append("")
        lines.append("| id | health | size | title | author | uploaded |")
        lines.append("|---|---|---|---|---|---|")
        for b in target_rows:
            title = (b["title"] or "—")[:50]
            author = (b["author"] or "—")[:30]
            ts = b["uploaded_ts"] or "—"
            lines.append(
                f"| `{b['id']}` | `{b['health']}` | "
                f"{_fmt_bytes(b['raw_bytes'])} | {title} | "
                f"{author} | {ts} |"
            )

    print("\n".join(lines))
    return 0 if summary["by_health"].get("ok", 0) == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
