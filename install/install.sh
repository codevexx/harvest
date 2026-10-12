#!/bin/sh
# Harvest for macOS and Linux.
#
#   curl -fsSL https://harvest.codevexx.com/install.sh | sh
#
# Installs the Harvest agent for your user (no root needed), asks for your
# connect token, and starts it in the background:
#   macOS: a LaunchAgent (runs while you are signed in)
#   Linux: a systemd user service (runs at boot if lingering is allowed)
# It updates itself from GitHub Releases, checking each download's SHA-256.
#
# Options: --token hv_...  --name NAME  --server URL  --no-service  --uninstall
set -eu

SERVER="https://harvest.codevexx.com"
TOKEN=""
NAME=""
SERVICE=1
UNINSTALL=0
REPO="codevexx/harvest"

while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --server) SERVER="$2"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

OS=$(uname -s)
case "$OS" in
  Darwin) HOME_DIR="${HARVEST_HOME:-$HOME/Library/Application Support/Harvest}" ;;
  Linux) HOME_DIR="${HARVEST_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/harvest}" ;;
  *) fail "Harvest supports macOS and Linux here. On Windows, use Harvest-Setup.exe from $SERVER/download" ;;
esac
PLIST="$HOME/Library/LaunchAgents/com.codevexx.harvest.plist"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/harvest.service"

stop_service() {
  if [ "$OS" = Darwin ]; then
    launchctl bootout "gui/$(id -u)/com.codevexx.harvest" 2>/dev/null || true
  elif command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now harvest.service 2>/dev/null || true
  fi
}

if [ "$UNINSTALL" = 1 ]; then
  stop_service
  rm -f "$PLIST" "$UNIT"
  command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload 2>/dev/null || true
  rm -rf "$HOME_DIR"
  say "Harvest removed. (Remove the machine from your dashboard too, if you like.)"
  exit 0
fi

# --- Python 3.10+ ------------------------------------------------------------
PY=""
for p in python3.13 python3.12 python3.11 python3.10 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  cand=$(command -v "$p" 2>/dev/null || true)
  [ -n "$cand" ] || continue
  # macOS: /usr/bin/python3 is only a stub until the Command Line Tools are installed.
  if [ "$OS" = Darwin ] && [ "$cand" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
  if "$cand" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  if [ "$OS" = Darwin ]; then fail "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ (or: brew install python), then run this again."
  else fail "Python 3.10 or newer with venv is needed. For example: sudo apt install python3 python3-venv (Debian/Ubuntu) or sudo dnf install python3 (Fedora), then run this again."; fi
fi
say "Using $("$PY" -c 'import sys; print("Python", sys.version.split()[0])') at $PY"

# --- download and check the agent ----------------------------------------------
mkdir -p "$HOME_DIR"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/harvest.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
if [ -n "${HARVEST_TARBALL:-}" ]; then  # for testing: a local build
  cp "$HARVEST_TARBALL" "$TMP/agent.tar.gz"
  sha_want=$(cat "$HARVEST_TARBALL.sha256" | awk '{print $1}')
else
  URL="https://github.com/$REPO/releases/latest/download/harvest-agent.tar.gz"
  say "Downloading Harvest"
  curl -fsSL "$URL" -o "$TMP/agent.tar.gz" || fail "could not download $URL"
  sha_want=$(curl -fsSL "$URL.sha256" | awk '{print $1}') || fail "could not download the checksum"
fi
if command -v sha256sum >/dev/null 2>&1; then sha_got=$(sha256sum "$TMP/agent.tar.gz" | awk '{print $1}')
else sha_got=$(shasum -a 256 "$TMP/agent.tar.gz" | awk '{print $1}'); fi
[ "$sha_got" = "$sha_want" ] || fail "the download did not match its published checksum; not installing"

rm -rf "$HOME_DIR/app.new"
mkdir -p "$HOME_DIR/app.new"
tar -xzf "$TMP/agent.tar.gz" -C "$HOME_DIR/app.new"
[ -f "$HOME_DIR/app.new/worker/daemon.py" ] || fail "the download is not a Harvest agent"
rm -rf "$HOME_DIR/app.old"
[ -d "$HOME_DIR/app" ] && mv "$HOME_DIR/app" "$HOME_DIR/app.old"
mv "$HOME_DIR/app.new" "$HOME_DIR/app"
say "Installed Harvest $(cat "$HOME_DIR/app/VERSION")"

# --- its own Python environment -------------------------------------------------
if [ ! -x "$HOME_DIR/venv/bin/python" ]; then "$PY" -m venv "$HOME_DIR/venv" || fail "could not create a Python environment (on Debian/Ubuntu: sudo apt install python3-venv)"; fi
"$HOME_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade requests psutil

# --- settings ----------------------------------------------------------------------
CONFIG="$HOME_DIR/config.json"
if [ -z "$TOKEN" ] && [ ! -f "$CONFIG" ]; then
  if [ -r /dev/tty ]; then
    printf 'Paste your connect token (from the dashboard: Machines > Connect a machine): '
    read -r TOKEN < /dev/tty
  fi
fi
if [ -n "$TOKEN" ]; then
  case "$TOKEN" in hv_*) ;; *) fail "that does not look like a connect token (it starts with hv_)" ;; esac
  [ -n "$NAME" ] || NAME=$(hostname -s 2>/dev/null || hostname)
  umask 077
  HARVEST_SERVER="$SERVER" HARVEST_TOKEN="$TOKEN" HARVEST_NAME="$NAME" HARVEST_PY="$PY" "$PY" - "$CONFIG" <<'EOF'
