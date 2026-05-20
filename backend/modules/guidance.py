"""
Analyst Guidance Engine.
Looks at all findings and generates prioritized "what to do next" recommendations.
This is the key value-add: tells the investigator what matters and what to investigate next.
"""
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from backend.models import GuidanceItem


def _item(priority: int, severity: str, title: str, detail: str,
          action: str = None, pivot_url: str = None, pivot_label: str = None,
          category: str = "general") -> GuidanceItem:
    return GuidanceItem(
        priority=priority,
        severity=severity,
        title=title,
        detail=detail,
        action=action,
        pivot_url=pivot_url,
        pivot_label=pivot_label,
        category=category,
    )


def analyze_account_flags(account: dict, red_flags: list, enrichment=None) -> List[GuidanceItem]:
    items = []
    if not account:
        return items

    username = account.get("username", "")
    platform = account.get("platform", "")
    created_at = account.get("created_at")
    followers = account.get("metrics", {}).get("followers")
    following = account.get("metrics", {}).get("following")
    post_count = account.get("metrics", {}).get("post_count")
    # Override with vxtwitter enrichment if available (more accurate)
    if enrichment:
        enr = enrichment.model_dump() if hasattr(enrichment, 'model_dump') else enrichment
        if enr.get("vx_following_count") is not None:
            following = enr["vx_following_count"]
        if enr.get("vx_tweet_count") is not None and post_count is None:
            post_count = enr["vx_tweet_count"]
    bio = account.get("bio", "") or ""
    verified = account.get("verified", False)

    # Account age check
    if created_at:
        try:
            if isinstance(created_at, str):
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            else:
                dt = created_at
            age_days = (datetime.now(tz=timezone.utc) - dt).days
            if age_days < 7:
                items.append(_item(
                    1, "critical",
                    f"Account is only {age_days} day(s) old",
                    f"@{username} on {platform} was created very recently. "
                    "Extremely new accounts posting sensitive content are high-risk indicators.",
                    action="Check if older accounts with the same identity exist elsewhere. "
                           "Look for pattern of creating new accounts after previous ones are banned.",
                    category="account"
                ))
            elif age_days < 30:
                items.append(_item(
                    2, "high",
                    f"Account is only {age_days} days old",
                    f"@{username} was created less than a month ago.",
                    action="Cross-check username on other platforms to see if this is a new account for an existing identity.",
                    category="account"
                ))
        except Exception:
            pass

    # Follower ratio
    if followers is not None and following is not None and followers > 0 and following > 0:
        ratio = followers / following if following > 0 else 999
        if ratio < 0.05 and following > 500:
            items.append(_item(
                2, "high",
                "Suspicious follower/following ratio",
                f"{followers} followers vs {following} following — follow-farming pattern. "
                "This account follows many but is followed by few.",
                action="Check follow/following lists for bot networks or coordinated accounts.",
                category="account"
            ))

    # No bio AND no profile image — both missing = likely throwaway
    if not bio and not account.get("profile_image"):
        items.append(_item(
            3, "medium",
            "Account has no bio and no profile image",
            "Incomplete profile — no bio or picture. Likely a throwaway or pure amplification account.",
            action="Focus investigation on who is being amplified rather than this account itself.",
            category="account"
        ))

    # Very low following count (from account dict, may also come from enrichment)
    following_val = following
    if following_val is not None and following_val < 20 and (followers or 0) > 1000:
        items.append(_item(
            2, "high",
            f"Only follows {following_val} account(s) despite {followers:,} followers",
            f"This is a very unusual ratio — most accounts with {followers:,} followers follow hundreds or thousands back. "
            f"Following only {following_val} suggests a highly curated, possibly anonymous, or strategically managed account.",
            action="Examine WHO this account follows — those 11 accounts may reveal real identity or affiliation.",
            pivot_url=f"https://twitter.com/{username}/following",
            pivot_label="View following list",
            category="account"
        ))

    # Username pattern (numbers at end)
    import re
    if re.search(r'\d{4,}$', username):
        items.append(_item(
            4, "low",
            "Auto-generated username pattern",
            f"Username '{username}' ends with a long number sequence — common in bulk-created accounts.",
            category="account"
        ))

    return items


