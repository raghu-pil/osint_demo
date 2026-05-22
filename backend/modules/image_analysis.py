"""
LLM-powered image analysis for OSINT investigations.

Sends an uploaded screenshot to Claude API to extract:
  - Who is in the image (person, title, organisation)
  - What event/context is shown
  - What text/watermarks are visible
  - Targeted search queries to find the original unmanipulated footage

The resulting context is used to drive Google/YouTube searches that
pure visual reverse-image search cannot find (e.g. lipsynced videos
that look identical to the original frame-by-frame).
"""
import base64
import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, Optional

import requests

logger = logging.getLogger(__name__)

DEEPFAKE_KEYWORDS = {
    "deepfake", "deep fake", "lipsync", "lip sync", "ai generated", "ai-generated",
    "face swap", "faceswap", "synthetic", "manipulated", "morphed", "doctored",
    "not real", "fake video", "fake audio", "cloned voice", "voice clone",
}


def detect_deepfake_claim(text: str) -> bool:
    """Return True if the text contains a claim that the content is AI-generated/deepfaked."""
    t = text.lower()
    return any(kw in t for kw in DEEPFAKE_KEYWORDS)


_BASE_ANALYSIS_PROMPT = """You are assisting a forensic investigator verifying whether this image or video frame is authentic and finding the original unmanipulated source.

Analyse the image carefully and return a JSON object with these exact fields:

{
  "person": "Name, title, rank, organisation of the main person if identifiable; otherwise describe appearance/uniform.",
  "event": "Type of event (press conference, speech, interview, ceremony…) and contextual details.",
  "setting": "Physical location clues: flags, backdrop text, building/room, uniforms, insignia.",
  "media_present": ["News organisations/channels visible from mic logos, chyrons, watermarks"],
  "visible_text": ["All readable text: name plates, ticker, watermarks, captions, signage"],
  "context_match": "ONLY if post context was provided above: does the image match the claimed context? State clearly: MATCHES / DOES NOT MATCH / UNCERTAIN, and explain why in one sentence.",
  "search_queries": [
    "Specific Google query to find the ORIGINAL video/image",
    "YouTube-focused query to find original footage",
    "News-site query",
    "Official-source query (person's official statements/press releases)"
  ],
  "misinfo_queries": [
    "Query to find fact-checks/debunks: '<person> <event> fake OR deepfake OR false OR misinformation'",
    "Query targeting fact-check sites: 'site:snopes.com OR site:altnews.in OR site:boomlive.in <key terms>'",
    "Query for manipulation reports: '<person> <event> manipulated OR edited OR misleading OR out of context'"
  ],
  "original_source_queries": [
    "ONLY populate this if a deepfake/lipsync claim is present — queries to find the ORIGINAL footage this was based on.",
    "Focus ONLY on visual characteristics (person identity, setting, backdrop, attire, flags, logos, watermarks).",
    "Ignore what the person is supposedly saying — that audio is fake. Search for the visual event.",
    "Example: '<person name> <setting description e.g. blue backdrop> <visible watermark/logo> site:youtube.com'",
    "Example: '<news agency watermark> <person> <approximate year based on visible context>'"
  ],
  "deepfake_visual_context": "ONLY if deepfake claim detected: describe the visual characteristics that would identify the ORIGINAL footage — person's exact attire, backdrop colour/text, flags/logos visible, other people present, room/stage layout. This is what someone should search for.",
  "summary": "Two-sentence investigation summary: who this is, what to look for to verify authenticity."
}

Rules:
- Only state what is clearly visible. Never guess names unless very confident.
- search_queries: find the ORIGINAL authentic content.
- misinfo_queries: find whether this was ALREADY DEBUNKED. Use the identified person and event.
- original_source_queries: ONLY fill in when a deepfake/lipsync/manipulation claim is present. These queries find the ORIGINAL video the deepfake was created from, based purely on visual context (not the fake audio/speech).
- If you see agency watermarks (IANS, ANI, PTI, Reuters) — include them in queries.
- Return ONLY the JSON object, no markdown, no extra text."""


