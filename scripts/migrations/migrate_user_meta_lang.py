"""One-shot migration — repair corrupted `language` values in the user
uploads metadata CSV (TZ S-T2 Group C).

Before the admin EPUB fix, `_extract_epub_metadata` wrote the raw DC
<dc:language> through unchanged, so 3-letter ISO-639-2/B tags landed as
"['eng']" / "['rus']" etc. (and absent tags defaulted to "['en']"). The
v1 language filter (`_lang_mask`, Group B) now tolerates these at READ
time, but the stored data is still wrong. This migration normalizes the
column in place: 639-2/B → 639-1, order preserved, duplicates and unknown
codes dropped, empty/garbage → "".

Target file (prod): /workspace/spgc/derived/user_uploads_metadata.csv
(outside the repo — run on the prod host).

Usage:
    python -m scripts.migrations.migrate_user_meta_lang            # dry-run
    python -m scripts.migrations.migrate_user_meta_lang --apply    # rewrite
    python -m scripts.migrations.migrate_user_meta_lang --apply --path <csv>

Exit codes: 0 ok (whether or not changes were needed), 2 file missing.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

# Language normalization is the stdlib-only single source of truth
# (scripts/lang_norm.py). Re-exported here so callers/tests can import it
# from the migration module. try/except covers both import styles.
try:
    from scripts.lang_norm import normalize_lang_field
except ImportError:  # pragma: no cover — bare-name fallback
    from lang_norm import normalize_lang_field


def migrate(csv_path: Path, apply: bool = False) -> dict:
    """Normalize the `language` column. Returns a summary dict. When
    `apply`, rewrites the file in place (after a .bak copy)."""
    if not csv_path.exists():
        return {"status": "missing", "path": str(csv_path)}

    with open(csv_path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    changes = []
    for r in rows:
        old = r.get("language", "")
        new = normalize_lang_field(old)
        if new != (old or ""):
            changes.append({"id": r.get("id", "?"), "from": old, "to": new})
            r["language"] = new

    summary = {
        "status": "ok",
        "path": str(csv_path),
        "rows": len(rows),
        "changed": len(changes),
        "changes": changes,
        "applied": False,
    }

    if apply and changes:
        shutil.copyfile(csv_path, csv_path.with_suffix(csv_path.suffix + ".bak"))
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        summary["applied"] = True

    return summary


def _default_path() -> Path:
    try:
        from scripts.admin_server import USER_META
        return Path(USER_META)
    except Exception:
        return Path("/workspace/spgc/derived/user_uploads_metadata.csv")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="migrate_user_meta_lang")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the CSV (default: dry-run)")
    ap.add_argument("--path", default=None, help="CSV path override")
    args = ap.parse_args(argv)

    csv_path = Path(args.path) if args.path else _default_path()
    summary = migrate(csv_path, apply=args.apply)

    if summary["status"] == "missing":
        print(f"[migrate] FATAL: not found: {summary['path']}", file=sys.stderr)
        return 2

    mode = "APPLIED" if summary["applied"] else ("DRY-RUN" if args.apply else "DRY-RUN")
    print(f"[migrate] {mode}: {summary['changed']}/{summary['rows']} rows need "
          f"language repair in {summary['path']}")
    for ch in summary["changes"]:
        print(f"  {ch['id']}: {ch['from']!r} -> {ch['to']!r}")
    if summary["changed"] and not summary["applied"]:
        print("[migrate] re-run with --apply to write (a .bak is kept).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