def analyze_content_flags(post: dict, red_flags: list) -> List[GuidanceItem]:
    items = []
    if not post:
        return items

    text = (post.get("text") or "").lower()
    media = post.get("media") or []
    urls = post.get("embedded_urls") or []
    hashtags = post.get("hashtags") or []
    mentions = post.get("mentions") or []

    # Media present
    if media:
        items.append(_item(
            2, "high",
            f"{len(media)} media file(s) — run reverse image search",
            "Images and videos can reveal location, device, and prior appearances online. "
            "Reverse searching may reveal if this content was taken from another source.",
            action="Download and run reverse image search on each image. "
                   "Check if any image predates this post — that would indicate recycled content.",
            category="content"
        ))

    # Shortened URLs
    shorteners = ["bit.ly", "tinyurl", "t.co", "ow.ly", "goo.gl", "is.gd"]
    for url in urls:
        for sh in shorteners:
            if sh in url:
                items.append(_item(
                    2, "high",
                    "Post contains shortened URL — destination is hidden",
                    f"Shortened URL found: {url}. The real destination is obscured.",
                    action="Expand the URL using a URL expander service before visiting.",
                    pivot_url=f"https://checkshorturl.com/?url={url}",
                    pivot_label="Expand URL",
                    category="content"
                ))
                break

    # Many hashtags
    if len(hashtags) > 10:
        items.append(_item(
            3, "medium",
            f"{len(hashtags)} hashtags — possible spam/coordinated amplification",
            "Excessive hashtag use is a common indicator of coordinated inauthentic behavior.",
            action="Check if the same hashtag cluster appears across multiple unrelated accounts.",
            category="content"
        ))

    # Many mentions
    if len(mentions) > 8:
        items.append(_item(
            3, "medium",
            f"Post mentions {len(mentions)} users",
            "Mass mentioning can indicate coordinated brigading or spam.",
            action="Check if the mentioned accounts are all amplifying the same content.",
            category="content"
        ))

    return items


def analyze_cross_posts(cross_posts: list) -> List[GuidanceItem]:
    items = []
    if not cross_posts:
        return items

    archive = [c for c in cross_posts if c.get("platform") == "archive.org"]
    others = [c for c in cross_posts if c.get("platform") != "archive.org"]

    if archive:
        a = archive[0]
        items.append(_item(
            2, "high",
            "Content is archived on Wayback Machine",
            f"An archive snapshot exists. This can establish the earliest known date of the content.",
            action="Check the archive timestamp to verify if the content predates the current post.",
            pivot_url=a.get("url"),
            pivot_label="View Archive Snapshot",
            category="network"
        ))

    if others:
        platforms = list({c.get("platform", "?") for c in others})
        items.append(_item(
            2, "high",
            f"Content found on {len(others)} other platform(s)",
            f"Same or similar content detected on: {', '.join(platforms)}. "
            "This may indicate coordinated spread or identify the original source.",
            action="Compare timestamps across platforms to identify the original posting.",
            category="network"
        ))

    return items


def analyze_username_search(username_results: list) -> List[GuidanceItem]:
    items = []
    found = [r for r in username_results if r.get("status") == "found"]
    if not found:
        return items

    high_value = ["GitHub", "LinkedIn", "Reddit", "Keybase", "HackerNews"]
    hv_found = [r for r in found if r.get("platform") in high_value]

    if hv_found:
        for r in hv_found:
            items.append(_item(
                2, "high",
                f"Username found on {r['platform']} — high-value pivot",
                f"The same username exists on {r['platform']}, which typically has richer identity data.",
                action=f"Investigate the {r['platform']} profile for real identity clues (bio, activity, linked accounts).",
                pivot_url=r.get("url"),
                pivot_label=f"Open {r['platform']} Profile",
                category="identity"
            ))

    if len(found) > 5:
        items.append(_item(
            3, "medium",
            f"Username found on {len(found)} platforms",
            f"Broad online presence: {', '.join(r['platform'] for r in found[:8])}.",
            action="Cross-reference profile photos, bios, and locations across platforms for consistency.",
            category="identity"
        ))

    return items


def analyze_dark_web(dw_hits: list) -> List[GuidanceItem]:
    items = []
    if not dw_hits:
        return items

    breach_hits = [h for h in dw_hits if h.get("source") in ("hibp_breach", "dehashed")]
    paste_hits = [h for h in dw_hits if "paste" in h.get("source", "").lower() or h.get("type") == "paste"]
    tor_hits = [h for h in dw_hits if h.get("source") in ("ahmia", "intelx")]

    if breach_hits:
        data_classes = []
        for h in breach_hits:
            data_classes.extend(h.get("data_classes") or [])
        items.append(_item(
            1, "critical",
            f"Identity found in {len(breach_hits)} data breach(es)",
            f"Breach(es): {', '.join(h.get('name', '?') for h in breach_hits[:5])}. "
            f"Exposed data includes: {', '.join(set(data_classes[:8]))}.",
            action="Breach data may reveal real name, address, phone, or password hash. "
                   "Check Dehashed for full breach record details if you have access.",
            category="identity"
        ))

    if paste_hits:
        items.append(_item(
            2, "high",
            f"Identity found in {len(paste_hits)} paste(s)",
            "Username or email was posted on a paste site — may contain sensitive data dumps.",
            action="Review paste contents for additional identifiers.",
            category="identity"
        ))

    if tor_hits:
        items.append(_item(
            1, "critical",
            f"Content indexed on {len(tor_hits)} dark web source(s)",
            "This username/content appears in Tor-indexed results.",
            action="Review dark web mentions for context — may indicate criminal activity or targeted doxxing.",
            category="identity"
        ))

    return items


