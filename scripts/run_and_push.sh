#!/bin/zsh
# run_and_push.sh — runs locally because the LINQ API blocks GitHub Actions IPs.
# Scheduled via ~/Library/LaunchAgents/com.mattrobertson.rme-lunch.plist

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

export ICS_PATH="RME-Lunch.ics"
export CAL_NAME="Rocky Mountain Lunch (RME)"

python3 scripts/update_lunch_menu.py

if git diff --quiet -- "$ICS_PATH"; then
  echo "$(date -u +%FT%TZ)  No changes."
  exit 0
fi

git add "$ICS_PATH"
git commit -m "Update RME lunch menu $(date -u +%F)"
git push
echo "$(date -u +%FT%TZ)  Pushed."
