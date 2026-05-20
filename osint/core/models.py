"""
Core data models for OSINT results.
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime


@dataclass
class MediaItem:
    url: str
    media_type: str  # image | video | gif
    width: Optional[int] = None
    height: Optional[int] = None
    duration_seconds: Optional[float] = None
    perceptual_hash: Optional[str] = None  # pHash for reverse image search


@dataclass
class PostData:
    platform: str
    post_id: str
    url: str
    text: Optional[str] = None
    created_at: Optional[datetime] = None
    author_username: Optional[str] = None
    author_display_name: Optional[str] = None
    author_id: Optional[str] = None
    author_profile_url: Optional[str] = None
    like_count: Optional[int] = None
    repost_count: Optional[int] = None
    reply_count: Optional[int] = None
    view_count: Optional[int] = None
    media: List[MediaItem] = field(default_factory=list)
    hashtags: List[str] = field(default_factory=list)
    mentions: List[str] = field(default_factory=list)
    urls: List[str] = field(default_factory=list)
    language: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountData:
    platform: str
    username: str
    display_name: Optional[str] = None
    user_id: Optional[str] = None
    bio: Optional[str] = None
    created_at: Optional[datetime] = None
    followers: Optional[int] = None
    following: Optional[int] = None
    post_count: Optional[int] = None
    verified: bool = False
    profile_image_url: Optional[str] = None
    website: Optional[str] = None
    location: Optional[str] = None
    email: Optional[str] = None
    linked_accounts: List[str] = field(default_factory=list)
    recent_posts: List[PostData] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CrossPostResult:
    platform: str
    url: str
    posted_at: Optional[datetime] = None
    author: Optional[str] = None
    similarity_score: float = 0.0
    match_type: str = ""  # text_exact | text_fuzzy | media_hash | url_mention


@dataclass
class SherlockResult:
    platform: str
    url: str
    status: str  # found | not_found | rate_limited | error
    response_time_ms: Optional[float] = None


@dataclass
class DarkWebResult:
    source: str           # ahmia | darksearch | paste | breach
    url: Optional[str] = None
    title: Optional[str] = None
    snippet: Optional[str] = None
    indexed_at: Optional[datetime] = None
    result_type: str = ""  # forum_post | marketplace | paste | credential_leak


@dataclass
class RedFlag:
    severity: str         # high | medium | low
    category: str         # account_age | content | network | darkweb | behavior
    description: str
    evidence: Optional[str] = None


@dataclass
class OSINTReport:
    input_url: str
    generated_at: datetime
    platform: str
    post: Optional[PostData] = None
    account: Optional[AccountData] = None
    cross_posts: List[CrossPostResult] = field(default_factory=list)
    username_search: List[SherlockResult] = field(default_factory=list)
    darkweb_hits: List[DarkWebResult] = field(default_factory=list)
    red_flags: List[RedFlag] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