def _build_prompt(post_context: dict = None) -> tuple[str, bool]:
    """
    Build the Claude prompt, prepending scraped post context when available.
    Returns (prompt_string, is_deepfake_claim).
    """
    if not post_context:
        return _BASE_ANALYSIS_PROMPT, False

    post_text = post_context.get("post_text", "")
    is_deepfake = detect_deepfake_claim(post_text)

    lines = ["The following content was scraped from the social media post where this image/video appeared.",
             "Use it to cross-check the image and generate more targeted queries.",
             ""]
    if post_context.get("platform"):
        lines.append(f"Platform: {post_context['platform']}")
    if post_context.get("username"):
        name = post_context.get("display_name", "")
        lines.append(f"Account: @{post_context['username']}" + (f" ({name})" if name else ""))
    if post_context.get("bio"):
        lines.append(f"Account bio: {post_context['bio'][:200]}")
    if post_context.get("followers") is not None:
        lines.append(f"Followers: {post_context['followers']:,}")
    if post_text:
        lines.append(f"Post text: \"{post_text[:500]}\"")
    if post_context.get("post_date"):
        lines.append(f"Post date: {post_context['post_date'][:20]}")
    if post_context.get("post_url"):
        lines.append(f"Post URL: {post_context['post_url']}")

    context_block = "\n".join(lines)

    deepfake_instruction = ""
    if is_deepfake:
        deepfake_instruction = (
            "\n\n⚠️  DEEPFAKE / LIPSYNC CLAIM DETECTED in the post text above.\n"
            "This means the AUDIO or SPEECH in the video is likely fake, but the VISUAL content "
            "(the person's face, body, setting, backdrop) is probably taken from a REAL original video.\n"
            "Your primary task is to identify the visual characteristics that will help find the ORIGINAL footage:\n"
            "- Exact description of attire (suit colour, tie, uniform)\n"
            "- Backdrop details (colour, text, logos, flags)\n"
            "- Venue/stage layout\n"
            "- Visible agency watermarks (IANS, ANI, PTI, Reuters, etc.)\n"
            "- Other people visible in the frame\n"
            "Populate 'original_source_queries' and 'deepfake_visual_context' with this information.\n"
            "DO NOT search for what the person is saying — that speech is fabricated.\n"
        )

    prompt = (
        f"{context_block}{deepfake_instruction}\n\n"
        "Now analyse the image WITH the above context in mind. "
        "Pay special attention to whether the image matches the claimed context.\n\n"
        + _BASE_ANALYSIS_PROMPT
    )
    return prompt, is_deepfake


_CLAUDE_MAX_IMAGE_BYTES = 4_800_000  # 5 MB limit with some headroom


def _image_to_base64(file_path: str) -> tuple[str, str]:
    """
    Return (base64_data, media_type) for a local image file.
    Compresses the image if it exceeds Claude's 5 MB base64 limit.
    """
    import io
    suffix = Path(file_path).suffix.lower().lstrip(".")
    media_type = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    }.get(suffix, "image/jpeg")

    with open(file_path, "rb") as f:
        data = f.read()

    if len(data) <= _CLAUDE_MAX_IMAGE_BYTES:
        return base64.standard_b64encode(data).decode("utf-8"), media_type

    # Image is too large — compress with Pillow
    logger.info("Image %s is %d bytes, compressing for Claude…", file_path, len(data))
    try:
        from PIL import Image as _Img
        img = _Img.open(io.BytesIO(data))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Try reducing JPEG quality first
        for quality in (80, 65, 50, 35):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            compressed = buf.getvalue()
            if len(compressed) <= _CLAUDE_MAX_IMAGE_BYTES:
                logger.info("Compressed to %d bytes at quality=%d", len(compressed), quality)
                return base64.standard_b64encode(compressed).decode("utf-8"), "image/jpeg"

        # Still too big — scale down dimensions
        w, h = img.size
        while w > 100:
            w, h = int(w * 0.7), int(h * 0.7)
            resized = img.resize((w, h), _Img.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=70, optimize=True)
            compressed = buf.getvalue()
            if len(compressed) <= _CLAUDE_MAX_IMAGE_BYTES:
                logger.info("Scaled to %dx%d, %d bytes", w, h, len(compressed))
                return base64.standard_b64encode(compressed).decode("utf-8"), "image/jpeg"

    except ImportError:
        # Pillow not available — fall back to ffmpeg resize
        logger.warning("Pillow not available; trying ffmpeg resize")
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", file_path, "-vf", "scale=1280:-1", "-q:v", "5", tmp_path],
                capture_output=True, timeout=15
            )
            if Path(tmp_path).exists():
                with open(tmp_path, "rb") as f:
                    compressed = f.read()
                if len(compressed) <= _CLAUDE_MAX_IMAGE_BYTES:
                    return base64.standard_b64encode(compressed).decode("utf-8"), "image/jpeg"
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    logger.error("Could not compress image below 5 MB: %s", file_path)
    # Last resort: send original and let Claude reject it (will surface proper error)
    return base64.standard_b64encode(data).decode("utf-8"), media_type


