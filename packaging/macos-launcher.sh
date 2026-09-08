#!/bin/sh
set -eu
APP_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
RUNTIME="$APP_ROOT/Resources/runtime/Bilingual Manga Reader and Mokuro Converter"

# LaunchServices will not execute an app bundle again while its main process is
# still alive.  The reader server is intentionally long-running and has no
# native window, so keeping it as the bundle process made later Finder clicks
# appear to do nothing after the Chrome tab had been closed.  Detach the server
# from the short-lived bundle launcher so every click gets a fresh launcher;
# the Python launcher then either starts the server or opens the existing one.
if [ -z "${BMO_DATA_DIR+x}" ]; then
  DATA_ROOT="$HOME/Library/Application Support/Bilingual Manga Reader and Mokuro Converter"
  for LEGACY_DATA_ROOT in \
    "$HOME/Library/Application Support/Mokuro & Bilingual Manga Reader" \
    "$HOME/Library/Application Support/Bilingual Manga Offline"; do
    if [ ! -e "$DATA_ROOT" ] && [ -d "$LEGACY_DATA_ROOT" ]; then
      mv "$LEGACY_DATA_ROOT" "$DATA_ROOT" 2>/dev/null || DATA_ROOT="$LEGACY_DATA_ROOT"
    fi
  done
else
  DATA_ROOT="$BMO_DATA_DIR"
fi
LOG_DIR="$DATA_ROOT/logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true

nohup env \
  -u __CFBundleIdentifier \
  -u XPC_SERVICE_NAME \
  -u XPC_FLAGS \
  BMO_DATA_DIR="$DATA_ROOT" \
  "$RUNTIME" "$@" >>"$LOG_DIR/launcher.log" 2>&1 </dev/null &

exit 0
