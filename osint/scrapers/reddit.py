"""
Reddit scraper via the public JSON API (no auth required for public posts).
Optional PRAW support when client_id/secret are configured.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from .base import BaseScraper
from ..core.models import PostData, AccountData, MediaItem
from ..core.utils import get, safe_int, clean_text

logger = logging.getLogger(__name__)

REDDIT_JSON = "https://www.reddit.com/comments/{}.json?limit=1&raw_json=1"
REDDIT_USER_JSON = "https://www.reddit.com/user/{}/about.json?raw_json=1"
REDDIT_USER_POSTS = "https://www.reddit.com/user/{}/submitted.json?limit=10&raw_json=1"


class RedditScraper(BaseScraper):
    platform = "reddit"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session.headers.update({"User-Agent": "osint-tool/1.0 (research)"})

    def get_post(self, post_id: str, username: Optional[str] = None) -> Optional[PostData]:
        url = REDDIT_JSON.format(post_id)
        resp = get(self.session, url)
        if not resp:
            return None
        try:
            listing = resp.json()
        except Exception:
            return None
        if not listing or not isinstance(listing, list):
            return None
        post_data = listing[0]["data"]["children"][0]["data"]
        return self._parse_post(post_data)

    def get_account(self, username: str) -> Optional[AccountData]:
        resp = get(self.session, REDDIT_USER_JSON.format(username))
        if not resp:
            return None
        try:
            data = resp.json().get("data", {})
        except Exception:
            return None

        created_at = None
        if data.get("created_utc"):
            created_at = datetime.fromtimestamp(data["created_utc"], tz=timezone.utc)

        # Fetch recent posts
        recent_posts = []
        posts_resp = get(self.session, REDDIT_USER_POSTS.format(username))
        if posts_resp:
            try:
                children = posts_resp.json()["data"]["children"]
                recent_posts = [self._parse_post(c["data"]) for c in children]
            except Exception:
                pass

        return AccountData(
            platform="reddit",
            username=data.get("name", username),
            display_name=data.get("name"),
            user_id=data.get("id"),
            bio=clean_text(data.get("subreddit", {}).get("public_description")),
            created_at=created_at,
            followers=safe_int(data.get("subreddit", {}).get("subscribers")),
            post_count=safe_int(data.get("link_karma", 0)) + safe_int(data.get("comment_karma", 0)),
            verified=data.get("verified", False),
            profile_image_url=data.get("icon_img"),
            recent_posts=recent_posts,
            raw=data,
        )

    @staticmethod
    def _parse_post(p: dict) -> PostData:
        created_at = None
        if p.get("created_utc"):
            created_at = datetime.fromtimestamp(p["created_utc"], tz=timezone.utc)

        media_items = []
        # Gallery
        if p.get("gallery_data") and p.get("media_metadata"):
            for item_meta in p.get("gallery_data", {}).get("items", []):
                media_id = item_meta.get("media_id")
                if media_id and media_id in p["media_metadata"]:
                    m = p["media_metadata"][media_id]
                    if m.get("status") == "valid":
                        src = m.get("s", {})
                        media_items.append(MediaItem(
                            url=src.get("u", "").replace("&amp;", "&"),
                            media_type="image",
                            width=src.get("x"),
                            height=src.get("y"),
                        ))
        # Single image
        elif p.get("url", "").endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            media_items.append(MediaItem(url=p["url"], media_type="image"))
        # Video
        elif p.get("is_video") and p.get("media"):
            vid_url = p["media"].get("reddit_video", {}).get("fallback_url", "")
            if vid_url:
                media_items.append(MediaItem(
                    url=vid_url,
                    media_type="video",
                    width=p["media"]["reddit_video"].get("width"),
                    height=p["media"]["reddit_video"].get("height"),
                    duration_seconds=p["media"]["reddit_video"].get("duration"),
                ))

        sub = p.get("subreddit", "")
        post_id = p.get("id", "")
        return PostData(
            platform="reddit",
            post_id=post_id,
            url=f"https://www.reddit.com{p.get('permalink', '')}",
            text=(p.get("title", "") + "\n\n" + p.get("selftext", "")).strip(),
            created_at=created_at,
            author_username=p.get("author"),
            author_display_name=p.get("author"),
            author_profile_url=f"https://www.reddit.com/user/{p.get('author')}" if p.get("author") else None,
            like_count=safe_int(p.get("score")),
            repost_count=safe_int(p.get("num_crossposts")),
            reply_count=safe_int(p.get("num_comments")),
            view_count=safe_int(p.get("view_count")),
            media=media_items,
            raw=p,
        )
