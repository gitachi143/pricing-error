#!/usr/bin/env bash
# Deal Desk — one-shot macOS install for the checkout worker.
#
#   curl -fsSL "https://<your-app>.azurewebsites.net/api/worker/install.sh?t=<code>" | bash
#
# or, from a clone of the repo:
#
#   DEALDESK_URL=https://… WORKER_TOKEN=… bash worker/install_mac.sh
#
# It is idempotent — run it again any time to upgrade or repair.
#
# What it does:
#   1. puts the worker in ~/Library/Application Support/DealDesk
#   2. builds an isolated Python environment and installs Playwright + Chromium
#   3. writes your server URL and token to a 0600 config file
#   4. registers a launchd agent: starts at login, restarts if it ever dies
#   5. starts it
#
# Nothing runs as root. Uninstall with: dealdesk uninstall
set -euo pipefail

APP_DIR="${DEALDESK_HOME:-$HOME/Library/Application Support/DealDesk}"
LOG_DIR="$HOME/Library/Logs/DealDesk"
PLIST="$HOME/Library/LaunchAgents/com.dealdesk.worker.plist"
LABEL="com.dealdesk.worker"
BIN="$HOME/.local/bin/dealdesk"

say()  { printf "\033[1m▸\033[0m %s\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "This installer is for macOS. On Linux use a systemd unit instead."

# ---------------------------------------------------------------- python
say "Checking Python"
PY=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$("$c" -c 'import sys;print(sys.version_info>=(3,10))' 2>/dev/null || echo False)
    [[ "$v" == "True" ]] && { PY=$(command -v "$c"); break; }
  fi
done
[[ -n "$PY" ]] || die "Python 3.10+ not found. Install it from python.org, then re-run this."
ok "$($PY --version) at $PY"

# ---------------------------------------------------------------- files
say "Installing to $APP_DIR"
mkdir -p "$APP_DIR" "$LOG_DIR" "$(dirname "$PLIST")" "$(dirname "$BIN")"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "${WORKER_BUNDLE_URL:-}" ]]; then
  # Installed straight from the dashboard: pull the worker code from the server.
  say "Downloading the worker bundle"
  TMP=$(mktemp -d)
  curl -fsSL -H "Authorization: Bearer ${WORKER_TOKEN}" "$WORKER_BUNDLE_URL" -o "$TMP/worker.tar.gz" \
    || die "Could not download the worker bundle. Is the setup link still valid?"
  tar -xzf "$TMP/worker.tar.gz" -C "$APP_DIR"
  rm -rf "$TMP"
  ok "worker code downloaded"
