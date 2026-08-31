#!/usr/bin/env bash
# Copy a retrieval library to Google Cloud Storage.
#
# The bytes are the whole point. A record says a page said something; without the bytes it says
# a page said something and offers nothing to check, which is the state this tool exists to end.
# They live in one directory on one machine and are in no git repository, because committing
# third-party page content is exactly what a capture library must not do.
#
# The bucket has object versioning on and this never deletes, so a local file lost or corrupted
# does not propagate. Restoring is `gcloud storage rsync` in the other direction.
#
#     scripts/backup_to_gcs.sh                    # sync ~/.retrieved, then verify counts
#     RETRIEVED_HOME=/path scripts/backup_to_gcs.sh --dry-run

set -euo pipefail

LIBRARY="${RETRIEVED_HOME:-$HOME/.retrieved}"
BUCKET="${RETRIEVED_BUCKET:-gs://citations-library-backup/retrieved}"
SA="$HOME/.config/gcloud/factorization-circuits/sa.json"
LOG="$LIBRARY/backup_log.jsonl"

[ -d "$LIBRARY" ] || { echo "backup: no library at $LIBRARY" >&2; exit 1; }
[ -f "$SA" ] || { echo "backup: no service account key at $SA" >&2; exit 1; }

# The service account, not a user login: a scheduled job runs with no browser and no keychain
# prompt, and a backup that only works interactively is one that stops running.
export CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE="$SA"
# gcloud refuses to run under Python 3.7, and /usr/local/bin/python3 is 3.7 on this machine.
# An interactive shell finds a newer one; a launchd job does not, so the scheduled backup
# failed with "reinstall the Google Cloud CLI" while the manual one worked.
export CLOUDSDK_PYTHON="${CLOUDSDK_PYTHON:-/usr/local/opt/python@3.12/bin/python3}"


DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "  library : $LIBRARY"
echo "  bucket  : $BUCKET"

for dir in store retrievals; do
    [ -d "$LIBRARY/$dir" ] || { echo "  $dir: absent, skipped"; continue; }
    echo "  --- $dir"
    # No --delete-unmatched-destination-objects: mirroring a deletion turns one local mistake
    # into two, and surviving the mistake is what a backup is for.
    gcloud storage rsync --recursive $DRY "$LIBRARY/$dir" "$BUCKET/$dir" 2>&1 | tail -2
done

if [ -z "$DRY" ]; then
    # index.db and skipped.jsonl are small and rebuildable from the records, but a restore that
    # reproduces what was declined as well as what was kept is a better restore.
    for file in index.db skipped.jsonl; do
        [ -f "$LIBRARY/$file" ] && gcloud storage cp "$LIBRARY/$file" "$BUCKET/$file" 2>&1 | tail -1
    done
else
    echo "  dry run, nothing written"
    exit 0
fi

# Counting both sides is the check. A sync reporting success having transferred nothing reads
# exactly like one that had nothing to transfer, and only one of those is fine.
echo "  --- verifying"
for dir in store retrievals; do
    [ -d "$LIBRARY/$dir" ] || continue
    local_n=$(find "$LIBRARY/$dir" -type f | wc -l | tr -d ' ')
    remote_n=$(gcloud storage ls --recursive "$BUCKET/$dir/**" 2>/dev/null | grep -vc '/$' || echo 0)
    status=$([ "$local_n" -le "$remote_n" ] && echo ok || echo SHORT)
    printf "    %-12s local %6s   remote %6s   %s\n" "$dir" "$local_n" "$remote_n" "$status"
    printf '{"at":"%s","dir":"%s","local":%s,"remote":%s,"status":"%s"}\n' \
        "$started" "$dir" "$local_n" "$remote_n" "$status" >> "$LOG"
done
echo "  log     : ${LOG/#$HOME/\~}"
