"""
Dark web / underground intelligence gathering.

Sources used (all via clearnet — no Tor required for the indexing layer):
  1. Ahmia (ahmia.fi)  — public index of Tor hidden services
  2. DarkSearch.io     — dark web search API (free tier)
  3. Have I Been Pwned — credential breach lookup for email / username
  4. IntelligenceX    — darkweb/paste indexer (API key optional)
  5. Paste sites       — Pastebin, PrivateBin public search
  6. DeHashed          — breach database (API key optional)
"""
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import quote_plus

from ..core.models import DarkWebResult
from ..core.utils import get, jitter

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Ahmia — public Tor search index                                    #
# ------------------------------------------------------------------ #

AHMIA_SEARCH = "https://ahmia.fi/search/?q={query}"
AHMIA_API    = "https://ahmia.fi/search/?q={query}&format=json"


def search_ahmia(query: str, session) -> List[DarkWebResult]:
    url = AHMIA_API.format(query=quote_plus(query))
    resp = get(session, url, headers={"Accept": "application/json"})
    results = []
    if not resp:
        # Try HTML fallback
        return _ahmia_html(query, session)
    try:
        data = resp.json()
        for item in data.get("results", [])[:10]:
            results.append(DarkWebResult(
                source="ahmia",
                url=item.get("url"),
                title=item.get("name"),
                snippet=item.get("description"),
                result_type="forum_post",
            ))
    except Exception:
        return _ahmia_html(query, session)
    return results


def _ahmia_html(query: str, session) -> List[DarkWebResult]:
    from bs4 import BeautifulSoup
    url = AHMIA_SEARCH.format(query=quote_plus(query))
    resp = get(session, url)
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    results = []
    for li in soup.select("li.result")[:10]:
        title_el = li.select_one("h4 a") or li.select_one("a")
        snippet_el = li.select_one("p.description") or li.select_one("p")
        results.append(DarkWebResult(
            source="ahmia",
            url=title_el.get("href") if title_el else None,
            title=title_el.get_text(strip=True) if title_el else None,
            snippet=snippet_el.get_text(strip=True) if snippet_el else None,
            result_type="tor_index",
        ))
    return results


# ------------------------------------------------------------------ #
#  Have I Been Pwned (HIBP)                                           #
# ------------------------------------------------------------------ #

HIBP_BREACHES = "https://haveibeenpwned.com/api/v3/breachedaccount/{email}?truncateResponse=false"
HIBP_PASTES   = "https://haveibeenpwned.com/api/v3/pasteaccount/{email}"


def check_hibp(email: str, session, api_key: Optional[str] = None) -> List[DarkWebResult]:
    if not api_key:
        logger.info("HIBP API key not configured; skipping breach lookup")
        return []
    headers = {"hibp-api-key": api_key, "user-agent": "osint-research-tool"}
    results = []

    # Breaches
    resp = get(session, HIBP_BREACHES.format(email=email), headers=headers)
    if resp and resp.status_code == 200:
        for breach in resp.json():
            results.append(DarkWebResult(
                source="hibp_breach",
                url=f"https://haveibeenpwned.com/account/{email}",
                title=f"Breach: {breach.get('Name')}",
                snippet=f"Data classes: {', '.join(breach.get('DataClasses', [])[:5])}. "
                        f"Breach date: {breach.get('BreachDate')}.",
                indexed_at=_parse_date(breach.get("AddedDate")),
                result_type="credential_leak",
            ))

    # Pastes
    jitter(1.5)
    resp2 = get(session, HIBP_PASTES.format(email=email), headers=headers)
    if resp2 and resp2.status_code == 200:
        for paste in resp2.json():
            results.append(DarkWebResult(
                source="hibp_paste",
                url=paste.get("Source") + "/" + paste.get("Id", "") if paste.get("Source") else None,
                title=paste.get("Title") or "Untitled paste",
                snippet=f"Email found in paste on {paste.get('Date', 'unknown date')}",
                indexed_at=_parse_date(paste.get("Date")),
                result_type="paste",
            ))
    return results


# ------------------------------------------------------------------ #
#  IntelligenceX (free tier: no API key for basic lookups)            #
# ------------------------------------------------------------------ #

INTELX_SEARCH  = "https://2.intelx.io/intelligent/search"
INTELX_RESULT  = "https://2.intelx.io/intelligent/search/result?id={id}&limit=10"


