"""
Auto-actions module.
Automatically performs investigative actions that would otherwise be manual:
- Reverse image/video search (Yandex)
- Cross-platform profile scraping (Linktree, TikTok, Blogger, etc.)
- Twitter following list extraction
- URL expansion for shortened links
Each action returns a structured result dict that is stored on the GuidanceItem
and shown inline in the UI.
"""
import logging
import re
import json
import time
from typing import Dict, List, Optional, Any
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── Reverse image search ──────────────────────────────────────────────────────

def yandex_reverse_search_by_url(image_url: str) -> Dict:
    """Submit image URL to Yandex reverse image search and parse results."""
    results = {
        "engine": "yandex",
        "matches": [],
        "search_url": f"https://yandex.com/images/search?rpt=imageview&url={quote_plus(image_url)}",
        "earliest_match": None,
        "error": None,
    }
    try:
        r = requests.get(
            f"https://yandex.com/images/search",
            params={"rpt": "imageview", "url": image_url},
            headers=HEADERS, timeout=20
        )
        if r.status_code != 200:
            results["error"] = f"HTTP {r.status_code}"
            return results

        soup = BeautifulSoup(r.text, "lxml")

        # Yandex embeds results as JSON in a script tag
        for script in soup.find_all("script"):
            text = script.string or ""
            if "cbir" in text or "serpList" in text or "similar" in text:
                # Try to extract JSON data
                m = re.search(r'"serpList"\s*:\s*(\[.+?\])', text, re.DOTALL)
                if m:
                    try:
                        items = json.loads(m.group(1))
                        for item in items[:10]:
                            results["matches"].append({
                                "title": item.get("title", ""),
                                "url": item.get("url", ""),
                                "domain": item.get("domain", ""),
                                "snippet": item.get("snippet", ""),
                            })
                    except Exception:
                        pass
                    break

        # Fallback: parse HTML cbir-similar section
        if not results["matches"]:
            for site_el in soup.select(".cbir-similar__item, .serp-item, [data-bem]")[:10]:
                link = site_el.select_one("a")
                title = site_el.select_one(".cbir-similar__site-title, .title")
                if link and link.get("href"):
                    href = link["href"]
                    if href.startswith("http") and "yandex" not in href:
                        results["matches"].append({
                            "title": title.get_text(strip=True) if title else "",
                            "url": href,
                            "domain": urlparse(href).netloc,
                        })

        if not results["matches"]:
            results["error"] = "No matches found (Yandex may require CAPTCHA for this image)"
    except Exception as e:
        results["error"] = str(e)

    return results


def google_lens_search_url(image_url: str) -> str:
    return f"https://lens.google.com/uploadbyurl?url={quote_plus(image_url)}"


def tineye_search_url(image_url: str) -> str:
    return f"https://tineye.com/search?url={quote_plus(image_url)}"


def run_reverse_search(media_file) -> Dict:
    """Run reverse search on a media file. For video, use thumbnail if available."""
    mf = media_file if isinstance(media_file, dict) else media_file.model_dump()
    image_url = mf.get("source_url", "")
    result = {
        "media_type": mf.get("media_type"),
        "filename": mf.get("filename"),
        "search_links": {
            "google_lens": google_lens_search_url(image_url),
            "tineye": tineye_search_url(image_url),
            "yandex": f"https://yandex.com/images/search?rpt=imageview&url={quote_plus(image_url)}",
        },
        "yandex_results": None,
    }
    if mf.get("media_type") == "image":
        result["yandex_results"] = yandex_reverse_search_by_url(image_url)
    return result


# ── Linktree scraping ─────────────────────────────────────────────────────────

def scrape_linktree(username: str) -> Dict:
    """Fetch Linktree profile and extract all links. Linktree embeds JSON in page."""
    result = {"found": False, "links": [], "bio": None, "avatar": None, "error": None}
    try:
        r = requests.get(
            f"https://linktr.ee/{username}",
            headers=HEADERS, timeout=15
        )
        if r.status_code != 200:
            result["error"] = f"HTTP {r.status_code}"
            return result

        soup = BeautifulSoup(r.text, "lxml")

        # Linktree embeds all data as __NEXT_DATA__ JSON
        next_data = soup.find("script", {"id": "__NEXT_DATA__"})
        if next_data and next_data.string:
            try:
                data = json.loads(next_data.string)
                # Navigate to the user data
                props = data.get("props", {}).get("pageProps", {})
                account = props.get("account", {})
                links = props.get("links", [])

                result["found"] = True
                result["bio"] = account.get("description") or account.get("bio")
                result["avatar"] = account.get("profilePictureUrl") or account.get("avatar")
                result["username"] = account.get("username", username)
                result["page_title"] = account.get("pageTitle")

                # Account creation timestamp (milliseconds)
                created_ms = account.get("createdAt")
                if created_ms:
                    try:
                        from datetime import datetime, timezone
                        result["created_at"] = datetime.fromtimestamp(
                            int(created_ms) / 1000, tz=timezone.utc
                        ).isoformat()
                    except Exception:
                        pass

                # Social links
                result["social_links"] = []
                for sl in (props.get("socialLinks") or []):
                    if sl.get("url"):
                        result["social_links"].append({"platform": sl.get("type",""), "url": sl.get("url","")})

                for link in links:
                    if not link.get("active", True):
                        continue
                    entry = {
                        "title": link.get("title", ""),
                        "url": link.get("url", "") or link.get("originalUrl", ""),
                        "type": link.get("type", ""),
                    }
                    if entry["url"]:
                        domain = urlparse(entry["url"]).netloc.lower().lstrip("www.")
                        entry["domain"] = domain
                        result["links"].append(entry)

                result["no_links_note"] = "Account exists but has no links configured" if not result["links"] else None
                return result
            except Exception as e:
                logger.debug("Linktree JSON parse error: %s", e)

        # Fallback: HTML scraping
        for link_el in soup.select("a[href]"):
            href = link_el.get("href", "")
            if href.startswith("http") and "linktr.ee" not in href:
                title = link_el.get_text(strip=True)
                result["links"].append({"title": title[:80], "url": href, "domain": urlparse(href).netloc})
        if result["links"]:
            result["found"] = True

    except Exception as e:
        result["error"] = str(e)
    return result


