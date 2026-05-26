"""
Repost / share tracker — works for any platform URL.

Sources used:
  Twitter/X posts:
    1. Twitter API v2 retweeted_by + quote_tweets  (needs Basic plan or user OAuth)
    2. Nitter /retweets page                        (fallback, often down)
    3. Nitter search                                (fallback)
    4. SerpAPI Google — URL string search           (web shares + X quote tweets)

  All other platforms (Instagram, Facebook, YouTube, TikTok, …):
    1. SerpAPI Google — URL string search           (web shares)
    2. Manual links: X url: search + Google search

Returns:
  retweeters   : [{username, profile_url, source}]
  quote_tweets : [{username, tweet_url, snippet, source, created_at, metrics}]
  web_shares   : [{title, url, source, snippet}]
  manual_links : {key: url}
  total        : int
  twitter_api_warning : str | None
"""
import re
import logging
from typing import Dict, Any, List
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

_NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
]


# ── Twitter API v2 ────────────────────────────────────────────────────────────

class TwitterAPIError(Exception):
    pass


def _twitter_get(path: str, bearer_token: str, params: Dict) -> Dict:
    r = requests.get(
        f"https://api.twitter.com/2/{path}",
        headers={"Authorization": f"Bearer {bearer_token}"},
        params=params,
        timeout=15,
    )
    if r.status_code in (401, 403):
        raise TwitterAPIError(f"Twitter API auth error ({r.status_code}): invalid or app-only token — user OAuth required")
    if r.status_code == 402:
        err = r.json() if r.text else {}
        raise TwitterAPIError(
            f"Twitter API tier too low ({err.get('title', 'CreditsDepleted')}): "
            "Basic plan ($100/mo) or user OAuth token required. "
            "Use the manual X search link below to find reposts."
        )
    if r.status_code == 429:
        raise TwitterAPIError("Twitter API rate limited — try again later")
    if r.status_code not in (200, 404):
        raise TwitterAPIError(f"Twitter API returned {r.status_code}")
    return r.json()


def _twitter_api_retweeted_by(tweet_id: str, bearer_token: str) -> List[Dict]:
    data = _twitter_get(
        f"tweets/{tweet_id}/retweeted_by",
        bearer_token,
        {"user.fields": "username,name,profile_image_url", "max_results": 100},
    )
    results = []
    for u in (data.get("data") or []):
        uname = u.get("username", "")
        results.append({
            "username":    uname,
            "profile_url": f"https://x.com/{uname}",
            "source":      "twitter_api_v2",
        })
    logger.info("Twitter API retweeted_by: %d", len(results))
    return results


def _twitter_api_quote_tweets(tweet_id: str, bearer_token: str) -> List[Dict]:
    data = _twitter_get(
        f"tweets/{tweet_id}/quote_tweets",
        bearer_token,
        {
            "tweet.fields": "created_at,author_id,public_metrics,text",
            "expansions": "author_id",
            "user.fields": "username,name",
            "max_results": 100,
        },
    )
    tweets = data.get("data") or []
    users  = {u["id"]: u for u in (data.get("includes") or {}).get("users", [])}
    results = []
    for t in tweets:
        author = users.get(t.get("author_id", ""), {})
        uname  = author.get("username", "")
        tid    = t.get("id", "")
        results.append({
            "username":   uname,
            "tweet_url":  f"https://x.com/{uname}/status/{tid}" if uname and tid else "",
            "tweet_id":   tid,
            "snippet":    (t.get("text") or "")[:250],
            "source":     "twitter_api_v2",
            "created_at": t.get("created_at", ""),
            "metrics":    t.get("public_metrics", {}),
        })
    logger.info("Twitter API quote_tweets: %d", len(results))
    return results


# ── Nitter ────────────────────────────────────────────────────────────────────

def _fetch_nitter_retweeters(username: str, tweet_id: str,
                              nitter_base: str = "") -> List[Dict]:
    instances = ([nitter_base] if nitter_base else []) + _NITTER_INSTANCES
    for base in instances:
        url = f"{base.rstrip('/')}/{username}/status/{tweet_id}/retweets"
        try:
            r = requests.get(url, headers=_HEADERS, timeout=10, allow_redirects=True)
            if r.status_code != 200:
                continue
            retweeters = []
            for m in re.finditer(
                r'<a[^>]+class="[^"]*username[^"]*"[^>]*href="/([^"?/]+)"[^>]*>',
                r.text,
            ):
                handle = m.group(1).lstrip("@")
                if handle.lower() not in _SKIP_HANDLES and handle.lower() != username.lower():
                    retweeters.append({
                        "username":    handle,
                        "profile_url": f"https://x.com/{handle}",
                        "source":      "nitter_retweets",
                    })
            seen, unique = set(), []
            for item in retweeters:
                k = item["username"].lower()
                if k not in seen:
                    seen.add(k)
                    unique.append(item)
            if unique or r.status_code == 200:
                logger.info("Nitter %s: %d retweeters", base, len(unique))
                return unique
        except Exception as e:
            logger.debug("Nitter %s failed: %s", base, e)
    return []