def analyze_media_files(media_files: list) -> List[GuidanceItem]:
    items = []
    if not media_files:
        return items

    # GPS found
    gps_files = [m for m in media_files if m.get("gps_lat")]
    if gps_files:
        for m in gps_files:
            addr = m.get("gps_address", "")
            lat, lon = m.get("gps_lat"), m.get("gps_lon")
            items.append(_item(
                1, "critical",
                f"GPS coordinates embedded in media: {lat:.4f}, {lon:.4f}",
                f"File '{m.get('filename')}' contains GPS metadata. "
                f"Location: {addr or 'reverse geocoding failed'}.",
                action="Verify if this location matches the claimed context of the post. "
                       "Discrepancy between claimed and actual location is a key red flag.",
                pivot_url=f"https://www.google.com/maps?q={lat},{lon}",
                pivot_label="Open in Google Maps",
                category="geo"
            ))

    # Device info
    device_files = [m for m in media_files if m.get("metadata", {}).get("Make")]
    if device_files:
        m = device_files[0]
        make = m["metadata"].get("Make", "")
        model = m["metadata"].get("Model", "")
        items.append(_item(
            3, "medium",
            f"Device fingerprint in EXIF: {make} {model}",
            f"Image was taken with {make} {model}. EXIF also includes capture timestamp.",
            action="Cross-reference the device model with other media from the account. "
                   "If multiple posts share the same device model, it links them to the same person.",
            category="content"
        ))

    return items


def analyze_identity_pivots(pivots: list) -> List[GuidanceItem]:
    items = []
    for pivot in pivots:
        itype = pivot.get("identifier_type", "")
        identifier = pivot.get("identifier", "")
        breaches = pivot.get("hibp_breaches") or []

        if breaches and itype == "email":
            items.append(_item(
                1, "critical",
                f"Email {identifier} found in {len(breaches)} breach(es)",
                f"Breaches: {', '.join(b.get('name','?') for b in breaches[:5])}",
                action="Use breach data to identify real name, address, or phone number.",
                category="identity"
            ))

        if pivot.get("gravatar_name") or pivot.get("gravatar_avatar"):
            items.append(_item(
                2, "high",
                f"Gravatar profile linked to email {identifier}",
                f"Display name: {pivot.get('gravatar_name', 'N/A')}. Gravatar links email to identity.",
                action="Review the Gravatar profile for additional personal information.",
                pivot_url=f"https://gravatar.com/{hashlib.md5(identifier.strip().lower().encode()).hexdigest()}",
                pivot_label="View Gravatar",
                category="identity"
            ))

    return items


import hashlib  # needed for gravatar link


