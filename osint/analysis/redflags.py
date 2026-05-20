"""
Aggregates red flags from all analysis modules and scores the overall risk.
"""
from typing import List, Tuple
from ..core.models import RedFlag, OSINTReport


SEVERITY_WEIGHT = {"high": 10, "medium": 4, "low": 1}


def compute_risk_score(flags: List[RedFlag]) -> int:
    """0–100 composite risk score."""
    raw = sum(SEVERITY_WEIGHT.get(f.severity, 0) for f in flags)
    return min(100, raw)


def risk_label(score: int) -> str:
    if score >= 60:
        return "CRITICAL"
    if score >= 30:
        return "HIGH"
    if score >= 15:
        return "MEDIUM"
    if score >= 5:
        return "LOW"
    return "MINIMAL"


def summarise_flags(flags: List[RedFlag]) -> dict:
    by_severity = {"high": 0, "medium": 0, "low": 0}
    by_category: dict = {}
    for f in flags:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_category[f.category] = by_category.get(f.category, 0) + 1
    score = compute_risk_score(flags)
    return {
        "score": score,
        "label": risk_label(score),
        "total": len(flags),
        "by_severity": by_severity,
        "by_category": by_category,
    }


def darkweb_flags(darkweb_hits: list) -> List[RedFlag]:
    flags = []
    if not darkweb_hits:
        return flags
    breach_hits = [h for h in darkweb_hits if h.result_type == "credential_leak"]
    paste_hits  = [h for h in darkweb_hits if h.result_type == "paste"]
    tor_hits    = [h for h in darkweb_hits if h.result_type in ("tor_index", "forum_post")]

    if breach_hits:
        flags.append(RedFlag(
            severity="high",
            category="darkweb",
            description=f"Username/email found in {len(breach_hits)} credential breach(es).",
            evidence=", ".join(h.title or "" for h in breach_hits[:3]),
        ))
    if paste_hits:
        flags.append(RedFlag(
            severity="medium",
            category="darkweb",
            description=f"Username/email appears in {len(paste_hits)} paste(s).",
            evidence=", ".join(h.url or "" for h in paste_hits[:3]),
        ))
    if tor_hits:
        flags.append(RedFlag(
            severity="high",
            category="darkweb",
            description=f"Username/content found in {len(tor_hits)} Tor-indexed result(s).",
            evidence=", ".join(h.title or "" for h in tor_hits[:3]),
        ))
    return flags


def network_flags(cross_posts: list, sherlock_results: list) -> List[RedFlag]:
    flags = []
    found_platforms = [r.platform for r in sherlock_results if r.status == "found"]
    if len(found_platforms) > 10:
        flags.append(RedFlag(
            severity="medium",
            category="network",
            description=f"Username found on {len(found_platforms)} platforms — broad online presence.",
            evidence=", ".join(found_platforms[:8]),
        ))

    platforms_with_xposts = list({c.platform for c in cross_posts if c.platform not in ("archive.org", "web")})
    if len(platforms_with_xposts) > 3:
        flags.append(RedFlag(
            severity="medium",
            category="network",
            description=f"Content found on {len(platforms_with_xposts)} additional platforms — possible coordinated spread.",
            evidence=", ".join(platforms_with_xposts),
        ))
    return flags
