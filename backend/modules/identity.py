"""
Identity pivot module.
Given email, phone, or username — expand to linked accounts, breaches, profiles.
"""
import hashlib
import logging
import re
from typing import List, Dict, Optional

import requests

from backend.models import IdentityPivot

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OSINT-Research/1.0)"}


# ── Email pivots ──────────────────────────────────────────────────────────────

def gravatar_lookup(email: str) -> Dict:
    """Check Gravatar for a profile photo and name linked to this email."""
    h = hashlib.md5(email.strip().lower().encode()).hexdigest()
    profile_url = f"https://www.gravatar.com/{h}.json"
    avatar_url = f"https://www.gravatar.com/avatar/{h}?d=404"
    try:
        r = requests.get(profile_url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            entry = r.json().get("entry", [{}])[0]
            return {
                "found": True,
                "display_name": entry.get("displayName"),
                "real_name": entry.get("name", {}).get("formatted"),
                "location": entry.get("currentLocation"),
                "about": entry.get("aboutMe"),
                "avatar_url": f"https://www.gravatar.com/avatar/{h}",
                "profile_url": f"https://gravatar.com/{h}",
            }
        # 404 = account exists but no public profile; still show avatar URL
        if r.status_code == 404:
            # Verify avatar exists
            av = requests.get(avatar_url, headers=HEADERS, timeout=8)
            if av.status_code == 200:
                return {"found": True, "avatar_url": f"https://www.gravatar.com/avatar/{h}"}
    except Exception as e:
        logger.debug("Gravatar lookup failed: %s", e)
    return {"found": False}


def hibp_check(email: str, api_key: str) -> List[Dict]:
    """Check HaveIBeenPwned for breaches."""
    if not api_key:
        return []
    results = []
    try:
        headers = {**HEADERS, "hibp-api-key": api_key}
        r = requests.get(
            f"https://haveibeenpwned.com/api/v3/breachedaccount/{email}?truncateResponse=false",
            headers=headers, timeout=15
        )
        if r.status_code == 200:
            for b in r.json():
                results.append({
                    "name": b.get("Name"),
                    "domain": b.get("Domain"),
                    "breach_date": b.get("BreachDate"),
                    "data_classes": b.get("DataClasses", []),
                    "is_verified": b.get("IsVerified"),
                    "description": b.get("Description", "")[:200],
                })
    except Exception as e:
        logger.debug("HIBP failed: %s", e)
    return results


def emailrep_check(email: str) -> Dict:
    """Check EmailRep.io for reputation."""
    try:
        r = requests.get(
            f"https://emailrep.io/{email}",
            headers={**HEADERS, "Key": ""},
            timeout=10
        )
        if r.status_code == 200:
            d = r.json()
            return {
                "reputation": d.get("reputation"),
                "suspicious": d.get("suspicious"),
                "references": d.get("references"),
                "details": d.get("details", {}),
            }
    except Exception as e:
        logger.debug("EmailRep failed: %s", e)
    return {}


PLATFORM_REGISTER_CHECKS = {
    "Twitter": "https://api.twitter.com/i/users/email_available.json?email={email}",
}

HOLEHE_SITES = {
    "Instagram":  ("https://www.instagram.com/accounts/account_recovery_send_ajax/", "email"),
    "GitHub":     ("https://github.com/password_reset", "email"),
    "Spotify":    ("https://spclient.wg.spotify.com/signup/public/v1/account", "email"),
    "Adobe":      ("https://account.adobe.com/", None),
    "Dropbox":    ("https://www.dropbox.com/register", "email"),
    "Pinterest":  ("https://www.pinterest.com/resource/UserRegisterResource/", "email"),
    "LinkedIn":   ("https://www.linkedin.com/uas/request-password-reset", "email"),
}


def check_email_platforms(email: str) -> List[Dict]:
    """Quick check which services this email might be registered on."""
    results = []
    # We use a safe approach: just return actionable manual search links
    # rather than making requests that could alert the target
    from urllib.parse import quote_plus
    enc = quote_plus(email)
    manual_checks = [
        {"platform": "Google Account", "url": f"https://accounts.google.com/", "note": "Use 'Forgot password' flow"},
        {"platform": "Microsoft", "url": "https://account.live.com/password/reset", "note": "Use 'Forgot password' flow"},
        {"platform": "Apple ID", "url": "https://iforgot.apple.com", "note": "Use 'Forgot Apple ID' flow"},
        {"platform": "Facebook", "url": f"https://www.facebook.com/login/identify/?email={enc}", "note": "Account finder"},
    ]
    return manual_checks


# ── Phone pivots ──────────────────────────────────────────────────────────────

def validate_phone(phone: str) -> Dict:
    """Validate and parse phone number."""
    try:
        import phonenumbers
        p = phonenumbers.parse(phone, None)
        if phonenumbers.is_valid_number(p):
            from phonenumbers import geocoder, carrier, timezone
            return {
                "valid": True,
                "country": geocoder.description_for_number(p, "en"),
                "carrier": carrier.name_for_number(p, "en"),
                "number_type": str(phonenumbers.number_type(p)),
                "international_format": phonenumbers.format_number(
                    p, phonenumbers.PhoneNumberFormat.INTERNATIONAL
                ),
                "timezones": list(timezone.time_zones_for_number(p)),
            }
    except ImportError:
        pass
    except Exception:
        pass
    return {"valid": False, "raw": phone}


def phone_platform_check(phone: str) -> List[Dict]:
    """Manual search links for phone number investigation."""
    from urllib.parse import quote_plus
    enc = quote_plus(phone)
    return [
        {"platform": "Truecaller", "url": f"https://www.truecaller.com/search/in/{enc}"},
        {"platform": "WhatsApp", "url": "https://wa.me/" + re.sub(r'\D', '', phone)},
        {"platform": "Telegram", "url": f"https://t.me/{re.sub(r'\\D', '', phone)}"},
        {"platform": "Google Search", "url": f"https://www.google.com/search?q={enc}+phone"},
    ]


# ── Username pivots ───────────────────────────────────────────────────────────

PROBE_SITES = {
    "Twitter":    "https://twitter.com/{u}",
    "Instagram":  "https://www.instagram.com/{u}/",
    "Reddit":     "https://www.reddit.com/user/{u}",
    "TikTok":     "https://www.tiktok.com/@{u}",
    "YouTube":    "https://www.youtube.com/@{u}",
    "GitHub":     "https://github.com/{u}",
    "Facebook":   "https://www.facebook.com/{u}",
    "LinkedIn":   "https://www.linkedin.com/in/{u}",
    "Pinterest":  "https://www.pinterest.com/{u}/",
    "Tumblr":     "https://{u}.tumblr.com",
    "Medium":     "https://medium.com/@{u}",
    "Twitch":     "https://www.twitch.tv/{u}",
    "Telegram":   "https://t.me/{u}",
    "Steam":      "https://steamcommunity.com/id/{u}",
    "Keybase":    "https://keybase.io/{u}",
    "GitLab":     "https://gitlab.com/{u}",
    "Mastodon":   "https://mastodon.social/@{u}",
    "HackerNews": "https://news.ycombinator.com/user?id={u}",
    "DevTo":      "https://dev.to/{u}",
    "Pastebin":   "https://pastebin.com/u/{u}",
}


def probe_username(username: str) -> List[Dict]:
    """Quick HEAD-request probe for username across platforms."""
    results = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    import time
    for platform, url_tpl in PROBE_SITES.items():
        url = url_tpl.format(u=username)
        try:
            r = session.head(url, timeout=6, allow_redirects=True)
            if r.status_code in (200, 302):
                status = "found"
            elif r.status_code == 404:
                status = "not_found"
            else:
                status = f"unknown_{r.status_code}"
        except Exception:
            status = "error"
        results.append({"platform": platform, "url": url, "status": status})
        time.sleep(0.1)
    return results


# ── Main orchestrator ─────────────────────────────────────────────────────────

def run_identity_pivot(identifier: str, identifier_type: str, config: dict) -> IdentityPivot:
    pivot = IdentityPivot(identifier=identifier, identifier_type=identifier_type)

    if identifier_type == "email":
        # Gravatar
        grav = gravatar_lookup(identifier)
        if grav.get("found"):
            pivot.gravatar_name = grav.get("display_name") or grav.get("real_name")
            pivot.gravatar_avatar = grav.get("avatar_url")

        # HIBP
        api_key = config.get("hibp_api_key", "")
        pivot.hibp_breaches = hibp_check(identifier, api_key)

        # Manual platform checks
        pivot.manual_search_links = {
            item["platform"]: item["url"]
            for item in check_email_platforms(identifier)
        }

        # Emailrep
        rep = emailrep_check(identifier)
        if rep:
            pivot.platforms_found.append({"source": "emailrep", "data": str(rep)})

    elif identifier_type == "phone":
        info = validate_phone(identifier)
        pivot.platforms_found = [{"source": "validation", **info}]
        pivot.manual_search_links = {
            item["platform"]: item["url"]
            for item in phone_platform_check(identifier)
        }

    elif identifier_type == "username":
        probe_results = probe_username(identifier)
        pivot.platforms_found = [
            {"platform": r["platform"], "url": r["url"], "status": r["status"]}
            for r in probe_results if r["status"] == "found"
        ]

    return pivot


def extract_identifiers(report_data: dict) -> List[Dict[str, str]]:
    """Pull emails, phones, usernames from report data."""
    identifiers = []
    import re

    text_sources = []
    if report_data.get("post"):
        text_sources.append(report_data["post"].get("text", "") or "")
    if report_data.get("account"):
        acct = report_data["account"]
        text_sources.append(acct.get("bio", "") or "")
        if acct.get("email"):
            identifiers.append({"identifier": acct["email"], "type": "email"})
        if acct.get("username"):
            identifiers.append({"identifier": acct["username"], "type": "username"})

    combined = " ".join(text_sources)
    emails = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', combined)
    for e in set(emails):
        identifiers.append({"identifier": e, "type": "email"})

    phones = re.findall(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', combined)
    for p in set(phones):
        if len(re.sub(r'\D', '', p)) >= 10:
            identifiers.append({"identifier": p, "type": "phone"})

    return identifiers
