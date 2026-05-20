"""
Shared utilities: HTTP session, user-agent rotation, rate limiting.
"""
import time
import random
import logging
import hashlib
from typing import Optional, Dict, Any
from functools import wraps

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]


def make_session(retries: int = 3, backoff: float = 0.5, timeout: int = 20) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    session._default_timeout = timeout
    return session


def get(session: requests.Session, url: str, **kwargs) -> Optional[requests.Response]:
    timeout = kwargs.pop("timeout", getattr(session, "_default_timeout", 20))
    try:
        resp = session.get(url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        logger.warning("GET %s failed: %s", url, e)
        return None


def safe_int(val: Any) -> Optional[int]:
    try:
        return int(str(val).replace(",", "").replace("K", "000").replace("M", "000000"))
    except (ValueError, TypeError):
        return None


def hash_content(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def jitter(base: float = 1.0, spread: float = 0.5) -> None:
    time.sleep(base + random.uniform(0, spread))


def clean_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return " ".join(text.split()).strip()
