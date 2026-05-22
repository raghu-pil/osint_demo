"""
Basic web page scraper for non-social-media URLs.
Extracts Open Graph metadata, article content, and lead image.
Used as fallback when the URL platform is "unknown".
"""
import logging
import re
from typing import Dict, Any, Optional
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def scrape_web_page(url: str, timeout: int = 10) -> Dict[str, Any]:
    """
    Fetch a web page and extract structured content via Open Graph tags,
    meta tags, and article body text.

    Returns a dict compatible with case.post structure.
    """
    result = {
        "success": False,
        "url": url,
        "platform": "web",
        "text": None,
        "title": None,
        "description": None,
        "image_url": None,
        "author": None,
        "published_at": None,
        "site_name": None,
        "error": None,
    }

    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        result["error"] = str(e)
        logger.warning("Web scrape failed for %s: %s", url, e)
        return result

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")

        def _meta(prop=None, name=None):
            if prop:
                tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"property": prop})
                if tag:
                    return tag.get("content", "").strip()
            if name:
                tag = soup.find("meta", attrs={"name": name})
                if tag:
                    return tag.get("content", "").strip()
            return ""

        title = (_meta("og:title") or _meta(name="title") or
                 (soup.title.string.strip() if soup.title else ""))
        description = _meta("og:description") or _meta(name="description")
        image_url = _meta("og:image")
        site_name = _meta("og:site_name")
        published_at = (_meta("article:published_time") or
                        _meta("og:article:published_time") or
                        _meta(name="date") or _meta(name="pubdate"))
        author = (_meta("article:author") or _meta(name="author") or
                  _meta("og:article:author"))

        # Extract article body text
        body_text = ""
        article = (soup.find("article") or soup.find(id="article-body") or
                   soup.find(class_=re.compile(r"article|story|content|body", re.I)))
        if article:
            paragraphs = article.find_all("p")
            body_text = " ".join(p.get_text(" ", strip=True) for p in paragraphs[:8])
        if not body_text:
            paragraphs = soup.find_all("p")
            body_text = " ".join(p.get_text(" ", strip=True) for p in paragraphs[:6])

        # Build a combined text field
        parts = []
        if title:
            parts.append(title)
        if description and description != title:
            parts.append(description)
        if body_text:
            parts.append(body_text[:600])
        full_text = "\n\n".join(parts)

        result.update({
            "success": True,
            "title": title,
            "description": description,
            "image_url": image_url if image_url and image_url.startswith("http") else None,
            "author": author,
            "published_at": published_at,
            "site_name": site_name,
            "text": full_text or title,
            "domain": urlparse(url).netloc.replace("www.", ""),
        })

        logger.info("Web scrape: %s — title: %s", url, title[:60] if title else "(none)")

    except Exception as e:
        result["error"] = f"Parse error: {e}"
        logger.warning("Web scrape parse error for %s: %s", url, e)

    return result


def web_scrape_to_post(url: str) -> Optional[Dict]:
    """Return a case.post-compatible dict from a web page scrape."""
    data = scrape_web_page(url)
    if not data.get("success") or not data.get("text"):
        return None
    return {
        "url": url,
        "platform": "web",
        "post_id": None,
        "text": data.get("text", ""),
        "created_at": data.get("published_at"),
        "author_username": None,
        "author_display_name": data.get("author") or data.get("site_name"),
        "engagement": {},
        "hashtags": [],
        "mentions": [],
        "embedded_urls": [],
        "_web_meta": {
            "title": data.get("title"),
            "description": data.get("description"),
            "image_url": data.get("image_url"),
            "site_name": data.get("site_name"),
            "domain": data.get("domain"),
        },
    }