import json, os, sys
path = sys.argv[1]
cfg = {}
if os.path.exists(path):
    try: cfg = json.load(open(path))
    except ValueError: pass
cfg.update(coordinator=os.environ["HARVEST_SERVER"], token=os.environ["HARVEST_TOKEN"],
           name=os.environ["HARVEST_NAME"], python=os.environ["HARVEST_PY"])
with open(path, "w") as f: json.dump(cfg, f, indent=2)
EOF
  chmod 600 "$CONFIG"
fi
[ -f "$CONFIG" ] || fail "no connect token given. Run again and paste it when asked (or add --token hv_...)."

# --- start in the background ----------------------------------------------------------
RUN="$HOME_DIR/venv/bin/python -m worker.daemon"
PATHS="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ "$SERVICE" = 0 ]; then
  say "Done. Start it yourself with:"
  echo "  cd \"$HOME_DIR/app\" && HARVEST_HOME=\"$HOME_DIR\" $RUN"
  exit 0
fi
if [ "$OS" = Darwin ]; then
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.codevexx.harvest</string>
  <key>ProgramArguments</key><array>
    <string>$HOME_DIR/venv/bin/python</string><string>-m</string><string>worker.daemon</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME_DIR/app</string>
  <key>EnvironmentVariables</key><dict>
    <key>HARVEST_HOME</key><string>$HOME_DIR</string>
    <key>PATH</key><string>$PATHS</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$HOME_DIR/harvest.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/harvest.log</string>
</dict>
</plist>
EOF
  stop_service
  launchctl bootstrap "gui/$(id -u)" "$PLIST" || fail "could not start the background service"
  say "Harvest is running. It starts by itself whenever you sign in."
  echo "Log: $HOME_DIR/harvest.log    Remove: curl -fsSL $SERVER/install.sh | sh -s -- --uninstall"
else
  if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
    say "No systemd user session here, so Harvest is not set to start by itself. Start it with:"
    echo "  cd \"$HOME_DIR/app\" && HARVEST_HOME=\"$HOME_DIR\" nohup $RUN >> \"$HOME_DIR/harvest.log\" 2>&1 &"
    exit 0
  fi
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<EOF
[Unit]
Description=Harvest agent (runs tasks from harvest.codevexx.com while this machine is idle)
After=network-online.target

[Service]
WorkingDirectory=$HOME_DIR/app
Environment=HARVEST_HOME=$HOME_DIR
Environment=PATH=$PATHS
ExecStart=$HOME_DIR/venv/bin/python -m worker.daemon
Restart=always
RestartSec=10
Nice=10

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now harvest.service || fail "could not start the service"
  if loginctl enable-linger "$(id -un)" 2>/dev/null; then
    say "Harvest is running, and starts at boot even before anyone logs in."
  else
    say "Harvest is running. To keep it running when you are logged out, run once: sudo loginctl enable-linger $(id -un)"
  fi
  echo "Log: journalctl --user -u harvest -f    Remove: curl -fsSL $SERVER/install.sh | sh -s -- --uninstall"
fi
