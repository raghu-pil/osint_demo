"""
Detect platform and extract structured info from any social media URL.
"""
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, parse_qs


PLATFORM_PATTERNS = {
    "twitter": [
        r"(?:twitter|x)\.com/([^/]+)/status/(\d+)",
        r"(?:twitter|x)\.com/([^/]+)$",
        r"t\.co/\w+",
    ],
    "instagram": [
        r"instagram\.com/p/([A-Za-z0-9_-]+)",
        r"instagram\.com/reel/([A-Za-z0-9_-]+)",
        r"instagram\.com/tv/([A-Za-z0-9_-]+)",
        r"instagram\.com/([^/]+)/?$",
    ],
    "reddit": [
        r"reddit\.com/r/([^/]+)/comments/([A-Za-z0-9]+)",
        r"reddit\.com/user/([^/]+)",
        r"redd\.it/([A-Za-z0-9]+)",
    ],
    "facebook": [
        r"facebook\.com/([^/]+)/posts/(\d+)",
        r"facebook\.com/photo\?fbid=(\d+)",
        r"facebook\.com/video\.php\?v=(\d+)",
        r"fb\.watch/([A-Za-z0-9_-]+)",
        r"facebook\.com/([^/]+)/?$",
    ],
    "tiktok": [
        r"tiktok\.com/@([^/]+)/video/(\d+)",
        r"tiktok\.com/@([^/]+)/?$",
        r"vm\.tiktok\.com/([A-Za-z0-9]+)",
    ],
    "youtube": [
        r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]+)",
        r"youtube\.com/shorts/([A-Za-z0-9_-]+)",
        r"youtube\.com/channel/([A-Za-z0-9_-]+)",
        r"youtube\.com/@([A-Za-z0-9_-]+)",
    ],
    "linkedin": [
        r"linkedin\.com/posts/([^_]+)_([A-Za-z0-9-]+)",
        r"linkedin\.com/in/([^/]+)",
    ],
    "telegram": [
        r"t\.me/([^/]+)/(\d+)",
        r"t\.me/([^/]+)/?$",
        r"telegram\.me/([^/]+)",
    ],
    "mastodon": [
        r"mastodon\.\w+/@([^/]+)/(\d+)",
        r"hachyderm\.io/@([^/]+)/(\d+)",
    ],
}


@dataclass
class ParsedURL:
    raw: str
    platform: str
    username: Optional[str] = None
    post_id: Optional[str] = None
    is_profile: bool = False
    normalized: Optional[str] = None


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for platform, patterns in PLATFORM_PATTERNS.items():
        for p in patterns:
            if re.search(p, url_lower):
                return platform
    return "unknown"


def parse_url(url: str) -> ParsedURL:
    url = url.strip()
    platform = detect_platform(url)
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    qs = parse_qs(parsed.query)

    username = None
    post_id = None
    is_profile = False
    normalized = url

    if platform == "twitter":
        m = re.search(r"(?:twitter|x)\.com/([^/?#]+)/status/(\d+)", url)
        if m:
            username, post_id = m.group(1), m.group(2)
            normalized = f"https://twitter.com/{username}/status/{post_id}"
        else:
            m = re.search(r"(?:twitter|x)\.com/([^/?#]+)", url)
            if m:
                username = m.group(1)
                is_profile = True

    elif platform == "instagram":
        m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
        if m:
            post_id = m.group(1)
            normalized = f"https://www.instagram.com/p/{post_id}/"
        else:
            m = re.search(r"instagram\.com/([^/?#]+)", url)
            if m:
                username = m.group(1)
                is_profile = True

    elif platform == "reddit":
        m = re.search(r"reddit\.com/r/([^/]+)/comments/([A-Za-z0-9]+)", url)
        if m:
            post_id = m.group(2)
            normalized = f"https://www.reddit.com/r/{m.group(1)}/comments/{post_id}/"
        else:
            m = re.search(r"reddit\.com/user/([^/?#]+)", url)
            if m:
                username = m.group(1)
                is_profile = True
        # handle redd.it shortlinks
        m2 = re.search(r"redd\.it/([A-Za-z0-9]+)", url)
        if m2 and not post_id:
            post_id = m2.group(1)

    elif platform == "tiktok":
        m = re.search(r"tiktok\.com/@([^/]+)/video/(\d+)", url)
        if m:
            username, post_id = m.group(1), m.group(2)
            normalized = f"https://www.tiktok.com/@{username}/video/{post_id}"
        else:
            m = re.search(r"tiktok\.com/@([^/?#]+)", url)
            if m:
                username = m.group(1)
                is_profile = True

    elif platform == "youtube":
        m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]+)", url)
        if m:
            post_id = m.group(1)
            normalized = f"https://www.youtube.com/watch?v={post_id}"
        else:
            m = re.search(r"youtube\.com/shorts/([A-Za-z0-9_-]+)", url)
            if m:
                post_id = m.group(1)
            else:
                m = re.search(r"youtube\.com/@([A-Za-z0-9_.-]+)", url)
                if m:
                    username = m.group(1)
                    is_profile = True

    elif platform == "facebook":
        m = re.search(r"facebook\.com/([^/?#]+)/posts/(\d+)", url)
        if m:
            username, post_id = m.group(1), m.group(2)
        else:
            m = re.search(r"(?:fbid|v)=(\d+)", url)
            if m:
                post_id = m.group(1)
            else:
                m = re.search(r"fb\.watch/([A-Za-z0-9_-]+)", url)
                if m:
                    post_id = m.group(1)

    elif platform == "linkedin":
        # Post URL: /posts/{username}_{slug}-{activity_id}-{share_code}/
        m = re.search(
            r"linkedin\.com/posts/([^/?#_]+)_.*?-(\d{15,})-[A-Za-z0-9]+",
            url, re.IGNORECASE,
        )
        if m:
            username = m.group(1)
            post_id = m.group(2)
            normalized = url.split("?")[0].rstrip("/") + "/"
        else:
            m = re.search(r"linkedin\.com/in/([^/?#]+)", url, re.IGNORECASE)
            if m:
                username = m.group(1).rstrip("/")
                is_profile = True

    elif platform == "telegram":
        m = re.search(r"t(?:elegram)?\.me/([^/?#]+)/(\d+)", url)
        if m:
            username, post_id = m.group(1), m.group(2)
            normalized = f"https://t.me/{username}/{post_id}"
        else:
            m = re.search(r"t(?:elegram)?\.me/([^/?#]+)", url)
            if m:
                username = m.group(1)
                is_profile = True

    return ParsedURL(
        raw=url,
        platform=platform,
        username=username,
        post_id=post_id,
        is_profile=is_profile,
        normalized=normalized or url,
    )
