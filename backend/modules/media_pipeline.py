"""
Media-first investigation pipeline.

Upload an image/video → reverse search via SerpAPI → find all social media
accounts sharing the content → scrape each account → rank by investigative
severity → return ranked leads.

Severity scoring (0-100, higher = investigate first):
  +30  earliest known appearance (likely the source)
  +25  account created < 6 months ago
  +15  account created < 2 years ago
  +20  very low followers (< 500) — small account with viral content
  +10  low followers (< 5000)
  -15  large established account (> 500k followers)
  +15  anonymous (no bio / display name)
  -10  verified account
  +10  same image appeared on multiple platforms from same user
"""
import logging
import re
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Dict, List, Optional, Any
from urllib.parse import urlparse, parse_qs

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Social media URL parser ───────────────────────────────────────────────────

def parse_social_url(url: str) -> Optional[Dict]:
    """
    Parse a URL and return structured social media info.
    Returns None if the URL is not a recognised social media URL.
    """
    try:
        p = urlparse(url)
        host = p.netloc.lower().replace("www.", "").replace("m.", "")
        path = p.path.strip("/")
        parts = [s for s in path.split("/") if s]
    except Exception:
        return None

    if not parts:
        return None

    # Twitter / X
    if host in ("twitter.com", "x.com"):
        username = parts[0] if parts else None
        if username and username.startswith("@"):
            username = username[1:]
        if not username or username in ("search", "i", "explore", "settings", "notifications"):
            return None
        post_id = parts[2] if len(parts) >= 3 and parts[1] == "status" else None
        return {
            "platform": "twitter",
            "username": username,
            "account_url": f"https://twitter.com/{username}",
            "post_url": f"https://twitter.com/{username}/status/{post_id}" if post_id else None,
            "post_id": post_id,
        }

    # Instagram
    if host == "instagram.com":
        if parts[0] == "p" and len(parts) >= 2:
            shortcode = parts[1]
            return {"platform": "instagram", "username": None,
                    "account_url": None, "post_url": f"https://instagram.com/p/{shortcode}", "post_id": shortcode}
        if parts[0] == "reel" and len(parts) >= 2:
            return {"platform": "instagram", "username": None,
                    "account_url": None, "post_url": f"https://instagram.com/reel/{parts[1]}", "post_id": parts[1]}
        username = parts[0]
        if username in ("explore", "accounts", "stories"):
            return None
        return {"platform": "instagram", "username": username,
                "account_url": f"https://instagram.com/{username}", "post_url": None, "post_id": None}

    # YouTube
    if host in ("youtube.com", "youtu.be"):
        vid = None
        channel = None
        if host == "youtu.be" and parts:
            vid = parts[0]
        elif "v" in parse_qs(p.query):
            vid = parse_qs(p.query)["v"][0]
        elif parts and parts[0] == "shorts" and len(parts) >= 2:
            vid = parts[1]
        elif parts and parts[0] in ("channel", "c", "user") and len(parts) >= 2:
            channel = parts[1]
        elif parts and parts[0].startswith("@"):
            channel = parts[0][1:]
        if not vid and not channel:
            return None
        return {"platform": "youtube", "username": channel,
                "account_url": f"https://youtube.com/@{channel}" if channel else None,
                "post_url": f"https://youtube.com/watch?v={vid}" if vid else None,
                "post_id": vid or channel}

    # Reddit
    if host == "reddit.com":
        if parts and parts[0] in ("u", "user") and len(parts) >= 2:
            user = parts[1]
            return {"platform": "reddit", "username": user,
                    "account_url": f"https://reddit.com/u/{user}", "post_url": None, "post_id": None}
        if parts and parts[0] == "r" and len(parts) >= 4 and parts[2] == "comments":
            post_id = parts[3]
            subreddit = parts[1]
            return {"platform": "reddit", "username": None,
                    "account_url": f"https://reddit.com/r/{subreddit}",
                    "post_url": f"https://reddit.com/r/{subreddit}/comments/{post_id}/",
                    "post_id": post_id, "subreddit": subreddit}
        return None

    # TikTok
    if host == "tiktok.com":
        username = None
        for part in parts:
            if part.startswith("@"):
                username = part[1:]
                break
        if not username:
            return None
        post_id = parts[-1] if "video" in parts else None
        return {"platform": "tiktok", "username": username,
                "account_url": f"https://tiktok.com/@{username}",
                "post_url": f"https://tiktok.com/@{username}/video/{post_id}" if post_id else None,
                "post_id": post_id}

    # Facebook
    if host in ("facebook.com", "fb.com", "fb.watch"):
        page = parts[0] if parts else None
        if page and page not in ("watch", "groups", "events", "marketplace", "photo"):
            return {"platform": "facebook", "username": page,
                    "account_url": f"https://facebook.com/{page}",
                    "post_url": url if "posts" in parts or "videos" in parts else None,
                    "post_id": None}
        return None

    return None


