"""
Account history module.
Scrapes recent post timeline for an account, extracts bio links,
and enriches profile data with posting patterns and top posts.
"""
import logging
import re
import time
from typing import List, Dict, Optional, Any
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

WORKING_NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.cz",
    "https://nitter.1d4.us",
]


def get_vxtwitter_profile(username: str) -> Dict:
    """Fetch enriched profile from vxtwitter (no auth, returns real name, location, date)."""
    try:
        r = requests.get(
            f"https://api.vxtwitter.com/{username}",
            headers=HEADERS, timeout=12
        )
        if r.status_code == 200:
            d = r.json()
            created_str = d.get("created_at", "")
            created_iso = None
            try:
                created_iso = datetime.strptime(
                    created_str, "%a %b %d %H:%M:%S +0000 %Y"
                ).replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                created_iso = created_str
            return {
                "display_name": d.get("name"),
                "location": d.get("location"),
                "created_at_iso": created_iso,
                "tweet_count": d.get("tweet_count"),
                "followers_count": d.get("followers_count"),
                "following_count": d.get("following_count"),
                "profile_image_url": d.get("profile_image_url"),
                "user_id": str(d.get("id", "")),
                "protected": d.get("protected", False),
            }
    except Exception as e:
        logger.debug("vxtwitter profile failed for %s: %s", username, e)
    return {}


def get_twitter_timeline_syndication(username: str) -> List[Dict]:
    """Try Twitter embed/syndication endpoints for timeline."""
    posts = []

    # Try the Twitter profile embed which returns embedded tweets JSON
    try:
        r = requests.get(
            f"https://cdn.syndication.twimg.com/timeline/profile?screen_name={username}&count=20",
            headers={**HEADERS, "Referer": "https://twitter.com/"},
            timeout=15
        )
        if r.status_code == 200 and r.text.strip():
            data = r.json()
            for tweet in (data.get("tweets") or data.get("body", {}).get("tweets", []) or [])[:20]:
                if not isinstance(tweet, dict):
                    continue
                posts.append({
                    "post_id": tweet.get("id_str") or tweet.get("id"),
                    "text": tweet.get("full_text") or tweet.get("text", ""),
                    "created_at": tweet.get("created_at"),
                    "likes": tweet.get("favorite_count"),
                    "reposts": tweet.get("retweet_count"),
                    "replies": tweet.get("reply_count"),
                    "views": None,
                    "url": f"https://twitter.com/{username}/status/{tweet.get('id_str') or tweet.get('id')}",
                })
    except Exception as e:
        logger.debug("Syndication timeline failed for %s: %s", username, e)

    return posts


