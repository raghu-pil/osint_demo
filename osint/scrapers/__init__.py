from .twitter import TwitterScraper
from .reddit import RedditScraper
from .instagram import InstagramScraper
from .tiktok import TikTokScraper
from .youtube import YouTubeScraper

REGISTRY = {
    "twitter": TwitterScraper,
    "reddit": RedditScraper,
    "instagram": InstagramScraper,
    "tiktok": TikTokScraper,
    "youtube": YouTubeScraper,
}


def get_scraper(platform: str, session=None, config: dict = None):
    cls = REGISTRY.get(platform)
    if not cls:
        return None
    return cls(session=session, config=config or {})