# ── Per-platform account scrapers ─────────────────────────────────────────────

def scrape_twitter_account(username: str, post_url: Optional[str] = None) -> Dict:
    """Scrape Twitter account via vxtwitter/fxtwitter."""
    info = {"platform": "twitter", "username": username,
            "account_url": f"https://twitter.com/{username}",
            "display_name": None, "bio": None, "avatar": None,
            "followers": None, "created_at": None, "verified": False,
            "post_text": None, "post_date": None}
    try:
        r = requests.get(f"https://api.vxtwitter.com/{username}", headers=HEADERS, timeout=15)
        if r.status_code == 200:
            d = r.json()
            info["display_name"] = d.get("displayName") or d.get("name")
            info["bio"] = d.get("description") or d.get("desc")
            info["avatar"] = d.get("profilePicture") or d.get("user_avatar")
            info["followers"] = d.get("tweetCount") and None  # vxtwitter doesn't always return followers
            info["created_at"] = d.get("created")
    except Exception as e:
        logger.debug("vxtwitter scrape failed for %s: %s", username, e)

    # Try to get post details if we have a specific post URL
    if post_url:
        try:
            # Extract post ID from URL
            m = re.search(r'/status/(\d+)', post_url)
            if m:
                post_id = m.group(1)
                r2 = requests.get(f"https://api.fxtwitter.com/{username}/status/{post_id}",
                                  headers=HEADERS, timeout=15)
                if r2.status_code == 200:
                    d2 = r2.json().get("tweet", {})
                    info["post_text"] = d2.get("text", "")[:300]
                    info["post_date"] = d2.get("created_at", "")
                    info["likes"] = d2.get("likes")
                    info["reposts"] = d2.get("retweets")
                    info["views"] = d2.get("views")
                    # Get author info from post
                    author = d2.get("author", {})
                    if not info["display_name"]:
                        info["display_name"] = author.get("name")
                    if not info["avatar"]:
                        info["avatar"] = author.get("avatar_url")
                    info["followers"] = author.get("followers")
                    info["verified"] = author.get("verified", False)
                    info["created_at"] = author.get("created_at")
        except Exception as e:
            logger.debug("fxtwitter post scrape failed: %s", e)

    return info


