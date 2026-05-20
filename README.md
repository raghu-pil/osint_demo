# OSINT Investigation Tool

A forensic OSINT platform for social media investigations. Submit a URL → the tool automatically scrapes the post, maps the account's identity across platforms, searches dark web breach databases, downloads and analyses media, and produces prioritised investigative leads with an interactive network graph.

Built for forensic investigators. All data is stored locally. No cloud dependencies.

---

## What it does

Given a social media URL (Twitter/X, Reddit, Instagram, TikTok, YouTube, Telegram), it runs a 11-step pipeline:

| Step | What happens |
|------|-------------|
| URL Analysis | Parses platform, post ID, username |
| Post Scraping | Extracts full post text, engagement stats, media URLs (fxtwitter → syndication → Nitter fallback chain) |
| Account Profile | Followers, bio, creation date, location via vxtwitter |
| Account Timeline | Bio link extraction, posting pattern analysis, timezone inference |
| Cross-Post Detection | Wayback Machine, perceptual hash matching |
| Username Enumeration | Sherlock across 400+ platforms |
| Dark Web Search | BreachDirectory, DarkSearch.io, Ahmia, IntelligenceX, **direct .onion engines via Tor** |
| Media Download & EXIF | Downloads images/video, extracts GPS, device fingerprint, SHA-256 chain of custody |
| Identity Pivots | Email → Gravatar, HIBP, platform registration check; phone → carrier/country |
| Analyst Guidance | Prioritised action list generated from all findings |
| Auto Investigations | Automatically performs the top actions: Linktree scrape, Blogger profile, reverse image search, Twitter following list |

---

## Quick start

```bash
git clone <this-repo>
cd osint
bash setup.sh          # installs Python deps, Tor, optional NLP
./run.sh               # starts server on port 8000
```

Open **http://localhost:8000** in a browser.

---

## Requirements

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | 3.12 tested |
| tor | any | `apt-get install -y tor` — enables .onion search |
| (optional) ffmpeg | any | `apt-get install -y ffmpeg` — video metadata + keyframe OCR |
| (optional) tesseract | any | `apt-get install -y tesseract-ocr` — OCR on images |

---

## Setup (step by step)

### 1. Clone and install

```bash
git clone <this-repo> osint-tool
cd osint-tool
bash setup.sh
```

`setup.sh` installs:
- FastAPI, uvicorn, pydantic
- yt-dlp (media download)
- Pillow, piexif, mutagen (media metadata)
- spaCy + en_core_web_sm (entity extraction)
- sherlock-project (username enumeration)
- geopy (reverse geocoding)
- reportlab (PDF reports)
- pysocks (Tor SOCKS5 proxy)
- Tor daemon (dark web .onion search)

### 2. Configure

Copy and edit `config.yaml`:

```yaml
server:
  host: "0.0.0.0"
  port: 8000

# Optional API keys — tool works without them, but these unlock more
hibp_api_key: ""          # haveibeenpwned.com/API/Key  ($3.50/month)
dehashed_api_key: ""      # dehashed.com                ($5/month)
dehashed_email: ""        # your Dehashed account email
intelx_api_key: ""        # intelx.io
darksearch_api_key: ""    # darksearch.io

# LLM analysis (see below)
llm:
  enabled: false
  provider: "ollama"      # ollama | anthropic | openai
  model: "llama3:8b"
  ollama_base_url: "http://localhost:11434"
```

### 3. Start Tor (for dark web .onion search)

```bash
# Debian/Ubuntu
service tor start

# macOS (Homebrew)
brew services start tor

# Manual
tor --RunAsDaemon 1

# Verify
curl --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip
```

When Tor is running, the tool automatically searches:
- **Ahmia** (via Tor, more complete than clearnet)
- **Torch** (.onion, oldest Tor search engine)
- **Tor66** (.onion directory search)
- **DuckDuckGo .onion** hidden service

### 4. Run

```bash
./run.sh
# Server starts at http://localhost:8000
```

---

## LLM Analysis (GPU server)

The LLM step summarises all findings, extracts key facts, and suggests investigative leads. Three provider options:

### Option A — Ollama (local GPU, recommended)

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model (choose based on your GPU VRAM)
ollama pull llama3:8b        # 8B params, ~5GB VRAM
ollama pull llama3:70b       # 70B params, ~40GB VRAM
ollama pull mistral          # 7B, good for OSINT
ollama pull gemma2:27b       # strong reasoning
```

Set in `config.yaml`:
```yaml
llm:
  enabled: true
  provider: "ollama"
  model: "llama3:8b"
  ollama_base_url: "http://localhost:11434"
```

Then install the Python client:
```bash
pip install anthropic   # not needed for Ollama, but install for consistency
```

### Option B — Anthropic Claude (API)

```bash
pip install anthropic
export ANTHROPIC_API_KEY="sk-ant-..."
```

```yaml
llm:
  enabled: true
  provider: "anthropic"
  model: "claude-sonnet-4-6"
