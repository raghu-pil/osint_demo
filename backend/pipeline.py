"""
Pipeline orchestrator — runs all analysis modules and assembles the Case.
Each step updates the case JSON on disk so the frontend can poll for progress.
"""
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.config import config
from backend.models import Case, CaseStatus, ProgressStep, StepStatus

logger = logging.getLogger(__name__)

# Add osint package to path
OSINT_DIR = Path(__file__).parent.parent
if str(OSINT_DIR) not in sys.path:
    sys.path.insert(0, str(OSINT_DIR))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(case: "Case", message: str, level: str = "info"):
    """Append a human-readable log entry and persist the case."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    case.logs.append({"ts": ts, "msg": message, "level": level})


def _step(name: str, label: str) -> ProgressStep:
    return ProgressStep(name=name, label=label)


class CaseManager:
    def __init__(self, cases_dir: str):
        self.cases_dir = Path(cases_dir)
        self.cases_dir.mkdir(parents=True, exist_ok=True)

    def create(self, url: str, notes: Optional[str] = None, name: Optional[str] = None,
               parent_id: Optional[str] = None, parent_label: Optional[str] = None) -> Case:
        case_id = uuid.uuid4().hex[:12]
        case = Case(
            id=case_id,
            url=url,
            name=name or None,
            notes=notes,
            parent_id=parent_id or None,
            parent_label=parent_label or None,
            status=CaseStatus.PENDING,
            created_at=_now(),
            updated_at=_now(),
            steps=[
                _step("url_parse",       "URL Analysis"),
                _step("share_trace",     "Share Token Decode"),
                _step("scrape_post",     "Post Scraping"),
                _step("account",         "Account Profile"),
                _step("account_history", "Account Timeline & Enrichment"),
                _step("cross_posts",     "Cross-Post Detection"),
                _step("username",        "Username Enumeration"),
                _step("dark_web",        "Dark Web Search"),
                _step("media",           "Media Download & EXIF"),
                _step("identity",        "Identity Pivots"),
                _step("guidance",        "Analyst Guidance"),
                _step("auto_actions",    "Auto Investigations"),
            ]
        )
        self.save(case)
        return case

    def get(self, case_id: str) -> Optional[Case]:
        p = self.cases_dir / case_id / "case.json"
        if not p.exists():
            return None
        with open(p) as f:
            return Case(**json.load(f))

    def list_all(self) -> list:
        import logging as _logging
        _log = _logging.getLogger(__name__)
        cases = []
        for d in sorted(self.cases_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if d.is_dir():
                try:
                    c = self.get(d.name)
                    if c:
                        cases.append(c)
                except Exception as e:
                    _log.warning("Skipping corrupt case %s: %s", d.name, e)
        return cases

    def save(self, case: Case):
        d = self.cases_dir / case.id
        d.mkdir(exist_ok=True)
        case.updated_at = _now()
        with open(d / "case.json", "w") as f:
            json.dump(case.model_dump(), f, indent=2, default=str)

    def case_dir(self, case_id: str) -> Path:
        d = self.cases_dir / case_id
        d.mkdir(exist_ok=True)
        return d

    def _set_step(self, case: Case, name: str, status: StepStatus, message: str = None):
        for step in case.steps:
            if step.name == name:
                step.status = status
                step.message = message
                if status == StepStatus.RUNNING:
                    step.started_at = _now()
                elif status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED):
                    step.completed_at = _now()
        self.save(case)

    def step_start(self, case: Case, name: str):
        self._set_step(case, name, StepStatus.RUNNING)

    def step_done(self, case: Case, name: str, message: str = None):
        self._set_step(case, name, StepStatus.COMPLETED, message)

    def step_fail(self, case: Case, name: str, message: str = None):
        self._set_step(case, name, StepStatus.FAILED, message)

    def step_skip(self, case: Case, name: str, message: str = None):
        self._set_step(case, name, StepStatus.SKIPPED, message)


def run_pipeline(case_id: str, manager: CaseManager):
    case = manager.get(case_id)
    if not case:
        logger.error("Case %s not found", case_id)
        return

    case.status = CaseStatus.RUNNING
    manager.save(case)

    case_dir = manager.case_dir(case_id)
    media_dir = case_dir / "media"
    media_dir.mkdir(exist_ok=True)

    try:
        # ── Step 1: URL Parse ─────────────────────────────────────────────────
        manager.step_start(case, "url_parse")
        _log(case, "Identifying platform and content type from URL…")
        manager.save(case)
        try:
            from osint.core.url_parser import parse_url
            parsed = parse_url(case.url)
            case.platform = parsed.platform
            _log(case, f"Platform identified: {parsed.platform or 'unknown'}")
            manager.step_done(case, "url_parse", f"Platform: {parsed.platform}, post_id: {parsed.post_id}")
        except Exception as e:
            case.errors.append(f"URL parse: {e}")
            manager.step_fail(case, "url_parse", str(e))
            parsed = None

        # ── Step 1b: ShareTrace ───────────────────────────────────────────────
        manager.step_start(case, "share_trace")
        _log(case, "Checking for share tracking tokens that identify who shared this link…")
        manager.save(case)
        try:
            from backend.modules.share_trace import trace_share
            from osint.core.utils import make_session as _make_session
            _st_session = _make_session()
            st = trace_share(case.url, session=_st_session)
            case.share_trace = st if st else None
            if st:
                if st.get("sharer_username"):
                    manager.step_done(case, "share_trace",
                        f"Sharer: @{st['sharer_username']} ({st.get('platform','')})")
                    _log(case, f"ShareTrace: identified sharer @{st['sharer_username']} on {st.get('platform','')}")
                elif st.get("sharer_user_code"):
                    manager.step_done(case, "share_trace",
                        f"User code: {st['sharer_user_code']} ({st.get('platform','')})")
                    _log(case, f"ShareTrace: sharer user code {st['sharer_user_code']} ({st.get('platform','')})")
                elif st.get("sharer_email"):
                    manager.step_done(case, "share_trace",
                        f"Author: {st['sharer_display_name']} <{st['sharer_email']}>")
                    _log(case, f"ShareTrace: {st['sharer_display_name']} <{st['sharer_email']}>")
                elif st.get("creator_user_id") or st.get("sharer_user_id"):
                    uid = st.get("creator_user_id") or st.get("sharer_user_id")
                    manager.step_done(case, "share_trace",
                        f"User ID: {uid} ({st.get('platform','')})")
                    _log(case, f"ShareTrace: user ID {uid} on {st.get('platform','')}")
                else:
                    manager.step_done(case, "share_trace",
                        f"Partial data ({st.get('platform','?')})")
            else:
                manager.step_skip(case, "share_trace", "No share token recognised in URL")
        except Exception as e:
            case.errors.append(f"ShareTrace: {e}")
            manager.step_fail(case, "share_trace", str(e))

        # ── Step 2: Scrape Post ───────────────────────────────────────────────
        manager.step_start(case, "scrape_post")
        _log(case, "Fetching post content and engagement metrics…")
        manager.save(case)
        osint_report = None
        try:
            from osint.main import run as osint_run
            # Skip Sherlock + dark web here — the pipeline runs them in dedicated
            # steps below so they show their own progress labels instead of
            # blocking this step for 60-90 seconds.
            scrape_config = {**config, "skip_sherlock": True, "skip_darkweb": True}
            osint_report = osint_run(case.url, scrape_config, verbose=False)
            from osint.output.formatter import report_to_dict
            report_dict = report_to_dict(osint_report)

            case.post = report_dict.get("post")
            case.red_flags = report_dict.get("red_flags") or []
            case.risk_score = report_dict.get("risk_assessment", {}).get("score", 0)
            case.risk_label = report_dict.get("risk_assessment", {}).get("label", "MINIMAL")
            case.manual_searches = report_dict.get("dark_web", {}).get("manual_searches") or []

            if case.post:
                author = case.post.get('author_username', '?')
                _log(case, f"Post retrieved — author @{author}, risk score {case.risk_score}")
                manager.step_done(case, "scrape_post",
                    f"Got post by @{case.post.get('author_username', '?')}")
            else:
                _log(case, "No structured post data returned; continuing with available information")
                manager.step_done(case, "scrape_post", "No structured post data found")
        except Exception as e:
            case.errors.append(f"Post scrape: {e}")
            manager.step_fail(case, "scrape_post", str(e))
            report_dict = {}

        # ── Step 2b: Web article fallback for unknown platforms ───────────────
        if not case.post and case.platform in (None, "unknown", ""):
            try:
                from backend.modules.web_scraper import web_scrape_to_post, scrape_web_page
                _log(case, "Non-social URL — fetching page content and metadata…")
                manager.save(case)
                post = web_scrape_to_post(case.url)
                if post:
                    case.post = post
                    case.platform = "web"
                    meta = post.get("_web_meta", {})
                    _log(case, f"Page scraped: {meta.get('title','')[:60]}")
                    manager.step_done(case, "scrape_post",
                                      f"Web page: {meta.get('domain','')}")
                    # Download lead image if present
                    img_url = meta.get("image_url")
                    if img_url and not config.get("skip_media_download"):
                        try:
                            import requests as _req, hashlib, mimetypes
                            from backend.models import MediaFileSummary
                            r = _req.get(img_url, headers={"User-Agent": "Mozilla/5.0"},
                                         timeout=15, stream=True)
                            r.raise_for_status()
                            ct = r.headers.get("content-type", "image/jpeg")
                            ext = mimetypes.guess_extension(ct.split(";")[0].strip()) or ".jpg"
                            if ext == ".jpe": ext = ".jpg"
                            data = r.content
                            sha = hashlib.sha256(data).hexdigest()
                            fname = f"lead_image{ext}"
                            media_dir = case_dir / "media"
                            media_dir.mkdir(exist_ok=True)
                            fpath = media_dir / fname
                            fpath.write_bytes(data)
                            mf = MediaFileSummary(
                                filename=fname, media_type="image",
                                file_size=len(data), hash_sha256=sha,
                                hash_md5=hashlib.md5(data).hexdigest(),
                                source_url=img_url,
                                local_path=str(fpath),
                            )
                            case.media_files = [mf]
                            _log(case, f"Lead image downloaded: {fname}")
                        except Exception as img_e:
                            _log(case, f"Lead image download failed: {img_e}", level="warn")
            except Exception as e:
                _log(case, f"Web scrape fallback failed: {e}", level="warn")

        # ── Step 3: Account ───────────────────────────────────────────────────
        manager.step_start(case, "account")
        _log(case, "Retrieving account profile and public information…")
        manager.save(case)
        try:
            if osint_report and osint_report.account:
                from osint.output.formatter import report_to_dict
                case.account = report_to_dict(osint_report).get("account")
                followers = case.account.get('metrics',{}).get('followers','?')
                _log(case, f"Account profile loaded — @{case.account.get('username','?')}, {followers} followers")
                manager.step_done(case, "account",
                    f"@{case.account.get('username','?')}: "
                    f"{followers} followers")
            else:
                manager.step_skip(case, "account", "No account data from scraper")
        except Exception as e:
            case.errors.append(f"Account: {e}")
            manager.step_fail(case, "account", str(e))

        # ── Step 3b: Account History & Bio Enrichment ─────────────────────────
        manager.step_start(case, "account_history")
        _log(case, "Pulling extended account history, posting patterns, and bio links…")
        manager.save(case)
        try:
            from backend.modules.account_history import enrich_account
            from backend.models import AccountEnrichment
            username = (case.account or {}).get("username", "") or (
                (case.post or {}).get("author_username", ""))
            platform = case.platform or "twitter"
            if username:
                enriched = enrich_account(case.account or {}, platform)
                case.account_enrichment = AccountEnrichment(
                    vx_display_name=enriched.get("vx_display_name"),
                    vx_location=enriched.get("vx_location"),
                    vx_created_at=enriched.get("vx_created_at"),
                    vx_tweet_count=enriched.get("vx_tweet_count"),
                    vx_following_count=enriched.get("vx_following_count"),
                    vx_user_id=enriched.get("vx_user_id"),
                    vx_protected=enriched.get("vx_protected"),
                    bio_links=enriched.get("bio_links", []),
                    bio_emails=enriched.get("bio_emails", []),
                    bio_handles=enriched.get("bio_handles", []),
                    recent_posts=enriched.get("recent_posts", []),
                    top_posts=enriched.get("top_posts", []),
                    posting_patterns=enriched.get("posting_patterns", {}),
                    post_count_scraped=enriched.get("post_count_scraped", 0),
                )
                # Enrich account dict with vxtwitter data
                if case.account:
                    if enriched.get("vx_location") and not case.account.get("location"):
                        case.account["location"] = enriched["vx_location"]
                    if enriched.get("vx_created_at") and not case.account.get("created_at"):
                        case.account["created_at"] = enriched["vx_created_at"]
                    if enriched.get("vx_display_name"):
                        case.account["vx_display_name"] = enriched["vx_display_name"]
                n_posts = enriched.get("post_count_scraped", 0)
                n_links = len(enriched.get("bio_links", []))
                manager.step_done(case, "account_history",
                    f"{n_posts} posts scraped, {n_links} bio links found")
            else:
                manager.step_skip(case, "account_history", "No username available")
        except Exception as e:
            case.errors.append(f"Account history: {e}")
            manager.step_fail(case, "account_history", str(e))

        # ── Step 3c: Intelligence summary (LLM) ──────────────────────────────
        anthropic_key = config.get("anthropic_api_key", "")
        if anthropic_key and case.post:
            try:
                from backend.modules.post_analysis import analyze_post
                _log(case, "Generating intelligence summary from post content…")
                manager.save(case)
                intel = analyze_post(
                    post=case.post,
                    account=case.account,
                    account_enrichment=case.account_enrichment,
                    media_files=case.media_files,
                    anthropic_api_key=anthropic_key,
                )
                case.post_intelligence = intel
                if intel.get("success"):
                    assessment = intel.get("assessment", "unclear")
                    _log(case, f"Intelligence summary complete — assessment: {assessment}")
                else:
                    _log(case, f"Intelligence summary unavailable: {intel.get('error','')}", level="warn")
                manager.save(case)
            except Exception as e:
                case.errors.append(f"Intelligence summary: {e}")

        # ── Step 4: Cross-posts ───────────────────────────────────────────────
        if not config.get("skip_crossposts"):
            manager.step_start(case, "cross_posts")
            _log(case, "Searching for the same content on other platforms…")
            manager.save(case)
            try:
                if osint_report:
                    from osint.output.formatter import report_to_dict
                    cp = report_to_dict(osint_report).get("cross_platform", {})
                    case.cross_posts = cp.get("cross_posts_found") or []
                    manager.step_done(case, "cross_posts", f"{len(case.cross_posts)} found")
                else:
                    manager.step_skip(case, "cross_posts", "No post to cross-search")
            except Exception as e:
                case.errors.append(f"Cross-posts: {e}")
                manager.step_fail(case, "cross_posts", str(e))
        else:
            manager.step_skip(case, "cross_posts", "Disabled in config")

        # ── Step 5: Username search (Sherlock) ───────────────────────────────
        if not config.get("skip_sherlock"):
            manager.step_start(case, "username")
            _log(case, "Scanning 400+ social platforms for this username (this may take 1–2 minutes)…")
            manager.save(case)
            try:
                uname = ((case.account or {}).get("username")
                         or (case.post or {}).get("author_username"))
                if uname:
                    from osint.intelligence.sherlock_runner import run_sherlock
                    results = run_sherlock(uname, timeout=config.get("sherlock_timeout", 60))
                    case.username_search = [
                        r.model_dump() if hasattr(r, "model_dump") else dict(r)
                        for r in results
                    ]
                    manager.step_done(case, "username", f"Found on {len(results)} platforms")
                else:
                    manager.step_skip(case, "username", "No username to search")
            except Exception as e:
                case.errors.append(f"Username: {e}")
                manager.step_fail(case, "username", str(e))
        else:
            manager.step_skip(case, "username", "Disabled in config")

        # ── Step 6: Dark web (enhanced) ───────────────────────────────────────
        if not config.get("skip_darkweb"):
            manager.step_start(case, "dark_web")
            _log(case, "Querying breach databases and dark web sources for identity leaks…")
            manager.save(case)
            try:
                from backend.modules.darkweb_enhanced import gather_enhanced_intel
                username = (case.account or {}).get("username") or (case.post or {}).get("author_username")
                email = None
                display_name = (case.account_enrichment.vx_display_name
                                if case.account_enrichment else None) or (case.account or {}).get("display_name")
                # Extract email from identity pivots if found
                for piv in (case.identity_pivots or []):
                    if piv.identifier_type == "email":
                        email = piv.identifier
                        break

                dw_result = gather_enhanced_intel(
                    username=username,
                    email=email,
                    display_name=display_name,
                    config=config,
                )
                case.dark_web = dw_result.get("hits", [])
                # Merge manual searches
                existing = {m.get("label") for m in (case.manual_searches or [])}
                for ms in dw_result.get("manual_searches", []):
                    if ms.get("label") not in existing:
                        case.manual_searches.append(ms)
                sources = dw_result.get("sources_checked", [])
                case.dark_web_sources_checked = sources
                manager.step_done(case, "dark_web",
                    f"{len(case.dark_web)} hits across {len(sources)} sources: {', '.join(sources[:4])}")
            except Exception as e:
                case.errors.append(f"Dark web: {e}")
                manager.step_fail(case, "dark_web", str(e))
        else:
            manager.step_skip(case, "dark_web", "Disabled in config")

        # ── Step 7: Media download ────────────────────────────────────────────
        if not config.get("skip_media_download"):
            manager.step_start(case, "media")
            _log(case, "Downloading media files and extracting metadata (GPS, camera info, timestamps)…")
            manager.save(case)
            try:
                from backend.modules.media import download_post_media, run_ocr
                post_data = case.post or {}
                mfiles = download_post_media(post_data, str(media_dir))
                keyframes_dir = str(case_dir / "keyframes")
                for mf in mfiles:
                    ocr = run_ocr(mf, keyframes_dir)
                    if ocr:
                        mf.ocr_text = ocr
                case.media_files = mfiles
                manager.step_done(case, "media", f"{len(mfiles)} file(s) downloaded")
            except Exception as e:
                case.errors.append(f"Media: {e}")
                manager.step_fail(case, "media", str(e))
        else:
            manager.step_skip(case, "media", "Disabled in config")

        # ── Step 7b: Combined image + content analysis (LLM) ────────────────
        if anthropic_key and case.media_files:
            img_file = next(
                (mf for mf in case.media_files if mf.media_type in ("image", "video")), None
            )
            if img_file:
                try:
                    from backend.modules.image_analysis import analyze_image, run_context_searches
                    from pathlib import Path as _P

                    _log(case, "Running visual + content analysis (image + post context → Claude)…")
                    manager.save(case)

                    # For video: use first extracted keyframe (extract it now if not yet done)
                    img_path = img_file.local_path
                    if img_file.media_type == "video":
                        kf_dir = case_dir / "keyframes" / _P(img_file.filename).stem
                        kf_dir.mkdir(parents=True, exist_ok=True)
                        kf_path = kf_dir / "kf_000.jpg"
                        if not kf_path.exists():
                            import subprocess as _sp
                            _sp.run(
                                ["ffmpeg", "-ss", "1", "-i", str(img_file.local_path),
                                 "-frames:v", "1", "-q:v", "2", str(kf_path), "-y"],
                                capture_output=True, timeout=20
                            )
                        if kf_path.exists():
                            img_path = str(kf_path)
                        else:
                            _log(case, "Could not extract keyframe — skipping image analysis", level="warn")
                            raise ValueError("No keyframe available")

                    # For Claude analysis we use base64 directly (more reliable than
                    # depending on a third-party host URL being accessible to Claude)
                    pub_url = None

                    # Build post context from scraped data
                    post = case.post or {}
                    acct = case.account or {}
                    def _str(v):
                        return str(v) if v is not None else ""

                    post_ctx = {
                        "platform":     case.platform or "",
                        "username":     _str(acct.get("username") or post.get("username", "")),
                        "display_name": _str(acct.get("display_name", "")),
                        "bio":          _str(acct.get("bio", "")),
                        "followers":    acct.get("followers"),
                        "post_text":    _str(post.get("text", "")),
                        "post_date":    _str(post.get("created_at", "")),
                        "post_url":     case.url,
                    }

                    analysis = analyze_image(img_path, anthropic_key,
                                             public_url=pub_url, post_context=post_ctx)

                    if analysis.get("success"):
                        # Merge into existing post_intelligence or store separately
                        existing = case.post_intelligence or {}
                        existing["image_analysis"] = analysis
                        existing["image_context_match"] = analysis.get("context_match", "")
                        case.post_intelligence = existing

                        # Run context + misinfo searches
                        serpapi_key = config.get("serpapi_api_key", "")
                        if serpapi_key:
                            ctx = run_context_searches(analysis, serpapi_key, max_results=5)
                            existing["context_search"] = ctx
                            existing["misinfo_articles"] = [
                                {"title": r.get("title",""), "url": r.get("link",""),
                                 "source": r.get("source",""), "snippet": r.get("snippet",""),
                                 "query": r.get("query","")}
                                for r in ctx.get("misinfo", [])
                            ]
                            existing["original_source_results"] = ctx.get("original_source", [])
                            existing["is_deepfake_claim"] = analysis.get("is_deepfake_claim", False)
                            existing["deepfake_visual_context"] = analysis.get("deepfake_visual_context", "")
                            case.post_intelligence = existing
                            _log(case, f"Image analysis complete — context match: {analysis.get('context_match','N/A')[:60]}")
                        else:
                            _log(case, "Image analysis complete (no SerpAPI key — skipping context search)")
                    else:
                        _log(case, f"Image analysis skipped: {analysis.get('error','')}", level="warn")
                    manager.save(case)
                except Exception as e:
                    _log(case, f"Image+content analysis error: {e}", level="warn")

        # ── Step 8: Identity pivots ───────────────────────────────────────────
        manager.step_start(case, "identity")
        _log(case, "Correlating any emails, phone numbers, or handles found in the profile…")
        manager.save(case)
        try:
            from backend.modules.identity import extract_identifiers, run_identity_pivot
            full_report = {
                "post": case.post,
                "account": case.account,
            }
            identifiers = extract_identifiers(full_report)
            pivots = []
            for id_info in identifiers[:5]:  # limit to 5 to avoid rate limits
                pivot = run_identity_pivot(
                    id_info["identifier"], id_info["type"], config
                )
                pivots.append(pivot)
            case.identity_pivots = pivots
            manager.step_done(case, "identity", f"{len(pivots)} identifier(s) pivoted")
        except Exception as e:
            case.errors.append(f"Identity: {e}")
            manager.step_fail(case, "identity", str(e))

        # ── Step 9: Analyst guidance ──────────────────────────────────────────
        manager.step_start(case, "guidance")
        _log(case, "Analysing all collected data and generating prioritised investigation leads…")
        manager.save(case)
        try:
            from backend.modules.guidance import generate_guidance
            report_for_guidance = {
                "post": case.post,
                "account": case.account,
                "red_flags": case.red_flags,
                "cross_platform": {
                    "cross_posts_found": case.cross_posts,
                    "username_found_on": case.username_search,
                },
                "dark_web": {"hits": case.dark_web},
            }
            case.guidance = generate_guidance(
                report_for_guidance,
                media_files=case.media_files,
                identity_pivots=case.identity_pivots,
                account_enrichment=case.account_enrichment,
            )
            # For web article cases, inject a lead that lets the investigator dig deeper
            if case.platform in ("web", None, "unknown", "") and case.post:
                from backend.models import GuidanceItem
                meta = case.post.get("_web_meta", {})
                title = meta.get("title") or case.post.get("text", "")[:60]
                domain = meta.get("domain") or ""
                if not any(g.pivot_url == case.url for g in case.guidance):
                    case.guidance.insert(0, GuidanceItem(
                        priority=1, severity="info",
                        title=f"Web article: {title[:70]}",
                        detail=f"Source: {domain}. Review this page for context and follow any referenced accounts or claims.",
                        action="Investigate linked accounts or search for related content.",
                        pivot_url=case.url,
                        pivot_label=f"Open {domain}",
                        category="content",
                    ))

            # Check if investigated account is a known misinfo/factcheck account
            from backend.modules.known_accounts import enrich_guidance_with_known_accounts
            enrich_guidance_with_known_accounts(case)
            n_critical = sum(1 for g in case.guidance if g.severity == "critical")
            _log(case, f"Generated {len(case.guidance)} leads ({n_critical} critical)")
            manager.step_done(case, "guidance", f"{len(case.guidance)} recommendations")
        except Exception as e:
            case.errors.append(f"Guidance: {e}")
            manager.step_fail(case, "guidance", str(e))

        # ── Step 10: Auto Actions ─────────────────────────────────────────────
        manager.step_start(case, "auto_actions")
        _log(case, "Running automated follow-up actions: profile scraping, reverse image search, link expansion…")
        manager.save(case)
        try:
            from backend.modules.auto_actions import run_all_auto_actions
            serpapi_key = config.get("serpapi_api_key", "")
            auto_results = run_all_auto_actions(case, serpapi_key=serpapi_key, case_dir=str(case_dir))
            case.auto_actions = auto_results

            # Attach results to relevant guidance items
            for g in case.guidance:
                title_lower = g.title.lower()
                if "reverse image search" in title_lower or "media file" in title_lower:
                    rs = {k: v for k, v in auto_results.items() if k.startswith("reverse_search_")}
                    if rs:
                        g.auto_result = rs
                        g.auto_status = "done"
                elif "follows" in title_lower or "following" in title_lower:
                    following = auto_results.get("twitter_following")
                    if following:
                        g.auto_result = following
                        g.auto_status = "done" if following.get("found") else "partial"
                elif "linktree" in title_lower:
                    lt = auto_results.get("profile_linktree")
                    if lt:
                        g.auto_result = lt
                        g.auto_status = "done"
                elif "tiktok" in title_lower:
                    tt = auto_results.get("profile_tiktok")
                    if tt:
                        g.auto_result = tt
                        g.auto_status = "done"

            # Generate NEW guidance from auto-action findings
            from backend.models import GuidanceItem

            # Blogger: check for display name mismatch
            blogger = auto_results.get("profile_blogger", {})
            if blogger.get("found") and blogger.get("display_name"):
                blogger_name = blogger["display_name"]
                twitter_name = (case.account_enrichment.vx_display_name if case.account_enrichment else None) or \
                               (case.account or {}).get("display_name") or ""
                posts = blogger.get("posts", [])
                oldest_post_note = f" Has posts dating to {posts[0].get('url','?')[:50] if posts else 'unknown date'}." if posts else ""
                case.guidance.insert(0, GuidanceItem(
                    priority=1,
                    severity="critical" if blogger_name.lower() != twitter_name.lower() else "medium",
                    title=f"Blogger account found: display name is \"{blogger_name}\"",
                    detail=f"Username InsiderWB is registered on Blogger with display name \"{blogger_name}\" "
                           f"(Twitter: \"{twitter_name}\").{oldest_post_note} "
                           "This account may predate Twitter by years — critical identity correlation opportunity.",
                    action="Investigate Blogger posts for personal details, writing style, location clues. "
                           "The earlier persona may have been less cautious about anonymity.",
                    pivot_url=f"https://insiderwb.blogspot.com",
                    pivot_label="Open Blogger profile",
                    category="identity",
                    auto_result=blogger,
                    auto_status="done",
                ))

            # Linktree: account squatting signal
            linktree = auto_results.get("profile_linktree", {})
            if linktree.get("found") and not linktree.get("links") and linktree.get("created_at"):
                case.guidance.append(GuidanceItem(
                    priority=3,
                    severity="medium",
                    title="Linktree account claimed but empty",
                    detail=f"Linktree account created on {linktree['created_at'][:10]} has no links. "
                           "This may indicate the username was recently claimed to establish presence or prevent others from using it.",
                    action="Monitor the Linktree for future link additions — they may reveal other platforms.",
                    pivot_url="https://linktr.ee/InsiderWB",
                    pivot_label="Open Linktree",
                    category="identity",
                    auto_result=linktree,
                    auto_status="done",
                ))

            n_done = sum(1 for v in auto_results.values() if not v.get("error"))
            manager.step_done(case, "auto_actions",
                f"{len(auto_results)} actions run, {n_done} successful")
            manager.save(case)
        except Exception as e:
            case.errors.append(f"Auto actions: {e}")
            manager.step_fail(case, "auto_actions", str(e))

        case.status = CaseStatus.COMPLETED
        n_leads = len(case.guidance)
        n_critical = sum(1 for g in case.guidance if g.severity == "critical")
        _log(case, f"Investigation complete — {n_leads} leads generated, {n_critical} critical")

    except Exception as e:
        logger.exception("Pipeline crashed for case %s", case_id)
        case.status = CaseStatus.FAILED
        case.errors.append(f"Pipeline crash: {e}")
        _log(case, f"Investigation stopped due to an error: {e}", level="error")

    manager.save(case)
    logger.info("Pipeline finished for case %s: %s", case_id, case.status)