def scrape_reddit_account(username: str = None, post_url: str = None,
                          subreddit: str = None, post_id: str = None) -> Dict:
    """Scrape Reddit user or post via Reddit JSON API."""
    info = {"platform": "reddit", "username": username,
            "account_url": f"https://reddit.com/u/{username}" if username else None,
            "display_name": username, "bio": None, "avatar": None,
            "followers": None, "created_at": None, "verified": False,
            "post_text": None, "post_date": None}
    try:
        if post_id and subreddit:
            r = requests.get(
                f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json",
                headers={**HEADERS, "Accept": "application/json"}, timeout=15
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    post_data = data[0].get("data", {}).get("children", [{}])[0].get("data", {})
                    info["username"] = post_data.get("author", username)
                    info["account_url"] = f"https://reddit.com/u/{info['username']}"
                    info["display_name"] = info["username"]
                    info["post_text"] = (post_data.get("selftext") or post_data.get("title", ""))[:300]
                    info["post_date"] = datetime.fromtimestamp(
                        post_data.get("created_utc", 0), tz=timezone.utc
                    ).isoformat() if post_data.get("created_utc") else None
                    info["likes"] = post_data.get("score")
                    info["post_url"] = f"https://reddit.com{post_data.get('permalink', '')}"
                    username = info["username"]

        if username and username not in ("[deleted]", "AutoModerator"):
            r2 = requests.get(
                f"https://www.reddit.com/u/{username}/about.json",
                headers={**HEADERS, "Accept": "application/json"}, timeout=15
            )
            if r2.status_code == 200:
                d = r2.json().get("data", {})
                info["followers"] = d.get("total_karma")
                info["created_at"] = datetime.fromtimestamp(
                    d.get("created_utc", 0), tz=timezone.utc
                ).isoformat() if d.get("created_utc") else None
                info["avatar"] = d.get("icon_img") or d.get("snoovatar_img")
                info["bio"] = d.get("subreddit", {}).get("public_description")
                info["verified"] = d.get("verified", False)
    except Exception as e:
        logger.debug("Reddit scrape failed: %s", e)
    return info


def scrape_youtube(video_id: str = None, channel: str = None, url: str = None) -> Dict:
    """Scrape YouTube via OEmbed and page meta."""
    info = {"platform": "youtube", "username": channel,
            "account_url": f"https://youtube.com/@{channel}" if channel else url,
            "display_name": None, "bio": None, "avatar": None,
            "followers": None, "created_at": None, "verified": False,
            "post_text": None, "post_date": None}
    try:
        target = url or (f"https://youtube.com/watch?v={video_id}" if video_id else f"https://youtube.com/@{channel}")
        r = requests.get(
            f"https://www.youtube.com/oembed?url={target}&format=json",
            headers=HEADERS, timeout=15
        )
        if r.status_code == 200:
            d = r.json()
            info["display_name"] = d.get("author_name")
            info["post_text"] = d.get("title")
            info["avatar"] = d.get("thumbnail_url")
            info["account_url"] = d.get("author_url") or info["account_url"]
    except Exception as e:
        logger.debug("YouTube OEmbed failed: %s", e)
    return info


def scrape_generic(url: str, platform: str = "web", username: str = None) -> Dict:
    """Scrape basic page metadata for any URL."""
    info = {"platform": platform, "username": username,
            "account_url": url, "post_url": url,
            "display_name": None, "bio": None, "avatar": None,
            "followers": None, "created_at": None, "verified": False,
            "post_text": None, "post_date": None}
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if r.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "lxml")
            title = soup.find("title")
            og_title = soup.find("meta", property="og:title")
            og_desc = soup.find("meta", property="og:description")
            og_img = soup.find("meta", property="og:image")
            info["display_name"] = (og_title and og_title.get("content")) or (title and title.get_text()[:80])
            info["bio"] = og_desc and og_desc.get("content", "")[:200]
            info["avatar"] = og_img and og_img.get("content")
    except Exception as e:
        logger.debug("Generic scrape failed for %s: %s", url, e)
    return info


def scrape_account(parsed: Dict) -> Dict:
    """Dispatch to the right scraper based on platform."""
    platform = parsed.get("platform", "web")
    username = parsed.get("username")
    post_url = parsed.get("post_url")

    if platform == "twitter":
        return scrape_twitter_account(username, post_url)
    elif platform == "reddit":
        return scrape_reddit_account(
            username=username,
            post_url=post_url,
            subreddit=parsed.get("subreddit"),
            post_id=parsed.get("post_id"),
        )
    elif platform == "youtube":
        return scrape_youtube(
            video_id=parsed.get("post_id") if not parsed.get("username") else None,
            channel=parsed.get("username"),
            url=post_url or parsed.get("account_url"),
        )
    else:
        return scrape_generic(
            url=post_url or parsed.get("account_url", ""),
            platform=platform,
            username=username,
        )


# ── Severity scoring ──────────────────────────────────────────────────────────

