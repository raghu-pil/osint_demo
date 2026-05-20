"""
Account behaviour pattern analysis.
Looks for indicators of bot activity, coordinated inauthentic behaviour,
newly-created accounts, sudden follower spikes, etc.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from ..core.models import AccountData, PostData, RedFlag


def analyse_account(account: AccountData) -> List[RedFlag]:
    flags: List[RedFlag] = []
    now = datetime.now(tz=timezone.utc)

    # ---- Account age ------------------------------------------------
    if account.created_at:
        age_days = (now - account.created_at).days
        if age_days < 7:
            flags.append(RedFlag(
                severity="high",
                category="account_age",
                description=f"Account created {age_days} day(s) ago — very new.",
                evidence=f"created_at={account.created_at.isoformat()}",
            ))
        elif age_days < 30:
            flags.append(RedFlag(
                severity="medium",
                category="account_age",
                description=f"Account is only {age_days} days old.",
                evidence=f"created_at={account.created_at.isoformat()}",
            ))

    # ---- Follower / following ratio --------------------------------
    if account.followers is not None and account.following is not None:
        if account.followers > 0 and account.following > 0:
            ratio = account.followers / account.following
            if ratio < 0.05 and account.following > 1000:
                flags.append(RedFlag(
                    severity="medium",
                    category="behavior",
                    description="Follows many accounts but has very few followers (follow-farming pattern).",
                    evidence=f"followers={account.followers}, following={account.following}",
                ))
        if account.following > 5000 and (account.followers or 0) < 500:
            flags.append(RedFlag(
                severity="medium",
                category="behavior",
                description="High following count with low followers — possible spam / follow-back bot.",
                evidence=f"following={account.following}, followers={account.followers}",
            ))

    # ---- Zero engagement accounts ----------------------------------
    if account.post_count == 0 and account.followers is not None and account.followers > 100:
        flags.append(RedFlag(
            severity="medium",
            category="behavior",
            description="Account has followers but zero posts — possible sockpuppet or purchased account.",
        ))

    # ---- Suspicious username patterns ------------------------------
    if account.username:
        import re
        if re.search(r'\d{4,}$', account.username):
            flags.append(RedFlag(
                severity="low",
                category="account_age",
                description="Username ends with a long number sequence — common in auto-generated accounts.",
                evidence=f"username={account.username}",
            ))
        if len(account.username) > 20 and re.search(r'[A-Z].*[A-Z].*[A-Z]', account.username):
            flags.append(RedFlag(
                severity="low",
                category="behavior",
                description="Username has unusual mixed-case pattern.",
            ))

    # ---- No bio + no profile image --------------------------------
    if not account.bio and not account.profile_image_url:
        flags.append(RedFlag(
            severity="low",
            category="behavior",
            description="Account has no bio and no profile image — may be a throwaway.",
        ))

    return flags


def analyse_posting_patterns(posts: List[PostData]) -> List[RedFlag]:
    """Look for bot-like posting cadence in a list of recent posts."""
    flags: List[RedFlag] = []
    if len(posts) < 3:
        return flags

    timestamps = sorted(
        [p.created_at for p in posts if p.created_at],
        reverse=True,
    )
    if len(timestamps) < 3:
        return flags

    # Check for machine-like posting intervals (< 1 min between posts)
    intervals = [(timestamps[i] - timestamps[i + 1]).total_seconds()
                 for i in range(len(timestamps) - 1)]
    very_fast = [iv for iv in intervals if 0 < iv < 60]
    if len(very_fast) >= 3:
        flags.append(RedFlag(
            severity="high",
            category="behavior",
            description=f"{len(very_fast)} consecutive posts were made less than 60 seconds apart — bot-like cadence.",
            evidence=f"min_interval={min(very_fast):.1f}s",
        ))

    # Check for perfectly regular intervals (±2 s variance)
    if len(intervals) >= 5:
        import statistics
        stdev = statistics.stdev(intervals[:10])
        mean = statistics.mean(intervals[:10])
        if stdev < 2 and mean < 300:
            flags.append(RedFlag(
                severity="high",
                category="behavior",
                description="Posts appear at nearly identical time intervals — highly automated.",
                evidence=f"mean_interval={mean:.1f}s, stdev={stdev:.2f}s",
            ))

    return flags
