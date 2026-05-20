"""
YouTube scraper via oEmbed + Data API v3 (optional).
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

OEMBED = "https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={}&format=json"
DATA_API_VIDEO = "https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics,contentDetails&id={}&key={}"
DATA_API_CHANNEL = "https://www.googleapis.com/youtube/v3/channels?part=snippet,statistics&id={}&key={}"


class YouTubeScraper(BaseScraper):
    platform = "youtube"

    def get_post(self, post_id: str, username: Optional[str] = None) -> Optional[PostData]:
        api_key = self.config.get("youtube_api_key")
        if api_key:
            post = self._api_get_video(post_id, api_key)
            if post:
                return post
        return self._oembed_get(post_id)

    def get_account(self, username: str) -> Optional[AccountData]:
        api_key = self.config.get("youtube_api_key")
        if api_key:
            # resolve channel id from username
            channel_id = self._resolve_channel_id(username, api_key)
            if channel_id:
                return self._api_get_channel(channel_id, username, api_key)
        return self._web_get_account(username)

    def _oembed_get(self, video_id: str) -> Optional[PostData]:
        url = OEMBED.format(video_id)
        resp = get(self.session, url)
        if not resp:
            return None
        try:
            d = resp.json()
        except Exception:
            return None
        return PostData(
            platform="youtube",
            post_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            text=clean_text(d.get("title")),
            author_username=d.get("author_name"),
            author_display_name=d.get("author_name"),
            author_profile_url=d.get("author_url"),
            media=[MediaItem(
                url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                media_type="image",
                width=d.get("width"),
                height=d.get("height"),
            )],
            raw=d,
        )

    def _api_get_video(self, video_id: str, api_key: str) -> Optional[PostData]:
        url = DATA_API_VIDEO.format(video_id, api_key)
        resp = get(self.session, url)
        if not resp:
            return None
        items = resp.json().get("items", [])
        if not items:
            return None
        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        details = item.get("contentDetails", {})

        created_at = None
        if snippet.get("publishedAt"):
            try:
                created_at = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
            except Exception:
                pass

        channel_id = snippet.get("channelId", "")
        return PostData(
            platform="youtube",
            post_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            text=f"{snippet.get('title', '')}\n\n{snippet.get('description', '')}".strip(),
            created_at=created_at,
            author_username=snippet.get("channelTitle"),
            author_display_name=snippet.get("channelTitle"),
            author_id=channel_id,
            author_profile_url=f"https://www.youtube.com/channel/{channel_id}" if channel_id else None,
            like_count=safe_int(stats.get("likeCount")),
            view_count=safe_int(stats.get("viewCount")),
            reply_count=safe_int(stats.get("commentCount")),
            media=[MediaItem(
                url=snippet.get("thumbnails", {}).get("maxres", {}).get("url", ""),
                media_type="image",
            )],
            raw=item,
        )

    def _resolve_channel_id(self, username: str, api_key: str) -> Optional[str]:
        url = f"https://www.googleapis.com/youtube/v3/channels?part=id&forHandle={username}&key={api_key}"
        resp = get(self.session, url)
        if resp:
            items = resp.json().get("items", [])
            if items:
                return items[0].get("id")
        return None

    def _api_get_channel(self, channel_id: str, username: str, api_key: str) -> Optional[AccountData]:
        url = DATA_API_CHANNEL.format(channel_id, api_key)
        resp = get(self.session, url)
        if not resp:
            return None
        items = resp.json().get("items", [])
        if not items:
            return None
        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})

        created_at = None
        if snippet.get("publishedAt"):
            try:
                created_at = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
            except Exception:
                pass

        return AccountData(
            platform="youtube",
            username=snippet.get("customUrl", username).lstrip("@"),
            display_name=snippet.get("title"),
            user_id=channel_id,
            bio=clean_text(snippet.get("description")),
            created_at=created_at,
            followers=safe_int(stats.get("subscriberCount")),
            post_count=safe_int(stats.get("videoCount")),
            profile_image_url=snippet.get("thumbnails", {}).get("default", {}).get("url"),
            location=snippet.get("country"),
            raw=item,
        )

    def _web_get_account(self, username: str) -> Optional[AccountData]:
        handle = username if username.startswith("@") else f"@{username}"
        url = f"https://www.youtube.com/{handle}"
        resp = get(self.session, url)
        if not resp:
            return None
        m = re.search(r'var ytInitialData\s*=\s*(\{.*?\});\s*</script>', resp.text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
            header = data["header"]["c4TabbedHeaderRenderer"]
            return AccountData(
                platform="youtube",
                username=username,
                display_name=header.get("title"),
                bio=clean_text(header.get("tagline", {}).get("channelTaglineRenderer", {}).get("content")),
                followers=safe_int(header.get("subscriberCountText", {}).get("simpleText", "").split()[0]),
                profile_image_url=(header.get("avatar", {}).get("thumbnails", [{}])[0].get("url")),
                raw=header,
            )
        except Exception:
            return None