def score_account(account: Dict, all_accounts: List[Dict], earliest_date: Optional[str]) -> Dict:
    """Compute 0-100 severity score. Higher = investigate first."""
    score = 40
    reasons = []

    followers = account.get("followers") or 0
    if followers < 200:
        score += 20; reasons.append("very low followers")
    elif followers < 2000:
        score += 10; reasons.append("low followers")
    elif followers > 500_000:
        score -= 15; reasons.append("large established account")

    if account.get("verified"):
        score -= 10; reasons.append("verified account")

    bio = (account.get("bio") or "").strip()
    display = (account.get("display_name") or "").strip()
    if not bio and not display:
        score += 15; reasons.append("anonymous / no bio")
    elif not bio:
        score += 7; reasons.append("no bio")

    # Account age
    created = account.get("created_at")
    if created:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).days
            if age_days < 180:
                score += 25; reasons.append("account < 6 months old")
            elif age_days < 730:
                score += 15; reasons.append("account < 2 years old")
        except Exception:
            pass

    # Earliest known poster
    post_date = account.get("post_date") or account.get("post_date")
    if earliest_date and post_date and post_date <= earliest_date:
        score += 30; reasons.append("earliest known poster (likely source)")

    score = max(0, min(100, score))
    if score >= 75:
        label = "CRITICAL"
    elif score >= 55:
        label = "HIGH"
    elif score >= 35:
        label = "MEDIUM"
    else:
        label = "LOW"

    account["severity_score"] = score
    account["severity_label"] = label
    account["score_reasons"] = reasons
    return account


# ── Proactive known-account visual check ─────────────────────────────────────

def _phash_distance(path_or_url_a: str, img_bytes_b: bytes) -> Optional[int]:
    """Return perceptual hash distance between two images (lower = more similar)."""
    try:
        import imagehash
        from PIL import Image
        img_a = Image.open(path_or_url_a).convert("RGB")
        img_b = Image.open(BytesIO(img_bytes_b)).convert("RGB")
        return imagehash.phash(img_a) - imagehash.phash(img_b)
    except Exception:
        return None


def extract_image_keywords(file_path: str) -> List[str]:
    """
    Extract distinctive text/watermarks from an image using OCR.
    Returns a list of meaningful tokens (news org names, watermarks, etc.)
    """
    keywords = []
    # Common Indian and international news watermarks to specifically look for
    KNOWN_WATERMARKS = [
        "IANS", "ANI", "PTI", "AFP", "Reuters", "AP ",
        "ABP", "TV9", "NDTV", "Zee News", "India Today",
        "Aaj Tak", "Republic", "Times Now", "Lokmat",
        "Doordarshan", "DD News", "ETV", "News18",
        "Geo News", "ARY", "Dawn", "SAMAA",
        "BBC", "CNN", "Al Jazeera", "RT ",
    ]
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(file_path)
        text = pytesseract.image_to_string(img, config="--psm 11")
        # Check for known watermarks
        for wm in KNOWN_WATERMARKS:
            if wm.strip().lower() in text.lower():
                keywords.append(wm.strip())
        # Also grab any short ALL-CAPS words (often station callsigns / org names)
        caps_words = re.findall(r'\b[A-Z]{2,8}\b', text)
        for w in caps_words:
            if w not in keywords and w not in ("THE", "AND", "FOR", "BUT", "ARE", "WAS"):
                keywords.append(w)
        logger.info("OCR extracted keywords: %s", keywords[:10])
    except Exception as e:
        logger.debug("OCR failed: %s", e)
    return keywords[:8]


