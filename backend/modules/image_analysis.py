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

ANALYSIS_PROMPT = """You are assisting a forensic investigator who is trying to verify whether this image or video frame is authentic, and to find the original unmanipulated source.

Analyse the image carefully and return a JSON object with these fields:

{
  "person": "Name, title, rank, and organisation of the main person if identifiable. If not identifiable, describe their appearance and uniform.",
  "event": "Type of event shown (press conference, speech, interview, ceremony, etc.) and any contextual details.",
  "setting": "Physical location clues: flags visible, backdrop text, building/room, uniforms, insignia.",
  "media_present": ["List of news organisations/channels visible from microphone logos, chyrons, watermarks"],
  "visible_text": ["All readable text in the image: name plates, ticker text, watermarks, captions, signage"],
  "search_queries": [
    "Specific Google search query 1 to find the original video",
    "Specific Google search query 2 (YouTube-focused)",
    "Specific Google search query 3 (news site focused)",
    "Specific Google search query 4 (if person identified, their official statements)"
  ],
  "summary": "Two-sentence investigation summary: who this likely is, what original footage to look for."
}

Rules:
- Only state what is clearly visible. Do not guess names unless very confident.
- Search queries should be specific and targeted, not generic. Include the person's name if identified.
- If you see a watermark like IANS, ANI, PTI — include it in queries since the original is likely from that agency.
- Return ONLY the JSON object, no markdown, no extra text."""


def _image_to_base64(file_path: str) -> tuple[str, str]:
    """Return (base64_data, media_type) for a local image file."""
    suffix = Path(file_path).suffix.lower().lstrip(".")
    media_type = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    }.get(suffix, "image/jpeg")
    with open(file_path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8"), media_type


def analyze_image(file_path: str, anthropic_api_key: str,
                  public_url: Optional[str] = None) -> Dict[str, Any]:
    """
    Send image to Claude API and extract investigation context.

    Returns a dict with:
      success: bool
      person, event, setting, media_present, visible_text: extracted fields
      search_queries: list of targeted search strings
      summary: two-sentence summary
      error: set if success=False
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

        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": [
                    image_content,
                    {"type": "text", "text": ANALYSIS_PROMPT},
                ],
            }],
        )

        raw = response.content[0].text.strip()
        logger.info("Claude image analysis response: %s…", raw[:120])

        # Parse JSON — handle optional markdown code fence
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
            else:
                return {"success": False, "error": "Claude returned non-JSON", "raw": raw}

        data["success"] = True
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
    if not queries:
        return {"success": False, "error": "No search queries from analysis"}

    try:
        from serpapi import GoogleSearch
    except ImportError:
        return {"success": False, "error": "google-search-results not installed"}

    results = {"google": [], "youtube": [], "queries_used": []}

    # Run the first 2 queries on Google
    for q in queries[:2]:
        try:
            res = GoogleSearch({
                "engine": "google",
                "q": q,
                "api_key": serpapi_key,
                "num": max_results,
            }).get_dict()
            for item in res.get("organic_results", [])[:max_results]:
                results["google"].append({
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                    "source": item.get("source", ""),
                    "snippet": item.get("snippet", ""),
                    "query": q,
                })
            results["queries_used"].append(q)
        except Exception as e:
            logger.warning("Google search failed for query '%s': %s", q, e)

    # Run the 3rd query on YouTube if available
    if len(queries) >= 3:
        try:
            yt_q = queries[2]
            res = GoogleSearch({
                "engine": "youtube",
                "search_query": yt_q,
                "api_key": serpapi_key,
            }).get_dict()
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

    results["success"] = True
    results["manual_links"] = [
        {
            "label": f"Google: {q[:60]}{'…' if len(q)>60 else ''}",
            "url": f"https://www.google.com/search?q={requests.utils.quote(q)}",
        }
        for q in queries
    ] + [
        {
            "label": f"YouTube: {q[:50]}{'…' if len(q)>50 else ''}",
            "url": f"https://www.youtube.com/results?search_query={requests.utils.quote(q)}",
        }
        for q in queries[:3]
    ]
    return results