elif [[ -f "$SRC_DIR/run_worker.py" ]]; then
  cp "$SRC_DIR"/*.py "$APP_DIR"/
  [[ -f "$SRC_DIR/vault.example.json" ]] && cp "$SRC_DIR/vault.example.json" "$APP_DIR"/
  ok "worker code copied from the repo"
else
  die "Can't find the worker code. Run this from the repo, or use the link from the Setup page."
fi

# ---------------------------------------------------------------- venv
say "Building the Python environment (this takes a minute the first time)"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  "$PY" -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$APP_DIR/venv/bin/python" -m pip install --quiet "httpx==0.28.1" "playwright==1.47.0"
ok "dependencies installed"

say "Installing the browser Chromium build"
if "$APP_DIR/venv/bin/python" -m playwright install chromium 2>&1 | tail -1; then
  ok "browser ready"
else
  warn "Chromium install had trouble — re-run: '$APP_DIR/venv/bin/python' -m playwright install chromium"
fi

# ---------------------------------------------------------------- config
say "Writing config"
: "${DEALDESK_URL:?DEALDESK_URL is not set — use the command from the dashboard's Setup page}"
: "${WORKER_TOKEN:?WORKER_TOKEN is not set — use the command from the dashboard's Setup page}"
cat > "$APP_DIR/config.env" <<CFG
DEALDESK_URL=$DEALDESK_URL
WORKER_TOKEN=$WORKER_TOKEN
WORKER_DRY_RUN=${WORKER_DRY_RUN:-1}
WORKER_HEADLESS=${WORKER_HEADLESS:-0}
WORKER_PROFILE=$APP_DIR/browser-profile
CFG
chmod 600 "$APP_DIR/config.env"
ok "config.env written (0600)"

if [[ ! -f "$APP_DIR/vault.json" ]]; then
  if [[ -f "$APP_DIR/vault.example.json" ]]; then
    cp "$APP_DIR/vault.example.json" "$APP_DIR/vault.json"
  else
    echo '{"address":{},"accounts":{},"cards":{}}' > "$APP_DIR/vault.json"
  fi
  chmod 600 "$APP_DIR/vault.json"
  ok "empty vault created — fill it in with: dealdesk vault"
else
  chmod 600 "$APP_DIR/vault.json"
  ok "existing vault kept"
fi

# ---------------------------------------------------------------- launchd
say "Registering the background agent"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_DIR/venv/bin/python</string>
    <string>$APP_DIR/run_worker.py</string>
  </array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DEALDESK_CONFIG</key><string>$APP_DIR/config.env</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <!-- start when you log in -->
  <key>RunAtLoad</key><true/>
  <!-- and put it back if it ever exits, for any reason -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOG_DIR/worker.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/worker.err.log</string>
</dict></plist>
PL
ok "launchd agent written"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
ok "agent started"

# ---------------------------------------------------------------- cli
cat > "$BIN" <<CLI
#!/usr/bin/env bash
# Deal Desk worker control
set -euo pipefail
APP_DIR="$APP_DIR"; LABEL="$LABEL"; PLIST="$PLIST"; LOG_DIR="$LOG_DIR"
case "\${1:-status}" in
  status)  launchctl print "gui/\$(id -u)/\$LABEL" 2>/dev/null \\
             | grep -E '^\s+(state|pid|last exit code)' || echo "not running";
           echo; tail -n 15 "\$LOG_DIR/worker.log" 2>/dev/null || true ;;
  start)   launchctl kickstart -k "gui/\$(id -u)/\$LABEL" && echo "started" ;;
  stop)    launchctl bootout "gui/\$(id -u)/\$LABEL" 2>/dev/null && echo "stopped" || echo "not running" ;;
  restart) launchctl kickstart -k "gui/\$(id -u)/\$LABEL" && echo "restarted" ;;
  logs)    tail -f "\$LOG_DIR/worker.log" ;;
  vault)   "\$APP_DIR/venv/bin/python" "\$APP_DIR/vault_server.py" ;;
  login)   shift; set -a; . "\$APP_DIR/config.env"; set +a;
           "\$APP_DIR/venv/bin/python" "\$APP_DIR/run_worker.py" --login "\$@" ;;
  live)    sed -i '' 's/WORKER_DRY_RUN=1/WORKER_DRY_RUN=0/' "\$APP_DIR/config.env";
           launchctl kickstart -k "gui/\$(id -u)/\$LABEL"; echo "DRY RUN OFF — the worker will now pay for things" ;;
  dry)     sed -i '' 's/WORKER_DRY_RUN=0/WORKER_DRY_RUN=1/' "\$APP_DIR/config.env";
           launchctl kickstart -k "gui/\$(id -u)/\$LABEL"; echo "dry run on — nothing will be purchased" ;;
  uninstall)
           launchctl bootout "gui/\$(id -u)/\$LABEL" 2>/dev/null || true
           rm -f "\$PLIST" "\$HOME/.local/bin/dealdesk"
           echo "Agent removed. Your data is still in \$APP_DIR (delete it by hand if you want it gone)." ;;
  *) echo "usage: dealdesk {status|start|stop|restart|logs|vault|login <merchant>|live|dry|uninstall}" ;;
esac
CLI
chmod +x "$BIN"

echo
printf "\033[1m  Worker installed and running.\033[0m\n\n"
echo "  dealdesk status          how it's doing"
echo "  dealdesk logs            follow the log"
echo "  dealdesk vault           enter card + login details (localhost only)"
echo "  dealdesk login <site>    first sign-in to a merchant, by hand"
echo "  dealdesk live            turn OFF dry run (it will start paying)"
echo
echo "  It starts automatically when you log in, and restarts itself if it crashes."
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "Add this to your shell profile so 'dealdesk' is on your PATH:"
     echo "       echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
esac
echo
echo "  It is in DRY RUN: it will do everything except the final payment click."
echo "  Check the dashboard's Setup page — the worker should show as online within a minute."