def ocr_text_search_known_accounts(
    file_path: str,
    api_key: str,
    max_accounts: int = 5,
) -> List[Dict]:
    """
    Extract text/watermarks from image via OCR, then search for that text
    on each known misinfo account using Google.  Works even for recent tweets
    not yet indexed by visual search.
    """
    if not api_key:
        return []

    keywords = extract_image_keywords(file_path)
    if not keywords:
        logger.info("OCR found no distinctive keywords — skipping text-based search")
        return []

    search_text = " ".join(keywords[:4])
    logger.info("OCR text search using keywords: %s", search_text)

    from backend.modules.known_accounts import MISINFO_ACCOUNTS, FACTCHECK_ACCOUNTS
    matches = []

    # Check misinfo + factcheck accounts — split budget roughly 60/40
    misinfo_limit = max(1, int(max_accounts * 0.6))
    factcheck_limit = max_accounts - misinfo_limit
    accounts_to_check = (
        [("misinfo", u, m) for u, m in list(MISINFO_ACCOUNTS.items())[:misinfo_limit]] +
        [("factcheck", u, m) for u, m in list(FACTCHECK_ACCOUNTS.items())[:factcheck_limit]]
    )

    for acct_type, username, meta in accounts_to_check:
        try:
            from serpapi import GoogleSearch
            q = f'(site:twitter.com/{username} OR site:x.com/{username}) {search_text}'
            res = GoogleSearch({
                "engine": "google",
                "q": q,
                "api_key": api_key,
                "num": 5,
            }).get_dict()

            organic = res.get("organic_results", [])
            for item in organic[:3]:
                link = item.get("link", "")
                if "twitter.com" in link or "x.com" in link:
                    if acct_type == "misinfo":
                        score, label = 90, "CRITICAL"
                        reasons = [
                            f"OCR text match on known misinfo account @{username}",
                            f"Keywords detected: {', '.join(keywords[:4])}",
                            f"Known misinfo: {meta.get('note','')}",
                        ]
                    else:
                        score, label = 75, "HIGH"
                        reasons = [
                            f"FACT-CHECKER @{username} ({meta.get('org','')}) referenced this content",
                            f"Keywords detected: {', '.join(keywords[:4])}",
                            "Fact-checker coverage indicates this content has been disputed/debunked",
                        ]
                    matches.append({
                        "username": username,
                        "platform": "twitter",
                        "account_url": f"https://twitter.com/{username}",
                        "post_url": link,
                        "known_type": acct_type,
                        "known_note": meta.get("note") or meta.get("org", ""),
                        "known_org": meta.get("org", "") if acct_type == "factcheck" else "",
                        "region": meta.get("region", ""),
                        "match_title": item.get("title", ""),
                        "match_snippet": item.get("snippet", ""),
                        "match_method": "ocr_text_search",
                        "ocr_keywords": keywords,
                        "severity_score": score,
                        "severity_label": label,
                        "score_reasons": reasons,
                    })
                    break
            time.sleep(0.2)
        except Exception as e:
            logger.debug("OCR text search failed for @%s: %s", username, e)

    logger.info("OCR text search complete: %d matches found", len(matches))
    return matches


