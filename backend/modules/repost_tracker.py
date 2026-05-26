"""
Repost / retweet tracker for Twitter/X URLs.

Four complementary sources:
  1. Twitter API v2 recent search — url: operator finds quote tweets (needs bearer token)
  2. Nitter /retweets page — silent retweeters (username list, no comment)
  3. Nitter search — finds posts quoting this URL on Nitter instances
  4. SerpAPI Google search — quote tweets and cross-platform web shares

Returns a structured dict with:
  retweeters   : list of {username, display_name, profile_url}  (Nitter)
  quote_tweets : list of {username, tweet_url, snippet, source} (Twitter API / SerpAPI)
  web_shares   : list of {title, url, source, snippet}          (SerpAPI, non-Twitter)
  manual_links : search URLs the investigator can open directly
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
    """Make a Twitter API v2 GET request, raising TwitterAPIError on auth/tier failures."""
    r = requests.get(
        f"https://api.twitter.com/2/{path}",
        headers={"Authorization": f"Bearer {bearer_token}"},
        params=params,
        timeout=15,
    )
    if r.status_code in (401, 403):
        raise TwitterAPIError(f"Twitter API auth error ({r.status_code}): invalid token")
    if r.status_code == 402:
        err = r.json() if r.text else {}
        raise TwitterAPIError(
            f"Twitter API tier too low ({err.get('title','CreditsDepleted')}): "
            "Basic plan ($100/mo) required for these endpoints. "
            "Use the manual X search link below to find reposts."
        )
    if r.status_code == 429:
        raise TwitterAPIError("Twitter API rate limited — try again later")
    if r.status_code not in (200, 404):
        raise TwitterAPIError(f"Twitter API returned {r.status_code}")
    return r.json()


def _twitter_api_retweeted_by(tweet_id: str, bearer_token: str) -> List[Dict]:
    """GET /2/tweets/:id/retweeted_by — users who plain-retweeted."""
    if not bearer_token:
        return []
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
    logger.info("Twitter API retweeted_by: %d retweeters", len(results))
    return results


def _twitter_api_quote_tweets(tweet_id: str, bearer_token: str) -> List[Dict]:
    """GET /2/tweets/:id/quote_tweets — quote tweets."""
    if not bearer_token:
        return []
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
    logger.info("Twitter API quote_tweets: %d found", len(results))
    return results


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
            seen = set()
            unique = []
            for item in retweeters:
                k = item["username"].lower()
                if k not in seen:
                    seen.add(k)
                    unique.append(item)
            if unique or r.status_code == 200:
                logger.info("Nitter %s returned %d retweeters", base, len(unique))
                return unique
        except Exception as e:
            logger.debug("Nitter %s failed: %s", base, e)
    return []


# ── Nitter search ─────────────────────────────────────────────────────────────

def _nitter_search_quotes(username: str, tweet_id: str,
                           nitter_base: str = "") -> List[Dict]:
    """Search Nitter for tweets that link to this tweet URL."""
    instances = ([nitter_base] if nitter_base else []) + _NITTER_INSTANCES
    tweet_url_path = f"{username}/status/{tweet_id}"

    for base in instances:
        search_url = f"{base.rstrip('/')}/search?q={quote_plus(tweet_url_path)}&f=tweets"
        try:
            r = requests.get(search_url, headers=_HEADERS, timeout=12, allow_redirects=True)
            if r.status_code != 200:
                continue

            results = []
            # Each tweet result has a timeline-item with a tweet-link
            for m in re.finditer(
                r'href="/([A-Za-z0-9_]+)/status/(\d+)"[^>]*class="[^"]*tweet-link',
                r.text,
            ):
                uname = m.group(1)
                tid   = m.group(2)
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

            seen = set()
            unique = []
            for item in results:
                k = item["tweet_id"]
                if k not in seen:
                    seen.add(k)
                    unique.append(item)

            if unique:
                logger.info("Nitter search %s found %d quote tweets", base, len(unique))
                return unique
            # If we got a 200 but no results, still stop (instance is alive, no results)
            if r.status_code == 200:
                return []
        except Exception as e:
            logger.debug("Nitter search %s failed: %s", base, e)
    return []


# ── SerpAPI quote tweets + web shares ────────────────────────────────────────

def _search_reposts(tweet_url: str, username: str, tweet_id: str,
                    serpapi_key: str, max_results: int = 20) -> Dict:
    """
    Search Google via SerpAPI for:
     - Quote tweets (site:x.com linking to this tweet's URL path)
     - Web shares (any non-X page referencing this tweet)
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

    short_url = f"x.com/{username}/status/{tweet_id}"

    queries = [
        # Any site (including X) referencing this tweet URL — catches quote tweets + web shares
        (f'"{short_url}"', "any"),
    ]

    seen_links: set = set()

    for query, _ in queries:
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

                # Classify: is this an X/Twitter post or an external web page?
                m = re.search(
                    r'(?:twitter|x)\.com/([A-Za-z0-9_]{1,50})/status/(\d+)',
                    link,
                )
                if m:
                    uname = m.group(1)
                    tid   = m.group(2)
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
                    # Non-X page that references the tweet URL
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


# ── Merge + deduplicate quote tweets across sources ───────────────────────────

def _merge_quote_tweets(lists: List[List[Dict]], exclude_username: str) -> List[Dict]:
    seen_tids: set = set()
    merged = []
    for lst in lists:
        for item in lst:
            tid = item.get("tweet_id", "")
            uname = item.get("username", "").lower()
            key = tid or item.get("tweet_url", "")
            if key and key not in seen_tids and uname != exclude_username.lower():
                seen_tids.add(key)
                merged.append(item)
    return merged


# ── Main entry point ──────────────────────────────────────────────────────────

def find_reposts(post: Dict, config: Dict) -> Dict[str, Any]:
    """
    Find who retweeted / quote-tweeted / shared a Twitter/X post.

    post   : case.post dict (needs platform, post_id, author_username, url)
    config : app config dict (serpapi_api_key, nitter_instance, twitter_bearer_token)
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

    tweet_id  = post.get("post_id") or post.get("id") or ""
    username  = post.get("author_username") or ""
    tweet_url = post.get("url") or f"https://x.com/{username}/status/{tweet_id}"

    if not tweet_id or not username:
        result["error"] = "Missing tweet_id or username"
        return result

    result["manual_links"] = {
        "twitter_search": f"https://x.com/search?q={quote_plus('url:' + tweet_url)}&src=typed_query&f=live",
        "google_quotes":  f"https://www.google.com/search?q={quote_plus(username + '/status/' + tweet_id + ' site:x.com')}",
    }

    bearer_token = config.get("twitter_bearer_token", "")
    serpapi_key  = config.get("serpapi_api_key", "")
    nitter_base  = config.get("nitter_instance", "")

    # 1a. Twitter API v2 retweeters (needs user-context OAuth, not app-only bearer)
    api_ok = False
    if bearer_token:
        try:
            result["retweeters"] = _twitter_api_retweeted_by(tweet_id, bearer_token)
            api_ok = True
        except TwitterAPIError as e:
            result["twitter_api_warning"] = str(e)
            logger.warning("Twitter API retweeted_by: %s", e)
        except Exception as e:
            logger.warning("Twitter API retweeted_by failed: %s", e)

    # 1b. Twitter API v2 quote tweets
    api_quotes: List[Dict] = []
    if bearer_token and api_ok:
        try:
            api_quotes = _twitter_api_quote_tweets(tweet_id, bearer_token)
        except TwitterAPIError as e:
            result["twitter_api_warning"] = str(e)
            logger.warning("Twitter API quote_tweets: %s", e)
        except Exception as e:
            logger.warning("Twitter API quote_tweets failed: %s", e)

    # 2. Nitter retweeters (fallback when Twitter API unavailable)
    if not api_ok:
        try:
            result["retweeters"] = _fetch_nitter_retweeters(username, tweet_id, nitter_base)
        except Exception as e:
            logger.warning("Nitter retweet fetch failed: %s", e)

    # 3. Nitter search for quote tweets (fallback when Twitter API unavailable)
    nitter_quotes: List[Dict] = []
    if not api_ok:
        try:
            nitter_quotes = _nitter_search_quotes(username, tweet_id, nitter_base)
        except Exception as e:
            logger.warning("Nitter quote search failed: %s", e)

    # 4. SerpAPI quote tweets + web shares
    serp_quotes: List[Dict] = []
    if serpapi_key:
        try:
            serpapi_result = _search_reposts(tweet_url, username, tweet_id, serpapi_key)
            serp_quotes               = serpapi_result.get("quote_tweets", [])
            result["web_shares"]      = serpapi_result.get("web_shares", [])
            if serpapi_result.get("error"):
                result["error"] = serpapi_result["error"]
        except Exception as e:
            logger.warning("SerpAPI repost search failed: %s", e)

    # Merge all quote tweet sources, deduplicating by tweet_id
    result["quote_tweets"] = _merge_quote_tweets(
        [api_quotes, nitter_quotes, serp_quotes], username
    )

    result["total"] = (
        len(result["retweeters"])
        + len(result["quote_tweets"])
        + len(result["web_shares"])
    )

    sources = []
    if api_quotes:    sources.append(f"Twitter API ({len(api_quotes)})")
    if nitter_quotes: sources.append(f"Nitter search ({len(nitter_quotes)})")
    if serp_quotes:   sources.append(f"SerpAPI ({len(serp_quotes)})")
    if sources:
        result["quote_tweet_sources"] = sources

    return result
