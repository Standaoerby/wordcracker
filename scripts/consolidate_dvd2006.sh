#!/bin/bash
# Consolidate PG DVD July 2006 -> /data/raw_text/
# Skip if already present. Prefer UTF-8 .zip, fallback to Latin-1 (-8.zip).
# Idempotent: re-run anytime.

set -u
SRC="${1:-/data/pg_dvd_2006}"
DST="${2:-/data/raw_text}"

mkdir -p "$DST"

unique_ids=$(find "$SRC" -name '*.zip' -printf '%f\n' \
  | sed -nE 's/^([0-9]+).*\.zip$/\1/p' \
  | sort -u -n)

total=$(echo "$unique_ids" | wc -l)
echo "Unique PG IDs in DVD: $total"
echo "Current raw_text:     $(ls "$DST" 2>/dev/null | wc -l)"

added=0; skipped=0; failed=0; count=0

while IFS= read -r pgid; do
    count=$((count+1))
    if (( count % 500 == 0 )); then
        echo "  progress $count/$total  (added=$added skipped=$skipped failed=$failed)"
    fi

    target="$DST/pg${pgid}.txt"
    if [ -e "$target" ]; then
        skipped=$((skipped+1))
        continue
    fi

    zip_path=$(find "$SRC" -path "*/${pgid}/${pgid}.zip" 2>/dev/null | head -1)
    if [ -z "$zip_path" ]; then
        zip_path=$(find "$SRC" -path "*/${pgid}/${pgid}-8.zip" 2>/dev/null | head -1)
    fi

    if [ -z "$zip_path" ]; then
        failed=$((failed+1))
        continue
    fi

    tmpdir=$(mktemp -d)
    if unzip -qq -o "$zip_path" -d "$tmpdir" 2>/dev/null; then
        txt=$(find "$tmpdir" -name '*.txt' \
                ! -name '*-index.txt' \
                ! -name '*-readme.txt' \
                ! -name '*-body.txt' \
                ! -name '*-pal.txt' \
                -size +1k | head -1)

        if [ -n "$txt" ]; then
            if file -b "$txt" | grep -qE "ISO-8859|Non-ISO extended-ASCII"; then
                iconv -f ISO-8859-1 -t UTF-8//IGNORE "$txt" > "$target" 2>/dev/null || cp "$txt" "$target"
            else
                cp "$txt" "$target"
            fi
            added=$((added+1))
        else
            failed=$((failed+1))
        fi
    else
        failed=$((failed+1))
    fi
    rm -rf "$tmpdir"
done <<< "$unique_ids"

final=$(ls "$DST" 2>/dev/null | wc -l)
size=$(du -sh "$DST" 2>/dev/null | cut -f1)
echo
echo "=== DVD 2006 consolidation ==="
echo "Added:   $added"
echo "Skipped: $skipped (already in raw_text)"
echo "Failed:  $failed"
echo "Total in $DST: $final ($size)"