def _nitter_search_quotes(username: str, tweet_id: str,
                           nitter_base: str = "") -> List[Dict]:
    instances = ([nitter_base] if nitter_base else []) + _NITTER_INSTANCES
    tweet_url_path = f"{username}/status/{tweet_id}"
    for base in instances:
        search_url = f"{base.rstrip('/')}/search?q={quote_plus(tweet_url_path)}&f=tweets"
        try:
            r = requests.get(search_url, headers=_HEADERS, timeout=12, allow_redirects=True)
            if r.status_code != 200:
                continue
            results = []
            for m in re.finditer(
                r'href="/([A-Za-z0-9_]+)/status/(\d+)"[^>]*class="[^"]*tweet-link',
                r.text,
            ):
                uname, tid = m.group(1), m.group(2)
                if (uname.lower() not in _SKIP_HANDLES
                        and uname.lower() != username.lower()
                        and tid != tweet_id):
                    results.append({
                        "username":  uname,
                        "tweet_url": f"https://x.com/{uname}/status/{tid}",
                        "tweet_id":  tid,
                        "snippet":   "",
                        "source":    "nitter_search",
                    })
            seen, unique = set(), []
            for item in results:
                k = item["tweet_id"]
                if k not in seen:
                    seen.add(k)
                    unique.append(item)
            if unique:
                logger.info("Nitter search %s: %d quotes", base, len(unique))
                return unique
            if r.status_code == 200:
                return []
        except Exception as e:
            logger.debug("Nitter search %s failed: %s", base, e)
    return []


# ── SerpAPI web search ────────────────────────────────────────────────────────

def _serpapi_url_search(post_url: str, exclude_own_domain: str,
                        serpapi_key: str, max_results: int = 20) -> Dict:
    """
    Search Google for references to post_url.
    Results from the same domain as the post are classified as quote_tweets
    (for X) or ignored; everything else is a web_share.
    """
    quote_tweets: List[Dict] = []
    web_shares:   List[Dict] = []

    if not serpapi_key:
        return {"quote_tweets": quote_tweets, "web_shares": web_shares}

    try:
        from serpapi import GoogleSearch
    except ImportError:
        return {"quote_tweets": quote_tweets, "web_shares": web_shares,
                "error": "serpapi not installed"}

    # Strip scheme; exclude source platform so results are external references
    clean_url = re.sub(r'^https?://(www\.)?', '', post_url).rstrip('/')
    own_domain = re.sub(r'/.*', '', clean_url)   # e.g. "x.com"
    # For x.com also exclude twitter.com (same content, different domain)
    exclude = f'-site:{own_domain}'
    if 'x.com' in own_domain or 'twitter.com' in own_domain:
        exclude = '-site:x.com -site:twitter.com'
    query = f'"{clean_url}" {exclude}'

    try:
        results = GoogleSearch({
            "engine": "google",
            "q": query,
            "api_key": serpapi_key,
            "num": max_results,
        }).get_dict()

        seen_links: set = set()
        for item in results.get("organic_results", []):
            link    = item.get("link", "")
            title   = item.get("title", "")
            snippet = item.get("snippet", "")
            source  = item.get("source", "") or _domain(link)

            if link in seen_links:
                continue
            seen_links.add(link)

            link_domain = _domain(link)

            # X/Twitter result → classify as quote tweet
            if re.search(r'(twitter|x)\.com', link_domain):
                m = re.search(
                    r'(?:twitter|x)\.com/([A-Za-z0-9_]{1,50})/status/(\d+)',
                    link,
                )
                if m:
                    uname = m.group(1)
                    tid   = m.group(2)
                    # Exclude the original tweet and reserved handles
                    orig_tid = re.search(r'/status/(\d+)', post_url)
                    if (uname.lower() not in _SKIP_HANDLES
                            and (not orig_tid or tid != orig_tid.group(1))):
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
        logger.warning("SerpAPI url search failed: %s", e)

    return {"quote_tweets": quote_tweets, "web_shares": web_shares}


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lstrip("www.")
    except Exception:
        return ""


# ── Manual links by platform ──────────────────────────────────────────────────

def _manual_links(post_url: str, platform: str,
                  username: str = "", tweet_id: str = "") -> Dict[str, str]:
    """Build relevant manual search links for the given platform."""
    links: Dict[str, str] = {}

    # X url: search works for ANY URL (finds X posts that link to it)
    links["x_url_search"] = (
        f"https://x.com/search?q={quote_plus('url:' + post_url)}"
        f"&src=typed_query&f=live"
    )

    # Google: search for external pages referencing this URL (exclude the source platform)
    clean = re.sub(r'^https?://(www\.)?', '', post_url).rstrip('/')
    if platform in ("twitter", "x"):
        # Exclude x.com/twitter.com so results are articles/forums that reference the tweet
        google_q = f'"{clean}" -site:x.com -site:twitter.com'
    elif platform == "instagram":
        google_q = f'"{clean}" -site:instagram.com'
    elif platform == "facebook":
        google_q = f'"{clean}" -site:facebook.com'
    elif platform == "youtube":
        google_q = f'"{clean}" -site:youtube.com -site:youtu.be'
    elif platform == "tiktok":
        google_q = f'"{clean}" -site:tiktok.com'
    else:
        google_q = f'"{clean}"'
    links["google_search"] = f"https://www.google.com/search?q={quote_plus(google_q)}"

    return links


