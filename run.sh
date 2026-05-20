#!/bin/bash
set -e
cd "$(dirname "$0")"

PYTHON=$(which python3 2>/dev/null || which python 2>/dev/null)
if [ -z "$PYTHON" ]; then echo "Python not found. Run ./setup.sh first."; exit 1; fi

# Warn if serpapi_api_key is missing
SERPAPI_SET=$(grep -E '^serpapi_api_key:\s*".+"' config.yaml 2>/dev/null | grep -v '""' | wc -l)
if [ "$SERPAPI_SET" -eq 0 ]; then
  echo ""
  echo "⚠  WARNING: serpapi_api_key is not set in config.yaml"
  echo "   Image search investigations will fail without it."
  echo "   Get a free key at https://serpapi.com then edit config.yaml"
  echo ""
fi

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