def proactive_known_account_check(
    file_path: str,
    public_url: str,
    api_key: str,
    max_accounts: int = 8,
) -> Dict[str, Any]:
    """
    For each known misinformation account, search Google Images restricted
    to their Twitter/X profile and compare thumbnails perceptually against
    the uploaded image.  Uses 1 SerpAPI credit per account checked.

    Returns:
      {
        "confirmed_matches": [...],   # accounts with visually similar images found
        "checked": [...],             # accounts checked (no match)
        "manual_links": {...},        # free manual-verification links for all known accounts
      }
    """
    from backend.modules.known_accounts import MISINFO_ACCOUNTS, FACTCHECK_ACCOUNTS

    result = {
        "confirmed_matches": [],
        "checked": [],
        "manual_links": {},
        "errors": [],
    }

    # Build manual links for ALL known accounts — free, no API call
    for username, meta in {**MISINFO_ACCOUNTS, **FACTCHECK_ACCOUNTS}.items():
        result["manual_links"][username] = {
            "twitter_media": f"https://twitter.com/{username}/media",
            "google_images": f"https://www.google.com/search?q=site%3Atwitter.com%2F{username}&tbm=isch",
            "type": "misinfo" if username in MISINFO_ACCOUNTS else "factcheck",
            "note": meta.get("note") or meta.get("org", ""),
        }

    if not api_key:
        result["errors"].append("No SerpAPI key — skipping proactive check")
        return result

    # Check misinfo + factcheck accounts — split budget roughly 60/40
    misinfo_limit = max(1, int(max_accounts * 0.6))
    factcheck_limit = max_accounts - misinfo_limit
    accounts_to_check = (
        [("misinfo", u, m) for u, m in list(MISINFO_ACCOUNTS.items())[:misinfo_limit]] +
        [("factcheck", u, m) for u, m in list(FACTCHECK_ACCOUNTS.items())[:factcheck_limit]]
    )

    for acct_type, username, meta in accounts_to_check:
        logger.info("Proactive check: searching Google Images for @%s (%s)", username, acct_type)
        try:
            from serpapi import GoogleSearch
            res = GoogleSearch({
                "engine": "google_images",
                "q": f"site:twitter.com/{username} OR site:x.com/{username}",
                "api_key": api_key,
                "num": 10,
                "safe": "off",
            }).get_dict()

            img_results = res.get("images_results", [])
            best_diff = None
            best_match = None

            for img in img_results[:8]:
                thumb = img.get("thumbnail") or img.get("original")
                if not thumb:
                    continue
                try:
                    r = requests.get(thumb, timeout=10, verify=False)
                    diff = _phash_distance(file_path, r.content)
                    if diff is not None:
                        if best_diff is None or diff < best_diff:
                            best_diff = diff
                            best_match = img
                except Exception:
                    continue

            entry = {
                "username": username,
                "platform": "twitter",
                "account_url": f"https://twitter.com/{username}",
                "known_type": acct_type,
                "known_note": meta.get("note") or meta.get("org", ""),
                "known_org": meta.get("org", "") if acct_type == "factcheck" else "",
                "region": meta.get("region", ""),
                "google_images_count": len(img_results),
                "best_hash_diff": best_diff,
            }

            if best_diff is not None and best_diff < 15:
                entry["match_thumbnail"] = best_match.get("thumbnail", "")
                entry["match_title"] = best_match.get("title", "")
                entry["post_url"] = best_match.get("link", "")
                if acct_type == "misinfo":
                    entry["severity_score"] = 95
                    entry["severity_label"] = "CRITICAL"
                    entry["score_reasons"] = [
                        f"PROACTIVE MATCH — visually similar image on known misinfo account @{username}",
                        f"Hash similarity: {64 - best_diff}/64",
                        f"Known misinfo: {meta.get('note','')}",
                    ]
                else:
                    entry["severity_score"] = 80
                    entry["severity_label"] = "HIGH"
                    entry["score_reasons"] = [
                        f"FACT-CHECKER @{username} ({meta.get('org','')}) has visually similar content",
                        f"Hash similarity: {64 - best_diff}/64",
                        "Fact-checker coverage indicates this content has been disputed/debunked",
                    ]
                result["confirmed_matches"].append(entry)
                logger.info("Proactive match found: @%s [%s] (hash diff %d)", username, acct_type, best_diff)
            else:
                entry["match_found"] = False
                result["checked"].append(entry)

            time.sleep(0.2)

        except Exception as e:
            logger.warning("Proactive check failed for @%s: %s", username, e)
            result["errors"].append(f"@{username}: {e}")

    # Second pass: OCR-based text search for accounts not caught by phash
    # This finds recent tweets not yet indexed visually (e.g. just-posted content)
    ocr_matches = ocr_text_search_known_accounts(file_path, api_key, max_accounts=max_accounts)
    confirmed_usernames = {m["username"].lower() for m in result["confirmed_matches"]}
    for match in ocr_matches:
        uname = match.get("username", "").lower()
        if uname not in confirmed_usernames:
            result["confirmed_matches"].append(match)
            confirmed_usernames.add(uname)
            logger.info("OCR text search matched @%s", uname)

    result["ocr_keywords"] = extract_image_keywords(file_path)

    logger.info(
        "Proactive check complete: %d matches (%d via phash, %d via OCR), %d accounts with manual links",
        len(result["confirmed_matches"]),
        len(result["confirmed_matches"]) - len(ocr_matches),
        len(ocr_matches),
        len(result["manual_links"])
    )
    return result


# ── Main entry point ──────────────────────────────────────────────────────────