def analyze_account_enrichment(enrichment) -> List[GuidanceItem]:
    items = []
    if not enrichment:
        return items

    enr = enrichment.model_dump() if hasattr(enrichment, 'model_dump') else enrichment

    # Bio emails → trigger identity pivot
    for email in enr.get("bio_emails", []):
        items.append(_item(
            1, "critical",
            f"Email found in bio: {email}",
            "An email address is publicly listed in the account bio. "
            "This is the strongest identity lead — it can be traced across breach databases, Gravatar, and platform registrations.",
            action=f"Run identity pivot on {email}: check HIBP, Gravatar, email registration across platforms.",
            pivot_url=f"https://haveibeenpwned.com/account/{email}",
            pivot_label="Check HIBP",
            category="identity"
        ))

    # Bio social links → cross-platform presence
    social_links = [l for l in enr.get("bio_links", []) if l.get("is_social")]
    if social_links:
        for link in social_links[:5]:
            items.append(_item(
                2, "high",
                f"Bio links to {link.get('platform', link.get('domain'))}",
                f"Account bio explicitly links to: {link['url']}. "
                "This is an investigator-confirmed cross-platform identity link.",
                action=f"Investigate the {link.get('platform','linked')} profile for additional identity data.",
                pivot_url=link["url"],
                pivot_label=f"Open {link.get('platform','linked profile')}",
                category="identity"
            ))

    # Telegram handle in bio
    for handle in enr.get("bio_handles", []):
        if handle.get("platform") == "Telegram":
            items.append(_item(
                1, "critical",
                f"Telegram handle in bio: {handle.get('handle')}",
                "A Telegram account is explicitly linked. Telegram accounts can reveal phone number, "
                "group memberships, and may have less anonymity than Twitter.",
                action="Investigate the Telegram account — check group memberships and contact info.",
                pivot_url=handle.get("url"),
                pivot_label="Open Telegram",
                category="identity"
            ))

    # Posting patterns
    patterns = enr.get("posting_patterns", {})
    if patterns.get("bot_like_cadence"):
        items.append(_item(
            2, "high",
            "Bot-like posting cadence detected",
            "Multiple posts made less than 60 seconds apart — suggests automated posting.",
            action="Cross-check with bot detection tools and look for identical posts across accounts.",
            category="account"
        ))
    if patterns.get("timezone_inference"):
        items.append(_item(
            4, "info",
            f"Timezone inference: {patterns['timezone_inference']}",
            f"Based on {patterns.get('total_analyzed',0)} posts, peak activity at UTC hour {patterns.get('peak_hour_utc','?')}.",
            action="Compare inferred timezone with claimed location to detect inconsistencies.",
            category="account"
        ))

    # Top posts
    top = enr.get("top_posts", [])
    if top:
        best = top[0]
        likes = best.get("likes") or 0
        views = best.get("views") or 0
        if likes > 1000 or views > 50000:
            items.append(_item(
                3, "medium",
                f"High-engagement historical post found ({likes} likes, {views} views)",
                f"Viral post: \"{str(best.get('text',''))[:100]}...\"",
                action="Investigate viral post context — when was it, what triggered the engagement spike?",
                pivot_url=best.get("url"),
                pivot_label="View post",
                category="content"
            ))

    return items


def generate_guidance(report: dict, media_files: list = None, identity_pivots: list = None, account_enrichment=None) -> List[GuidanceItem]:
    """Main entry point — takes all report data and returns prioritized guidance."""
    all_items = []

    post = report.get("post") or {}
    account = report.get("account") or {}
    red_flags = report.get("red_flags") or []
    cross_posts = report.get("cross_platform", {}).get("cross_posts_found") or []
    username_results = report.get("cross_platform", {}).get("username_found_on") or []
    dw_hits = report.get("dark_web", {}).get("hits") or []

    all_items.extend(analyze_account_flags(account, red_flags, enrichment=account_enrichment))
    all_items.extend(analyze_content_flags(post, red_flags))
    all_items.extend(analyze_cross_posts(cross_posts))
    all_items.extend(analyze_username_search(username_results))
    all_items.extend(analyze_dark_web(dw_hits))

    if media_files:
        mf_dicts = [m.model_dump() if hasattr(m, 'model_dump') else m for m in media_files]
        all_items.extend(analyze_media_files(mf_dicts))

    if identity_pivots:
        pv_dicts = [p.model_dump() if hasattr(p, 'model_dump') else p for p in identity_pivots]
        all_items.extend(analyze_identity_pivots(pv_dicts))

    if account_enrichment:
        all_items.extend(analyze_account_enrichment(account_enrichment))

    # Convert existing red flags to guidance items
    severity_map = {"high": "high", "medium": "medium", "low": "low"}
    priority_map = {"high": 2, "medium": 3, "low": 4}
    for flag in red_flags:
        sev = flag.get("severity", "low")
        all_items.append(_item(
            priority_map.get(sev, 4),
            severity_map.get(sev, "low"),
            flag.get("description", ""),
            flag.get("evidence", ""),
            category=flag.get("category", "general")
        ))

    # Add manual search suggestions if nothing found
    if not account and not post:
        all_items.append(_item(
            5, "info",
            "Basic page analysis completed",
            "No structured social media post data was extracted. "
            "The URL may require authentication or is a non-standard page.",
            action="Try submitting the direct URL to the post, not a profile page.",
            category="general"
        ))

    # Deduplicate by title
    seen_titles = set()
    unique = []
    for item in all_items:
        if item.title not in seen_titles:
            seen_titles.add(item.title)
            unique.append(item)

    # Sort by priority, then severity
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    unique.sort(key=lambda x: (x.priority, sev_order.get(x.severity, 9)))

    return unique
