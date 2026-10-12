#!/usr/bin/env bash
# natron-mcp installer shim (macOS/Linux).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$DIR/install.py" "$@"
elif command -v python >/dev/null 2>&1; then
  exec python "$DIR/install.py" "$@"
else
  echo "[x] Python 3 not found on PATH" >&2
  exit 1
fi