def run_media_investigation(
    file_path: str,
    api_key: str,
    max_results: int = 50,
    anthropic_api_key: str = "",
) -> Dict[str, Any]:
    """
    Full media-first investigation:
      1. Upload to public host
      2. LLM image analysis (Claude): extract person, event, search queries
      3. Context-driven Google + YouTube search using LLM-generated queries
      4. Visual reverse search via SerpAPI (Google Lens + Yandex)
      5. Parse result URLs for social media accounts
      6. Scrape each account
      7. Score and rank by investigative severity

    Returns a dict with `discovered_accounts` (ranked list) and raw search data.
    """
    from backend.modules.reverse_search import upload_to_catbox, _search_google_lens, _search_yandex

    result = {
        "success": False,
        "public_url": None,
        "raw_matches": [],
        "discovered_accounts": [],
        "llm_analysis": None,
        "context_search": None,
        "errors": [],
    }

    # Step 1: upload
    public_url = upload_to_catbox(file_path)
    if not public_url:
        result["errors"].append("Failed to upload to public host (Imgur/Catbox)")
        return result
    result["public_url"] = public_url
    logger.info("Uploaded media to %s", public_url)

    # Step 2: LLM image analysis — extract context and generate search queries
    if anthropic_api_key:
        logger.info("Running Claude image analysis…")
        from backend.modules.image_analysis import analyze_image, run_context_searches
        analysis = analyze_image(file_path, anthropic_api_key, public_url=public_url)
        result["llm_analysis"] = analysis
        if analysis.get("success"):
            logger.info("Claude identified: %s | %s", analysis.get("person","?"), analysis.get("event","?"))
            # Step 3: context-driven searches using Claude's generated queries
            ctx = run_context_searches(analysis, api_key, max_results=5)
            result["context_search"] = ctx
            if ctx.get("success"):
                logger.info("Context search: %d Google + %d YouTube results",
                            len(ctx.get("google",[])), len(ctx.get("youtube",[])))
        else:
            logger.warning("Claude analysis failed: %s", analysis.get("error"))
    else:
        logger.info("No anthropic_api_key set — skipping LLM image analysis")

    # Step 4: visual reverse search
    gl_matches = _search_google_lens(api_key, public_url, max_results=max_results)
    yx_matches = _search_yandex(api_key, public_url, max_results=max_results)
    all_matches = gl_matches + yx_matches
    result["raw_matches"] = all_matches
    logger.info("Reverse search returned %d total matches", len(all_matches))

    # Step 3: extract social media URLs and deduplicate by account
    seen_accounts = {}  # key: (platform, username or url)
    for match in all_matches:
        url = match.get("link", "")
        if not url:
            continue
        parsed = parse_social_url(url)
        if not parsed:
            # Still include non-social matches as generic web hits
            host = urlparse(url).netloc.replace("www.", "")
            key = ("web", url[:80])
            if key not in seen_accounts:
                seen_accounts[key] = {
                    "platform": "web",
                    "username": None,
                    "account_url": url,
                    "post_url": url,
                    "display_name": match.get("title", ""),
                    "source_domain": match.get("source", host),
                    "match_engine": match.get("engine", ""),
                    "match_thumbnail": match.get("thumbnail", ""),
                    "match_title": match.get("title", ""),
                    "post_date": match.get("date", ""),
                }
            continue

        acct_key = parsed.get("username") or (parsed.get("account_url") or url)[:60]
        key = (parsed["platform"], acct_key)
        if key not in seen_accounts:
            seen_accounts[key] = {
                **parsed,
                "match_engine": match.get("engine", ""),
                "match_thumbnail": match.get("thumbnail", ""),
                "match_title": match.get("title", ""),
                "source_domain": match.get("source", ""),
                "post_date": match.get("date", ""),
            }

    # Also parse URLs from Claude's context searches
    ctx = result.get("context_search") or {}
    misinfo_articles = []  # non-social fact-check / news articles from misinfo search

    if ctx.get("success"):
        # General semantic results (same person/event)
        for item in ctx.get("google", []) + ctx.get("youtube", []):
            url = item.get("link", "")
            if not url:
                continue
            parsed = parse_social_url(url)
            if not parsed:
                continue
            acct_key = parsed.get("username") or (parsed.get("account_url") or url)[:60]
            key = (parsed["platform"], acct_key)
            if key not in seen_accounts:
                seen_accounts[key] = {
                    **parsed,
                    "match_engine": "semantic_search",
                    "match_title": item.get("title", ""),
                    "source_domain": item.get("source", "") or item.get("channel", ""),
                    "post_date": item.get("published", ""),
                    "_search_query": item.get("query", ""),
                }

        # Misinformation-focused results (debunks, fact-checks, fake/deepfake reports)
        for item in ctx.get("misinfo", []):
            url = item.get("link", "")
            if not url:
                continue
            parsed = parse_social_url(url)
            if parsed:
                acct_key = parsed.get("username") or (parsed.get("account_url") or url)[:60]
                key = (parsed["platform"], acct_key)
                if key not in seen_accounts:
                    seen_accounts[key] = {
                        **parsed,
                        "match_engine": "misinfo_search",
                        "match_title": item.get("title", ""),
                        "source_domain": item.get("source", ""),
                        "post_date": item.get("published", ""),
                        "_search_query": item.get("query", ""),
                    }
                else:
                    # Upgrade match_engine if already found another way
                    seen_accounts[key]["match_engine"] = "misinfo_search"
            else:
                # Non-social URL (news site, fact-checker) — store as article
                misinfo_articles.append({
                    "title": item.get("title", ""),
                    "url": url,
                    "source": item.get("source", ""),
                    "snippet": item.get("snippet", ""),
                    "query": item.get("query", ""),
                })

        logger.info("After context search: %d account candidates, %d misinfo articles",
                    len(seen_accounts), len(misinfo_articles))

    result["misinfo_articles"] = misinfo_articles

    # Step 4: scrape each social media account (skip generic web)
    accounts = []
    for (platform, key), info in seen_accounts.items():
        if platform == "web":
            accounts.append(info)
            continue
        logger.info("Scraping %s account: %s", platform, key)
        try:
            scraped = scrape_account(info)
            merged = {**info, **{k: v for k, v in scraped.items() if v is not None}}
            accounts.append(merged)
        except Exception as e:
            logger.warning("Scrape failed for %s %s: %s", platform, key, e)
            accounts.append(info)
        time.sleep(0.3)

    # Step 5: score and rank
    earliest_date = min(
        (a.get("post_date") for a in accounts if a.get("post_date")),
        default=None
    )
    for a in accounts:
        score_account(a, accounts, earliest_date)
        # Accounts found via misinfo search are talking about authenticity — boost priority
        if a.get("match_engine") == "misinfo_search":
            a["severity_score"] = min(100, a.get("severity_score", 0) + 15)
            a.setdefault("score_reasons", []).append("found in misinformation search results")
        # Boost/flag known misinformation/factcheck accounts
        try:
            from backend.modules.known_accounts import apply_known_account_scoring
            apply_known_account_scoring(a)
        except Exception:
            pass

    # Sort: misinfo_search first, then by score descending
    accounts.sort(key=lambda a: (
        0 if a.get("match_engine") == "misinfo_search" else 1,
        -a.get("severity_score", 0),
        a.get("platform", "")
    ))
    for i, a in enumerate(accounts):
        a["rank"] = i + 1

    # Step 6: proactive check against known misinfo accounts
    proactive = proactive_known_account_check(file_path, public_url, api_key, max_accounts=8)
    result["proactive_check"] = proactive

    # Merge confirmed proactive matches into accounts (avoid duplicates)
    existing_usernames = {a.get("username", "").lower() for a in accounts if a.get("username")}
    for match in proactive.get("confirmed_matches", []):
        uname = match.get("username", "").lower()
        if uname not in existing_usernames:
            # Try to scrape the account for full details
            try:
                scraped = scrape_account(match)
                match = {**match, **{k: v for k, v in scraped.items() if v is not None}}
            except Exception:
                pass
            accounts.append(match)
            existing_usernames.add(uname)

    # Re-sort after adding proactive matches
    accounts.sort(key=lambda a: (-a.get("severity_score", 0), a.get("platform", "")))
    for i, a in enumerate(accounts):
        a["rank"] = i + 1

    result["discovered_accounts"] = accounts
    result["success"] = True
    logger.info("Media investigation complete: %d accounts discovered (%d proactive matches)",
                len(accounts), len(proactive.get("confirmed_matches", [])))
    return result
