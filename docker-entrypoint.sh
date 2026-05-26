#!/bin/sh
set -e

# Copy example config if none provided
if [ ! -f /app/config.yaml ]; then
    echo "[authintify] No config.yaml found — copying from example. Edit it to add API keys."
    cp /app/config.yaml.example /app/config.yaml
fi

# Start Tor daemon (best-effort — dark web search degrades gracefully without it)
service tor start 2>/dev/null || tor --RunAsDaemon 1 2>/dev/null || echo "[authintify] Tor not started — dark web search will be limited"

echo "[authintify] Starting server at http://0.0.0.0:${PORT:-8000}"
exec python -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1
