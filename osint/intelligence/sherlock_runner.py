"""
Sherlock integration — hunts a username across 400+ social networks.
Requires `sherlock` installed (pip install sherlock-project).
Falls back to direct HTTP probing for a curated list of platforms if sherlock
is not available.
"""
import json
import logging
import subprocess
import shutil
import tempfile
import os
import time
from typing import List, Optional

import requests

from ..core.models import SherlockResult
from ..core.utils import make_session, get

logger = logging.getLogger(__name__)

# Curated fallback list for when sherlock is not installed
PROBE_SITES = {
    "Twitter":       "https://twitter.com/{username}",
    "Instagram":     "https://www.instagram.com/{username}/",
    "Reddit":        "https://www.reddit.com/user/{username}",
    "TikTok":        "https://www.tiktok.com/@{username}",
    "YouTube":       "https://www.youtube.com/@{username}",
    "GitHub":        "https://github.com/{username}",
    "Facebook":      "https://www.facebook.com/{username}",
    "Pinterest":     "https://www.pinterest.com/{username}/",
    "Tumblr":        "https://{username}.tumblr.com",
    "Medium":        "https://medium.com/@{username}",
    "DevTo":         "https://dev.to/{username}",
    "Twitch":        "https://www.twitch.tv/{username}",
    "Mastodon_soc":  "https://mastodon.social/@{username}",
    "Keybase":       "https://keybase.io/{username}",
    "Telegram":      "https://t.me/{username}",
    "Steam":         "https://steamcommunity.com/id/{username}",
    "HackerNews":    "https://news.ycombinator.com/user?id={username}",
    "GitLab":        "https://gitlab.com/{username}",
    "Bitbucket":     "https://bitbucket.org/{username}/",
    "Pastebin":      "https://pastebin.com/u/{username}",
    "Flickr":        "https://www.flickr.com/people/{username}",
    "Quora":         "https://www.quora.com/profile/{username}",
    "VK":            "https://vk.com/{username}",
    "Telegram_chan": "https://t.me/s/{username}",
}


def run_sherlock(username: str, timeout: int = 120) -> List[SherlockResult]:
    """Run sherlock CLI if available; otherwise fall back to manual probing."""
    if shutil.which("sherlock"):
        return _run_sherlock_cli(username, timeout)
    logger.info("sherlock not found; using built-in probe list")
    return _probe_manually(username)


def _run_sherlock_cli(username: str, timeout: int) -> List[SherlockResult]:
    """Parse sherlock stdout — [+] lines are found, [-] are not found."""
    import re
    results: List[SherlockResult] = []
    try:
        proc = subprocess.run(
            ["sherlock", "--print-found", "--no-color", "--no-txt",
             "--timeout", "8", username],
            capture_output=True, text=True, timeout=timeout,
        )
        for line in proc.stdout.splitlines():
            # [+] Platform: https://...
            m = re.match(r'^\[\+\]\s+(.+?):\s+(https?://\S+)', line)
            if m:
                results.append(SherlockResult(
                    platform=m.group(1).strip(),
                    url=m.group(2).strip(),
                    status="found",
                ))
    except subprocess.TimeoutExpired:
        logger.warning("sherlock timed out for username: %s", username)
    except Exception as e:
        logger.error("sherlock error: %s", e)
    return results


def _probe_manually(username: str) -> List[SherlockResult]:
    """Quick HEAD-request probe against a curated list."""
    session = make_session(retries=1)
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    results: List[SherlockResult] = []

    for platform, url_tpl in PROBE_SITES.items():
        url = url_tpl.format(username=username)
        start = time.monotonic()
        try:
            resp = session.head(url, timeout=8, allow_redirects=True)
            elapsed = (time.monotonic() - start) * 1000
            if resp.status_code in (200, 302):
                status = "found"
            elif resp.status_code == 404:
                status = "not_found"
            elif resp.status_code == 429:
                status = "rate_limited"
            else:
                status = f"http_{resp.status_code}"
        except requests.RequestException:
            elapsed = 0
            status = "error"

        results.append(SherlockResult(
            platform=platform,
            url=url,
            status=status,
            response_time_ms=round(elapsed, 1),
        ))

    return results
