"""
ShareTrace — decode who shared/forwarded a social link.

Attempts to identify the *sharer* (the person who hit Share and generated
the URL) rather than the original author of the content.

Supported platforms:
  Discord    discord.gg/CODE or discord.com/invite/CODE  → inviter via public API
  GitHub     github.com/.../commit/SHA                   → author email via .patch
  TikTok     vm.tiktok.com/XXX or ?u_code=...            → sharer user code
  Instagram  instagram.com/reel/?igsh=...                 → sharer user ID / username
  Telegram   t.me/joinchat/HASH or t.me/+HASH            → invite creator (user_id)
"""
import re
import base64
import logging
import struct
from typing import Dict, Any, Optional
from urllib.parse import urlparse, parse_qs

# struct is used by _trace_telegram for creator user ID extraction

logger = logging.getLogger(__name__)


def trace_share(url: str, session=None) -> Dict[str, Any]:
    """
    Given a URL, attempt to identify who shared it.
    Returns a dict with platform + sharer info, or empty dict if not a share link.
    """
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().lstrip("www.")
        path = parsed.path
        qs = parse_qs(parsed.query)

        if host in ("discord.gg", "discord.com") and (
                "/invite/" in path or host == "discord.gg"):
            return _trace_discord(url, path, session)

        if host == "github.com":
            return _trace_github(url, path, session)

        if "tiktok.com" in host:
            return _trace_tiktok(url, parsed, qs, session)

        if "instagram.com" in host and qs.get("igsh"):
            return _trace_instagram(url, qs, session)

        if host in ("t.me", "telegram.me", "telegram.org"):
            return _trace_telegram(url, path)

    except Exception as e:
        logger.warning("share_trace error: %s", e)

    return {}


# ── helpers ───────────────────────────────────────────────────────────────────

def _get(session, url: str, **kw) -> Optional[Any]:
    try:
        import requests
        s = session or requests.Session()
        r = s.get(url, timeout=10, allow_redirects=True, **kw)
        if r.status_code < 400:
            return r
    except Exception as e:
        logger.debug("share_trace GET %s: %s", url, e)
    return None


# ── Discord ───────────────────────────────────────────────────────────────────

def _trace_discord(url: str, path: str, session) -> Dict[str, Any]:
    m = re.search(r"/invite/([A-Za-z0-9_-]+)|discord\.gg/([A-Za-z0-9_-]+)", url)
    if not m:
        return {}
    code = m.group(1) or m.group(2)
    api = f"https://discord.com/api/v9/invites/{code}?with_counts=true"
    resp = _get(session, api, headers={"User-Agent": "Mozilla/5.0"})
    if not resp:
        return {"platform": "discord", "share_token": code, "error": "API request failed"}
    try:
        data = resp.json()
    except Exception:
        return {"platform": "discord", "share_token": code, "error": "Invalid JSON from Discord API"}

    result: Dict[str, Any] = {"platform": "discord", "share_token": code}

    inviter = data.get("inviter") or {}
    if inviter:
        result["sharer_id"] = inviter.get("id")
        result["sharer_username"] = inviter.get("username")
        ah = inviter.get("avatar")
        if ah and inviter.get("id"):
            result["sharer_avatar"] = (
                f"https://cdn.discordapp.com/avatars/{inviter['id']}/{ah}.png"
            )
        result["confidence"] = "high"
        result["note"] = (
            f"Invite created by @{inviter.get('username','?')} (ID {inviter.get('id','')})"
        )
    else:
        result["note"] = "No inviter returned — may be a vanity/expired URL"
        result["confidence"] = "none"

    guild = data.get("guild") or {}
    if guild.get("name"):
        result["guild_name"] = guild["name"]
        result["guild_id"] = guild.get("id")

    channel = data.get("channel") or {}
    if channel.get("name"):
        result["channel_name"] = channel["name"]

    return result


# ── GitHub ────────────────────────────────────────────────────────────────────

def _trace_github(url: str, path: str, session) -> Dict[str, Any]:
    m = re.search(r"/([^/]+)/([^/]+)/commit/([0-9a-f]{7,40})", path, re.IGNORECASE)
    if not m:
        return {}
    user, repo, sha = m.group(1), m.group(2), m.group(3)
    patch_url = f"https://github.com/{user}/{repo}/commit/{sha}.patch"
    resp = _get(session, patch_url, headers={"User-Agent": "Mozilla/5.0"})
    if not resp:
        return {"platform": "github", "commit": sha, "error": "Patch endpoint unavailable"}

    text = resp.text
    result: Dict[str, Any] = {"platform": "github", "repository": f"{user}/{repo}", "commit": sha}

    from_m = re.search(r"^From: (.+?) <(.+?)>", text, re.MULTILINE)
    if from_m:
        result["sharer_display_name"] = from_m.group(1).strip()
        result["sharer_email"] = from_m.group(2).strip()
        result["confidence"] = "high"
        result["note"] = (
            f"Commit by {from_m.group(1).strip()} <{from_m.group(2).strip()}>"
        )

    date_m = re.search(r"^Date: (.+)", text, re.MULTILINE)
    if date_m:
        result["committed_at"] = date_m.group(1).strip()

    subj_m = re.search(r"^Subject: (.+)", text, re.MULTILINE)
    if subj_m:
        result["commit_message"] = re.sub(r"^\[PATCH[^\]]*\]\s*", "", subj_m.group(1)).strip()

    return result


