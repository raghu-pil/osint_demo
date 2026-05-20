"""
Instagram scraper via the public oEmbed endpoint + web JSON API.
Note: heavy anti-scraping — unauthenticated access is limited.
"""
import re
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from .base import BaseScraper
from ..core.models import PostData, AccountData, MediaItem
from ..core.utils import get, safe_int, clean_text

logger = logging.getLogger(__name__)

OEMBED_URL = "https://www.instagram.com/api/v1/oembed/?url=https://www.instagram.com/p/{}/&hidecaption=false"


class InstagramScraper(BaseScraper):
    platform = "instagram"

    def get_post(self, post_id: str, username: Optional[str] = None) -> Optional[PostData]:
        # Try oEmbed (returns limited data)
        post = self._oembed_get_post(post_id)
        if post:
            return post
        # Try the /p/{shortcode}/?__a=1 trick (may require cookies)
        return self._json_get_post(post_id)

    def get_account(self, username: str) -> Optional[AccountData]:
        url = f"https://www.instagram.com/{username}/?__a=1&__d=dis"
        resp = get(self.session, url, headers={"X-Requested-With": "XMLHttpRequest"})
        if not resp:
            return self._web_scrape_account(username)
        try:
            data = resp.json()
            user = data.get("graphql", {}).get("user") or data.get("data", {}).get("user", {})
            if user:
                return self._parse_account(user, username)
        except Exception:
            pass
        return self._web_scrape_account(username)

    def _oembed_get_post(self, post_id: str) -> Optional[PostData]:
        url = OEMBED_URL.format(post_id)
        resp = get(self.session, url)
        if not resp:
            return None
        try:
            d = resp.json()
        except Exception:
            return None
        if "error" in d:
            return None

        return PostData(
            platform="instagram",
            post_id=post_id,
            url=f"https://www.instagram.com/p/{post_id}/",
            text=clean_text(d.get("title")),
            author_username=d.get("author_name"),
            author_display_name=d.get("author_name"),
            author_profile_url=d.get("author_url"),
            media=[MediaItem(url=d.get("thumbnail_url", ""), media_type="image")] if d.get("thumbnail_url") else [],
            raw=d,
        )

    def _json_get_post(self, post_id: str) -> Optional[PostData]:
        url = f"https://www.instagram.com/p/{post_id}/?__a=1&__d=dis"
        resp = get(self.session, url, headers={"X-Requested-With": "XMLHttpRequest"})
        if not resp:
            return None
        try:
            data = resp.json()
            item = data.get("graphql", {}).get("shortcode_media") or data.get("items", [{}])[0]
            return self._parse_post(item, post_id)
        except Exception:
            return None

    @staticmethod
    def _parse_post(item: dict, post_id: str) -> Optional[PostData]:
        if not item:
            return None

        user = item.get("user") or item.get("owner", {})
        caption = ""
        if isinstance(item.get("caption"), dict):
            caption = item["caption"].get("text", "")
        elif isinstance(item.get("edge_media_to_caption"), dict):
            edges = item["edge_media_to_caption"].get("edges", [])
            if edges:
                caption = edges[0].get("node", {}).get("text", "")

        taken_at = item.get("taken_at") or item.get("taken_at_timestamp")
        created_at = datetime.fromtimestamp(int(taken_at), tz=timezone.utc) if taken_at else None

        media_items = []
        if item.get("carousel_media"):
            for cm in item["carousel_media"]:
                media_items.extend(_extract_media(cm))
        else:
            media_items = _extract_media(item)

        return PostData(
            platform="instagram",
            post_id=post_id,
            url=f"https://www.instagram.com/p/{post_id}/",
            text=clean_text(caption),
            created_at=created_at,
            author_username=user.get("username"),
            author_display_name=user.get("full_name"),
            author_id=str(user.get("pk") or user.get("id", "")),
            author_profile_url=f"https://www.instagram.com/{user.get('username')}" if user.get("username") else None,
            like_count=safe_int(item.get("like_count") or item.get("edge_media_preview_like", {}).get("count")),
            reply_count=safe_int(item.get("comment_count") or item.get("edge_media_to_comment", {}).get("count")),
            media=media_items,
            raw=item,
        )

    @staticmethod
    def _parse_account(user: dict, username: str) -> AccountData:
        return AccountData(
            platform="instagram",
            username=user.get("username", username),
            display_name=user.get("full_name"),
            user_id=str(user.get("id") or user.get("pk", "")),
            bio=clean_text(user.get("biography")),
            followers=safe_int(user.get("follower_count") or user.get("edge_followed_by", {}).get("count")),
            following=safe_int(user.get("following_count") or user.get("edge_follow", {}).get("count")),
            post_count=safe_int(user.get("media_count") or user.get("edge_owner_to_timeline_media", {}).get("count")),
            verified=user.get("is_verified", False),
            profile_image_url=user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
            website=user.get("external_url"),
            raw=user,
        )

    def _web_scrape_account(self, username: str) -> Optional[AccountData]:
        """Last resort: parse the JSON blob embedded in page source."""
        resp = get(self.session, f"https://www.instagram.com/{username}/")
        if not resp:
            return None
        m = re.search(r'"user":\s*(\{.*?"biography".*?\})', resp.text, re.DOTALL)
        if not m:
            return None
        try:
            user = json.loads(m.group(1))
            return self._parse_account(user, username)
        except Exception:
            return None


def _extract_media(item: dict) -> list:
    media = []
    if item.get("video_url"):
        media.append(MediaItem(
            url=item["video_url"],
            media_type="video",
            width=item.get("original_width"),
            height=item.get("original_height"),
        ))
    elif item.get("display_url"):
        media.append(MediaItem(
            url=item["display_url"],
            media_type="image",
            width=item.get("original_width"),
            height=item.get("original_height"),
        ))
    return media
