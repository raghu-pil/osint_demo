#!/bin/bash
set -e
echo "=== OSINT Tool Setup ==="
echo "Detected OS: $(uname -s) $(uname -m)"

# Find python
PYTHON=$(which python3 2>/dev/null || which python 2>/dev/null)
if [ -z "$PYTHON" ]; then
  echo "ERROR: Python not found"; exit 1
fi
echo "Using Python: $PYTHON ($($PYTHON --version))"

PIP=$(which pip3 2>/dev/null || which pip 2>/dev/null)
if [ -z "$PIP" ]; then
  echo "Installing pip..."
  $PYTHON -m ensurepip --upgrade
  PIP="$PYTHON -m pip"
fi

echo ""
echo "--- Installing core dependencies ---"
$PIP install --quiet --upgrade pip
$PIP install --quiet \
  fastapi==0.111.0 \
  "uvicorn[standard]==0.29.0" \
  pydantic==2.7.1 \
  python-multipart==0.0.9 \
  aiofiles==23.2.1 \
  requests \
  urllib3 \
  beautifulsoup4 \
  lxml \
  PyYAML \
  tldextract \
  yt-dlp \
  Pillow \
  piexif \
  mutagen \
  imagehash \
  geopy \
  reportlab \
  rich

echo ""
echo "--- Installing optional NLP/identity dependencies ---"
$PIP install --quiet spacy phonenumbers 2>/dev/null || echo "  (spacy/phonenumbers install skipped - not critical)"
$PYTHON -m spacy download en_core_web_sm --quiet 2>/dev/null || echo "  (spaCy model download skipped)"

echo ""
echo "--- Installing username enumeration (sherlock) ---"
$PIP install --quiet sherlock-project 2>/dev/null || echo "  (sherlock install skipped)"

echo ""
echo "--- Creating directories ---"
mkdir -p cases

echo ""
echo "--- Installing system dependencies ---"
if command -v apt-get &>/dev/null; then
  apt-get install -y -qq tor curl 2>/dev/null && echo "  Installed: tor, curl" || echo "  (apt install skipped - may need sudo)"
elif command -v brew &>/dev/null; then
  brew install tor 2>/dev/null && echo "  Installed: tor (brew)" || echo "  (brew install skipped)"
else
  echo "  [!] Please install 'tor' manually for dark web search"
fi

echo ""
echo "--- Starting Tor daemon ---"
if command -v service &>/dev/null; then
  service tor start 2>/dev/null && echo "  Tor started" || tor --RunAsDaemon 1 2>/dev/null && echo "  Tor started (daemon)" || echo "  [!] Tor not started - run: service tor start"
elif command -v tor &>/dev/null; then
  tor --RunAsDaemon 1 2>/dev/null && echo "  Tor started" || echo "  [!] Tor failed to start"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit config.yaml and add API keys (optional but recommended)"
echo "     - hibp_api_key:     haveibeenpwned.com/API/Key  (\$3.50/month)"
echo "     - dehashed_api_key: dehashed.com               (\$5/month)"
echo "  2. For LLM analysis on GPU:"
echo "     - Install Ollama: curl -fsSL https://ollama.com/install.sh | sh"
echo "     - Pull a model:   ollama pull llama3:8b"
echo "     - Set in config:  llm.enabled: true, llm.provider: ollama"
echo "  3. Start the tool: ./run.sh"
echo "  4. Open browser:   http://localhost:8000"
