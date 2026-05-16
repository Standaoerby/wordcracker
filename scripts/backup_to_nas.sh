#!/bin/bash
# wordcracker · backup critical state to NAS.
#
# Reads credentials from ~/.wc_backup_creds (must be chmod 600):
#   NAS_USER=...
#   NAS_PASS=...
#   NAS_HOST=Storage       # or an IP if NetBIOS name doesn't resolve
#   NAS_SHARE=safe/WC      # path on the share
#
# What gets backed up (small, NON-regenerable bits only — ~50 MB):
#   - source code (scripts/, *.md, Dockerfile, docker-compose*)
#   - /data/spgc/derived/  (per-author affinity CSVs, word_dictionary.json,
#                           admin_jobs.json, user_uploads_metadata.csv)
#   - /data/spgc/SPGC-metadata-*.csv (the bibliographic CSV; tokens/counts dumps
#                                     are NOT backed up — re-downloadable from Zenodo)
#   - /data/raw_text/u*.txt (user-uploaded books — there is no other source)
#
# What is INTENTIONALLY skipped (regenerable, big):
#   - /data/chroma_db/  (87 GB, rebuild from raw_text via build_index_raw.py)
#   - /data/spgc/SPGC-{counts,tokens}-*/ (24 GB, Zenodo download)
#   - /data/raw_text/pg*.txt (21 GB, ibiblio rsync)
#
# Usage:
#   ./backup_to_nas.sh           # one-shot
#   crontab -e   →   0 4 * * *  /home/claude/wordcracker/scripts/backup_to_nas.sh >> /var/log/wc_backup.log 2>&1
set -euo pipefail

CREDS_FILE="${HOME}/.wc_backup_creds"
WORK_DIR="${HOME}/wordcracker"
KEEP_LAST=14   # retention: keep N most recent backups on NAS

# ---- creds ----
if [ ! -r "$CREDS_FILE" ]; then
    echo "[backup] FATAL: $CREDS_FILE missing or unreadable. Create it with:" >&2
    echo "  printf 'NAS_USER=pinhead\\nNAS_PASS=...\\nNAS_HOST=Storage\\nNAS_SHARE=safe/WC\\n' > $CREDS_FILE" >&2
    echo "  chmod 600 $CREDS_FILE" >&2
    exit 1
fi
PERM=$(stat -c %a "$CREDS_FILE")
if [ "$PERM" != "600" ] && [ "$PERM" != "400" ]; then
    echo "[backup] WARN: $CREDS_FILE has perms $PERM (should be 600/400)" >&2
fi
# shellcheck disable=SC1090
. "$CREDS_FILE"
: "${NAS_USER:?NAS_USER missing in $CREDS_FILE}"
: "${NAS_PASS:?NAS_PASS missing in $CREDS_FILE}"
: "${NAS_HOST:=Storage}"
: "${NAS_SHARE:=safe/WC}"

NAS_PATH="//${NAS_HOST}/${NAS_SHARE}"
TS=$(date +%Y%m%d-%H%M%S)
TARBALL="/tmp/wc_backup_${TS}.tar.gz"

# ---- gather ----
echo "[backup] $(date -Iseconds) start → $TARBALL"
cd /
# Use --ignore-failed-read so absent files don't kill the run.
tar --ignore-failed-read -czf "$TARBALL" \
    "home/claude/wordcracker/scripts" \
    "home/claude/wordcracker/Dockerfile" \
    "home/claude/wordcracker/docker-compose.yml" \
    "home/claude/wordcracker/docker-compose.override.yml" \
    $(find data/spgc/derived -maxdepth 2 \( -name '*.csv' -o -name '*.json' \) 2>/dev/null) \
    data/spgc/SPGC-metadata-2018-07-18.csv \
    $(ls data/raw_text/u*.txt 2>/dev/null || true) \
    2>/dev/null || true

if [ ! -s "$TARBALL" ]; then
    echo "[backup] FATAL: tarball empty" >&2
    exit 2
fi
SIZE_KB=$(($(stat -c%s "$TARBALL") / 1024))
echo "[backup] tarball: ${SIZE_KB} KB"

# ---- upload ----
# smbclient PUT places the file at the share root. Use SMB 3.0 (modern NAS).
smbclient "$NAS_PATH" \
    --user="${NAS_USER}%${NAS_PASS}" \
    --max-protocol=SMB3 \
    -c "put ${TARBALL} wc_backup_${TS}.tar.gz"
echo "[backup] uploaded to ${NAS_PATH}/wc_backup_${TS}.tar.gz"

# ---- retention: keep last $KEEP_LAST ----
OLD=$(smbclient "$NAS_PATH" \
        --user="${NAS_USER}%${NAS_PASS}" \
        --max-protocol=SMB3 \
        -c "ls wc_backup_*" 2>/dev/null \
        | awk '/wc_backup_.*\.tar\.gz/{print $1}' \
        | sort -r \
        | tail -n +$((KEEP_LAST + 1)) || true)
if [ -n "$OLD" ]; then
    echo "[backup] pruning $(echo "$OLD" | wc -l) old backup(s):"
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        echo "  rm $f"
        smbclient "$NAS_PATH" \
            --user="${NAS_USER}%${NAS_PASS}" \
            --max-protocol=SMB3 \
            -c "rm $f" 2>&1 | grep -v 'NT_STATUS_OBJECT_NAME_NOT_FOUND' || true
    done <<<"$OLD"
fi

# ---- cleanup local tarball ----
rm -f "$TARBALL"
echo "[backup] done at $(date -Iseconds)"
