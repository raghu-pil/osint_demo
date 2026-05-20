"""
TikTok scraper via oEmbed + web JSON blob extraction.
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


class TikTokScraper(BaseScraper):
    platform = "tiktok"

    def get_post(self, post_id: str, username: Optional[str] = None) -> Optional[PostData]:
        # oEmbed is the most stable public endpoint
        post = self._oembed_get(post_id, username)
        if post:
            return post
        if username:
            return self._web_get(username, post_id)
        return None

    def get_account(self, username: str) -> Optional[AccountData]:
        return self._web_get_account(username)

    def _oembed_get(self, post_id: str, username: Optional[str]) -> Optional[PostData]:
        target_url = (
            f"https://www.tiktok.com/@{username}/video/{post_id}"
            if username else f"https://vm.tiktok.com/{post_id}"
        )
        url = f"https://www.tiktok.com/oembed?url={target_url}"
        resp = get(self.session, url)
        if not resp:
            return None
        try:
            d = resp.json()
        except Exception:
            return None
        if d.get("error"):
            return None

        return PostData(
            platform="tiktok",
            post_id=post_id,
            url=target_url,
            text=clean_text(d.get("title")),
            author_username=d.get("author_unique_id") or (username or ""),
            author_display_name=d.get("author_name"),
            author_profile_url=d.get("author_url"),
            media=[MediaItem(url=d.get("thumbnail_url", ""), media_type="video")] if d.get("thumbnail_url") else [],
            raw=d,
        )

    def _web_get(self, username: str, post_id: str) -> Optional[PostData]:
        url = f"https://www.tiktok.com/@{username}/video/{post_id}"
        resp = get(self.session, url)
        if not resp:
            return None
        # Extract __UNIVERSAL_DATA_FOR_REHYDRATION__
        m = re.search(r'id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
        if not m:
            m = re.search(r'"ItemModule":\s*(\{.*?\})\s*,\s*"UserModule"', resp.text, re.DOTALL)
        if not m:
            return None
        try:
            blob = json.loads(m.group(1))
        except Exception:
            return None
        item_module = blob.get("ItemModule", {}) or blob
        item = item_module.get(post_id) or next(iter(item_module.values()), {})
        return self._parse_item(item, post_id, username)

    @staticmethod
    def _parse_item(item: dict, post_id: str, username: str) -> Optional[PostData]:
        if not item:
            return None
        author = item.get("author", {})
        stats = item.get("stats", {})
        video = item.get("video", {})
        create_time = item.get("createTime")
        created_at = datetime.fromtimestamp(int(create_time), tz=timezone.utc) if create_time else None

        return PostData(
            platform="tiktok",
            post_id=post_id,
            url=f"https://www.tiktok.com/@{username}/video/{post_id}",
            text=clean_text(item.get("desc")),
            created_at=created_at,
            author_username=author.get("uniqueId", username),
            author_display_name=author.get("nickname"),
            author_id=author.get("id"),
            author_profile_url=f"https://www.tiktok.com/@{author.get('uniqueId', username)}",
            like_count=safe_int(stats.get("diggCount")),
            repost_count=safe_int(stats.get("shareCount")),
            reply_count=safe_int(stats.get("commentCount")),
            view_count=safe_int(stats.get("playCount")),
            media=[MediaItem(
                url=video.get("playAddr", "") or video.get("downloadAddr", ""),
                media_type="video",
                width=video.get("width"),
                height=video.get("height"),
                duration_seconds=video.get("duration"),
            )],
            hashtags=[c.get("hashtagName", "") for c in item.get("challenges", [])],
            raw=item,
        )

    def _web_get_account(self, username: str) -> Optional[AccountData]:
        url = f"https://www.tiktok.com/@{username}"
        resp = get(self.session, url)
        if not resp:
            return None
        m = re.search(r'"UserModule":\s*(\{.*?\})\s*,\s*"', resp.text, re.DOTALL)
        if not m:
            return None
        try:
            blob = json.loads(m.group(1))
            user_data = blob.get("users", {}).get(username, {})
            stats = blob.get("stats", {}).get(username, {})
        except Exception:
            return None

        return AccountData(
            platform="tiktok",
            username=user_data.get("uniqueId", username),
            display_name=user_data.get("nickname"),
            user_id=user_data.get("id"),
            bio=clean_text(user_data.get("signature")),
            followers=safe_int(stats.get("followerCount")),
            following=safe_int(stats.get("followingCount")),
            post_count=safe_int(stats.get("videoCount")),
            verified=user_data.get("verified", False),
            profile_image_url=user_data.get("avatarLarger"),
            raw=user_data,
        )