def analyze_image(file_path: str, anthropic_api_key: str,
                  public_url: Optional[str] = None,
                  post_context: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Send image (+ optional scraped post context) to Claude and extract investigation context.

    post_context: dict with keys platform, username, display_name, bio, followers,
                  post_text, post_date, post_url — scraped from the source URL.

    Returns a dict with success, person, event, setting, visible_text, context_match,
    search_queries, misinfo_queries, summary, error.
    """
    if not anthropic_api_key:
        return {"success": False, "error": "anthropic_api_key not set in config.yaml"}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_api_key)

        # Prefer URL source (cheaper, no base64 overhead); fall back to base64
        if public_url and public_url.startswith("http"):
            image_content = {"type": "image", "source": {"type": "url", "url": public_url}}
        else:
            b64, media_type = _image_to_base64(file_path)
            image_content = {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            }

        prompt, is_deepfake = _build_prompt(post_context)

        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4000,
            messages=[{
                "role": "user",
                "content": [
                    image_content,
                    {"type": "text", "text": prompt},
                ],
            }],
        )

        raw = response.content[0].text.strip()
        logger.info("Claude image analysis response: %s…", raw[:120])

        # Parse JSON — handle markdown fences, trailing commas, and other Claude quirks
        def _try_parse(s: str):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                # Remove trailing commas before } or ]
                cleaned = re.sub(r',\s*([}\]])', r'\1', s)
                try:
                    return json.loads(cleaned)
                except json.JSONDecodeError:
                    return None

        data = _try_parse(raw)
        if data is None:
            # Strip markdown code fence if present
            stripped = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
            data = _try_parse(stripped)
        if data is None:
            # Extract the outermost {...} block
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                data = _try_parse(m.group(0))
        if data is None:
            return {"success": False, "error": f"JSON parse error — Claude returned: {raw[:200]}"}

        data["success"] = True
        data["is_deepfake_claim"] = is_deepfake
        # Clean up template text Claude sometimes echoes back in original_source_queries
        osq = data.get("original_source_queries", [])
        if osq and any("ONLY populate" in q or "Focus ONLY" in q or "Example:" in q for q in osq):
            data["original_source_queries"] = [q for q in osq
                                               if not any(t in q for t in ("ONLY populate", "Focus ONLY", "Example:", "Ignore what"))]
        return data

    except ImportError:
        return {"success": False, "error": "anthropic package not installed — run: pip install anthropic"}
    except Exception as e:
        logger.warning("Claude image analysis failed: %s", e)
        return {"success": False, "error": str(e)}


def run_context_searches(analysis: Dict, serpapi_key: str,
                         max_results: int = 5) -> Dict[str, Any]:
    """
    Take the search queries Claude generated and run Google + YouTube
    searches via SerpAPI. Returns structured results.
    """
    if not serpapi_key:
        return {"success": False, "error": "No SerpAPI key"}

    queries = analysis.get("search_queries", [])
    misinfo_queries = analysis.get("misinfo_queries", [])

    # Fallback: generate basic misinfo queries if Claude didn't return them
    if not misinfo_queries:
        person = analysis.get("person", "")
        event = analysis.get("event", "")
        base = " ".join(filter(None, [person[:60], event[:60]])).strip()
        if base:
            misinfo_queries = [
                f"{base} fake OR deepfake OR false OR misinformation",
                f"site:snopes.com OR site:altnews.in OR site:boomlive.in {base}",
                f"{base} fact check debunked",
            ]

    if not queries and not misinfo_queries:
        return {"success": False, "error": "No search queries from analysis"}

    try:
        from serpapi import GoogleSearch
    except ImportError:
        return {"success": False, "error": "google-search-results not installed"}

    original_source_queries = analysis.get("original_source_queries", [])
    # Only use these if they look like real queries (not Claude echoing template text)
    original_source_queries = [q for q in original_source_queries
                               if len(q) > 10 and not q.startswith("ONLY") and "Example:" not in q]

    results = {"google": [], "youtube": [], "misinfo": [], "original_source": [],
               "queries_used": [], "misinfo_queries_used": [], "original_source_queries_used": []}

    def _google_search(q, dest_list):
        try:
            res = GoogleSearch({"engine": "google", "q": q, "api_key": serpapi_key, "num": max_results}).get_dict()
            for item in res.get("organic_results", [])[:max_results]:
                dest_list.append({
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                    "source": item.get("source", ""),
                    "snippet": item.get("snippet", ""),
                    "query": q,
                })
            return True
        except Exception as e:
            logger.warning("Google search failed for '%s': %s", q, e)
            return False

    # Run general queries
    for q in queries[:2]:
        if _google_search(q, results["google"]):
            results["queries_used"].append(q)

    # Run YouTube with 3rd general query
    if len(queries) >= 3:
        try:
            yt_q = queries[2]
            res = GoogleSearch({"engine": "youtube", "search_query": yt_q, "api_key": serpapi_key}).get_dict()
            for item in res.get("video_results", [])[:max_results]:
                results["youtube"].append({
                    "title": item.get("title", {}).get("runs", [{}])[0].get("text", "") if isinstance(item.get("title"), dict) else item.get("title", ""),
                    "link": item.get("link", ""),
                    "channel": item.get("channel", {}).get("name", "") if isinstance(item.get("channel"), dict) else "",
                    "published": item.get("published_date", ""),
                    "query": yt_q,
                })
            if yt_q not in results["queries_used"]:
                results["queries_used"].append(yt_q)
        except Exception as e:
            logger.warning("YouTube search failed: %s", e)

    # Run misinformation-focused queries
    for q in misinfo_queries[:3]:
        if _google_search(q, results["misinfo"]):
            results["misinfo_queries_used"].append(q)

    # Run original-source queries (deepfake scenario: find the real footage)
    for q in original_source_queries[:3]:
        if _google_search(q, results["original_source"]):
            results["original_source_queries_used"].append(q)
    # Also run on YouTube — original footage is often there
    for q in original_source_queries[:2]:
        try:
            res = GoogleSearch({"engine": "youtube", "search_query": q, "api_key": serpapi_key}).get_dict()
            for item in res.get("video_results", [])[:max_results]:
                results["original_source"].append({
                    "title": item.get("title", {}).get("runs", [{}])[0].get("text", "") if isinstance(item.get("title"), dict) else item.get("title", ""),
                    "link": item.get("link", ""),
                    "channel": item.get("channel", {}).get("name", "") if isinstance(item.get("channel"), dict) else "",
                    "published": item.get("published_date", ""),
                    "query": q,
                    "source_type": "youtube",
                })
        except Exception as e:
            logger.warning("YouTube original-source search failed: %s", e)

    logger.info("Context search: %d general + %d misinfo + %d original-source results",
                len(results["google"]) + len(results["youtube"]),
                len(results["misinfo"]), len(results["original_source"]))

    results["success"] = True
    results["manual_links"] = [
        {"label": f"Google: {q[:60]}{'…' if len(q)>60 else ''}", "url": f"https://www.google.com/search?q={requests.utils.quote(q)}"}
        for q in queries
    ] + [
        {"label": f"YouTube: {q[:50]}{'…' if len(q)>50 else ''}", "url": f"https://www.youtube.com/results?search_query={requests.utils.quote(q)}"}
        for q in queries[:3]
    ] + [
        {"label": f"Misinfo check: {q[:60]}{'…' if len(q)>60 else ''}", "url": f"https://www.google.com/search?q={requests.utils.quote(q)}"}
        for q in misinfo_queries
    ] + [
        {"label": f"Find original: {q[:55]}{'…' if len(q)>55 else ''}", "url": f"https://www.google.com/search?q={requests.utils.quote(q)}"}
        for q in original_source_queries
    ]
    return results
