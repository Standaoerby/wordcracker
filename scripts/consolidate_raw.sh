#!/bin/bash
# Hard-link every available raw Gutenberg .txt into /data/raw_text/
# Idempotent: re-run after rsync pulls more files; already-present links skipped.
# Source priority: per-author direct downloads win over rsync mirror.

set -eu
TARGET=/data/raw_text
mkdir -p "$TARGET"

w=0
for f in /data/wodehouse_raw/pg*.txt; do
  [ -f "$f" ] || continue
  ln -f "$f" "$TARGET/$(basename "$f")" && w=$((w+1))
done

g=0
g_skip=0
while IFS= read -r f; do
  id=$(basename "$f" | sed 's/-0\.txt$//')
  target="$TARGET/pg${id}.txt"
  if [ ! -e "$target" ]; then
    ln -f "$f" "$target" && g=$((g+1))
  else
    g_skip=$((g_skip+1))
  fi
done < <(find /data/gutenberg_raw -name '*-0.txt' 2>/dev/null)

total=$(ls "$TARGET" | wc -l)
size=$(du -sh "$TARGET" | cut -f1)
echo "per-author: $w   rsync: $g (skipped $g_skip dupes)   total in raw_text: $total ($size)"
