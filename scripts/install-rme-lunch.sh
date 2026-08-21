#!/bin/bash
# Install a launchd job on this Mac that keeps RME-Lunch.ics up to date.
#
# Why this exists: the GitHub Actions version of this job cannot work. LINQ's
# load balancer (Server: awselb/2.0) returns a bare 403 to GitHub's Azure runner
# ranges before the request reaches their app. It is an IP-level deny, so no
# amount of header tuning fixes it. Running from this machine's own network does.
#
# Idempotent -- safe to re-run to upgrade or repair the install.
#
# Usage:   bash install-rme-lunch.sh
# Remove:  launchctl bootout gui/$(id -u)/com.mcr63.rme-lunch
#          rm -rf ~/.rme-lunch ~/Library/LaunchAgents/com.mcr63.rme-lunch.plist

set -euo pipefail

LABEL="com.mcr63.rme-lunch"
ROOT="$HOME/.rme-lunch"
REPO_DIR="$ROOT/repo"
VENV="$ROOT/venv"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNNER="$ROOT/update-lunch.sh"

# SSH by default: it works unattended without a credential-helper prompt.
# If you push over HTTPS with the macOS keychain helper, override this:
#   REPO_URL=https://github.com/mcr63/rme-lunch-2025.git bash install-rme-lunch.sh
REPO_URL="${REPO_URL:-git@github.com:mcr63/rme-lunch-2025.git}"

# 6:15 AM local. launchd tracks wall-clock time, so unlike the UTC cron this
# does NOT drift an hour when daylight saving ends.
HOUR=6
MINUTE=15

echo "==> Creating $ROOT"
mkdir -p "$ROOT" "$HOME/Library/LaunchAgents"

echo "==> Setting up repo clone at $REPO_DIR"
if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" remote set-url origin "$REPO_URL"
  git -C "$REPO_DIR" fetch --quiet origin
else
  git clone --quiet "$REPO_URL" "$REPO_DIR"
fi

echo "==> Building venv at $VENV"
if ! /usr/bin/python3 -c 'import sys' >/dev/null 2>&1; then
  echo "ERROR: /usr/bin/python3 is not usable. Run 'xcode-select --install' first." >&2
  exit 1
fi
if [ ! -x "$VENV/bin/python" ]; then
  /usr/bin/python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip requests

echo "==> Writing $RUNNER"
cat > "$RUNNER" <<'RUNNER_EOF'
#!/bin/bash
# Fetch the RME lunch menu, rebuild the ICS, push if it changed.
set -euo pipefail

ROOT="$HOME/.rme-lunch"
REPO_DIR="$ROOT/repo"
VENV="$ROOT/venv"

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export ICS_PATH="RME-Lunch.ics"
export CAL_NAME="Rocky Mountain Lunch (RME)"

cd "$REPO_DIR"

echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="

# launchd does NOT inherit your interactive ssh-agent, which is the classic
# reason this works in Terminal and fails at 6:15am. Check it up front so the
# log says "your key needs a keychain" instead of a bare git transport error.
export GIT_SSH_COMMAND="ssh -o BatchMode=yes"
if ! git ls-remote --quiet origin >/dev/null 2>&1; then
  echo "ERROR: cannot reach $(git remote get-url origin) without an interactive prompt." >&2
  echo "  launchd runs with no ssh-agent, so a passphrase-only key cannot work here." >&2
  echo "  Load the key into the login keychain so it works headless:" >&2
  echo "    ssh-add --apple-use-keychain ~/.ssh/id_ed25519" >&2
  echo "  and add to ~/.ssh/config:" >&2
  echo "    Host github.com" >&2
  echo "      UseKeychain yes" >&2
  echo "      AddKeysToAgent yes" >&2
  echo "      IdentityFile ~/.ssh/id_ed25519" >&2
  exit 1
fi

# Start from a clean copy of main every run. This clone is disposable and
# machine-owned, so discarding local state is always the right move -- it
# prevents a half-finished run from wedging every future run.
git fetch --quiet origin
git reset --quiet --hard origin/main

"$VENV/bin/python" scripts/update_lunch_menu.py

if git diff --quiet -- "$ICS_PATH"; then
  echo "No changes."
  exit 0
fi

git -c user.name="lunch-menu-bot" \
    -c user.email="actions@users.noreply.github.com" \
    commit --quiet -m "Update RME lunch menu $(date -u +%F)" -- "$ICS_PATH"

git push --quiet origin HEAD:main
echo "Pushed $(git rev-parse --short HEAD)."
RUNNER_EOF
chmod +x "$RUNNER"

echo "==> Writing $PLIST"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$RUNNER</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  </array>
  <key>StandardOutPath</key><string>$ROOT/lunch-update.log</string>
  <key>StandardErrorPath</key><string>$ROOT/lunch-update.log</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLIST_EOF

echo "==> Loading the launchd agent"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "==> Running once now to verify"
launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
sleep 25

echo
echo "----- $ROOT/lunch-update.log -----"
tail -n 30 "$ROOT/lunch-update.log" 2>/dev/null || echo "(no log yet -- give it a few more seconds)"
echo "----------------------------------"
echo
if grep -qE "Pushed |No changes\." "$ROOT/lunch-update.log" 2>/dev/null; then
  echo "VERIFIED: the job ran under launchd and reached GitHub."
else
  echo "WARNING: the verification run did not report success. Read the log above."
  echo "         A push failure here is usually the ssh-agent issue it describes."
fi
echo
echo "Installed. It runs weekdays at $HOUR:$(printf '%02d' $MINUTE) local time."
echo "Watch it:   tail -f $ROOT/lunch-update.log"
echo "Run it now: launchctl kickstart -k gui/$(id -u)/$LABEL"