def get_twitter_timeline_nitter(username: str) -> List[Dict]:
    """Scrape recent tweets from a working Nitter instance."""
    for base in WORKING_NITTER_INSTANCES:
        try:
            r = requests.get(f"{base}/{username}", headers=HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            # Check it's actually a Nitter page
            if not soup.select(".timeline-item") and not soup.select(".tweet-content"):
                continue
            posts = []
            for item in soup.select(".timeline-item:not(.show-more)")[:20]:
                text_el = item.select_one(".tweet-content")
                date_el = item.select_one(".tweet-date a")
                link_el = item.select_one(".tweet-link") or date_el

                stats = {}
                for stat in item.select(".tweet-stat"):
                    val_el = stat.select_one(".tweet-stat-count") or stat
                    icon_cls = next((c for c in (stat.select_one("[class*='icon-']") or stat).get("class", []) if "icon-" in c), "")
                    key = icon_cls.replace("icon-", "")
                    try:
                        val = int("".join(filter(str.isdigit, val_el.get_text(strip=True))) or "0")
                    except Exception:
                        val = 0
                    if key:
                        stats[key] = val

                created_at = None
                if date_el and date_el.get("title"):
                    try:
                        created_at = datetime.strptime(
                            date_el["title"], "%b %d, %Y · %I:%M %p UTC"
                        ).replace(tzinfo=timezone.utc).isoformat()
                    except Exception:
                        created_at = date_el.get_text(strip=True)

                tweet_url = None
                if link_el and link_el.get("href"):
                    href = link_el["href"]
                    tweet_url = href if href.startswith("http") else f"https://twitter.com{href}"

                if text_el:
                    posts.append({
                        "text": text_el.get_text(strip=True),
                        "created_at": created_at,
                        "likes": stats.get("heart", 0),
                        "reposts": stats.get("retweet", 0),
                        "replies": stats.get("comment", 0),
                        "views": stats.get("play"),
                        "url": tweet_url,
                    })
            if posts:
                logger.info("Got %d tweets from nitter %s", len(posts), base)
                return posts
        except Exception as e:
            logger.debug("Nitter %s failed: %s", base, e)
    return []


def get_twitter_timeline(username: str) -> List[Dict]:
    """Get timeline — try syndication, then nitter."""
    posts = get_twitter_timeline_syndication(username)
    if not posts:
        posts = get_twitter_timeline_nitter(username)
    return posts


# ── Bio link extraction ───────────────────────────────────────────────────────

PLATFORM_DOMAINS = {
    "twitter.com": "Twitter", "x.com": "Twitter",
    "instagram.com": "Instagram", "facebook.com": "Facebook",
    "t.me": "Telegram", "telegram.me": "Telegram",
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "tiktok.com": "TikTok", "reddit.com": "Reddit",
    "linkedin.com": "LinkedIn", "github.com": "GitHub",
    "medium.com": "Medium", "substack.com": "Substack",
    "linktr.ee": "Linktree", "beacons.ai": "Beacons",
    "discord.gg": "Discord", "discord.com": "Discord",
    "twitch.tv": "Twitch", "mastodon.social": "Mastodon",
    "tumblr.com": "Tumblr", "patreon.com": "Patreon",
    "ko-fi.com": "Ko-fi", "soundcloud.com": "SoundCloud",
    "spotify.com": "Spotify", "vk.com": "VK",
}


def extract_bio_links(bio: str, website: str = None) -> List[Dict]:
    """Extract and classify URLs from bio text and profile website field."""
    links = []
    seen = set()

    urls = re.findall(r'https?://[^\s<>\"\']+', bio or "")
    if website:
        urls.append(website)

    for url in urls:
        url = url.rstrip(".,;)")
        if url in seen:
            continue
        seen.add(url)
        domain = urlparse(url).netloc.lower().lstrip("www.")
        platform = next((name for d, name in PLATFORM_DOMAINS.items() if domain.endswith(d)), None)
        links.append({
            "url": url,
            "domain": domain,
            "platform": platform,
            "is_social": platform is not None,
        })

    return links


def extract_emails_from_text(text: str) -> List[str]:
    """Pull email addresses from any text."""
    return list(set(re.findall(
        r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', text or ""
    )))


def extract_handles_from_bio(bio: str, platform: str) -> List[Dict]:
    """Extract @mentions and platform handles from bio."""
    handles = []
    # @mentions
    for m in re.finditer(r'@([A-Za-z0-9_]{2,50})', bio or ""):
        handle = m.group(1)
        if handle.lower() != platform.lower():
            handles.append({"handle": f"@{handle}", "context": "bio_mention"})
    # Telegram t.me/username
    for m in re.finditer(r't\.me/([A-Za-z0-9_]+)', bio or ""):
        handles.append({"handle": m.group(1), "platform": "Telegram", "url": f"https://t.me/{m.group(1)}"})
    return handles


# ── Posting pattern analysis ──────────────────────────────────────────────────

def analyze_posting_patterns(posts: List[Dict]) -> Dict:
    """Derive behavioral patterns from post timestamps."""
    if not posts:
        return {}

    timestamps = []
    for p in posts:
        ts = p.get("created_at")
        if not ts:
            continue
        try:
            if isinstance(ts, str):
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                dt = ts
            timestamps.append(dt)
        except Exception:
            continue

    if not timestamps:
        return {}

    timestamps.sort(reverse=True)
    hours = [t.hour for t in timestamps]
    hour_counts = {}
    for h in hours:
        hour_counts[h] = hour_counts.get(h, 0) + 1
    peak_hour = max(hour_counts, key=hour_counts.get)

    # Posting frequency
    if len(timestamps) > 1:
        span_days = max(1, (timestamps[0] - timestamps[-1]).days)
        posts_per_day = round(len(timestamps) / span_days, 1)
    else:
        posts_per_day = None

    # Timezone inference from peak posting hour
    # UTC peak 8-12 → likely Europe/Asia, UTC 13-18 → likely Americas
    if 5 <= peak_hour <= 10:
        tz_inference = "Likely posting from Asia/Middle East (UTC+3 to UTC+9)"
    elif 11 <= peak_hour <= 15:
        tz_inference = "Likely posting from Europe/Africa (UTC+0 to UTC+4)"
    elif 16 <= peak_hour <= 22:
        tz_inference = "Likely posting from Americas (UTC-8 to UTC-3)"
    else:
        tz_inference = "Night posting pattern — possible automation"

    # Bot-like cadence check
    if len(timestamps) >= 3:
        intervals = [(timestamps[i] - timestamps[i+1]).total_seconds()
                     for i in range(len(timestamps)-1)]
        very_fast = [iv for iv in intervals if 0 < iv < 60]
        bot_signal = len(very_fast) >= 3
    else:
        bot_signal = False

    return {
        "posts_per_day": posts_per_day,
        "peak_hour_utc": peak_hour,
        "timezone_inference": tz_inference,
        "bot_like_cadence": bot_signal,
        "total_analyzed": len(timestamps),
    }


def get_top_posts(posts: List[Dict], n: int = 5) -> List[Dict]:
    """Return top N posts by engagement (likes + reposts + views/100)."""
    def score(p):
        return (p.get("likes") or 0) + (p.get("reposts") or 0) * 2 + (p.get("views") or 0) // 100
    return sorted(posts, key=score, reverse=True)[:n]


# ── Main orchestrator ─────────────────────────────────────────────────────────

def enrich_account(account_data: dict, platform: str) -> dict:
    """
    Enrich an account dict with vxtwitter profile data, timeline, bio links, patterns.
    Returns a new dict with all added fields.
    """
    enriched = dict(account_data)
    username = account_data.get("username", "")
    bio = account_data.get("bio", "") or ""
    website = account_data.get("website") or ""
    if not isinstance(website, str):
        website = ""

    # ── vxtwitter profile enrichment (real name, location, creation date) ──
    if platform == "twitter" and username:
        vx = get_vxtwitter_profile(username)
        if vx:
            enriched["vx_display_name"] = vx.get("display_name")
            enriched["vx_location"] = vx.get("location")
            enriched["vx_created_at"] = vx.get("created_at_iso")
            enriched["vx_tweet_count"] = vx.get("tweet_count")
            enriched["vx_following_count"] = vx.get("following_count")
            enriched["vx_user_id"] = vx.get("user_id")
            enriched["vx_protected"] = vx.get("protected")
            # Use better profile image if available
            if not enriched.get("profile_image") and vx.get("profile_image_url"):
                enriched["profile_image"] = vx.get("profile_image_url")
            # Merge location and created_at into account data if missing
            if not enriched.get("location") and vx.get("location"):
                enriched["location"] = vx.get("location")
            if not enriched.get("created_at") and vx.get("created_at_iso"):
                enriched["created_at"] = vx.get("created_at_iso")

    # ── Bio link extraction ──
    enriched["bio_links"] = extract_bio_links(bio, website)
    enriched["bio_emails"] = extract_emails_from_text(bio)
    enriched["bio_handles"] = extract_handles_from_bio(bio, username)

    # ── Timeline scraping ──
    recent_posts = []
    if platform == "twitter" and username:
        recent_posts = get_twitter_timeline(username)

    enriched["recent_posts"] = recent_posts
    enriched["top_posts"] = get_top_posts(recent_posts)
    enriched["posting_patterns"] = analyze_posting_patterns(recent_posts)
    enriched["post_count_scraped"] = len(recent_posts)

    return enriched