```

### Option C — OpenAI

```bash
pip install openai
export OPENAI_API_KEY="sk-..."
```

```yaml
llm:
  enabled: true
  provider: "openai"
  model: "gpt-4o"
```

---

## API keys — what they unlock

| Key | Source | Cost | Unlocks |
|-----|--------|------|---------|
| `hibp_api_key` | haveibeenpwned.com/API/Key | $3.50/month | Email breach history — most reliable |
| `dehashed_api_key` | dehashed.com | $5/month | Full breach records: name, address, phone, password hash |
| `intelx_api_key` | intelx.io/account?tab=developer | Free tier + paid | 10x more paste/darkweb results |
| `darksearch_api_key` | darksearch.io | Free + paid | Removes 10/day rate limit |
| `twitter_bearer_token` | developer.twitter.com | Free dev account | Twitter official API v2 (better scraping) |
| `youtube_api_key` | console.cloud.google.com | Free quota | YouTube channel + video metadata |

---

## Dark web sources

| Source | Requires | What it searches |
|--------|----------|-----------------|
| BreachDirectory | Nothing | Breach credential lookup |
| DarkSearch.io | Nothing (10/day) | Dark web page index |
| Ahmia | Nothing | Tor hidden service index (clearnet) |
| Ahmia via Tor | Tor running | Same, but through Tor (more results, anonymous) |
| Torch (.onion) | Tor running | Oldest Tor search engine |
| Tor66 (.onion) | Tor running | Tor directory search |
| IntelligenceX | Nothing (demo key) | Paste sites, darkweb indexer |
| HIBP | `hibp_api_key` | Email breach + paste check |
| Dehashed | `dehashed_api_key` | Full breach record search |

---

## REST API

```bash
# Submit URL for investigation
curl -X POST http://localhost:8000/api/cases \
  -H "Content-Type: application/json" \
  -d '{"url": "https://twitter.com/user/status/123456789"}'

# Poll for results (returns live progress)
curl http://localhost:8000/api/cases/{case_id}

# List all cases
curl http://localhost:8000/api/cases

# Download case report (JSON)
curl http://localhost:8000/api/cases/{case_id}/report > report.json

# Download media file
curl http://localhost:8000/api/cases/{case_id}/media/{filename} > file.mp4
```

---

## Architecture

```
osint/                    ← core intelligence engine (scrapers, analysis, intelligence)
  scrapers/               ← Twitter, Reddit, Instagram, TikTok, YouTube
  analysis/               ← content flags, account behaviour, red flags scoring
  intelligence/           ← Sherlock, cross-post detection, dark web
  core/                   ← URL parser, data models, HTTP utils

backend/                  ← FastAPI web layer
  main.py                 ← FastAPI app, CORS, routing
  pipeline.py             ← 11-step async pipeline orchestrator + case manager
  config.py               ← config.yaml loader
  models.py               ← Pydantic models (Case, GuidanceItem, etc.)
  modules/
    account_history.py    ← vxtwitter profile enrichment, timeline, bio link extraction
    auto_actions.py       ← auto-performs guidance: Linktree, Blogger, reverse search
    darkweb_enhanced.py   ← breach + dark web search (BreachDirectory, DarkSearch, .onion via Tor)
    guidance.py           ← analyst guidance engine (prioritised leads)
    identity.py           ← email/phone/username pivot (Gravatar, HIBP, platform check)
    media.py              ← media download, EXIF, GPS, OCR, reverse image search

frontend/
  index.html              ← single-page analyst workbench (no build step)

cases/                    ← investigation storage (JSON + media, created at runtime)
config.yaml               ← all configuration
setup.sh                  ← install script
run.sh                    ← start server
```

### Case storage

Each investigation is stored as a directory under `cases/`:
```
cases/{case_id}/
  case.json          ← full investigation data (progress, findings, guidance)
  media/             ← downloaded images, videos, audio
  keyframes/         ← video keyframes for OCR
```

---

## Network graph

The **Network Graph** tab shows all investigation entities as an interactive force-directed graph:

- **Drag** nodes to rearrange
- **Scroll** to zoom
- **Click** any node to see its full data in a detail panel
- **"Investigate this profile"** button on platform nodes starts a new investigation automatically

Node types: URL, Post, Account, Platforms (Sherlock results), Media, GPS locations, Identity aliases, Dark web hits, Breach records, Following accounts.

---

## Adding a new scraper

1. Create `osint/scrapers/yourplatform.py` extending `BaseScraper`
2. Implement `get_post(post_id, username)` and `get_account(username)`
3. Register in `osint/scrapers/__init__.py`
4. Add URL patterns in `osint/core/url_parser.py`

---

## License

For forensic investigation and security research use only. Respect platform terms of service and applicable law.
