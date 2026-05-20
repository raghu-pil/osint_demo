"""
Known account intelligence database.

Accounts are classified as:
  "misinfo"   - known misinformation spreaders / coordinated inauthentic accounts
  "factcheck" - fact-checking organisations that investigate misinformation
  "state_media" - government-linked or state-controlled media outlets
  "satire"    - satire/parody accounts (reduce false positives)

When a discovered account matches one of these, its severity score is adjusted
and a contextual reason is added to the lead.

Coverage: India, Pakistan, South/Southeast Asia, plus major global outlets.
Add new entries as they are identified — usernames are lowercase.
"""

from typing import Dict, Optional, Tuple

# ── Misinformation / inauthentic accounts ─────────────────────────────────────
# Score boost: +40 (CRITICAL territory)
MISINFO_ACCOUNTS: Dict[str, Dict] = {
    # India / Pakistan conflict disinformation
    "insiderwb":        {"note": "India-focused disinformation; posts doctored images/videos",  "region": "IN/PK"},
    "ghostofpakistan":  {"note": "Pakistan-linked coordinated inauthentic behaviour",           "region": "PK"},
    "hinducide":        {"note": "Anti-minority disinformation in India",                       "region": "IN"},
    "sanghacharya":     {"note": "Communal disinformation",                                     "region": "IN"},
    "iamswarajya":      {"note": "Recurrent misattributed images and doctored videos",         "region": "IN"},
    "rightwinghindus":  {"note": "Hate-inciting doctored content",                             "region": "IN"},
    # Pakistan Army ISPR / state-linked amplifiers
    "ispr_official":    {"note": "Pakistan military PR; context often distorted for India",    "region": "PK"},
    "drshahidmasood":   {"note": "Frequent debunking by PIBFactCheck & AltNews",               "region": "PK"},
    "arzeema_":         {"note": "Amplifier of ISPR narratives",                               "region": "PK"},
    # Global disinfo amplifiers
    "ruptly":           {"note": "RT-affiliated content; often used in conflict disinfo",      "region": "RU"},
    "partisanpolitics": {"note": "Known fabricated political content",                         "region": "US"},
    "tennesseehollow":  {"note": "Repeatedly flagged for fabricated US political content",     "region": "US"},
    "mrctv":            {"note": "Misrepresented context on media clips",                      "region": "US"},
    # Bangladesh / Myanmar
    "dailybd24":        {"note": "Bangladeshi fake news outlet accounts",                      "region": "BD"},
}

# ── Fact-checking organisations ───────────────────────────────────────────────
# Score boost: +35 (HIGH — fact-checkers sharing = content was investigated)
FACTCHECK_ACCOUNTS: Dict[str, Dict] = {
    # India — government
    "pibfactcheck":     {"org": "PIB Fact Check (Government of India)",         "region": "IN"},
    # India — independent
    "altnews":          {"org": "AltNews",                                       "region": "IN"},
    "altnewsindia":     {"org": "AltNews (alt handle)",                         "region": "IN"},
    "smhoaxslayer":     {"org": "SM HoaxSlayer",                                "region": "IN"},
    "boomlive":         {"org": "BOOM Live",                                    "region": "IN"},
    "thequint":         {"org": "The Quint (Fact Check desk)",                  "region": "IN"},
    "quintfactcheck":   {"org": "Quint WebQoof",                               "region": "IN"},
    "indiatoday":       {"org": "India Today Fact Check",                       "region": "IN"},
    "factcheckindia":   {"org": "FactCheck India",                              "region": "IN"},
    "newsmobi_lfact":   {"org": "NewsMobile FactCheck",                        "region": "IN"},
    "vishvasnews":      {"org": "Vishvas News",                                 "region": "IN"},
    "logicalindian":    {"org": "The Logical Indian",                           "region": "IN"},
    "factly_in":        {"org": "Factly.in",                                    "region": "IN"},
    "ndtvfactcheck":    {"org": "NDTV Fact Check",                              "region": "IN"},
    # Pakistan
    "dawndotcom":       {"org": "Dawn (Pakistan) — fact-check vertical",       "region": "PK"},
    "sochfactcheck":    {"org": "Soch Fact Check",                              "region": "PK"},
    # Bangladesh
    "bdnews24fact":     {"org": "bdnews24 Fact Check",                          "region": "BD"},
    # Southeast Asia
    "rapplerdotcom":    {"org": "Rappler (Philippines) fact-check",             "region": "PH"},
    "cekfaktacom":      {"org": "CekFakta (Indonesia)",                         "region": "ID"},
    # Global
    "afpfactcheck":     {"org": "AFP Fact Check",                               "region": "INT"},
    "reutersfact":      {"org": "Reuters Fact Check",                           "region": "INT"},
    "apfactcheck":      {"org": "AP Fact Check",                                "region": "INT"},
    "snopes":           {"org": "Snopes",                                       "region": "INT"},
    "politifact":       {"org": "PolitiFact",                                   "region": "INT"},
    "factcheckdotorg":  {"org": "FactCheck.org",                               "region": "INT"},
    "bbcreality":       {"org": "BBC Reality Check",                            "region": "INT"},
    "fullfact":         {"org": "Full Fact (UK)",                               "region": "GB"},
    "aapfactcheck":     {"org": "AAP FactCheck (Australia)",                   "region": "AU"},
}

