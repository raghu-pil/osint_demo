"""
Content analysis: language detection, keyword flagging, media metadata.
"""
import re
import logging
from typing import List, Optional

from ..core.models import PostData, MediaItem, RedFlag

logger = logging.getLogger(__name__)

# High-risk keyword categories
_KEYWORD_CATEGORIES = {
    "violence": [
        r"\b(kill|murder|assassinate|bomb|shoot|stab|attack|massacre|genocide)\b",
    ],
    "extremism": [
        r"\b(jihad|caliphate|infidel|crusade|white.power|race.war|ethnic.cleansing)\b",
    ],
    "doxxing": [
        r"\b(home.address|real.name|phone.number|dox|swat)\b",
        r"\b\d{1,5}\s+\w+\s+(st|ave|blvd|dr|rd|ln|ct)\b",  # address pattern
    ],
    "financial_fraud": [
        r"\b(pump.and.dump|rug.pull|scam|ponzi|crypto.giveaway|send.*bitcoin)\b",
    ],
    "coordinated_campaign": [
        r"\b(hashtag.campaign|brigading|mass.report|bot.army|astroturf)\b",
    ],
}


def analyse_content(post: PostData) -> List[RedFlag]:
    flags: List[RedFlag] = []
    text = (post.text or "").lower()

    for category, patterns in _KEYWORD_CATEGORIES.items():
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                flags.append(RedFlag(
                    severity="high",
                    category="content",
                    description=f"Post contains {category}-related language.",
                    evidence=f"matched: '{m.group()}'",
                ))

    # Excessive hashtags (spam indicator)
    if len(post.hashtags) > 15:
        flags.append(RedFlag(
            severity="medium",
            category="content",
            description=f"Post has {len(post.hashtags)} hashtags — possible spam.",
            evidence=f"hashtags={post.hashtags[:5]}...",
        ))

    # Excessive mentions
    if len(post.mentions) > 10:
        flags.append(RedFlag(
            severity="medium",
            category="content",
            description=f"Post mentions {len(post.mentions)} users — possible coordinated amplification.",
        ))

    # URL shorteners (often used to hide malicious destinations)
    suspicious_shorteners = ["bit.ly", "tinyurl", "t.co", "ow.ly", "goo.gl", "is.gd", "v.gd"]
    for url in post.urls:
        for sh in suspicious_shorteners:
            if sh in url:
                flags.append(RedFlag(
                    severity="low",
                    category="content",
                    description=f"Post contains shortened URL ({sh}) — destination may be hidden.",
                    evidence=url,
                ))
                break

    return flags


def detect_language(text: Optional[str]) -> Optional[str]:
    """Best-effort language detection without external deps."""
    if not text or len(text) < 20:
        return None
    # Check for non-Latin scripts
    if re.search(r'[؀-ۿ]', text):
        return "ar"
    if re.search(r'[一-鿿]', text):
        return "zh"
    if re.search(r'[Ѐ-ӿ]', text):
        return "ru"
    if re.search(r'[ऀ-ॿ]', text):
        return "hi"
    # Default: assume English for Latin scripts
    return "en"


def summarise_media(media_items: List[MediaItem]) -> List[dict]:
    summary = []
    for m in media_items:
        entry = {
            "type": m.media_type,
            "url": m.url,
        }
        if m.width and m.height:
            entry["dimensions"] = f"{m.width}x{m.height}"
        if m.duration_seconds:
            entry["duration_s"] = round(m.duration_seconds, 1)
        if m.perceptual_hash:
            entry["phash"] = m.perceptual_hash
        summary.append(entry)
    return summary
