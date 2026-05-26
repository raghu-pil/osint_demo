"""
Repost / retweet tracker for Twitter/X URLs.

Three complementary sources:
  1. Nitter /retweets page — silent retweeters (username list, no comment)
  2. SerpAPI Google search — quote tweets and cross-platform shares
  3. Direct Twitter/X search link — manual fallback

Returns a structured dict with:
  retweeters   : list of {username, display_name, profile_url}  (Nitter)
  quote_tweets : list of {username, tweet_url, snippet, source} (SerpAPI)
  web_shares   : list of {title, url, source, snippet}          (SerpAPI, non-Twitter)
  manual_links : search URLs the investigator can open directly
"""
import re
import logging
from typing import Dict, Any, List, Optional
from urllib.parse import quote_plus, urlparse

import requests

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_SKIP_HANDLES = frozenset({
    "search", "hashtag", "i", "home", "explore", "intent",
    "notifications", "messages", "settings", "share",
})

# Public Nitter instances to try in order
_NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
]


# ── Nitter retweeters ─────────────────────────────────────────────────────────

def _fetch_nitter_retweeters(username: str, tweet_id: str,
                              nitter_base: str = "") -> List[Dict]:
    """Scrape the /retweets page of a Nitter instance."""
    instances = ([nitter_base] if nitter_base else []) + _NITTER_INSTANCES
    for base in instances:
        url = f"{base.rstrip('/')}/{username}/status/{tweet_id}/retweets"
        try:
            r = requests.get(url, headers=_HEADERS, timeout=10, allow_redirects=True)
            if r.status_code != 200:
                continue
            # Each retweeter is in a <a class="username"> link
            retweeters = []
            for m in re.finditer(
                r'<a[^>]+class="[^"]*username[^"]*"[^>]*href="/([^"?/]+)"[^>]*>',
                r.text,
            ):
                handle = m.group(1).lstrip("@")
                if handle.lower() not in _SKIP_HANDLES and handle.lower() != username.lower():
                    retweeters.append({
                        "username": handle,
                        "profile_url": f"https://x.com/{handle}",
                        "source": "nitter_retweets",
                    })
            # Deduplicate
            seen = set()
            unique = []
            for r_item in retweeters:
                k = r_item["username"].lower()
                if k not in seen:
                    seen.add(k)
                    unique.append(r_item)
            if unique or r.status_code == 200:
                logger.info("Nitter %s returned %d retweeters", base, len(unique))
                return unique
        except Exception as e:
            logger.debug("Nitter %s failed: %s", base, e)
    return []


# ── SerpAPI quote tweets + web shares ────────────────────────────────────────

def _search_reposts(tweet_url: str, username: str, tweet_id: str,
                    serpapi_key: str, max_results: int = 20) -> Dict:
    """
    Search Google via SerpAPI for:
     - Quote tweets (site:x.com referencing this tweet URL)
     - Web shares (any page linking to this tweet)
    """
    quote_tweets: List[Dict] = []
    web_shares: List[Dict] = []

    if not serpapi_key:
        return {"quote_tweets": quote_tweets, "web_shares": web_shares}

    try:
        from serpapi import GoogleSearch
    except ImportError:
        return {"quote_tweets": quote_tweets, "web_shares": web_shares,
                "error": "serpapi not installed"}

    # Build a clean short URL for searching
    short_url = f"x.com/{username}/status/{tweet_id}"

    queries = [
        # Quote tweets on X
        (f'site:x.com OR site:twitter.com "{tweet_id}"', "quote_tweet"),
        # Broader web shares
        (f'"{short_url}" -site:x.com -site:twitter.com', "web_share"),
    ]

    seen_links = set()

    for query, result_type in queries:
        try:
            results = GoogleSearch({
                "engine": "google",
                "q": query,
                "api_key": serpapi_key,
                "num": max_results,
            }).get_dict()

            for item in results.get("organic_results", []):
                link    = item.get("link", "")
                title   = item.get("title", "")
                snippet = item.get("snippet", "")
                source  = item.get("source", "") or _domain(link)

                if link in seen_links:
                    continue
                seen_links.add(link)

                if result_type == "quote_tweet":
                    m = re.search(
                        r'(?:twitter|x)\.com/([A-Za-z0-9_]{1,50})/status/(\d+)',
                        link,
                    )
                    if m:
                        uname  = m.group(1)
                        tid    = m.group(2)
                        if (uname.lower() not in _SKIP_HANDLES
                                and uname.lower() != username.lower()
                                and tid != tweet_id):
                            quote_tweets.append({
                                "username":  uname,
                                "tweet_url": link,
                                "tweet_id":  tid,
                                "snippet":   snippet[:250],
                                "source":    "serpapi_google",
                            })
                else:
                    web_shares.append({
                        "title":   title[:120],
                        "url":     link,
                        "source":  source,
                        "snippet": snippet[:250],
                    })
        except Exception as e:
            logger.warning("SerpAPI repost search failed: %s", e)

    return {"quote_tweets": quote_tweets, "web_shares": web_shares}


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lstrip("www.")
    except Exception:
        return ""


# ── Main entry point ──────────────────────────────────────────────────────────

def find_reposts(post: Dict, config: Dict) -> Dict[str, Any]:
    """
    Find who retweeted / quote-tweeted / shared a Twitter/X post.

    post   : case.post dict (needs platform, post_id, author_username, url)
    config : app config dict (serpapi_api_key, nitter_instance)

    Returns:
      {
        retweeters   : [...],
        quote_tweets : [...],
        web_shares   : [...],
        manual_links : {...},
        total        : int,
        error        : str | None,
      }
    """
    result: Dict[str, Any] = {
        "retweeters":   [],
        "quote_tweets": [],
        "web_shares":   [],
        "manual_links": {},
        "total":        0,
        "error":        None,
    }

    if not post:
        return result

    platform = (post.get("platform") or "").lower()
    if platform not in ("twitter", "x"):
        result["error"] = f"Repost tracking only supported for Twitter/X (got: {platform})"
        return result

    tweet_id = post.get("post_id") or post.get("id") or ""
    username = post.get("author_username") or ""
    tweet_url = post.get("url") or f"https://x.com/{username}/status/{tweet_id}"

    if not tweet_id or not username:
        result["error"] = "Missing tweet_id or username"
        return result

    # Manual search links (always included)
    result["manual_links"] = {
        "twitter_search": f"https://x.com/search?q={quote_plus(f'url:{tweet_url}')}&src=typed_query",
        "google_quotes":  f"https://www.google.com/search?q={quote_plus(tweet_id + ' site:x.com')}",
    }

    serpapi_key  = config.get("serpapi_api_key", "")
    nitter_base  = config.get("nitter_instance", "")

    # 1. Nitter silent retweeters
    try:
        result["retweeters"] = _fetch_nitter_retweeters(username, tweet_id, nitter_base)
    except Exception as e:
        logger.warning("Nitter retweet fetch failed: %s", e)

    # 2. SerpAPI quote tweets + web shares
    if serpapi_key:
        try:
            serpapi_result = _search_reposts(tweet_url, username, tweet_id, serpapi_key)
            result["quote_tweets"] = serpapi_result.get("quote_tweets", [])
            result["web_shares"]   = serpapi_result.get("web_shares", [])
            if serpapi_result.get("error"):
                result["error"] = serpapi_result["error"]
        except Exception as e:
            logger.warning("SerpAPI repost search failed: %s", e)

    result["total"] = (
        len(result["retweeters"])
        + len(result["quote_tweets"])
        + len(result["web_shares"])
    )
    return result
