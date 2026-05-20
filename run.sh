#!/bin/bash
PYTHON=$(which python3 2>/dev/null || which python 2>/dev/null)
if [ -z "$PYTHON" ]; then echo "Python not found. Run setup.sh first."; exit 1; fi
cd "$(dirname "$0")"
# Kill any existing instance
pkill -f "uvicorn backend.main:app" 2>/dev/null || true
sleep 1
echo "Starting OSINT Tool at http://localhost:8000"
$PYTHON -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