# ── Merge + deduplicate ───────────────────────────────────────────────────────

def _merge_quote_tweets(lists: List[List[Dict]], exclude_tid: str = "") -> List[Dict]:
    seen: set = set()
    merged = []
    for lst in lists:
        for item in lst:
            key = item.get("tweet_id") or item.get("tweet_url", "")
            if key and key not in seen and key != exclude_tid:
                seen.add(key)
                merged.append(item)
    return merged


# ── Main entry point ──────────────────────────────────────────────────────────

def find_reposts(post: Dict, config: Dict) -> Dict[str, Any]:
    """
    Find who shared / retweeted / quoted a post.
    Works for any platform; Twitter/X gets additional native API sources.
    """
    result: Dict[str, Any] = {
        "retweeters":          [],
        "quote_tweets":        [],
        "web_shares":          [],
        "manual_links":        {},
        "total":               0,
        "error":               None,
        "twitter_api_warning": None,
        "platform":            None,
    }

    if not post:
        return result

    platform  = (post.get("platform") or "").lower()
    post_url  = post.get("url") or ""
    username  = post.get("author_username") or ""
    tweet_id  = post.get("post_id") or post.get("id") or ""

    if not post_url:
        result["error"] = "No post URL available"
        return result

    result["platform"] = platform
    result["manual_links"] = _manual_links(post_url, platform, username, tweet_id)

    bearer_token = config.get("twitter_bearer_token", "")
    serpapi_key  = config.get("serpapi_api_key", "")
    nitter_base  = config.get("nitter_instance", "")

    # ── Twitter/X-specific sources ────────────────────────────────────────────
    api_ok = False
    if platform in ("twitter", "x") and tweet_id:

        # Twitter API v2 retweeters
        if bearer_token:
            try:
                result["retweeters"] = _twitter_api_retweeted_by(tweet_id, bearer_token)
                api_ok = True
            except TwitterAPIError as e:
                result["twitter_api_warning"] = str(e)
                logger.warning("Twitter API retweeted_by: %s", e)
            except Exception as e:
                logger.warning("Twitter API retweeted_by failed: %s", e)

        # Twitter API v2 quote tweets
        api_quotes: List[Dict] = []
        if bearer_token and api_ok:
            try:
                api_quotes = _twitter_api_quote_tweets(tweet_id, bearer_token)
            except TwitterAPIError as e:
                result["twitter_api_warning"] = str(e)
                logger.warning("Twitter API quote_tweets: %s", e)
            except Exception as e:
                logger.warning("Twitter API quote_tweets failed: %s", e)

        # Nitter fallback
        if not api_ok:
            try:
                result["retweeters"] = _fetch_nitter_retweeters(username, tweet_id, nitter_base)
            except Exception as e:
                logger.warning("Nitter retweet fetch failed: %s", e)

        nitter_quotes: List[Dict] = []
        if not api_ok:
            try:
                nitter_quotes = _nitter_search_quotes(username, tweet_id, nitter_base)
            except Exception as e:
                logger.warning("Nitter quote search failed: %s", e)

        # SerpAPI for X
        serp_result: Dict = {}
        if serpapi_key:
            try:
                serp_result = _serpapi_url_search(post_url, "x.com", serpapi_key)
                result["web_shares"] = serp_result.get("web_shares", [])
            except Exception as e:
                logger.warning("SerpAPI search failed: %s", e)

        serp_quotes = serp_result.get("quote_tweets", [])
        result["quote_tweets"] = _merge_quote_tweets(
            [api_quotes, nitter_quotes, serp_quotes], tweet_id
        )

    # ── All other platforms ───────────────────────────────────────────────────
    else:
        if serpapi_key:
            try:
                serp_result = _serpapi_url_search(post_url, _domain(post_url), serpapi_key)
                # For non-X platforms, X results that reference the URL are still useful
                # but label them as "X shares" rather than "quote tweets"
                x_shares = serp_result.get("quote_tweets", [])
                for s in x_shares:
                    s["type"] = "x_share"
                result["quote_tweets"] = x_shares   # shown in "X Shares" section
                result["web_shares"]   = serp_result.get("web_shares", [])
            except Exception as e:
                logger.warning("SerpAPI search failed: %s", e)

    result["total"] = (
        len(result["retweeters"])
        + len(result["quote_tweets"])
        + len(result["web_shares"])
    )
    return result
