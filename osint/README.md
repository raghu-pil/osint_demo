# OSINT Social Media Intelligence Tool

Given a social media URL, produces a structured JSON report covering:

- **Post metadata** — text, media, timestamps, engagement metrics
- **Account profile** — bio, follower counts, account age, linked accounts
- **Cross-platform trace** — reposts on other platforms, earliest archive snapshot
- **Username discovery** — Sherlock-powered search across 400+ networks
- **Dark web intelligence** — Ahmia (Tor index), IntelligenceX, HIBP breach check, paste sites
- **Red-flag analysis** — bot indicators, coordinated behaviour, content risk scoring

---

## Supported platforms

| Platform  | Post scraping | Account scraping | Auth required       |
|-----------|---------------|------------------|---------------------|
| Twitter/X | ✅             | ✅                | Optional Bearer token (falls back to Nitter) |
| Reddit    | ✅             | ✅                | None (public JSON API) |
| Instagram | ⚠️ Limited    | ⚠️ Limited       | Login often needed |
| TikTok    | ✅             | ✅                | None (oEmbed + web) |
| YouTube   | ✅             | ✅                | Optional API key    |

---

## Quick start

```bash
# Install dependencies
pip install -r osint/requirements.txt

# Analyse a tweet
python -m osint.main "https://twitter.com/user/status/123456789"

# With a config file (API keys)
python -m osint.main "https://www.reddit.com/r/news/comments/abc123/" \
    --config osint/config.yaml \
    --output report.json

# Skip slow steps for quick check
python -m osint.main "https://www.tiktok.com/@user/video/123" \
    --skip-sherlock --skip-darkweb
```

---

## Configuration

Copy `osint/config.yaml`, fill in optional API keys:

| Key | Purpose | Where to get |
|-----|---------|--------------|
| `searxng_url` | Self-hosted metasearch for cross-post detection | [SearXNG docs](https://docs.searxng.org) |
| `twitter_bearer_token` | Full Twitter API v2 access | [developer.twitter.com](https://developer.twitter.com) |
| `youtube_api_key` | YouTube Data API v3 | [Google Cloud Console](https://console.developers.google.com) |
| `hibp_api_key` | Have I Been Pwned breach lookups | [haveibeenpwned.com](https://haveibeenpwned.com/API/Key) |
| `intelx_api_key` | IntelligenceX dark web search | [intelx.io](https://intelx.io/account?tab=developer) |

All keys are optional. The tool uses public/unauthenticated endpoints as fallbacks.

---

## Output format

```json
{
  "meta": { "input_url": "...", "platform": "twitter", "generated_at": "..." },
  "risk_assessment": {
    "score": 45,
    "label": "HIGH",
    "total": 6,
    "by_severity": { "high": 2, "medium": 3, "low": 1 },
    "by_category": { "account_age": 1, "behavior": 2, "darkweb": 2, "network": 1 }
  },
  "post": {
    "url": "...", "text": "...", "created_at": "...",
    "engagement": { "likes": 123, "reposts": 45, "replies": 12, "views": 5000 },
    "media": [{ "type": "image", "url": "...", "phash": "...", "reverse_image_search": {...} }]
  },
  "account": {
    "username": "...", "created_at": "...",
    "metrics": { "followers": 999, "following": 10, "post_count": 5 }
  },
  "cross_platform": {
    "cross_posts_found": [...],
    "username_found_on": [{ "platform": "GitHub", "url": "..." }],
    "username_not_found_on": ["LinkedIn", "Pinterest"]
  },
  "dark_web": {
    "hits": [{ "source": "ahmia", "type": "tor_index", "title": "...", "snippet": "..." }],
    "total": 1
  },
  "red_flags": [
    { "severity": "high", "category": "darkweb", "description": "Found in credential breach", "evidence": "..." }
  ],
  "errors": []
}
```

---

## Architecture

```
osint/
├── core/
│   ├── url_parser.py      # Detect platform, extract post_id / username
│   ├── models.py          # Dataclasses: PostData, AccountData, OSINTReport …
│   └── utils.py           # HTTP session, retries, jitter
├── scrapers/
│   ├── twitter.py         # API v2 → syndication API → Nitter fallback
│   ├── reddit.py          # Public JSON API
│   ├── instagram.py       # oEmbed + web JSON
│   ├── tiktok.py          # oEmbed + web JSON blob
│   └── youtube.py         # oEmbed + Data API v3
├── intelligence/
│   ├── searxng.py         # SearXNG metasearch integration
│   ├── sherlock_runner.py # Sherlock CLI wrapper + built-in probe fallback
│   ├── crosspost.py       # pHash, archive.org, URL mention search
│   └── darkweb.py         # Ahmia, IntelX, HIBP, paste sites
├── analysis/
│   ├── account.py         # Bot detection, account-age flags, cadence analysis
│   ├── content.py         # Keyword flagging, language detection
│   └── redflags.py        # Risk scoring aggregation
├── output/
│   └── formatter.py       # OSINTReport → JSON
└── main.py                # CLI entry point + orchestrator
```

---

## Legal notice

This tool is intended for **lawful OSINT research only** — journalism, security research, law enforcement, academic study. Do not use it to harass, stalk, or harm individuals. Comply with the terms of service of each platform and with applicable laws.