# ── TikTok profile ────────────────────────────────────────────────────────────

def scrape_tiktok_profile(username: str) -> Dict:
    """Fetch TikTok profile and extract bio, follower count, bio link."""
    result = {"found": False, "bio": None, "followers": None, "following": None,
              "likes": None, "bio_link": None, "nickname": None, "error": None}
    try:
        r = requests.get(
            f"https://www.tiktok.com/@{username}",
            headers={**HEADERS, "Accept": "text/html"},
            timeout=15
        )
        if r.status_code != 200:
            result["error"] = f"HTTP {r.status_code}"
            return result

        soup = BeautifulSoup(r.text, "lxml")

        # TikTok embeds user data in __UNIVERSAL_DATA_FOR_REHYDRATION__
        for script in soup.find_all("script", {"id": "__UNIVERSAL_DATA_FOR_REHYDRATION__"}):
            try:
                data = json.loads(script.string or "")
                # Navigate through the data structure
                for key, val in data.items():
                    if isinstance(val, dict):
                        for k2, v2 in val.items():
                            if isinstance(v2, dict) and "userInfo" in str(v2):
                                user_info = v2.get("userInfo", {})
                                user = user_info.get("user", {}) or v2.get("user", {})
                                stats = user_info.get("stats", {}) or v2.get("stats", {})
                                if user:
                                    result["found"] = True
                                    result["nickname"] = user.get("nickname")
                                    result["bio"] = user.get("signature")
                                    result["bio_link"] = user.get("bioLink", {}).get("link") if isinstance(user.get("bioLink"), dict) else user.get("bioLink")
                                    result["followers"] = stats.get("followerCount")
                                    result["following"] = stats.get("followingCount")
                                    result["likes"] = stats.get("heartCount") or stats.get("heart")
                                    return result
            except Exception:
                pass

        # Fallback: look for JSON-LD or meta tags
        for meta in soup.find_all("meta"):
            if meta.get("name") == "description":
                result["bio"] = meta.get("content", "")
                result["found"] = True
                break

    except Exception as e:
        result["error"] = str(e)
    return result


# ── Blogger profile ───────────────────────────────────────────────────────────

def scrape_blogger_profile(username: str) -> Dict:
    """Fetch Blogger profile."""
    result = {"found": False, "display_name": None, "bio": None,
              "posts": [], "location": None, "error": None}
    try:
        r = requests.get(
            f"https://{username}.blogspot.com",
            headers=HEADERS, timeout=12
        )
        if r.status_code != 200:
            # Try the /profile endpoint
            r = requests.get(
                f"https://www.blogger.com/profile/{username}",
                headers=HEADERS, timeout=12
            )
            if r.status_code != 200:
                result["error"] = f"HTTP {r.status_code}"
                return result

        soup = BeautifulSoup(r.text, "lxml")
        result["found"] = True

        # Blog title
        title_el = soup.find("title") or soup.select_one("h1.blog-title, .blog-title")
        if title_el:
            result["blog_title"] = title_el.get_text(strip=True)

        # Author
        author_el = soup.select_one(".profile-name, .author-name, [itemprop='author']")
        if author_el:
            result["display_name"] = author_el.get_text(strip=True)

        # Recent posts
        for post in soup.select(".post-title a, h3.post-title a")[:5]:
            result["posts"].append({
                "title": post.get_text(strip=True),
                "url": post.get("href", ""),
            })

    except Exception as e:
        result["error"] = str(e)
    return result


# ── Twitter following list ────────────────────────────────────────────────────

