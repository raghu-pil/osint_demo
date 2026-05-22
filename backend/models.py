from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class CaseStatus(str, Enum):
    PENDING = "pending"
    FRAME_SELECT = "frame_select"   # video uploaded, waiting for user to pick a frame
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProgressStep(BaseModel):
    name: str
    label: str
    status: StepStatus = StepStatus.PENDING
    message: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class GuidanceItem(BaseModel):
    priority: int          # 1 = highest
    severity: str          # critical | high | medium | low | info
    title: str
    detail: str
    action: Optional[str] = None        # what the investigator should do
    pivot_url: Optional[str] = None     # clickable link if applicable
    pivot_label: Optional[str] = None
    category: str = "general"           # account | content | network | identity | geo
    auto_result: Optional[Dict[str, Any]] = None   # result of automated action
    auto_status: Optional[str] = None             # done | failed | skipped


class MediaFileSummary(BaseModel):
    filename: str
    media_type: str
    file_size: int
    hash_sha256: str
    hash_md5: str
    source_url: str
    local_path: str
    metadata: Dict[str, Any] = {}
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None
    gps_address: Optional[str] = None
    ocr_text: Optional[str] = None
    reverse_search_urls: Dict[str, str] = {}


class IdentityPivot(BaseModel):
    identifier: str
    identifier_type: str    # email | phone | username | name
    platforms_found: List[Dict[str, str]] = []
    gravatar_name: Optional[str] = None
    gravatar_avatar: Optional[str] = None
    hibp_breaches: List[Dict] = []
    paste_hits: List[Dict] = []
    manual_search_links: Dict[str, str] = {}


class InvestigateRequest(BaseModel):
    url: str
    notes: Optional[str] = None
    name: Optional[str] = None
    parent_id: Optional[str] = None
    parent_label: Optional[str] = None


class DiscoveredAccount(BaseModel):
    rank: int = 0
    severity_score: int = 0
    severity_label: str = "LOW"
    score_reasons: List[str] = []
    platform: str
    username: Optional[str] = None
    account_url: Optional[str] = None
    post_url: Optional[str] = None
    display_name: Optional[str] = None
    bio: Optional[str] = None
    avatar: Optional[str] = None
    followers: Optional[int] = None
    created_at: Optional[str] = None
    verified: bool = False
    post_text: Optional[str] = None
    post_date: Optional[str] = None
    likes: Optional[int] = None
    reposts: Optional[int] = None
    views: Optional[int] = None
    match_engine: str = ""
    match_thumbnail: Optional[str] = None
    match_title: Optional[str] = None
    source_domain: Optional[str] = None


class PostHistory(BaseModel):
    text: str
    url: Optional[str] = None
    created_at: Optional[str] = None
    likes: Optional[int] = None
    reposts: Optional[int] = None
    replies: Optional[int] = None
    views: Optional[int] = None


class AccountEnrichment(BaseModel):
    # vxtwitter enriched profile fields
    vx_display_name: Optional[str] = None
    vx_location: Optional[str] = None
    vx_created_at: Optional[str] = None
    vx_tweet_count: Optional[int] = None
    vx_following_count: Optional[int] = None
    vx_user_id: Optional[str] = None
    vx_protected: Optional[bool] = None
    # Bio analysis
    bio_links: List[Dict] = []
    bio_emails: List[str] = []
    bio_handles: List[Dict] = []
    # Timeline
    recent_posts: List[Dict] = []
    top_posts: List[Dict] = []
    posting_patterns: Dict[str, Any] = {}
    post_count_scraped: int = 0


class Case(BaseModel):
    id: str
    url: str
    name: Optional[str] = None
    notes: Optional[str] = None
    parent_id: Optional[str] = None
    parent_label: Optional[str] = None
    source_type: str = "url"          # "url" | "media_upload"
    status: CaseStatus = CaseStatus.PENDING
    created_at: str
    updated_at: str

    # Pipeline progress
    steps: List[ProgressStep] = []

    # Core findings
    platform: Optional[str] = None
    post: Optional[Dict[str, Any]] = None
    account: Optional[Dict[str, Any]] = None
    account_enrichment: Optional[AccountEnrichment] = None

    # Analysis
    cross_posts: List[Dict] = []
    username_search: List[Dict] = []
    dark_web: List[Dict] = []
    dark_web_sources_checked: List[str] = []
    red_flags: List[Dict] = []
    risk_score: int = 0
    risk_label: str = "MINIMAL"

    # Media
    media_files: List[MediaFileSummary] = []

    # Identity pivots
    identity_pivots: List[IdentityPivot] = []

    # Analyst guidance
    guidance: List[GuidanceItem] = []

    # Manual search links generated
    manual_searches: List[Dict] = []

    # Auto-action results (keyed by action_id)
    auto_actions: Dict[str, Any] = {}

    # LLM intelligence summary (populated for both URL and media investigations)
    post_intelligence: Optional[Dict[str, Any]] = None

    # Media-first investigation results
    discovered_accounts: List[Dict[str, Any]] = []
    media_investigation: Optional[Dict[str, Any]] = None

    # Live activity log (human-readable, general language)
    logs: List[Dict[str, str]] = []

    errors: List[str] = []
