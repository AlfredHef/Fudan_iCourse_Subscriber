#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f data/icourse.db ]]; then
  echo "No database file; skipping server summary export."
  exit 0
fi
if [[ -z "${SUMMARY_SSH_PRIVATE_KEY:-}" ]]; then
  echo "SUMMARY_SSH_PRIVATE_KEY is required for server summary export." >&2
  exit 1
fi

umask 077
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
printf '%s\n' "$SUMMARY_SSH_PRIVATE_KEY" > "$work_dir/key"
unset SUMMARY_SSH_PRIVATE_KEY
mkdir "$work_dir/records"
python scripts/export_email_records.py data/icourse.db "$work_dir/records"

# Restrict this key server-side to write-only rsync in email-record.
# The checked-in host key prevents a substituted server from receiving data.
rsync -r --checksum --timeout=120 --chmod=F600,D700 \
  -e "ssh -i $work_dir/key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=.github/record-server-known-hosts -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3" \
  "$work_dir/records/" ubuntu@124.222.232.239:.