# ── State-controlled / government media ───────────────────────────────────────
# Score boost: +20 (elevated context — note affiliation, do not treat as misinfo)
STATE_MEDIA_ACCOUNTS: Dict[str, Dict] = {
    "rt_com":           {"org": "RT (Russia Today)",      "country": "RU", "note": "Sanctioned in EU/UK"},
    "rt_india":         {"org": "RT India",               "country": "RU"},
    "press_tv":         {"org": "PressTV",                "country": "IR"},
    "cgtn":             {"org": "CGTN (China Global TV)", "country": "CN"},
    "xinhualive":       {"org": "Xinhua News Agency",     "country": "CN"},
    "ptvpakistan":      {"org": "PTV Pakistan (state)",   "country": "PK"},
    "radiopakinews":    {"org": "Radio Pakistan",         "country": "PK"},
    "ddnews":           {"org": "Doordarshan News (India state TV)", "country": "IN"},
    "aniindia":         {"org": "ANI India (government-adjacent wire service)", "country": "IN"},
}


def lookup_account(platform: str, username: str) -> Optional[Tuple[str, Dict]]:
    """
    Look up an account by platform and username.
    Returns (account_type, metadata) or None.
    account_type: "misinfo" | "factcheck" | "state_media"
    """
    if not username:
        return None
    key = username.lower().strip("@")
    if key in MISINFO_ACCOUNTS:
        return ("misinfo", MISINFO_ACCOUNTS[key])
    if key in FACTCHECK_ACCOUNTS:
        return ("factcheck", FACTCHECK_ACCOUNTS[key])
    if key in STATE_MEDIA_ACCOUNTS:
        return ("state_media", STATE_MEDIA_ACCOUNTS[key])
    return None


def apply_known_account_scoring(account: Dict) -> Dict:
    """
    Check if account is in the known accounts database and adjust scoring.
    Mutates account in-place. Returns the account.
    """
    platform = account.get("platform", "")
    username = account.get("username", "")
    match = lookup_account(platform, username)
    if not match:
        return account

    acct_type, meta = match
    reasons = list(account.get("score_reasons", []))
    score = account.get("severity_score", 40)

    if acct_type == "misinfo":
        score = max(score, 85)  # floor at 85 regardless of other factors
        score = min(100, score + 40)
        reasons.insert(0, f"KNOWN MISINFO ACCOUNT — {meta.get('note','')}")
        account["known_type"] = "misinfo"
        account["known_note"] = meta.get("note", "")
        account["severity_label"] = "CRITICAL"

    elif acct_type == "factcheck":
        score = min(100, score + 35)
        reasons.insert(0, f"FACT-CHECKER — {meta.get('org','')} shared/referenced this content")
        account["known_type"] = "factcheck"
        account["known_org"] = meta.get("org", "")
        account["known_note"] = f"This fact-checking organisation shared or investigated this content."
        if score < 65:
            account["severity_label"] = "HIGH"

    elif acct_type == "state_media":
        score = min(100, score + 20)
        reasons.insert(0, f"STATE MEDIA — {meta.get('org','')} ({meta.get('country','')})")
        account["known_type"] = "state_media"
        account["known_org"] = meta.get("org", "")
        account["known_note"] = meta.get("note", "")

    account["severity_score"] = min(100, score)
    account["score_reasons"] = reasons
    # Re-derive label if not overridden
    if "severity_label" not in account or acct_type not in ("misinfo",):
        s = account["severity_score"]
        account["severity_label"] = "CRITICAL" if s >= 75 else "HIGH" if s >= 55 else "MEDIUM" if s >= 35 else "LOW"

    return account


def enrich_guidance_with_known_accounts(case) -> None:
    """
    Check if the case's own account (URL investigation) is a known account
    and prepend appropriate guidance.
    """
    from backend.models import GuidanceItem

    username = None
    if case.account:
        username = case.account.get("username") or case.account.get("screen_name")
    if not username and case.post:
        username = case.post.get("author_username")

    if not username:
        return

    platform = (case.platform or "twitter").lower()
    match = lookup_account(platform, username)
    if not match:
        return

    acct_type, meta = match

    if acct_type == "misinfo":
        case.guidance.insert(0, GuidanceItem(
            priority=0,
            severity="critical",
            title=f"⚠ KNOWN MISINFORMATION ACCOUNT: @{username}",
            detail=f"This account is in the known misinformation database. {meta.get('note','')} "
                   f"Region: {meta.get('region','?')}.",
            action="Treat all content from this account with extreme scepticism. "
                   "Look for the original source of media files — this account likely did not create them. "
                   "Cross-reference with PIBFactCheck, AltNews, and other fact-checkers.",
            pivot_url=f"https://twitter.com/search?q=@{username}&src=typed_query",
            pivot_label="Search for @" + username,
            category="account",
        ))
    elif acct_type == "factcheck":
        case.guidance.insert(0, GuidanceItem(
            priority=0,
            severity="high",
            title=f"Fact-checker account: @{username} ({meta.get('org','')})",
            detail=f"This is a known fact-checking organisation. "
                   f"If they shared this content, it was likely to debunk it. "
                   f"Search their recent posts for any debunk of this exact media.",
            action=f"Search {meta.get('org','')} for recent posts about this content.",
            pivot_url=f"https://twitter.com/{username}",
            pivot_label=f"Open {meta.get('org','')}",
            category="account",
        ))
    elif acct_type == "state_media":
        case.guidance.insert(0, GuidanceItem(
            priority=1,
            severity="high",
            title=f"State-controlled media: @{username} ({meta.get('org','')})",
            detail=f"This is a state-controlled or government-adjacent media outlet. {meta.get('note','')}",
            action="Apply heightened scrutiny to content framing and sourcing.",
            pivot_url=f"https://twitter.com/{username}",
            pivot_label=f"Open {meta.get('org','')}",
            category="account",
        ))
