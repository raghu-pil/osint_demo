"""
Serialize OSINTReport to clean, structured JSON.
"""
import json
from datetime import datetime
from typing import Any

from ..core.models import OSINTReport
from ..analysis.redflags import summarise_flags
from ..analysis.content import summarise_media
from ..intelligence.crosspost import reverse_image_search_urls


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o)} is not JSON serializable")


def report_to_dict(report: OSINTReport) -> dict:
    post = report.post
    account = report.account

    # --- Post section ---
    post_section = None
    if post:
        media_summary = summarise_media(post.media or [])
        # Attach reverse-image-search links to each image
        for item, m in zip(post.media or [], media_summary):
            if item.media_type == "image" and item.url:
                m["reverse_image_search"] = reverse_image_search_urls(item.url)

        post_section = {
            "platform": post.platform,
            "post_id": post.post_id,
            "url": post.url,
            "author_username": post.author_username,
            "author_display_name": post.author_display_name,
            "author_profile_url": post.author_profile_url,
            "text": post.text,
            "created_at": post.created_at,
            "language": post.language or report.metadata.get("detected_language"),
            "engagement": {
                "likes": post.like_count,
                "reposts": post.repost_count,
                "replies": post.reply_count,
                "views": post.view_count,
            },
            "media": media_summary,
            "hashtags": post.hashtags,
            "mentions": post.mentions,
            "embedded_urls": post.urls,
        }

    # --- Account section ---
    account_section = None
    if account:
        account_section = {
            "platform": account.platform,
            "username": account.username,
            "display_name": account.display_name,
            "user_id": account.user_id,
            "bio": account.bio,
            "created_at": account.created_at,
            "metrics": {
                "followers": account.followers,
                "following": account.following,
                "post_count": account.post_count,
            },
            "verified": account.verified,
            "location": account.location,
            "website": account.website,
            "profile_image": account.profile_image_url,
        }

    # --- Cross-posts ---
    crosspost_section = [
        {
            "platform": c.platform,
            "url": c.url,
            "posted_at": c.posted_at,
            "author": c.author,
            "match_type": c.match_type,
            "similarity": c.similarity_score,
        }
        for c in (report.cross_posts or [])
    ]

    # --- Username search (Sherlock) ---
    sherlock_found = [
        {"platform": r.platform, "url": r.url, "response_ms": r.response_time_ms}
        for r in (report.username_search or []) if r.status == "found"
    ]
    sherlock_not_found = [r.platform for r in (report.username_search or []) if r.status == "not_found"]

    # --- Dark web ---
    darkweb_section = [
        {
            "source": d.source,
            "type": d.result_type,
            "url": d.url,
            "title": d.title,
            "snippet": d.snippet,
            "indexed_at": d.indexed_at,
        }
        for d in (report.darkweb_hits or [])
    ]

    # --- Red flags ---
    flags_summary = summarise_flags(report.red_flags or [])
    flags_detail = [
        {
            "severity": f.severity,
            "category": f.category,
            "description": f.description,
            "evidence": f.evidence,
        }
        for f in (report.red_flags or [])
    ]

    manual_searches = report.metadata.get("manual_searches", [])

    return {
        "meta": {
            "input_url": report.input_url,
            "platform": report.platform,
            "generated_at": report.generated_at,
            "tool": "osint-tool v1.0",
        },
        "risk_assessment": flags_summary,
        "post": post_section,
        "account": account_section,
        "cross_platform": {
            "cross_posts_found": crosspost_section,
            "username_found_on": sherlock_found,
            "username_not_found_on": sherlock_not_found,
        },
        "dark_web": {
            "hits": darkweb_section,
            "total": len(darkweb_section),
            "manual_searches": manual_searches,
        },
        "red_flags": flags_detail,
        "errors": report.errors,
    }


def to_json(report: OSINTReport, indent: int = 2) -> str:
    return json.dumps(report_to_dict(report), default=_default, indent=indent, ensure_ascii=False)
