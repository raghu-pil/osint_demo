"""
Enhanced dark web and breach intelligence.

Free sources (no key needed):
  - BreachDirectory.org  — breach data search
  - DarkSearch.io        — dark web search engine (10 req/day free)
  - Ahmia.fi             — Tor hidden service index (clearnet + Tor)
  - IntelligenceX        — paste/darkweb indexer (public demo key)
  - Torch (.onion)       — oldest Tor search engine (via Tor)
  - DuckDuckGo .onion    — DDG onion hidden service (via Tor)
  - Tor66 (.onion)       — Tor directory search (via Tor)
  - Pastebin search      — public paste search
  - LeakCheck.io         — limited free tier

With API keys (set in config.yaml):
  - HIBP                 — hibp_api_key   ($3.50/month, most reliable)
  - Dehashed             — dehashed_api_key ($5/month, full breach records)
  - IntelligenceX        — intelx_api_key  (paid tier)
  - DarkSearch.io        — darksearch_api_key (paid removes rate limit)

Tor setup (headless):
  - Install: apt-get install -y tor
  - Start:   service tor start  OR  tor --RunAsDaemon 1
  - Tor runs on socks5://127.0.0.1:9050 by default
  - Enable:  set use_tor: true in config.yaml
"""
import logging
import socket
import time
from typing import List, Dict, Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/html, */*",
}

TOR_PROXIES = {
    "http":  "socks5h://127.0.0.1:9050",
    "https": "socks5h://127.0.0.1:9050",
}

# Known .onion search engines
ONION_SEARCH_ENGINES = [
    # (name, url_template, result_selector, title_sel, link_sel, snippet_sel)
    ("Ahmia onion",  "http://juhanurmihxlp77nkivkk4qfcpz3qjrqhzzqjbgvnzpdbqfq4fyjqoyd.onion/search/?q={q}", "li.result", "h4 a", "cite", "p.description"),
    ("Torch",        "http://xmh57jrknzkhv6y3ls3ubitzfqnkrwxhopf5ayieeo2cfvxjono3yoyd.onion/4a1f6b371c/search.cgi?q={q}&cmd=Search!", ".results dt", "a", None, "dd"),
    ("Tor66",        "http://tor66sewebgixwhcqfnp5inchfnw6duemjkit7ofzdxpjktynburxbyd.onion/search?q={q}&sorttype=rel", ".result-block", ".result-title a", None, ".result-desc"),
]


def is_tor_running(host: str = "127.0.0.1", port: int = 9050) -> bool:
    """Check if Tor SOCKS proxy is available."""
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except Exception:
        return False


def make_tor_session(timeout: int = 30) -> requests.Session:
    """Create a requests session that routes all traffic through Tor."""
    s = requests.Session()
    s.proxies = TOR_PROXIES
    s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; osint-research)"})
    return s


# ── Tor .onion search engines ─────────────────────────────────────────────────

def search_onion_engines(query: str) -> List[Dict]:
    """Search .onion search engines directly via Tor. Returns real dark web results."""
    if not is_tor_running():
        logger.info("Tor not running — skipping .onion search")
        return []

    session = make_tor_session(timeout=25)
    results = []

    for engine_name, url_tpl, result_sel, title_sel, link_sel, snippet_sel in ONION_SEARCH_ENGINES:
        try:
            url = url_tpl.format(q=quote_plus(query))
            r = session.get(url, timeout=25)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            items = soup.select(result_sel)
            logger.info("%s returned %d results for '%s'", engine_name, len(items), query)
            for item in items[:8]:
                title_el = item.select_one(title_sel) if title_sel else item
                link_el = item.select_one(link_sel) if link_sel else title_el
                snippet_el = item.select_one(snippet_sel) if snippet_sel else None

                title = title_el.get_text(strip=True) if title_el else ""
                url_val = (link_el.get("href") or link_el.get_text(strip=True)) if link_el else ""
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""

                if title or url_val:
                    results.append({
                        "source": engine_name,
                        "result_type": "darkweb_page",
                        "title": title[:200],
                        "url": url_val[:300],
                        "snippet": snippet[:300],
                    })
            time.sleep(1)
        except Exception as e:
            logger.debug("%s failed: %s", engine_name, e)
            continue

    return results


def search_ahmia_via_tor(query: str) -> List[Dict]:
    """Search Ahmia directly via Tor (more complete than clearnet version)."""
    if not is_tor_running():
        return []
    session = make_tor_session()
    results = []
    try:
        # Ahmia clearnet but through Tor for anonymity
        r = session.get(
            f"https://ahmia.fi/search/?q={quote_plus(query)}",
            timeout=25
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            for li in soup.select("li.result")[:10]:
                title_el = li.select_one("h4") or li.select_one("a")
                snippet_el = li.select_one(".description") or li.select_one("p")
                url_el = li.select_one("cite") or li.select_one("a")
                results.append({
                    "source": "ahmia_tor",
                    "result_type": "tor_index",
                    "title": title_el.get_text(strip=True) if title_el else "Tor page",
                    "url": url_el.get_text(strip=True) if url_el else None,
                    "snippet": snippet_el.get_text(strip=True)[:300] if snippet_el else None,
                })
    except Exception as e:
        logger.debug("Ahmia via Tor failed: %s", e)
    return results


# ── BreachDirectory (free, no key) ────────────────────────────────────────────

def search_breachdirectory(query: str) -> List[Dict]:
    """Search BreachDirectory for leaked credentials. Free, no auth required."""
    results = []
    try:
        r = requests.get(
            f"https://breachdirectory.org/api?func=auto&term={quote_plus(query)}",
            headers=HEADERS, timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            for entry in (data.get("result") or data.get("data") or [])[:20]:
                if isinstance(entry, dict):
                    results.append({
                        "source": "breachdirectory",
                        "result_type": "credential_leak",
                        "title": f"Breach: {entry.get('sources', ['Unknown'])[0] if entry.get('sources') else 'Unknown'}",
                        "snippet": f"Found in: {', '.join(str(s) for s in (entry.get('sources') or [])[:5])}. "
                                   f"Has password: {'Yes' if entry.get('password') else 'No'}",
                        "sha1": entry.get("sha1"),
                        "sources": entry.get("sources", []),
                    })
                elif isinstance(entry, str):
                    results.append({
                        "source": "breachdirectory",
                        "result_type": "credential_leak",
                        "title": f"Breach record found",
                        "snippet": str(entry)[:200],
                    })
    except Exception as e:
        logger.debug("BreachDirectory error: %s", e)
    return results


# ── DarkSearch.io (10 free searches/day) ─────────────────────────────────────

DARKSEARCH_URL = "https://darksearch.io/api/search"


def search_darksearch(query: str, api_key: str = "") -> List[Dict]:
    """Search dark web via DarkSearch.io. Free tier: 10 searches/day."""
    results = []
    try:
        params = {"query": query, "page": 1}
        headers = {**HEADERS}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        r = requests.get(DARKSEARCH_URL, params=params, headers=headers, timeout=20)
        if r.status_code == 200:
            data = r.json()
            for item in (data.get("data") or [])[:10]:
                results.append({
                    "source": "darksearch",
                    "result_type": "darkweb_page",
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("description", "")[:300],
                })
        elif r.status_code == 429:
            results.append({
                "source": "darksearch",
                "result_type": "rate_limited",
                "title": "DarkSearch daily limit reached",
                "snippet": "Free tier: 10 searches/day. Add darksearch_api_key for unlimited.",
            })
    except Exception as e:
        logger.debug("DarkSearch error: %s", e)
    return results


# ── Ahmia.fi (Tor index, free) ────────────────────────────────────────────────

def search_ahmia(query: str) -> List[Dict]:
    """Search Ahmia Tor index. Free, no auth."""
    results = []
    try:
        r = requests.get(
            f"https://ahmia.fi/search/?q={quote_plus(query)}",
            headers=HEADERS, timeout=20
        )
        if r.status_code != 200:
            return results
        soup = BeautifulSoup(r.text, "lxml")
        for li in soup.select("li.result")[:10]:
            title_el = li.select_one("h4") or li.select_one("a")
            snippet_el = li.select_one(".description") or li.select_one("p")
            url_el = li.select_one("cite") or li.select_one("a")
            results.append({
                "source": "ahmia",
                "result_type": "tor_index",
                "title": title_el.get_text(strip=True) if title_el else "Tor page",
                "url": url_el.get_text(strip=True) if url_el else None,
                "snippet": snippet_el.get_text(strip=True)[:300] if snippet_el else None,
            })
    except Exception as e:
        logger.debug("Ahmia error: %s", e)
    return results


# ── IntelligenceX (public demo key) ──────────────────────────────────────────

INTELX_SEARCH = "https://2.intelx.io/intelligent/search"
INTELX_RESULT = "https://2.intelx.io/intelligent/search/result?id={id}&limit=10"
INTELX_PUBLIC_KEY = "00000000-0000-0000-0000-000000000000"


def search_intelx(term: str, api_key: str = "") -> List[Dict]:
    """Search IntelligenceX. Free demo key works but is rate-limited."""
    results = []
    key = api_key or INTELX_PUBLIC_KEY
    headers = {"x-key": key, "Content-Type": "application/json"}
    try:
        payload = {
            "term": term, "buckets": [], "lookuplevel": 0,
            "maxresults": 15, "timeout": 20,
            "datefrom": "", "dateto": "", "sort": 4, "media": 0, "terminate": [],
        }
        r = requests.post(INTELX_SEARCH, json=payload, headers=headers, timeout=25)
        if r.status_code != 200:
            return results
        search_id = r.json().get("id")
        if not search_id:
            return results

        time.sleep(2)
        r2 = requests.get(INTELX_RESULT.format(id=search_id), headers=headers, timeout=20)
        if r2.status_code == 200:
            for rec in r2.json().get("records", [])[:10]:
                bucket = str(rec.get("bucket", "")).lower()
                rtype = "paste" if "paste" in bucket else "credential_leak" if "leak" in bucket else "darkweb_page"
                results.append({
                    "source": "intelx",
                    "result_type": rtype,
                    "title": rec.get("name") or rec.get("systemid", "IntelX result"),
                    "url": f"https://intelx.io/?did={rec.get('storageid')}",
                    "snippet": f"Bucket: {rec.get('bucket')} | Date: {str(rec.get('date',''))[:10]}",
                })
    except Exception as e:
        logger.debug("IntelX error: %s", e)
    return results


# ── HIBP (needs API key, $3.50/month) ────────────────────────────────────────

def check_hibp(email: str, api_key: str) -> List[Dict]:
    """Check Have I Been Pwned. Requires API key."""
    if not api_key:
        return []
    results = []
    try:
        headers = {**HEADERS, "hibp-api-key": api_key}
        r = requests.get(
            f"https://haveibeenpwned.com/api/v3/breachedaccount/{email}?truncateResponse=false",
            headers=headers, timeout=15
        )
        if r.status_code == 200:
            for b in r.json():
                results.append({
                    "source": "hibp",
                    "result_type": "credential_leak",
                    "title": f"Breach: {b.get('Name')}",
                    "url": f"https://haveibeenpwned.com/account/{email}",
                    "snippet": f"Domain: {b.get('Domain')} | Date: {b.get('BreachDate')} | "
                               f"Data: {', '.join(b.get('DataClasses', [])[:5])}",
                    "breach_date": b.get("BreachDate"),
                    "data_classes": b.get("DataClasses", []),
                    "is_verified": b.get("IsVerified"),
                })
        time.sleep(1.5)
        r2 = requests.get(
            f"https://haveibeenpwned.com/api/v3/pasteaccount/{email}",
            headers=headers, timeout=15
        )
        if r2.status_code == 200:
            for p in r2.json():
                results.append({
                    "source": "hibp_paste",
                    "result_type": "paste",
                    "title": p.get("Title") or "Untitled paste",
                    "url": f"{p.get('Source','')}/{p.get('Id','')}",
                    "snippet": f"Found on {p.get('Source','')} on {p.get('Date','?')}",
                })
    except Exception as e:
        logger.debug("HIBP error: %s", e)
    return results


# ── Dehashed (needs API key, $5/month) ───────────────────────────────────────

def search_dehashed(query: str, api_key: str, query_type: str = "username") -> List[Dict]:
    """
    Search Dehashed breach database. $5/month.
    query_type: username | email | name | phone | address | password | hashed_password | ip_address
    Returns full records: name, email, address, phone, password (hashed).
    """
    if not api_key:
        return []
    results = []
    try:
        r = requests.get(
            f"https://api.dehashed.com/search?query={query_type}:{quote_plus(query)}&size=20",
            auth=("your@email.com", api_key),  # Dehashed uses email+apikey as basic auth
            headers={**HEADERS, "Accept": "application/json"},
            timeout=20
        )
        if r.status_code == 200:
            for entry in r.json().get("entries", [])[:20]:
                fields = []
                if entry.get("name"): fields.append(f"Name: {entry['name']}")
                if entry.get("email"): fields.append(f"Email: {entry['email']}")
                if entry.get("username"): fields.append(f"Username: {entry['username']}")
                if entry.get("phone"): fields.append(f"Phone: {entry['phone']}")
                if entry.get("address"): fields.append(f"Address: {entry['address']}")
                if entry.get("password"): fields.append(f"Password: {entry['password']}")
                results.append({
                    "source": "dehashed",
                    "result_type": "full_breach_record",
                    "title": f"Dehashed: {entry.get('database_name', 'Unknown breach')}",
                    "snippet": " | ".join(fields[:5]),
                    "raw": {k: v for k, v in entry.items()
                            if k in ("name", "email", "username", "phone", "address", "ip_address")
                            and v},
                })
    except Exception as e:
        logger.debug("Dehashed error: %s", e)
    return results


# ── LeakCheck (free tier: 1/day without key) ─────────────────────────────────

def search_leakcheck(email: str) -> List[Dict]:
    """LeakCheck.io — free tier (very limited)."""
    results = []
    try:
        r = requests.get(
            f"https://leakcheck.io/api/public?key=&type=email&query={quote_plus(email)}",
            headers=HEADERS, timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("success") and data.get("found"):
                for source in data.get("sources", []):
                    results.append({
                        "source": "leakcheck",
                        "result_type": "credential_leak",
                        "title": f"LeakCheck: {source}",
                        "snippet": f"Email found in {source}",
                    })
    except Exception as e:
        logger.debug("LeakCheck error: %s", e)
    return results


# ── Paste search ──────────────────────────────────────────────────────────────

def search_pastes(query: str) -> List[Dict]:
    """Search public paste sites via Google dork. Returns manual search links."""
    from urllib.parse import quote_plus
    sites = "site:pastebin.com OR site:paste.ee OR site:ghostbin.com OR site:privatebin.net OR site:hastebin.com"
    q = f'{sites} "{query}"'
    return [{
        "source": "paste_google_dork",
        "result_type": "manual_search",
        "title": f"Google paste search for '{query}'",
        "url": f"https://www.google.com/search?q={quote_plus(q)}",
        "snippet": "Click to search paste sites via Google",
    }]


# ── Orchestrator ──────────────────────────────────────────────────────────────

def gather_enhanced_intel(
    username: str,
    email: str = None,
    display_name: str = None,
    config: dict = None,
) -> dict:
    """
    Run all dark web and breach intelligence sources.
    Returns {hits: [...], manual_searches: [...], sources_checked: [...]}
    """
    config = config or {}
    hits = []
    manual_searches = []
    sources_checked = []

    queries = []
    if username:
        queries.append(("username", username))
    if email:
        queries.append(("email", email))
    if display_name and display_name != username:
        queries.append(("name", display_name))

    for query_type, query in queries:
        logger.info("Dark web search: %s=%s", query_type, query)

        # BreachDirectory (free)
        sources_checked.append("BreachDirectory")
        hits.extend(search_breachdirectory(query))
        time.sleep(0.5)

        # DarkSearch (free tier)
        sources_checked.append("DarkSearch.io")
        ds_hits = search_darksearch(query, config.get("darksearch_api_key", ""))
        hits.extend([h for h in ds_hits if h.get("result_type") != "rate_limited"])
        if any(h.get("result_type") == "rate_limited" for h in ds_hits):
            manual_searches.append({
                "label": f"DarkSearch.io manual: '{query}'",
                "url": f"https://darksearch.io/?query={quote_plus(query)}",
            })
        time.sleep(0.5)

        # Ahmia clearnet
        sources_checked.append("Ahmia (Tor index)")
        hits.extend(search_ahmia(query))
        time.sleep(0.5)

        # IntelligenceX
        sources_checked.append("IntelligenceX")
        hits.extend(search_intelx(query, config.get("intelx_api_key", "")))
        time.sleep(0.5)

        # ── Tor .onion search engines (if Tor is running) ──────────────────
        if is_tor_running():
            # Search .onion engines directly via Tor
            sources_checked.append("Ahmia (via Tor)")
            hits.extend(search_ahmia_via_tor(query))
            time.sleep(1)

            sources_checked.append(".onion search engines")
            onion_hits = search_onion_engines(query)
            hits.extend(onion_hits)
            if onion_hits:
                logger.info("Onion search found %d results for '%s'", len(onion_hits), query)
            time.sleep(1)
        else:
            manual_searches.append({
                "label": "Start Tor for .onion search: sudo service tor start",
                "url": f"https://ahmia.fi/search/?q={quote_plus(query)}",
            })

        # HIBP (email only, needs key)
        if query_type == "email":
            if config.get("hibp_api_key"):
                sources_checked.append("HIBP (breach + paste)")
                hits.extend(check_hibp(query, config["hibp_api_key"]))
            else:
                manual_searches.append({
                    "label": f"HIBP (set hibp_api_key in config for auto-check): {query}",
                    "url": f"https://haveibeenpwned.com/account/{quote_plus(query)}",
                })

        # Dehashed (any query type, needs key)
        if config.get("dehashed_api_key"):
            sources_checked.append("Dehashed")
            hits.extend(search_dehashed(query, config["dehashed_api_key"], query_type))

        # LeakCheck (email only, limited free)
        if query_type == "email":
            lc_hits = search_leakcheck(query)
            if lc_hits:
                sources_checked.append("LeakCheck.io")
                hits.extend(lc_hits)

        # Paste sites manual search
        manual_searches.append({
            "label": f"Paste site search: '{query}'",
            "url": f"https://www.google.com/search?q={quote_plus(chr(34) + query + chr(34) + ' site:pastebin.com OR site:paste.ee OR site:ghostbin.com')}",
        })

        # General OSINT dork
        manual_searches.append({
            "label": f"OSINT dork: '{query}'",
            "url": f"https://www.google.com/search?q={quote_plus(chr(34) + query + chr(34))}",
        })

    # Deduplicate hits by title+url
    seen = set()
    unique_hits = []
    for h in hits:
        key = (h.get("title", ""), h.get("url", ""))
        if key not in seen:
            seen.add(key)
            unique_hits.append(h)

    return {
        "hits": unique_hits,
        "manual_searches": manual_searches,
        "sources_checked": list(dict.fromkeys(sources_checked)),
        "total_found": len(unique_hits),
    }