def get_twitter_following(username: str, user_id: str = None) -> Dict:
    """Try to get the list of accounts this user follows."""
    result = {"found": False, "accounts": [], "method": None, "error": None}

    # Try via vxtwitter (doesn't expose following list directly)
    # Try via Nitter profile page - following tab
    nitter_instances = [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.cz",
    ]

    for base in nitter_instances:
        try:
            r = requests.get(
                f"{base}/{username}/following",
                headers=HEADERS, timeout=10
            )
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            if "Making sure you're not a bot" in r.text or "Verifying" in r.text:
                continue

            accounts = []
            for item in soup.select(".timeline-item, .user-card"):
                name_el = item.select_one(".fullname, .username, .user-name")
                handle_el = item.select_one(".username, .user-screen-name")
                bio_el = item.select_one(".bio, .user-bio")
                link_el = item.select_one("a")

                name = name_el.get_text(strip=True) if name_el else None
                handle = handle_el.get_text(strip=True).lstrip("@") if handle_el else None
                bio = bio_el.get_text(strip=True) if bio_el else None
                url = None
                if handle:
                    url = f"https://twitter.com/{handle}"

                if name or handle:
                    accounts.append({
                        "display_name": name,
                        "username": handle,
                        "bio": bio,
                        "url": url,
                    })

            if accounts:
                result["found"] = True
                result["accounts"] = accounts
                result["method"] = f"nitter:{base}"
                return result

        except Exception as e:
            logger.debug("Nitter following failed at %s: %s", base, e)
            continue

    # Fallback: provide direct Twitter link
    result["error"] = "Nitter instances unavailable — see manual link below"
    result["manual_url"] = f"https://twitter.com/{username}/following"
    return result


# ── URL expander ──────────────────────────────────────────────────────────────

def expand_url(short_url: str) -> Dict:
    """Follow redirects to find the final destination URL."""
    result = {"original": short_url, "final": short_url, "chain": [], "error": None}
    try:
        r = requests.head(
            short_url, allow_redirects=True, timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        chain = [resp.url for resp in r.history] + [str(r.url)]
        result["chain"] = chain
        result["final"] = str(r.url)
        result["status_code"] = r.status_code
    except Exception as e:
        result["error"] = str(e)
    return result


# ── Generic profile scraper ───────────────────────────────────────────────────

def scrape_profile(platform: str, username: str, url: str) -> Dict:
    """Route to appropriate scraper based on platform."""
    platform_lower = platform.lower()
    if "linktree" in platform_lower or "linktr" in url:
        return {"platform": "Linktree", **scrape_linktree(username)}
    elif "tiktok" in platform_lower:
        return {"platform": "TikTok", **scrape_tiktok_profile(username)}
    elif "blogger" in platform_lower or "blogspot" in url:
        return {"platform": "Blogger", **scrape_blogger_profile(username)}
    else:
        # Generic scrape — extract title, description, any links
        return generic_page_scrape(platform, url)


def generic_page_scrape(platform: str, url: str) -> Dict:
    """Generic page scrape — extract title, description, links."""
    result = {"platform": platform, "found": False, "title": None,
              "description": None, "links": [], "error": None}
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if r.status_code != 200:
            result["error"] = f"HTTP {r.status_code}"
            return result
        soup = BeautifulSoup(r.text, "lxml")
        result["found"] = True
        result["title"] = soup.title.get_text(strip=True) if soup.title else None
        desc = soup.find("meta", {"name": "description"})
        if desc:
            result["description"] = desc.get("content", "")[:300]
        for a in soup.find_all("a", href=True)[:20]:
            href = a["href"]
            if href.startswith("http") and platform.lower() not in urlparse(href).netloc:
                result["links"].append({"text": a.get_text(strip=True)[:60], "url": href})
    except Exception as e:
        result["error"] = str(e)
    return result


# ── Main orchestrator ─────────────────────────────────────────────────────────

def run_all_auto_actions(case) -> Dict[str, Any]:
    """
    Run all automatable actions for a case.
    Returns a dict keyed by action_id with results.
    """
    results = {}
    username = None
    user_id = None

    if case.account:
        username = case.account.get("username")
    if not username and case.post:
        username = case.post.get("author_username")
    if case.account_enrichment:
        enr = case.account_enrichment
        user_id = enr.vx_user_id if hasattr(enr, 'vx_user_id') else enr.get("vx_user_id")

    # 1. Reverse image search for each media file
    for mf in (case.media_files or []):
        mf_dict = mf.model_dump() if hasattr(mf, 'model_dump') else mf
        action_id = f"reverse_search_{mf_dict.get('filename', 'media')}"
        results[action_id] = run_reverse_search(mf_dict)
        time.sleep(0.5)

    # 2. Twitter following list
    if username:
        results["twitter_following"] = get_twitter_following(username, user_id)
        time.sleep(0.5)

    # 3. Cross-platform profile scraping
    for hit in (case.username_search or []):
        platform = hit.get("platform", "")
        url = hit.get("url", "")
        if not url or not username:
            continue
        action_id = f"profile_{platform.lower().replace(' ', '_')}"
        results[action_id] = scrape_profile(platform, username, url)
        time.sleep(0.5)

    # 4. Expand shortened URLs from post
    if case.post:
        for url in (case.post.get("embedded_urls") or []):
            shorteners = ["bit.ly", "t.co", "tinyurl", "ow.ly", "is.gd"]
            if any(s in url for s in shorteners):
                results[f"expand_{url[:30]}"] = expand_url(url)

    return results
