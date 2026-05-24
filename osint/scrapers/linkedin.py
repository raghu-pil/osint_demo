"""
LinkedIn scraper — public post and profile pages.

LinkedIn blocks most scraping and requires login for full content.
This scraper extracts Open Graph meta tags and visible HTML that are
served server-side before JS renders. It covers the common case of
public posts/articles shared via link.
"""
import re
import logging
from typing import Optional
from datetime import datetime, timezone

from .base import BaseScraper
from ..core.models import PostData, AccountData, MediaItem
from ..core.utils import get, clean_text

logger = logging.getLogger(__name__)

# LinkedIn serves OG tags server-side even for logged-out visitors
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Post URL: /posts/{username}_{slug}-{activity_id}-{share_code}/
_POST_RE = re.compile(
    r"linkedin\.com/posts/([^/?#_]+)_.*?-(\d{15,})-[A-Za-z0-9]+",
    re.IGNORECASE,
)
_PROFILE_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)


def _og(html: str, prop: str) -> Optional[str]:
    m = re.search(
        rf'<meta[^>]+(?:property|name)=["\']og:{prop}["\'][^>]+content=["\'](.*?)["\']',
        html, re.IGNORECASE,
    ) or re.search(
        rf'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:property|name)=["\']og:{prop}["\']',
        html, re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


def _meta(html: str, name: str) -> Optional[str]:
    m = re.search(
        rf'<meta[^>]+name=["\'{name}["\'][^>]+content=["\'](.*?)["\']',
        html, re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


def _scrape_url(session, url: str) -> Optional[str]:
    resp = get(session, url, headers=_HEADERS, timeout=15, allow_redirects=True)
    if resp and resp.status_code == 200:
        return resp.text
    return None


class LinkedInScraper(BaseScraper):
    platform = "linkedin"

    def get_post(self, post_id: str, username: Optional[str] = None) -> Optional[PostData]:
        # Reconstruct the post URL — we need the original URL; post_id alone isn't enough
        # to build the full slug. Fall back to a profile search URL.
        if username:
            # Search LinkedIn for this activity
            url = f"https://www.linkedin.com/posts/{username}-{post_id}/"
            html = _scrape_url(self.session, url)
            if not html:
                # Try the activity URN redirect
                url = f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id}/"
                html = _scrape_url(self.session, url)
        else:
            url = f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id}/"
            html = _scrape_url(self.session, url)

        if not html:
            return None

        return self._parse_post_html(html, post_id, username, url)

    def get_post_from_url(self, raw_url: str) -> Optional[PostData]:
        """Scrape directly from the original URL (preferred — has full slug)."""
        # Strip query string for cleaner fetch
        clean_url = raw_url.split("?")[0].rstrip("/") + "/"
        html = _scrape_url(self.session, clean_url)
        if not html:
            return None

        m = _POST_RE.search(raw_url)
        username = m.group(1) if m else None
        post_id = m.group(2) if m else None
        return self._parse_post_html(html, post_id, username, clean_url)

    def _parse_post_html(self, html: str, post_id: Optional[str],
                         username: Optional[str], url: str) -> Optional[PostData]:
        title = _og(html, "title") or ""
        description = _og(html, "description") or ""
        image_url = (_og(html, "image") or "").replace("&amp;", "&") or None
        og_url = _og(html, "url") or url

        # LinkedIn OG title format: "{post excerpt}… | {Author Name} | {N} comments"
        author_display = None
        reply_count = None
        text = description  # description has the fuller text

        parts = [p.strip() for p in title.split(" | ")]
        if len(parts) >= 2:
            # Last part may be "{N} comments", second-to-last is author name
            last = parts[-1]
            reply_m = re.match(r"(\d+)\s+comments?", last, re.IGNORECASE)
            if reply_m:
                reply_count = int(reply_m.group(1))
                author_display = parts[-2] if len(parts) >= 3 else None
            else:
                author_display = last  # fallback: last segment is the author

        if not text and not author_display:
            return None

        media = []
        if image_url and not image_url.endswith("linkedin-logo"):
            media.append(MediaItem(url=image_url, media_type="image"))

        return PostData(
            platform="linkedin",
            post_id=post_id or "",
            url=og_url,
            text=clean_text(text),
            author_username=username,
            author_display_name=author_display,
            author_profile_url=(
                f"https://www.linkedin.com/in/{username}/" if username else None
            ),
            reply_count=reply_count,
            media=media,
            raw={"og_title": title, "og_description": description},
        )

    def get_account(self, username: str) -> Optional[AccountData]:
        url = f"https://www.linkedin.com/in/{username}/"
        html = _scrape_url(self.session, url)
        if not html:
            return None

        title = _og(html, "title") or ""
        description = _og(html, "description") or ""
        image_url = _og(html, "image")

        # OG title is usually "Name - Title | LinkedIn"
        display_name = title.split(" - ")[0].strip() if " - " in title else title.split("|")[0].strip()
        bio = description or None

        if not display_name:
            return None

        return AccountData(
            platform="linkedin",
            username=username,
            display_name=display_name or None,
            bio=bio,
            profile_image_url=image_url,
            raw={"og_title": title, "og_description": description},
        )
