"""
Twitter/X scraper.

Strategy (in priority order):
1. Official API v2 via Bearer token (if configured)
2. fxtwitter / vxtwitter  — third-party embed API, no auth, rich JSON
3. publish.twitter.com oEmbed — basic info, very stable
4. Twitter syndication API  — often blocked but tried as last resort
5. Nitter instances         — unstable, tried only if above all fail
"""
import re
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List

from bs4 import BeautifulSoup

from .base import BaseScraper
from ..core.models import PostData, AccountData, MediaItem
from ..core.utils import get, jitter, safe_int, clean_text

logger = logging.getLogger(__name__)

# Third-party Twitter embed APIs (no auth required, return rich JSON)
FXTWITTER_API  = "https://api.fxtwitter.com/{username}/status/{tweet_id}"
VXTWITTER_API  = "https://api.vxtwitter.com/{username}/status/{tweet_id}"

# Twitter's official oEmbed (very stable, basic fields)
OEMBED_URL     = "https://publish.twitter.com/oembed?url=https://twitter.com/{username}/status/{tweet_id}&dnt=true&omit_script=true"

# Syndication (often blocked by Twitter)
SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result?id={}&lang=en"

NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.cz",
    "https://nitter.1d4.us",
]


class TwitterScraper(BaseScraper):
    platform = "twitter"

    # ------------------------------------------------------------------ #
    #  Public entry points                                                 #
    # ------------------------------------------------------------------ #

    def get_post(self, post_id: str, username: Optional[str] = None) -> Optional[PostData]:
        # 1. Official API
        if self.config.get("twitter_bearer_token"):
            post = self._api_get_tweet(post_id)
            if post:
                return post

        # 2. fxtwitter / vxtwitter (best unauthenticated fallback)
        if username:
            post = self._fxtwitter_get(username, post_id)
            if post:
                return post

        # 3. oEmbed (always public, minimal fields)
        if username:
            post = self._oembed_get(username, post_id)
            if post:
                return post

        # 4. Syndication API
        post = self._syndication_get_tweet(post_id)
        if post:
            return post

        # 5. Nitter
        if username:
            post = self._nitter_get_tweet(username, post_id)
        return post

    def get_account(self, username: str) -> Optional[AccountData]:
        # Official API
        if self.config.get("twitter_bearer_token"):
            account = self._api_get_user(username)
            if account:
                return account
        # fxtwitter returns author info alongside the tweet; stored in _last_author
        cached = getattr(self, "_last_author", None)
        if cached and cached.username == username:
            return cached
        # Nitter profile page
        return self._nitter_get_user(username)

    # ------------------------------------------------------------------ #
    #  fxtwitter / vxtwitter (no-auth, rich data)                         #
    # ------------------------------------------------------------------ #

    def _fxtwitter_get(self, username: str, tweet_id: str) -> Optional[PostData]:
        for api_url_tpl in (FXTWITTER_API, VXTWITTER_API):
            url = api_url_tpl.format(username=username, tweet_id=tweet_id)
            resp = get(self.session, url, headers={"Accept": "application/json"})
            if not resp:
                continue
            try:
                data = resp.json()
            except Exception:
                continue

            # fxtwitter wraps under "tweet"; vxtwitter returns fields at top level
            tweet = data.get("tweet") or data
            if not tweet or not tweet.get("text"):
                continue

            post = self._parse_fxtwitter(tweet, tweet_id, username)
            if post:
                # Build author from nested "author" (fxtwitter) or flat user_* fields (vxtwitter)
                if tweet.get("author"):
                    self._last_author = self._parse_fxtwitter_author(tweet["author"])
                elif tweet.get("user_screen_name") or tweet.get("user_name"):
                    self._last_author = self._parse_vxtwitter_author(tweet)
                return post
        return None

    @staticmethod
    def _parse_fxtwitter(tweet: dict, tweet_id: str, username: str) -> Optional[PostData]:
        # Nested author (fxtwitter) or flat user_* (vxtwitter)
        author = tweet.get("author") or {}
        uname = (
            author.get("screen_name") or author.get("handle")
            or tweet.get("user_screen_name")
            or username
        )

        # Date: epoch or ISO string
        created_at = None
        if tweet.get("date_epoch"):
            created_at = datetime.fromtimestamp(tweet["date_epoch"], tz=timezone.utc)
        elif tweet.get("date"):
            try:
                created_at = datetime.fromisoformat(tweet["date"].replace("Z", "+00:00"))
            except Exception:
                pass
        elif tweet.get("created_at"):
            try:
                created_at = datetime.strptime(
                    tweet["created_at"], "%a %b %d %H:%M:%S +0000 %Y"
                ).replace(tzinfo=timezone.utc)
            except Exception:
                pass

        # Media — fxtwitter uses nested photos/videos, vxtwitter uses media_extended
        media_items: List[MediaItem] = []

        def _extract_media_block(block: dict):
            for photo in block.get("photos", []) or []:
                url = photo.get("url") or photo.get("media_url_https", "")
                if url and not any(x.url == url for x in media_items):
                    media_items.append(MediaItem(
                        url=url, media_type="image",
                        width=photo.get("width"), height=photo.get("height"),
                    ))
            for video in block.get("videos", []) or []:
                url = video.get("url") or video.get("media_url_https", "")
                if url and not any(x.url == url for x in media_items):
                    media_items.append(MediaItem(
                        url=url, media_type="video",
                        width=video.get("width"), height=video.get("height"),
                        duration_seconds=video.get("duration"),
                    ))

        _extract_media_block(tweet.get("media") or {})
        # Also extract media from quoted tweet — fxtwitter wraps it as "quote.media",
        # vxtwitter wraps it as "qrt" with its own media_extended/mediaURLs
        _extract_media_block((tweet.get("quote") or {}).get("media") or {})

        def _extract_media_extended(items):
            for m in items or []:
                murl = m.get("url") or m.get("media_url_https", "")
                mtype = m.get("type", "image")
                if murl and not any(x.url == murl for x in media_items):
                    media_items.append(MediaItem(
                        url=murl,
                        media_type="video" if mtype in ("video", "animated_gif") else "image",
                        width=(m.get("size") or {}).get("width"),
                        height=(m.get("size") or {}).get("height"),
                    ))

        # vxtwitter flat media_extended list (tweet itself + quoted tweet)
        _extract_media_extended(tweet.get("media_extended"))
        _extract_media_extended((tweet.get("qrt") or {}).get("media_extended"))

        # Hashtags / mentions
        hashtags = [h.lstrip("#") for h in (tweet.get("hashtags") or [])]
        mentions = [m.lstrip("@") for m in (tweet.get("mentions") or [])]
        # Also extract from text if not provided
        text = tweet.get("text") or tweet.get("full_text") or ""
        if not hashtags:
            hashtags = re.findall(r"#(\w+)", text)
        if not mentions:
            mentions = re.findall(r"@(\w+)", text)

        return PostData(
            platform="twitter",
            post_id=tweet_id,
            url=tweet.get("url") or f"https://twitter.com/{uname}/status/{tweet_id}",
            text=clean_text(text),
            created_at=created_at,
            author_username=uname,
            author_display_name=author.get("name"),
            author_id=str(author.get("id") or ""),
            author_profile_url=f"https://twitter.com/{uname}",
            like_count=safe_int(tweet.get("likes")),
            repost_count=safe_int(tweet.get("retweets")),
            reply_count=safe_int(tweet.get("replies")),
            view_count=safe_int(tweet.get("views")),
            media=media_items,
            hashtags=hashtags,
            mentions=mentions,
            urls=[u.get("expanded_url", "") for u in (tweet.get("urls") or [])],
            language=tweet.get("lang"),
            raw=tweet,
        )

    @staticmethod
    def _parse_fxtwitter_author(author: dict) -> Optional[AccountData]:
        if not author:
            return None
        username = author.get("screen_name") or author.get("handle") or ""
        joined = None
        if author.get("joined"):
            try:
                joined = datetime.fromisoformat(author["joined"].replace("Z", "+00:00"))
            except Exception:
                pass
        return AccountData(
            platform="twitter",
            username=username,
            display_name=author.get("name"),
            user_id=str(author.get("id") or ""),
            bio=clean_text(author.get("description")),
            created_at=joined,
            followers=safe_int(author.get("followers")),
            following=safe_int(author.get("following")),
            post_count=safe_int(author.get("tweets")),
            verified=bool(author.get("verified")),
            profile_image_url=author.get("avatar_url"),
            website=author.get("website") or author.get("url"),
            location=author.get("location"),
            raw=author,
        )

    @staticmethod
    def _parse_vxtwitter_author(tweet: dict) -> AccountData:
        """Build AccountData from vxtwitter's flat user_* fields."""
        username = tweet.get("user_screen_name") or ""
        return AccountData(
            platform="twitter",
            username=username,
            display_name=tweet.get("user_name"),
            profile_image_url=tweet.get("user_profile_image_url"),
            raw={"source": "vxtwitter_flat"},
        )

    # ------------------------------------------------------------------ #
    #  Twitter oEmbed (always public, returns basic info)                  #
    # ------------------------------------------------------------------ #

    def _oembed_get(self, username: str, tweet_id: str) -> Optional[PostData]:
        url = OEMBED_URL.format(username=username, tweet_id=tweet_id)
        resp = get(self.session, url)
        if not resp:
            return None
        try:
            d = resp.json()
        except Exception:
            return None

        # Extract tweet text from the HTML embed
        text = None
        if d.get("html"):
            soup = BeautifulSoup(d["html"], "lxml")
            p = soup.find("p")
            if p:
                text = clean_text(p.get_text())

        author_url = d.get("author_url", "")
        uname = author_url.rstrip("/").split("/")[-1] if author_url else username

        return PostData(
            platform="twitter",
            post_id=tweet_id,
            url=f"https://twitter.com/{uname}/status/{tweet_id}",
            text=text,
            author_username=uname,
            author_display_name=d.get("author_name"),
            author_profile_url=author_url or f"https://twitter.com/{uname}",
            raw=d,
        )

    # ------------------------------------------------------------------ #
    #  Official API v2                                                     #
    # ------------------------------------------------------------------ #

    def _api_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config['twitter_bearer_token']}"}

    def _api_get_tweet(self, tweet_id: str) -> Optional[PostData]:
        url = (
            f"https://api.twitter.com/2/tweets/{tweet_id}"
            "?tweet.fields=created_at,author_id,text,public_metrics,attachments,entities,lang"
            "&expansions=author_id,attachments.media_keys"
            "&media.fields=type,url,preview_image_url,width,height,duration_ms"
            "&user.fields=name,username,description,created_at,public_metrics,verified"
        )
        resp = get(self.session, url, headers=self._api_headers())
        if not resp:
            return None
        data = resp.json()
        tweet = data.get("data", {})
        includes = data.get("includes", {})
        users = {u["id"]: u for u in includes.get("users", [])}
        media_map = {m["media_key"]: m for m in includes.get("media", [])}

        author = users.get(tweet.get("author_id"), {})
        metrics = tweet.get("public_metrics", {})
        media_keys = tweet.get("attachments", {}).get("media_keys", [])
        media_items = [self._parse_api_media(media_map[k]) for k in media_keys if k in media_map]
        entities = tweet.get("entities", {})

        return PostData(
            platform="twitter",
            post_id=tweet_id,
            url=f"https://twitter.com/{author.get('username', '_')}/status/{tweet_id}",
            text=tweet.get("text"),
            created_at=_parse_dt(tweet.get("created_at")),
            author_username=author.get("username"),
            author_display_name=author.get("name"),
            author_id=tweet.get("author_id"),
            author_profile_url=f"https://twitter.com/{author.get('username')}" if author.get("username") else None,
            like_count=metrics.get("like_count"),
            repost_count=metrics.get("retweet_count"),
            reply_count=metrics.get("reply_count"),
            view_count=metrics.get("impression_count"),
            media=media_items,
            hashtags=[h["tag"] for h in entities.get("hashtags", [])],
            mentions=[m["username"] for m in entities.get("mentions", [])],
            urls=[u["expanded_url"] for u in entities.get("urls", [])],
            language=tweet.get("lang"),
            raw=tweet,
        )

    @staticmethod
    def _parse_api_media(m: dict) -> MediaItem:
        return MediaItem(
            url=m.get("url") or m.get("preview_image_url", ""),
            media_type=m.get("type", "image"),
            width=m.get("width"),
            height=m.get("height"),
            duration_seconds=(m.get("duration_ms") or 0) / 1000 or None,
        )

    def _api_get_user(self, username: str) -> Optional[AccountData]:
        url = (
            f"https://api.twitter.com/2/users/by/username/{username}"
            "?user.fields=name,description,created_at,public_metrics,verified,entities,location,url"
        )
        resp = get(self.session, url, headers=self._api_headers())
        if not resp:
            return None
        user = resp.json().get("data", {})
        metrics = user.get("public_metrics", {})
        return AccountData(
            platform="twitter",
            username=user.get("username", username),
            display_name=user.get("name"),
            user_id=user.get("id"),
            bio=clean_text(user.get("description")),
            created_at=_parse_dt(user.get("created_at")),
            followers=metrics.get("followers_count"),
            following=metrics.get("following_count"),
            post_count=metrics.get("tweet_count"),
            verified=user.get("verified", False),
            location=user.get("location"),
            raw=user,
        )

    # ------------------------------------------------------------------ #
    #  Syndication API (no auth, often blocked)                            #
    # ------------------------------------------------------------------ #

    def _syndication_get_tweet(self, tweet_id: str) -> Optional[PostData]:
        url = SYNDICATION_URL.format(tweet_id)
        resp = get(self.session, url)
        if not resp:
            return None
        try:
            d = resp.json()
        except Exception:
            return None
        if "error" in d:
            return None

        user = d.get("user", {})
        media_details = d.get("mediaDetails", [])
        entities = d.get("entities", {})

        media_items = []
        for m in media_details:
            mtype = m.get("type", "photo")
            if mtype == "photo":
                url_m = m.get("media_url_https", "")
            elif mtype in ("video", "animated_gif"):
                variants = m.get("video_info", {}).get("variants", [])
                variants = [v for v in variants if v.get("content_type") == "video/mp4"]
                variants.sort(key=lambda v: v.get("bitrate", 0), reverse=True)
                url_m = variants[0]["url"] if variants else ""
            else:
                url_m = ""
            media_items.append(MediaItem(
                url=url_m,
                media_type="video" if mtype in ("video", "animated_gif") else "image",
                width=m.get("original_info", {}).get("width"),
                height=m.get("original_info", {}).get("height"),
            ))

        created_at_str = d.get("created_at")
        try:
            created_at = datetime.strptime(
                created_at_str, "%a %b %d %H:%M:%S +0000 %Y"
            ).replace(tzinfo=timezone.utc) if created_at_str else None
        except Exception:
            created_at = None

        username = user.get("screen_name", "")
        return PostData(
            platform="twitter",
            post_id=tweet_id,
            url=f"https://twitter.com/{username}/status/{tweet_id}",
            text=d.get("text"),
            created_at=created_at,
            author_username=username,
            author_display_name=user.get("name"),
            author_id=str(user.get("id_str", "")),
            author_profile_url=f"https://twitter.com/{username}",
            like_count=safe_int(d.get("favorite_count")),
            repost_count=safe_int(d.get("retweet_count")),
            reply_count=safe_int(d.get("conversation_count")),
            media=media_items,
            hashtags=[h["text"] for h in entities.get("hashtags", [])],
            mentions=[m["screen_name"] for m in entities.get("user_mentions", [])],
            urls=[u.get("expanded_url", "") for u in entities.get("urls", [])],
            raw=d,
        )

    # ------------------------------------------------------------------ #
    #  Nitter fallback                                                     #
    # ------------------------------------------------------------------ #

    def _nitter_base(self) -> Optional[str]:
        configured = self.config.get("nitter_instance")
        if configured:
            return configured.rstrip("/")
        for base in NITTER_INSTANCES:
            resp = get(self.session, f"{base}/about", timeout=6)
            if resp and resp.status_code == 200:
                return base
        return None

    def _nitter_get_tweet(self, username: str, tweet_id: str) -> Optional[PostData]:
        base = self._nitter_base()
        if not base:
            logger.debug("No reachable Nitter instance")
            return None
        url = f"{base}/{username}/status/{tweet_id}"
        resp = get(self.session, url)
        if not resp:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        item = soup.select_one(".main-tweet .timeline-item") or soup.select_one(".main-tweet")
        if not item:
            return None

        text_el = item.select_one(".tweet-content")
        text = clean_text(text_el.get_text()) if text_el else None

        date_el = item.select_one(".tweet-date a")
        created_at = None
        if date_el and date_el.get("title"):
            try:
                created_at = datetime.strptime(
                    date_el["title"], "%b %d, %Y · %I:%M %p UTC"
                ).replace(tzinfo=timezone.utc)
            except Exception:
                pass

        stats = {}
        for stat in item.select(".tweet-stat"):
            icon = stat.select_one(".icon-retweet, .icon-heart, .icon-comment, .icon-play")
            if icon:
                key = icon["class"][0].replace("icon-", "")
                stats[key] = safe_int(stat.get_text(strip=True))

        media_items = []
        for img in item.select(".attachments img"):
            src = img.get("src", "")
            if src:
                if not src.startswith("http"):
                    src = base + src
                media_items.append(MediaItem(url=src, media_type="image"))
        for vid in item.select(".attachments video source"):
            src = vid.get("src", "")
            if src:
                if not src.startswith("http"):
                    src = base + src
                media_items.append(MediaItem(url=src, media_type="video"))

        return PostData(
            platform="twitter",
            post_id=tweet_id,
            url=f"https://twitter.com/{username}/status/{tweet_id}",
            text=text,
            created_at=created_at,
            author_username=username,
            author_profile_url=f"https://twitter.com/{username}",
            like_count=stats.get("heart"),
            repost_count=stats.get("retweet"),
            reply_count=stats.get("comment"),
            view_count=stats.get("play"),
            media=media_items,
        )

    def _nitter_get_user(self, username: str) -> Optional[AccountData]:
        base = self._nitter_base()
        if not base:
            return None
        url = f"{base}/{username}"
        resp = get(self.session, url)
        if not resp:
            return None
        soup = BeautifulSoup(resp.text, "lxml")
        profile = soup.select_one(".profile-card")
        if not profile:
            return None

        bio_el = profile.select_one(".profile-bio")
        bio = clean_text(bio_el.get_text()) if bio_el else None

        stats = {}
        for stat in profile.select(".profile-stat"):
            label_el = stat.select_one(".profile-stat-header")
            val_el = stat.select_one(".profile-stat-num")
            if label_el and val_el:
                stats[label_el.get_text(strip=True).lower()] = safe_int(val_el.get_text(strip=True))

        joined_el = profile.select_one(".profile-joindate span[title]")
        created_at = None
        if joined_el:
            try:
                created_at = datetime.strptime(
                    joined_el["title"], "%I:%M %p - %d %b %Y"
                ).replace(tzinfo=timezone.utc)
            except Exception:
                pass

        return AccountData(
            platform="twitter",
            username=username,
            display_name=clean_text(
                profile.select_one(".profile-name") and
                profile.select_one(".profile-name").get_text()
            ),
            bio=bio,
            created_at=created_at,
            followers=stats.get("followers"),
            following=stats.get("following"),
            post_count=stats.get("tweets"),
        )


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