def search_intelx(term: str, session, api_key: Optional[str] = None) -> List[DarkWebResult]:
    key = api_key or "00000000-0000-0000-0000-000000000000"  # public demo key
    headers = {"x-key": key, "Content-Type": "application/json"}
    payload = {
        "term": term,
        "buckets": [],
        "lookuplevel": 0,
        "maxresults": 10,
        "timeout": 15,
        "datefrom": "",
        "dateto": "",
        "sort": 4,
        "media": 0,
        "terminate": [],
    }
    try:
        r = session.post(INTELX_SEARCH, json=payload, headers=headers, timeout=20)
        if r.status_code != 200:
            return []
        search_id = r.json().get("id")
        if not search_id:
            return []
    except Exception as e:
        logger.debug("IntelX search failed: %s", e)
        return []

    jitter(2.0)
    resp2 = get(session, INTELX_RESULT.format(id=search_id), headers=headers)
    if not resp2:
        return []
    results = []
    try:
        records = resp2.json().get("records", [])
        for rec in records[:10]:
            results.append(DarkWebResult(
                source="intelx",
                url=f"https://intelx.io/?did={rec.get('storageid')}",
                title=rec.get("name") or rec.get("systemid"),
                snippet=f"Type: {rec.get('type')} | Bucket: {rec.get('bucket')} | Date: {rec.get('date', '')[:10]}",
                indexed_at=_parse_date(rec.get("date")),
                result_type="paste" if "paste" in str(rec.get("bucket", "")).lower() else "forum_post",
            ))
    except Exception as e:
        logger.warning("IntelX result parse error: %s", e)
    return results


# ------------------------------------------------------------------ #
#  Paste site searches                                                 #
# ------------------------------------------------------------------ #

def search_pastes(query: str, session, searxng_client=None) -> List[DarkWebResult]:
    """Search paste sites via SearXNG or direct Google dork."""
    results = []
    paste_sites = "site:pastebin.com OR site:paste.ee OR site:ghostbin.com OR site:privatebin.net"
    search_query = f'{paste_sites} "{query}"'

    if searxng_client:
        hits = searxng_client.search(search_query, engines="general", max_results=10)
        for hit in hits:
            results.append(DarkWebResult(
                source="paste_search",
                url=hit.get("url"),
                title=hit.get("title"),
                snippet=hit.get("content"),
                result_type="paste",
            ))
    # When SearXNG is unavailable we return nothing — the search URL is stored
    # in metadata by the caller, not treated as a real darkweb hit.
    return results


def paste_search_url(query: str) -> str:
    """Return a Google dork URL the analyst can run manually (not a hit)."""
    paste_sites = "site:pastebin.com OR site:paste.ee OR site:ghostbin.com OR site:privatebin.net"
    q = f'{paste_sites} "{query}"'
    return f"https://www.google.com/search?q={quote_plus(q)}"


# ------------------------------------------------------------------ #
#  Orchestrator                                                        #
# ------------------------------------------------------------------ #

def gather_darkweb_intel(
    username: Optional[str],
    email: Optional[str],
    display_name: Optional[str],
    session,
    config: dict,
    searxng_client=None,
) -> dict:
    """
    Returns a dict with:
      hits       — list of DarkWebResult (real confirmed findings)
      manual_searches — URLs the analyst can check manually
    """
    results: List[DarkWebResult] = []
    manual_searches: List[dict] = []
    queries = _build_queries(username, email, display_name)

    for q in queries:
        logger.debug("Dark web search: %r", q)
        jitter(1.0, 0.5)
        hits = search_ahmia(q, session)
        results.extend(hits)

        jitter(1.0, 0.5)
        hits = search_intelx(q, session, api_key=config.get("intelx_api_key"))
        results.extend(hits)

    if email:
        jitter(1.5)
        results.extend(check_hibp(email, session, api_key=config.get("hibp_api_key")))

    if username:
        # Paste search: only real hits if SearXNG available
        jitter(0.5)
        paste_hits = search_pastes(username, session, searxng_client)
        results.extend(paste_hits)
        # Always surface a manual paste dork for the analyst
        manual_searches.append({
            "label": f"Paste site search for '{username}'",
            "url": paste_search_url(username),
        })
        manual_searches.append({
            "label": f"Google OSINT search for '{username}'",
            "url": f"https://www.google.com/search?q={quote_plus(chr(34) + username + chr(34))}+site:twitter.com+OR+site:reddit.com+OR+site:t.me",
        })

    # Deduplicate by URL
    seen: set = set()
    deduped: List[DarkWebResult] = []
    for r in results:
        key = r.url or r.title or ""
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return {"hits": deduped, "manual_searches": manual_searches}


def _build_queries(username, email, display_name) -> List[str]:
    queries = []
    if username:
        queries.append(username)
    if email:
        queries.append(email)
    if display_name and display_name != username:
        queries.append(f'"{display_name}"')
    return queries


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None
