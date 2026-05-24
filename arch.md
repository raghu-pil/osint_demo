# Authintify — Architecture & Flow Reference

> Last updated: May 2026  
> Goal: Given a URL / video / photo / audio → maximum intel on the content and the person who posted it.
>
> **Name note:** This tool is **authintify**. The "Authentify" tab integrates with
> a separate external tool called **authentify** via API call. The name difference is intentional.

---

## Table of Contents
1. [System Overview](#1-system-overview)
2. [Input Modes](#2-input-modes)
3. [URL Investigation Pipeline (11 steps)](#3-url-investigation-pipeline)
4. [Media-First Investigation Pipeline](#4-media-first-investigation-pipeline)
5. [All External API Calls](#5-all-external-api-calls)
6. [Tab-by-Tab: What Feeds Each UI Tab](#6-tab-by-tab-data-sources)
7. [Data Storage](#7-data-storage)
8. [Config & Feature Flags](#8-config--feature-flags)

---

## 1. System Overview

```
User Browser
    │
    ├── GET /           → frontend/index.html  (single-page app, no build step)
    ├── POST /api/cases → start URL investigation
    ├── POST /api/media-cases → start media investigation
    └── GET /api/cases/{id} → poll for results (frontend polls every 2s)

FastAPI Server  (backend/main.py → uvicorn)
    ├── backend/routes/cases.py   — HTTP routing
    ├── backend/pipeline.py       — URL investigation orchestrator
    ├── backend/routes/cases.py   — Media investigation orchestrator (_run_media_pipeline)
    └── backend/modules/          — Individual analysis modules
         ├── account_history.py
         ├── auto_actions.py
         ├── darkweb_enhanced.py
         ├── guidance.py
         ├── identity.py
         ├── image_analysis.py
         ├── known_accounts.py
         ├── media.py
         ├── media_pipeline.py
         ├── post_analysis.py
         ├── reverse_search.py
         └── web_scraper.py

osint/  (core scraping library)
    ├── scrapers/     — platform scrapers (twitter, instagram, reddit, tiktok, youtube)
    ├── intelligence/ — sherlock, crosspost detection, dark web, searxng
    ├── analysis/     — red flags, account analysis, content analysis
    ├── core/         — url parser, http utils, data models, NER (spaCy)
    └── output/       — JSON formatter

Storage: cases/{case_id}/
    ├── case.json      — full case data (all pipeline results)
    ├── media/         — downloaded images/videos/audio
    └── keyframes/     — extracted video keyframes
```

---

## 2. Input Modes

| Mode | Entry Point | Trigger |
|------|-------------|---------|
| **URL Investigation** | Paste a social media or web URL | `POST /api/cases` → `run_pipeline()` |
| **Media Upload** | Drag-drop image/video file | `POST /api/media-cases` → `_run_media_pipeline()` |
| **Reverse Search on demand** | Click "Reverse Search" button on a media item | `POST /api/cases/{id}/reverse-search` |

Supported URL platforms: Twitter/X, Instagram, Reddit, TikTok, YouTube, Telegram, generic web pages.

---

## 3. URL Investigation Pipeline

Defined in `backend/pipeline.py`. Runs as a FastAPI `BackgroundTask`.  
The frontend polls `GET /api/cases/{id}` every 2 seconds to stream progress.

```
POST /api/cases  →  create case (status: pending)  →  background: run_pipeline()
```

### Step 1 — URL Parse
**File:** `osint/core/url_parser.py`  
Extracts platform, post ID, username from the URL.  
No external calls. Pure regex + tldextract.

---

### Step 2 — Post Scrape
**File:** `osint/main.py` → `osint/scrapers/{platform}.py`

**Twitter/X** — tries sources in order until one succeeds:
1. `api.twitter.com/2/tweets/{id}` — official API *(needs `twitter_bearer_token`)*
2. `api.fxtwitter.com/{user}/status/{id}` — free, no auth, best fallback
3. `api.vxtwitter.com/{user}/status/{id}` — free, no auth
4. `cdn.syndication.twimg.com/tweet-result?id={id}` — free, Twitter's embed API
5. Nitter instances (3–4 tried) — free, scraping

**Instagram** — scrapes `instagram.com/{shortcode}/?__a=1` (unofficial, breaks often)  
**Reddit** — `reddit.com/{url}.json` (free, no auth)  
**TikTok** — scrapes page HTML, parses `__UNIVERSAL_DATA_FOR_REHYDRATION__` JSON  
**YouTube** — YouTube Data API v3 *(needs `youtube_api_key`)* or HTML scrape fallback  
**Telegram** — scrapes `t.me/{channel}/{id}?embed=1`  
**Generic web page** — `backend/modules/web_scraper.py` — scrapes title, OG tags, article text, lead image

**Returns:** post text, engagement (likes/shares/views), author username, media URLs, hashtags, embedded URLs.

---

### Step 2b — LLM Intelligence Summary *(optional)*
**File:** `backend/modules/post_analysis.py`  
**External call:** Anthropic Claude API (`claude-sonnet-4-6`) — content analysis, authenticity assessment  
**Requires:** `anthropic_api_key` in config  
**Cost:** ~$0.003 per post (Sonnet pricing)

---

### Step 3 — Account Profile
**File:** `osint/scrapers/{platform}.py`  
Uses same scraper as Step 2. Extracts: username, display name, bio, follower/following counts, verified status, location, account creation date, profile photo.

---

### Step 3b — Account History & Bio Enrichment
**File:** `backend/modules/account_history.py`

| Call | URL | Auth | Cost |
|------|-----|------|------|
| vxtwitter account details | `api.vxtwitter.com/{username}` | None | Free |
| vxtwitter timeline | `api.vxtwitter.com/{username}/timeline` | None | Free |
| Nitter timeline (fallback) | `{nitter_instance}/{username}` | None | Free |

**Also:** extracts emails, phone numbers, handles from bio text using regex + spaCy NER.  
**Returns:** post count, posting time patterns, recent posts, top posts, bio links.

---

### Step 4 — Cross-Post Detection
**File:** `osint/intelligence/crosspost.py`

| Call | URL | Auth | Cost |
|------|-----|------|------|
| Wayback Machine CDX API | `web.archive.org/cdx/search/cdx?url={url}` | None | Free |
| SearXNG metasearch | `{searxng_url}/search?q={text}` | None | Free (self-hosted) |
| pHash visual similarity | local comparison of downloaded images | None | Free |

Searches for the same content (text quote, URL, or image hash) on other platforms.

---

### Step 5 — Username Enumeration (Sherlock)
**File:** `osint/intelligence/sherlock_runner.py`  
Runs `sherlock-project` CLI against 400+ platform endpoints.  
All calls are plain HTTPS GETs to public profile URLs — no API keys.  
Takes 60–90 seconds. Controlled by `sherlock_timeout` config.  
Can be disabled with `skip_sherlock: true`.

---

### Step 6 — Dark Web & Breach Intelligence
**File:** `backend/modules/darkweb_enhanced.py`

| Source | URL | Auth | Limit | Cost |
|--------|-----|------|-------|------|
| BreachDirectory | `breachdirectory.org/api?func=auto&term={q}` | None | Unknown | **Free** |
| DarkSearch.io | `darksearch.io/api/search?query={q}` | Optional API key | 10/day free | **Free** / paid removes limit |
| Ahmia.fi | `ahmia.fi/search/?q={q}` | None | None | **Free** |
| IntelligenceX | `2.intelx.io/intelligent/search` | Public demo key built-in | Rate limited | **Free** (limited) / paid |
| LeakCheck.io | `leakcheck.io/api/public?type=email&query={q}` | None | 1/day | **Free** (very limited) |
| HIBP breaches | `haveibeenpwned.com/api/v3/breachedaccount/{email}` | `hibp-api-key` header | None | **$3.50/month** |
| HIBP pastes | `haveibeenpwned.com/api/v3/pasteaccount/{email}` | `hibp-api-key` header | None | **$3.50/month** |
| Dehashed | `api.dehashed.com/search?query={type}:{q}` | Email + API key (Basic Auth) | None | **$5/month** |
| Torch (.onion) | `xmh57jr...onion/search.cgi?q={q}` | None | Tor required | **Free** |
| Tor66 (.onion) | `tor66se...onion/search?q={q}` | None | Tor required | **Free** |
| Ahmia (.onion) | `juhanum...onion/search/?q={q}` | None | Tor required | **Free** |
| DDG (.onion) | via Tor | None | Tor required | **Free** |

**Note:** Tor calls only happen if `tor` is running locally (`socks5h://127.0.0.1:9050`).  
Queries run for: username, email (if found), display name.

---

### Step 7 — Media Download & EXIF
**File:** `backend/modules/media.py`  
Downloads all media URLs found in the post using `yt-dlp` (video) or direct HTTPS (images).

**Local processing only (no external calls):**
- SHA256 + MD5 hash of each file
- EXIF extraction via Pillow (camera make/model, datetime, GPS coordinates)
- Video metadata via `ffprobe` (codec, duration, resolution)
- GPS → human address via `geopy` / Nominatim (OpenStreetMap, free)

**Image upload for reverse search:**  
Tries hosts in order until one succeeds: Catbox.moe → Imgur → freeimage.host → tmpfiles.org → gofile.io  
All free, no API keys.

---

### Step 8 — Identity Pivots
**File:** `backend/modules/identity.py`

Extracts identifiers (emails, phones, usernames) from bio + post text, then runs:

| Call | URL | Auth | Cost |
|------|-----|------|------|
| Gravatar profile | `gravatar.com/{md5(email)}.json` | None | **Free** |
| Gravatar avatar | `gravatar.com/avatar/{md5(email)}?d=404` | None | **Free** |
| HIBP breaches | `haveibeenpwned.com/api/v3/breachedaccount/{email}` | API key | **$3.50/month** |
| EmailRep.io | `emailrep.io/{email}` | Optional key | **Free** (limited) |
| Phone validation | local via `phonenumbers` library | None | **Free** |
| Username probe (20 sites) | HEAD requests to profile URLs | None | **Free** |

---

### Step 9 — Analyst Guidance Generation
**File:** `backend/modules/guidance.py`  
Pure local logic — no external calls.  
Weights all findings from previous steps and generates prioritized leads:
- Account age < 7 days → CRITICAL
- Follower ratio anomalies → HIGH
- Breach history → HIGH
- Known misinfo spreader match → CRITICAL
- GPS coordinates found → HIGH
- No EXIF (stripped) → MEDIUM

---

### Step 10 — Auto Investigations
**File:** `backend/modules/auto_actions.py`  
Automatically runs actions that would otherwise be manual:

| Action | Call | Auth | Cost |
|--------|------|------|------|
| Linktree scrape | `linktr.ee/{username}` (parses `__NEXT_DATA__` JSON) | None | **Free** |
| TikTok profile | `tiktok.com/@{username}` (parses `__UNIVERSAL_DATA_FOR_REHYDRATION__`) | None | **Free** |
| Blogger profile | `{username}.blogspot.com` | None | **Free** |
| Twitter following list | Nitter `/following` page (3–4 instances tried) | None | **Free** |
| Yandex reverse image | `yandex.com/images/search?rpt=imageview&url={url}` | None | **Free** (CAPTCHA risk) |
| SerpAPI reverse image | via SerpAPI SDK | `serpapi_api_key` | 100/month free, then paid |
| Shortened URL expand | HEAD request chain following | None | **Free** |

---

## 4. Media-First Investigation Pipeline

Triggered by: `POST /api/media-cases` (file upload).  
Orchestrated in: `backend/routes/cases.py → _run_media_pipeline()`

```
Upload file
    │
    ├── If video: extract 8 keyframes (ffmpeg) → user picks frame
    │
    ├── Step 1: Reverse Search + Known Account Check
    │   └── backend/modules/media_pipeline.py → run_media_investigation()
    │       ├── Upload image to public host (Catbox → Imgur → freeimage → tmpfiles → gofile)
    │       ├── SerpAPI Google Lens (needs serpapi_api_key)
    │       ├── SerpAPI Yandex Images (needs serpapi_api_key)
    │       ├── Claude vision analysis (needs anthropic_api_key) → deepfake detection, context
    │       ├── OCR → extract text → SearXNG / web search for context
    │       ├── Context search: "original source" + "doctored" queries
    │       └── Known account scoring (backend/modules/known_accounts.py)
    │
    └── Step 2: Generate Guidance
        └── backend/modules/guidance.py
            └── Rank discovered accounts by severity score
```

**Discovered accounts are ranked by:**
- Account creation date (new = higher risk)
- Follower count (very low = suspicious)
- Match in known misinformation spreaders database
- Whether account has been in breaches
- Whether it's the earliest known appearance

---

## 5. All External API Calls

### Summary Table — Grouped by Cost

#### Free, No Key Required

| Service | What it does | Rate limit | Module |
|---------|-------------|------------|--------|
| fxtwitter API | Twitter post data (best free fallback) | Generous | `scrapers/twitter.py` |
| vxtwitter API | Twitter post + account data | Generous | `scrapers/twitter.py` |
| Twitter syndication CDN | Tweet embed data | Unknown | `scrapers/twitter.py` |
| Nitter instances (3–4) | Twitter profile + timeline scraping | Per-instance | `scrapers/twitter.py`, `auto_actions.py` |
| Reddit JSON API | Reddit post + comments | 60/min | `scrapers/reddit.py` |
| TikTok HTML scrape | TikTok post + profile | Aggressive blocking | `scrapers/tiktok.py` |
| Instagram HTML scrape | Instagram post | Aggressive blocking | `scrapers/instagram.py` |
| Telegram embed | Telegram post | Unknown | `scrapers/` |
| Wayback Machine CDX | Historical URL appearances | None stated | `intelligence/crosspost.py` |
| BreachDirectory | Leaked credentials lookup | Unknown | `darkweb_enhanced.py` |
| DarkSearch.io | Dark web search | **10/day** | `darkweb_enhanced.py` |
| Ahmia.fi | Tor index search (clearnet) | None stated | `darkweb_enhanced.py` |
| IntelligenceX (demo key) | Paste + dark web search | Rate limited | `darkweb_enhanced.py` |
| LeakCheck.io | Email breach check | **1/day** | `darkweb_enhanced.py` |
| Gravatar | Email → profile photo + name | None | `identity.py` |
| EmailRep.io | Email reputation | 10/day without key | `identity.py` |
| Nominatim (OpenStreetMap) | GPS → address (reverse geocode) | 1/second | `media.py` |
| Sherlock (400+ platforms) | Username enumeration | None (HEAD requests) | `sherlock_runner.py` |
| Linktree | Bio link scraping | None | `auto_actions.py` |
| TikTok profile (direct) | Bio, followers, bio link | Blocking risk | `auto_actions.py` |
| Nitter following | Twitter following list | Per-instance | `auto_actions.py` |
| Yandex reverse image | Visual similarity search | CAPTCHA after ~5 | `auto_actions.py` |
| Image upload hosts | Make local file publicly accessible for reverse search | None | `reverse_search.py` |
| Catbox.moe | Image hosting for reverse search | None stated | `reverse_search.py` |
| Imgur (anonymous) | Image hosting fallback | 50 uploads/day | `reverse_search.py` |
| freeimage.host | Image hosting fallback | Unknown | `reverse_search.py` |
| tmpfiles.org | Image hosting fallback | Unknown | `reverse_search.py` |
| gofile.io | Image hosting fallback | Unknown | `reverse_search.py` |

#### Free with API Key

| Service | Key name in config | What it does | Free tier |
|---------|-------------------|-------------|-----------|
| Twitter/X API v2 | `twitter_bearer_token` | Official tweet data (more reliable than fxtwitter) | 500k tweets/month (Basic) |
| YouTube Data API v3 | `youtube_api_key` | Video metadata, channel info | 10,000 units/day |
| SearXNG (self-hosted) | `searxng_url` | Metasearch for cross-post detection | Unlimited (your server) |

#### Paid / Paid Tier Needed

| Service | Key name in config | What it does | Cost |
|---------|-------------------|-------------|------|
| SerpAPI | `serpapi_api_key` | Google Lens + Yandex reverse image search | **100/month free**, then $50/month |
| Anthropic Claude API | `anthropic_api_key` | Image analysis, deepfake detection, content intelligence | **~$0.003/post** (Sonnet) |
| Have I Been Pwned | `hibp_api_key` | Email breach history | **$3.50/month** |
| Dehashed | `dehashed_api_key` + `dehashed_email` | Full breach records (name, address, phone, password) | **$5/month** |
| IntelligenceX (paid) | `intelx_api_key` | Expanded paste + dark web search | **$300+/year** |
| DarkSearch.io (paid) | `darksearch_api_key` | Remove 10/day rate limit | Unknown |

#### Requires Tor (Optional)

| Service | Type | What it does |
|---------|------|-------------|
| Torch (.onion) | Dark web search engine | Direct .onion search |
| Tor66 (.onion) | Dark web search engine | Direct .onion search |
| Ahmia (.onion) | Tor index | Via Tor for anonymity |
| DuckDuckGo (.onion) | Search engine | Via Tor for anonymity |

**Setup:** `apt install tor && service tor start` — tool auto-detects if Tor is running on `127.0.0.1:9050`.

---

## 6. Tab-by-Tab Data Sources

### Tab: Guidance
**File:** `backend/modules/guidance.py`  
**Data from:** All pipeline steps (aggregated)  
**External calls:** None — pure local scoring of all collected data  
Shows: prioritized leads ranked by severity (CRITICAL / HIGH / MEDIUM / LOW), suggested next actions, pivot links.

---

### Tab: Post
**Data from:** Step 2 (post scrape)  
**External calls:** fxtwitter / vxtwitter / syndication / Nitter / platform scrapers  
Shows: post text, engagement stats (likes, shares, views, replies), hashtags, mentioned accounts, embedded URLs, post date.

---

### Tab: Account
**Data from:** Step 3 (account profile) + Step 3b (account history)  
**External calls:** vxtwitter account endpoint, Nitter, platform scraper  
Shows: username, display name, bio, followers/following, verified badge, location, account creation date, profile photo.

---

### Tab: Timeline
**Data from:** Step 3b (account history/enrichment)  
**External calls:** `api.vxtwitter.com/{username}/timeline`, Nitter timeline pages  
Shows: recent posts, top posts, posting frequency, timezone inference, bio links extracted by NER.

---

### Tab: Cross-Posts
**Data from:** Step 4 (cross-post detection)  
**External calls:** Wayback Machine CDX API, SearXNG (if configured), pHash local comparison  
Shows: other platforms where this content appeared, URL, post date, similarity score.

---

### Tab: Username Search
**Data from:** Step 5 (Sherlock)  
**External calls:** HEAD requests to 400+ platform profile URLs (no API keys)  
Shows: table of platforms where this username exists, with direct profile URLs.

---

### Tab: Dark Web
**Data from:** Step 6 (dark web & breach intelligence)  
**External calls:** BreachDirectory, DarkSearch, Ahmia, IntelligenceX, LeakCheck, HIBP (if key), Dehashed (if key), .onion engines (if Tor)  
Shows: breach hits, paste site appearances, dark web page results — each with source, type, title, URL, snippet.

---

### Tab: Media
**Data from:** Step 7 (media download + EXIF)  
**External calls:** yt-dlp for download, Nominatim for GPS reverse geocode, image upload hosts + SerpAPI for reverse search  
Shows: downloaded file grid — filename, SHA256/MD5 hash, file size, GPS coordinates (clickable map), camera model, capture timestamp, OCR extracted text, reverse image search results.

---

### Tab: Identity
**Data from:** Step 8 (identity pivots)  
**External calls:** Gravatar, HIBP, EmailRep.io, username HEAD probes (20 sites)  
Shows: Gravatar profile (photo, name, location), HIBP breach list with breach names/dates/data types, EmailRep reputation score, platform registration results.

---

### Tab: Red Flags
**Data from:** Steps 2–8 (all steps)  
**External calls:** None — aggregated from all previous results  
Shows: scored risk indicators — account age, follower ratio, breach history, suspicious posting patterns, metadata anomalies.

---

### Tab: Network Graph
**Data from:** All pipeline steps  
**External calls:** None — renders from cached case.json  
Shows: interactive force-directed graph. Node types:
- URL → Post → Account
- Account → Platforms (Sherlock hits)
- Post → Media files → GPS locations
- Account → Identity (emails/phones) → Breach records
- Cross-posts on other platforms

---

### Tab: Authentify
**Data from:** Currently a placeholder (`sample_report.pdf`)  
**Should be:** Dynamic PDF generated from case.json via reportlab  
**External calls:** None (report generation is local)

---

## 7. Data Storage

All data is stored locally. No cloud sync.

```
cases/
└── {case_id}/          # 12-char hex UUID
    ├── case.json       # Full case: post, account, all results, step statuses
    ├── media/          # Downloaded images/videos
    │   └── {filename}
    └── keyframes/      # Extracted video frames
        └── {video_stem}/
            └── kf_000.jpg ... kf_007.jpg
```

`case.json` structure (key fields):
```
{
  id, url, name, notes, status, platform,
  created_at, updated_at,
  steps: [{name, status, label, started_at, completed_at, message}],
  post: {text, likes, shares, author_username, media_urls, ...},
  account: {username, bio, followers, created_at, ...},
  account_enrichment: {recent_posts, posting_patterns, bio_links, ...},
  cross_posts: [{platform, url, date, similarity}],
  username_search: [{platform, url, status}],
  dark_web: {hits: [], manual_searches: [], sources_checked: []},
  media_files: [{filename, hash_sha256, hash_md5, gps, exif, ocr_text, ...}],
  identity_pivots: [{identifier, type, hibp_breaches, gravatar_*, platforms_found}],
  guidance: [{priority, severity, title, detail, action, pivot_url}],
  red_flags: [{flag, severity, detail}],
  post_intelligence: {assessment, summary, ...},  // Claude analysis
  risk_score: 0-100,
  risk_label: MINIMAL|LOW|MEDIUM|HIGH|CRITICAL,
  errors: [],
  logs: [{ts, msg, level}]
}
```

---

## 8. Config & Feature Flags

**File:** `config.yaml`

| Key | Default | Effect |
|-----|---------|--------|
| `twitter_bearer_token` | `""` | Unlocks official Twitter API (more reliable) |
| `youtube_api_key` | `""` | Unlocks YouTube Data API v3 |
| `anthropic_api_key` | `""` | Unlocks Claude image analysis + post intelligence |
| `serpapi_api_key` | `""` | Required for reverse image search (Google Lens + Yandex) |
| `hibp_api_key` | `""` | Unlocks HIBP email breach lookup |
| `dehashed_api_key` | `""` | Unlocks Dehashed full breach records |
| `dehashed_email` | `""` | Required with dehashed_api_key |
| `intelx_api_key` | `""` | Upgrades IntelligenceX from demo key to full |
| `darksearch_api_key` | `""` | Removes DarkSearch 10/day limit |
| `searxng_url` | `""` | Unlocks metasearch for cross-post detection |
| `nitter_instance` | `""` | Preferred Nitter instance (falls back to public list) |
| `skip_sherlock` | `false` | Skip username enumeration (saves 60–90s) |
| `skip_darkweb` | `false` | Skip all dark web / breach queries |
| `skip_crossposts` | `false` | Skip Wayback + SearXNG cross-post search |
| `skip_media_download` | `false` | Skip media download + EXIF extraction |
| `sherlock_timeout` | `60` | Seconds before Sherlock is killed |
| `llm.enabled` | `false` | Enable LLM analysis (anthropic / openai / ollama) |
| `llm.provider` | `anthropic` | Which LLM backend to use |

---

## Minimum Viable Config (free, no keys)

The tool runs with zero API keys using:
- fxtwitter/vxtwitter for Twitter data
- Sherlock for username enumeration  
- BreachDirectory + DarkSearch (10/day) + Ahmia for dark web
- Gravatar for email identity
- Yandex direct scrape for reverse image (CAPTCHA risk)

**Recommended minimum paid setup ($9/month total):**
- `serpapi_api_key` — reliable reverse image search (Google Lens + Yandex)
- `hibp_api_key` — reliable breach history ($3.50/month)
- `anthropic_api_key` — image analysis + deepfake detection (~$5/month at typical volume)
