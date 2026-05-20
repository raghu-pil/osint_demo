#!/bin/bash
set -e
cd "$(dirname "$0")"

PYTHON=$(which python3 2>/dev/null || which python 2>/dev/null)
if [ -z "$PYTHON" ]; then echo "Python not found. Run ./setup.sh first."; exit 1; fi

# Start Tor if not already running
if ! pgrep -x tor > /dev/null 2>&1; then
  if command -v service &>/dev/null; then
    service tor start 2>/dev/null && echo "Tor started" || echo "[!] Tor not started (dark web search may be limited)"
  elif command -v tor &>/dev/null; then
    tor --RunAsDaemon 1 2>/dev/null && echo "Tor started" || true
  fi
fi

# Kill any existing instance on port 8000
pkill -f "uvicorn backend.main:app" 2>/dev/null || true
sleep 1

PORT=${PORT:-8000}
echo "Starting OSINT Tool at http://0.0.0.0:$PORT"
echo "Open in browser: http://localhost:$PORT"
$PYTHON -m uvicorn backend.main:app --host 0.0.0.0 --port $PORT
