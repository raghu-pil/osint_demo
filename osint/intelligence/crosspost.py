"""
Cross-post / repost detection.

Strategies:
1. Perceptual image hashing (pHash) — finds visually similar images across platforms
2. Text fingerprinting — exact and fuzzy text matches via SearXNG
3. URL mention search — finds posts that embed the original URL
4. Archive.org — check if URL was archived (captures earliest known posting date)
"""
import io
import logging
import hashlib
from typing import List, Optional
from urllib.parse import quote_plus
from datetime import datetime, timezone

from ..core.models import PostData, CrossPostResult, MediaItem
from ..core.utils import get, jitter

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Perceptual hashing                                                  #
# ------------------------------------------------------------------ #

def compute_phash(image_bytes: bytes) -> Optional[str]:
    """Return hex pHash string, or None if imaging libs unavailable."""
    try:
        import imagehash
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        return str(imagehash.phash(img))
    except ImportError:
        logger.debug("imagehash/Pillow not installed; skipping pHash")
        return None
    except Exception as e:
        logger.debug("pHash error: %s", e)
        return None


def phash_distance(h1: str, h2: str) -> int:
    """Hamming distance between two pHash strings."""
    try:
        import imagehash
        return imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2)
    except Exception:
        return 64


def enrich_media_hashes(media_items: List[MediaItem], session) -> None:
    """Download media and attach pHash in-place (images only)."""
    for item in media_items:
        if item.media_type != "image" or not item.url:
            continue
        resp = get(session, item.url)
        if resp:
            item.perceptual_hash = compute_phash(resp.content)


# ------------------------------------------------------------------ #
#  Wayback Machine / Archive.org earliest capture                      #
# ------------------------------------------------------------------ #

WAYBACK_AVAILABILITY = "https://archive.org/wayback/available?url={url}&timestamp=19700101"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx?url={url}&output=json&limit=1&fl=timestamp,original,statuscode&from=20000101&to=20991231&fastLatest=false"


def get_archive_info(url: str, session) -> dict:
    """Return earliest archived snapshot info for a URL."""
    encoded = quote_plus(url)
    cdx_url = WAYBACK_CDX.format(url=encoded)
    resp = get(session, cdx_url)
    if not resp:
        return {}
    try:
        rows = resp.json()
        if len(rows) < 2:
            return {}
        header, first = rows[0], rows[1]
        result = dict(zip(header, first))
        ts = result.get("timestamp", "")
        if ts and len(ts) >= 14:
            try:
                result["archived_at"] = datetime(
                    int(ts[:4]), int(ts[4:6]), int(ts[6:8]),
                    int(ts[8:10]), int(ts[10:12]), int(ts[12:14]),
                    tzinfo=timezone.utc,
                ).isoformat()
            except Exception:
                pass
        result["archive_url"] = f"https://web.archive.org/web/{ts}/{url}" if ts else None
        return result
    except Exception:
        return {}


# ------------------------------------------------------------------ #
#  Google/Bing reverse image search stubs                             #
# ------------------------------------------------------------------ #

def reverse_image_search_urls(image_url: str) -> dict:
    """Return search URLs for manual reverse image lookup."""
    encoded = quote_plus(image_url)
    return {
        "google": f"https://lens.google.com/uploadbyurl?url={encoded}",
        "bing": f"https://www.bing.com/images/search?q=imgurl:{encoded}&view=detailv2&iss=sbi",
        "yandex": f"https://yandex.com/images/search?url={encoded}&rpt=imageview",
        "tineye": f"https://tineye.com/search?url={encoded}",
    }


# ------------------------------------------------------------------ #
#  URL mention search                                                  #
# ------------------------------------------------------------------ #

def find_url_mentions(original_url: str, session) -> List[dict]:
    """
    Search for pages that mention / embed the original URL.
    Uses Google's site-exclusion trick via SearXNG if available,
    otherwise falls back to a direct Google search URL (not scraped).
    """
    results = []
    encoded = quote_plus(f'"{original_url}"')
    # We return the search queries as actionable leads rather than scraping Google
    results.append({
        "engine": "google",
        "query_url": f"https://www.google.com/search?q={encoded}",
        "note": "manual check recommended",
    })
    results.append({
        "engine": "bing",
        "query_url": f"https://www.bing.com/search?q={encoded}",
        "note": "manual check recommended",
    })
    return results


# ------------------------------------------------------------------ #
#  Orchestrator                                                        #
# ------------------------------------------------------------------ #

def detect_crossposts(
    post: PostData,
    session,
    searxng_client=None,
) -> List[CrossPostResult]:
    results: List[CrossPostResult] = []

    # 1. Enrich media with pHash
    if post.media:
        enrich_media_hashes(post.media, session)

    # 2. Archive.org check
    archive = get_archive_info(post.url, session)
    if archive.get("archived_at"):
        results.append(CrossPostResult(
            platform="archive.org",
            url=archive.get("archive_url", ""),
            match_type="archive_snapshot",
            similarity_score=1.0,
        ))

    # 3. SearXNG full-text search
    if searxng_client and post.text:
        jitter(0.3)
        xposts = searxng_client.find_crossposts(
            post.text,
            [m.url for m in post.media if m.url],
            post.author_username,
        )
        results.extend(xposts)

    return results
