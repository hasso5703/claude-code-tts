#!/usr/bin/env bash
# claude-code-tts installer.
#
# Usage (from a clone):
#   ./install.sh [--mode local|spool] [--voice NAME] [--rate N] [--engine E]
#
# Neural French voice (macOS Apple Silicon) — one command: creates a venv,
# installs mlx-audio, downloads the model, wires hooks:
#   python3 claude_tts.py setup-kokoro    # fast, Apache-2.0 (recommended)
#   python3 claude_tts.py setup-voxtral   # top quality, non-commercial
# Switch anytime:  python3 claude_tts.py preset kokoro|voxtral
#
# Usage (one-liner):
#   CCTTS_RAW_URL="https://raw.githubusercontent.com/hasso5703/claude-code-tts/main/claude_tts.py" \
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/hasso5703/claude-code-tts/main/install.sh)"
set -euo pipefail

# Find a Python 3 interpreter.
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[0]==3 else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "error: Python 3 is required but was not found on PATH." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)"
SRC="$SCRIPT_DIR/claude_tts.py"

# If running via curl|bash without a local copy, fetch claude_tts.py.
if [ ! -f "$SRC" ]; then
  if [ -n "${CCTTS_RAW_URL:-}" ]; then
    TMP="$(mktemp -d)"
    SRC="$TMP/claude_tts.py"
    echo "Downloading claude_tts.py from $CCTTS_RAW_URL"
    curl -fsSL "$CCTTS_RAW_URL" -o "$SRC"
  else
    echo "error: claude_tts.py not found next to install.sh, and CCTTS_RAW_URL is unset." >&2
    exit 1
  fi
fi

echo "Using Python: $("$PY" --version 2>&1)"
exec "$PY" "$SRC" install "$@"