# ── TikTok ────────────────────────────────────────────────────────────────────

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _trace_tiktok(url: str, parsed, qs: dict, session) -> Dict[str, Any]:
    result: Dict[str, Any] = {"platform": "tiktok"}

    def _absorb_qs(q: dict):
        u_code = (q.get("u_code") or [None])[0]
        if u_code:
            result["sharer_user_code"] = u_code
            result["confidence"] = "medium"
            result["note"] = f"TikTok share — user code: {u_code}"
        for key, dest in [
            ("tt_from", "share_method"),
            ("utm_source", "share_source"),
            ("share_app_id", "share_app_id"),
        ]:
            val = (q.get(key) or [None])[0]
            if val and dest not in result:
                result[dest] = val

    _absorb_qs(qs)

    # Short URL: follow redirect to get full URL with tracking params
    if "vm.tiktok.com" in parsed.netloc or "vt.tiktok.com" in parsed.netloc:
        try:
            import requests as _req
            s = session or _req.Session()
            r = s.get(url, allow_redirects=True, timeout=12,
                      headers={"User-Agent": _MOBILE_UA})
            if r.url and r.url != url:
                from urllib.parse import urlparse as _up, parse_qs as _pqs
                final_qs = _pqs(_up(r.url).query)
                _absorb_qs(final_qs)
                result["resolved_url"] = r.url
        except Exception as e:
            logger.debug("TikTok redirect: %s", e)

    if not result.get("sharer_user_code") and not result.get("share_method"):
        return {}
    return result


# ── Instagram ─────────────────────────────────────────────────────────────────

def _trace_instagram(url: str, qs: dict, session) -> Dict[str, Any]:
    igsh = (qs.get("igsh") or [None])[0]
    if not igsh:
        return {}

    result: Dict[str, Any] = {"platform": "instagram", "igsh_token": igsh}

    # Instagram embeds a "sharer":{} object in the page's relay JSON when the URL
    # carries a valid igsh token — fetch with Chromium impersonation to get it.
    try:
        from curl_cffi import requests as cffi_req
        r = cffi_req.get(url, impersonate="chrome120", timeout=20)
        if r.status_code == 200:
            text = r.text
            # Extract the sharer block: "sharer":{"profile_pic_url":"...","full_name":"...","username":"...","id":"..."}
            # sharer field can be an object or null depending on whether the share
            # event is still "fresh" — Instagram only serves identity on first access
            null_m = re.search(r'"sharer"\s*:\s*null', text)
            obj_m  = re.search(
                r'"sharer"\s*:\s*\{([^}]{0,800})\}',
                text, re.DOTALL,
            )

            if obj_m:
                inner = obj_m.group(1)
                uname_m = re.search(r'"username"\s*:\s*"([^"]+)"', inner)
                name_m  = re.search(r'"full_name"\s*:\s*"([^"]+)"', inner)
                uid_m   = re.search(r'"id"\s*:\s*"(\d+)"', inner)
                pic_m   = re.search(r'"profile_pic_url"\s*:\s*"([^"]+)"', inner)

                if uname_m:
                    result["sharer_username"]     = uname_m.group(1)
                if name_m:
                    result["sharer_display_name"] = name_m.group(1)
                if uid_m:
                    result["sharer_user_id"]      = uid_m.group(1)
                if pic_m:
                    pic_url = pic_m.group(1).encode().decode("unicode_escape", errors="replace")
                    result["sharer_avatar"]       = pic_url.replace("\\/", "/")

                if result.get("sharer_username"):
                    result["confidence"] = "high"
                    result["note"] = (
                        f"Identified via igsh token: "
                        f"@{result['sharer_username']} ({result.get('sharer_display_name','')})"
                    )
                    result["pivot_url"] = (
                        f"https://www.instagram.com/{result['sharer_username']}/"
                    )
                else:
                    result["confidence"] = "low"
                    result["note"] = "igsh token present but no identity fields in sharer object"

            elif null_m:
                # Instagram recognised the igsh token but returned null — the share
                # event has already been consumed (link opened before this analysis ran)
                result["confidence"] = "low"
                result["note"] = (
                    "igsh share token detected but already consumed — Instagram only "
                    "exposes sharer identity on the first access of a fresh link. "
                    "If you received this link directly, open it in a browser while "
                    "logged in to Instagram to see who shared it."
                )
            else:
                result["confidence"] = "low"
                result["note"] = "igsh token present but no sharer context found in page"
    except ImportError:
        result["confidence"] = "low"
        result["note"] = "igsh token detected; install curl_cffi to resolve sharer identity"
    except Exception as e:
        logger.debug("Instagram curl_cffi: %s", e)
        result["confidence"] = "low"
        result["note"] = f"igsh token detected; fetch failed: {e}"

    return result


# ── Telegram ──────────────────────────────────────────────────────────────────

def _trace_telegram(url: str, path: str) -> Dict[str, Any]:
    m = (
        re.search(r"/joinchat/([A-Za-z0-9_=-]+)", path)
        or re.search(r"/\+([A-Za-z0-9_=-]+)", path)
    )
    if not m:
        return {}
    token = m.group(1)
    result: Dict[str, Any] = {"platform": "telegram", "invite_token": token, "invite_url": url}

    try:
        padded = token.replace("-", "+").replace("_", "/")
        padded += "=" * ((4 - len(padded) % 4) % 4)
        decoded = base64.b64decode(padded, validate=False)
        result["decoded_hex"] = decoded.hex()
        # First 4 bytes (little-endian uint32) may encode creator user_id
        if len(decoded) >= 4:
            uid = struct.unpack("<I", decoded[:4])[0]
            if uid > 1000:
                result["creator_user_id"] = uid
                result["confidence"] = "low"
                result["note"] = (
                    f"Telegram invite — possible creator user ID: {uid} "
                    "(needs Telegram API verification)"
                )
    except Exception as e:
        logger.debug("Telegram token decode: %s", e)

    if not result.get("confidence"):
        result["confidence"] = "low"
    return result
