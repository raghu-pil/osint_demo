"""
SearXNG integration for cross-platform content discovery.
Queries a self-hosted (or public) SearXNG instance.
"""
import logging
from typing import List, Optional
from urllib.parse import quote_plus

from ..core.utils import get, jitter
from ..core.models import CrossPostResult

logger = logging.getLogger(__name__)

DEFAULT_ENGINES = "general,social media,news"


class SearXNGClient:
    def __init__(self, base_url: str, session=None, language: str = "en"):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.language = language

    def search(self, query: str, engines: str = DEFAULT_ENGINES, max_results: int = 20) -> List[dict]:
        url = (
            f"{self.base_url}/search"
            f"?q={quote_plus(query)}"
            f"&format=json"
            f"&engines={quote_plus(engines)}"
            f"&language={self.language}"
        )
        resp = get(self.session, url, headers={"Accept": "application/json"})
        if not resp:
            logger.warning("SearXNG unreachable at %s", self.base_url)
            return []
        try:
            data = resp.json()
            return data.get("results", [])
        except Exception as e:
            logger.error("SearXNG parse error: %s", e)
            return []

    def find_crossposts(self, text: str, media_urls: List[str], author: Optional[str]) -> List[CrossPostResult]:
        """
        Search for the same content across platforms via SearXNG.
        Returns structured CrossPostResult objects.
        """
        results: List[CrossPostResult] = []
        seen_urls: set = set()

        queries = _build_queries(text, media_urls, author)
        for query, match_type in queries:
            jitter(0.5, 0.3)
            hits = self.search(query, max_results=10)
            for hit in hits:
                url = hit.get("url", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                results.append(CrossPostResult(
                    platform=_detect_platform(url),
                    url=url,
                    author=hit.get("metadata"),
                    match_type=match_type,
                    similarity_score=_score(match_type),
                ))

        return results


def _build_queries(text: Optional[str], media_urls: List[str], author: Optional[str]):
    queries = []
    if text and len(text) > 30:
        # First meaningful sentence as exact phrase
        sentence = text.split(".")[0][:120].strip()
        if len(sentence) > 20:
            queries.append((f'"{sentence}"', "text_exact"))
    if text and author:
        short = " ".join(text.split()[:8])
        queries.append((f'{short} {author}', "text_fuzzy"))
    if author:
        queries.append((f'site:twitter.com OR site:reddit.com OR site:instagram.com "{author}"', "author_cross"))
    return queries


def _detect_platform(url: str) -> str:
    url = url.lower()
    for p in ["twitter", "reddit", "instagram", "tiktok", "youtube", "facebook", "telegram", "mastodon"]:
        if p in url:
            return p
    return "web"


def _score(match_type: str) -> float:
    return {"text_exact": 0.95, "text_fuzzy": 0.7, "media_hash": 0.99, "author_cross": 0.5}.get(match_type, 0.5)
