"""
LLM-powered analysis of social media posts and URL content.

Sends post text, account context, and media metadata to Claude to produce:
  - Plain-language summary of what's being claimed
  - Key entities (people, places, organisations, events)
  - Credibility signals and red flags
  - Verification queries for Google/YouTube/news sites
  - Overall assessment: likely authentic / needs verification / likely disinformation
"""
import json
import logging
import re
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

POST_ANALYSIS_PROMPT = """You are assisting a forensic investigator analysing a social media post for potential disinformation.

Here is the post data:
{post_data}

Analyse this and return a JSON object with these fields:

{{
  "summary": "Two to three sentences summarising what the post claims or shows.",
  "claims": ["Specific claim 1", "Specific claim 2"],
  "entities": {{
    "people": ["Named individuals mentioned or shown"],
    "organisations": ["Organisations, institutions, agencies"],
    "locations": ["Places mentioned or visible"],
    "events": ["Events referenced"]
  }},
  "credibility_signals": ["Observations that support authenticity"],
  "red_flags": ["Observations that suggest manipulation or disinformation"],
  "assessment": "authentic | needs_verification | likely_disinformation | unclear",
  "assessment_reason": "One sentence explaining the assessment.",
  "verification_queries": [
    "Specific search query to verify claim 1",
    "Specific search query to verify claim 2",
    "YouTube query to find original footage if video",
    "News site query to cross-check"
  ]
}}

Rules:
- Be factual and concise. Do not speculate beyond what the data shows.
- If the post text is in a language other than English, translate it and note the language.
- verification_queries should be specific and actionable, not generic.
- Return ONLY the JSON object, no markdown."""


def analyze_post(post: Dict, account: Dict, account_enrichment: Any,
                 media_files: list, anthropic_api_key: str) -> Dict[str, Any]:
    """
    Send post context to Claude and return structured intelligence summary.
    """
    if not anthropic_api_key:
        return {"success": False, "error": "anthropic_api_key not set in config.yaml"}

    # Build a concise context string for Claude
    lines = []

    text = (post or {}).get("text") or (post or {}).get("full_text", "")
    if text:
        lines.append(f"Post text: {text[:1000]}")

    platform = (post or {}).get("platform") or "unknown"
    lines.append(f"Platform: {platform}")

    if post:
        if post.get("created_at"):
            lines.append(f"Posted: {post['created_at']}")
        if post.get("likes") is not None:
            lines.append(f"Engagement: {post.get('likes',0)} likes, {post.get('reposts',0)} reposts, {post.get('views',0)} views")

    username = (account or {}).get("username") or (post or {}).get("author_username", "")
    if username:
        lines.append(f"Account: @{username}")

    enr = account_enrichment
    if enr:
        if hasattr(enr, "vx_display_name") and enr.vx_display_name:
            lines.append(f"Display name: {enr.vx_display_name}")
        if hasattr(enr, "vx_created_at") and enr.vx_created_at:
            lines.append(f"Account created: {enr.vx_created_at[:10]}")
        if hasattr(enr, "vx_tweet_count") and enr.vx_tweet_count:
            lines.append(f"Tweet count: {enr.vx_tweet_count}")

    followers = (account or {}).get("metrics", {}).get("followers") or (account or {}).get("followers_count")
    if followers:
        lines.append(f"Followers: {followers:,}")

    if media_files:
        types = [m.media_type if hasattr(m, "media_type") else m.get("media_type", "?") for m in media_files]
        lines.append(f"Media: {', '.join(types)}")

    bio = (account or {}).get("bio") or (account or {}).get("description", "")
    if bio:
        lines.append(f"Account bio: {bio[:200]}")

    post_data = "\n".join(lines) or "No post data available."

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_api_key)

        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": POST_ANALYSIS_PROMPT.format(post_data=post_data),
            }],
        )

        raw = response.content[0].text.strip()
        logger.info("Post analysis response: %s…", raw[:100])

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
            else:
                return {"success": False, "error": "Non-JSON response", "raw": raw}

        data["success"] = True
        return data

    except ImportError:
        return {"success": False, "error": "anthropic package not installed"}
    except Exception as e:
        logger.warning("Post analysis failed: %s", e)
        return {"success": False, "error": str(e)}
