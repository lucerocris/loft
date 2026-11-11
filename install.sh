#!/usr/bin/env bash
# Install Loft for the current user (Linux or macOS). Safe to run again.
#
#   ./install.sh              link `loft` into ~/.local/bin and add a launcher
#   ./install.sh --voice      also set up local speech-to-text (faster-whisper venv)
#   ./install.sh --cagents    also link the old `cagents` name (backs up an existing file)
#   ./install.sh --no-launcher
#
# Nothing here needs root. Nothing is started; run `loft setup` next.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${LOFT_BIN_DIR:-$HOME/.local/bin}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
VOICE=0 CAGENTS=0 LAUNCHER=1
for a in "$@"; do
  case "$a" in
    --voice) VOICE=1 ;;
    --cagents) CAGENTS=1 ;;
    --no-launcher) LAUNCHER=0 ;;
    -h|--help) sed -n 2,9p "$0"; exit 0 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done
OS="$(uname -s)"
say() { printf '%s\n' "$*"; }
ok() { printf '  \033[32mok\033[0m  %s\n' "$*"; }
note() { printf '  \033[33mnote\033[0m %s\n' "$*"; }

say "Installing Loft from $REPO"

# 1. python 3.9+
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { say "python3 not found. Install Python 3.9+ (macOS: xcode-select --install or brew install python)."; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { say "Loft needs Python 3.9+ (found $("$PY" -V))."; exit 1; }
ok "$("$PY" -V)"

# 2. claude
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then ok "claude found"
else note "Claude Code not found. Install it first: https://docs.anthropic.com/claude-code"; fi

# 3. the CLI
mkdir -p "$BIN"
chmod +x "$REPO/bin/loft"
ln -sfn "$REPO/bin/loft" "$BIN/loft"
ok "linked $BIN/loft"
if [ "$CAGENTS" = 1 ]; then
  if [ -e "$BIN/cagents" ] && [ ! -L "$BIN/cagents" ]; then
    mv "$BIN/cagents" "$BIN/cagents.pre-loft"; note "moved the old $BIN/cagents to cagents.pre-loft"
  fi
  ln -sfn "$REPO/bin/loft" "$BIN/cagents"; ok "linked $BIN/cagents (alias)"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) note "$BIN is not on your PATH. Add: export PATH=\"$BIN:\$PATH\"" ;; esac

# 4. launcher
if [ "$LAUNCHER" = 1 ]; then
  if [ "$OS" = Linux ]; then
    for f in "$REPO"/static/icons/loft-*; do
      size="$(basename "$f" | sed -E 's/loft-(.*)\.(png|svg)/\1/')"; ext="${f##*.}"
      mkdir -p "$DATA/icons/hicolor/$size/apps"; cp "$f" "$DATA/icons/hicolor/$size/apps/loft.$ext"
    done
    mkdir -p "$DATA/applications"
    # the desktop app (rpm/deb: loft-desktop on PATH, or an AppImage in ~/Applications)
    DESK="$(command -v loft-desktop || true)"
    [ -n "$DESK" ] || DESK="$(ls "$HOME"/Applications/Loft*.AppImage 2>/dev/null | tail -1 || true)"
    if [ -n "$DESK" ] && [ -f /usr/share/applications/Loft.desktop ]; then
      # the rpm/deb ships its own launcher; don't add a second "Loft"
      rm -f "$DATA/applications/loft.desktop"
      ok "launcher: the Loft desktop app ($DESK)"
    else
      if [ -n "$DESK" ]; then EXEC="$DESK"; WMCLASS=loft-desktop; KIND="desktop app"
      else EXEC="$REPO/bin/loft app --browser"; WMCLASS=loft; KIND="Chrome --app window; install the desktop app for a native one"; fi
      cat > "$DATA/applications/loft.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Loft
Comment=Claude Code background agents across your accounts
Exec=$EXEC
Icon=loft
Terminal=false
Categories=Development;Utility;
StartupWMClass=$WMCLASS
EOF
      ok "launcher: $DATA/applications/loft.desktop ($KIND)"
    fi
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DATA/applications" >/dev/null 2>&1 || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$DATA/icons/hicolor" >/dev/null 2>&1 || true
  elif [ "$OS" = Darwin ]; then
    APP="$HOME/Applications/Loft.app"
    if [ -x /Applications/Loft.app/Contents/MacOS/loft-desktop ] || [ -x "$APP/Contents/MacOS/loft-desktop" ]; then
      ok "launcher: the Loft desktop app (Spotlight: Loft)"
    elif command -v swiftc >/dev/null 2>&1; then
      # native window (WebKit), no browser involved
      TMP="$(mktemp -d)"
      mkdir -p "$TMP/Loft.app/Contents/MacOS" "$TMP/Loft.app/Contents/Resources" "$TMP/loft.iconset"
      swiftc -O -o "$TMP/Loft.app/Contents/MacOS/Loft" "$REPO/macos/Loft.swift"
      for s in 16 32 128 256; do cp "$REPO/static/icons/loft-${s}x${s}.png" "$TMP/loft.iconset/icon_${s}x${s}.png"; done
      iconutil -c icns -o "$TMP/Loft.app/Contents/Resources/loft.icns" "$TMP/loft.iconset"
      cat > "$TMP/Loft.app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Loft</string>
  <key>CFBundleDisplayName</key><string>Loft</string>
  <key>CFBundleIdentifier</key><string>dev.loft.app</string>
  <key>CFBundleExecutable</key><string>Loft</string>
  <key>CFBundleIconFile</key><string>loft</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSMicrophoneUsageDescription</key><string>Loft transcribes voice input on this Mac.</string>
  <key>LoftPython</key><string>$PY</string>
  <key>LoftCLI</key><string>$REPO/bin/loft</string>
</dict></plist>
EOF
      codesign --force -s - "$TMP/Loft.app" >/dev/null 2>&1 || true
      mkdir -p "$HOME/Applications"
      rm -rf "$APP"
      mv "$TMP/Loft.app" "$APP"
      rm -rf "$TMP"
      ok "app: $APP (Spotlight: Loft)"
    elif command -v osacompile >/dev/null 2>&1; then
      note "swiftc missing (xcode-select --install); Loft.app will open in a browser window"
      mkdir -p "$HOME/Applications"
      rm -rf "$APP"
      osacompile -o "$APP" -e "do shell script \"'$REPO/bin/loft' app --browser >/dev/null 2>&1 &\""
      ok "launcher: $APP (Spotlight: Loft; Chrome --app window, the desktop .dmg replaces it)"
    else
      note "osacompile missing; open Loft with \`loft app\`"
    fi
  fi
fi

# 5. voice (optional)
if [ "$VOICE" = 1 ]; then
  VENV="$DATA/loft/venv"
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q faster-whisper numpy
  ok "voice venv: $VENV"
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  if [ "$OS" = Darwin ]; then hint="brew install ffmpeg"
  elif command -v dnf >/dev/null 2>&1; then hint="sudo dnf install ffmpeg"
  elif command -v apt-get >/dev/null 2>&1; then hint="sudo apt-get install ffmpeg"
  else hint="install ffmpeg with your package manager"; fi
  note "ffmpeg not found (only voice input needs it): $hint"
fi

say ""
say "Next: loft setup     (accounts, then it opens the app)"
