"""
Reverse image/video search via SerpAPI.

Sources:
  - Google Lens  (visual matches, text in images, products, general)
  - Yandex       (facial similarity, deepfake detection, finding original sources)

For local files, images are temporarily uploaded to Catbox.moe (free, no key)
to obtain a public URL that SerpAPI can fetch. Catbox URLs are ephemeral.

Requires:
  - serpapi_api_key in config.yaml  (get at serpapi.com, free tier: 100 searches/month)
  - pip install google-search-results

For video files, keyframes are extracted via ffmpeg before searching.
"""
import logging
import os
import subprocess
import tempfile
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests

logger = logging.getLogger(__name__)


# ── Public image uploader (tries multiple hosts) ──────────────────────────────

def _upload_imgur(file_path: str) -> Optional[str]:
    """Upload to Imgur anonymous (no account required)."""
    import base64
    try:
        with open(file_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        r = requests.post(
            "https://api.imgur.com/3/image",
            headers={"Authorization": "Client-ID 546c25a59c58ad7"},
            data={"image": data, "type": "base64"},
            timeout=30, verify=False,
        )
        link = r.json().get("data", {}).get("link")
        if link:
            logger.info("Uploaded to Imgur: %s", link)
            return link
    except Exception as e:
        logger.warning("Imgur upload error: %s", e)
    return None


def _upload_freeimage(file_path: str) -> Optional[str]:
    """Upload to freeimage.host (anonymous, no account needed)."""
    import base64
    try:
        with open(file_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        r = requests.post(
            "https://freeimage.host/api/1/upload",
            data={"key": "6d207e02198a847aa98d0a2a901485a5", "source": data, "format": "json"},
            timeout=30,
        )
        if r.status_code == 200:
            url = r.json().get("image", {}).get("url")
            if url:
                logger.info("Uploaded to freeimage.host: %s", url)
                return url
        logger.warning("freeimage.host upload failed: %s", r.text[:80])
    except Exception as e:
        logger.warning("freeimage.host upload error: %s", e)
    return None


def _upload_tmpfiles(file_path: str) -> Optional[str]:
    """Upload to tmpfiles.org (anonymous, 60-day expiry)."""
    try:
        with open(file_path, "rb") as f:
            r = requests.post("https://tmpfiles.org/api/v1/upload",
                              files={"file": f}, timeout=30)
        if r.status_code == 200:
            url = r.json().get("data", {}).get("url", "")
            if url:
                direct = url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
                logger.info("Uploaded to tmpfiles.org: %s", direct)
                return direct
        logger.warning("tmpfiles.org upload failed: %s", r.text[:80])
    except Exception as e:
        logger.warning("tmpfiles.org upload error: %s", e)
    return None


def _upload_gofile(file_path: str) -> Optional[str]:
    """Upload to gofile.io (anonymous, no expiry stated)."""
    try:
        # Get best server first
        srv = requests.get("https://api.gofile.io/getServer", timeout=10).json()
        server = srv.get("data", {}).get("server", "store1")
        with open(file_path, "rb") as f:
            r = requests.post(f"https://{server}.gofile.io/uploadFile",
                              files={"file": f}, timeout=30)
        if r.status_code == 200 and r.json().get("status") == "ok":
            link = r.json()["data"].get("directLink") or r.json()["data"].get("downloadPage")
            if link:
                logger.info("Uploaded to gofile.io: %s", link)
                return link
        logger.warning("gofile.io upload failed: %s", r.text[:80])
    except Exception as e:
        logger.warning("gofile.io upload error: %s", e)
    return None


def upload_to_catbox(file_path: str) -> Optional[str]:
    """
    Upload a local image to a public host and return a URL SerpAPI can fetch.
    Tries multiple hosts in order until one succeeds.
    """
    for name, fn in [
        ("freeimage.host", _upload_freeimage),
        ("tmpfiles.org",   _upload_tmpfiles),
        ("Imgur",          _upload_imgur),
        ("gofile.io",      _upload_gofile),
    ]:
        url = fn(file_path)
        if url:
            return url
        logger.info("%s failed — trying next host", name)

    # Last resort: Catbox (frequently rejects anonymous uploads)
    try:
        with open(file_path, "rb") as f:
            r = requests.post("https://catbox.moe/user/api.php",
                              data={"reqtype": "fileupload"},
                              files={"fileToUpload": f}, timeout=30)
        if r.status_code == 200 and r.text.startswith("https://"):
            logger.info("Uploaded to Catbox: %s", r.text.strip())
            return r.text.strip()
    except Exception as e:
        logger.warning("Catbox upload error: %s", e)

    logger.error("All upload hosts failed for %s", file_path)
    return None


# ── Video keyframe extraction ─────────────────────────────────────────────────

def extract_keyframes(video_path: str, output_dir: str, n_frames: int = 5) -> List[str]:
    """
    Extract N evenly-spaced keyframes from a video using ffmpeg.
    Returns list of frame file paths.
    """
    frames = []
    try:
        # Get video duration
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=15,
        )
        duration = float(r.stdout.strip()) if r.stdout.strip() else 30.0
        os.makedirs(output_dir, exist_ok=True)

        for i in range(n_frames):
            ts = (i + 0.5) * duration / n_frames
            frame_path = os.path.join(output_dir, f"kf_{i:03d}.jpg")
            result = subprocess.run(
                ["ffmpeg", "-ss", str(ts), "-i", video_path,
                 "-frames:v", "1", "-q:v", "2", frame_path, "-y"],
                capture_output=True, timeout=20,
            )
            if result.returncode == 0 and os.path.exists(frame_path):
                frames.append(frame_path)

        logger.info("Extracted %d keyframes from %s", len(frames), video_path)
    except FileNotFoundError:
        logger.warning("ffmpeg not found — cannot extract keyframes")
    except Exception as e:
        logger.warning("Keyframe extraction failed: %s", e)
    return frames


# ── SerpAPI search ────────────────────────────────────────────────────────────

def _check_serpapi_error(result: dict) -> None:
    """Raise only for account/quota/auth errors. Ignore 'no results' responses."""
    err = result.get("error", "")
    if not err:
        return
    # These are legitimate "nothing found" responses — not errors
    no_results_phrases = [
        "hasn't returned any results",
        "no results",
        "no visual matches",
    ]
    if any(p in err.lower() for p in no_results_phrases):
        logger.info("SerpAPI returned no results (not an error): %s", err)
        return
    # Anything else (quota, auth, invalid key) is a real error
    raise RuntimeError(f"SerpAPI error: {err}")


def _search_google_lens(api_key: str, image_url: str, max_results: int = 50) -> List[Dict]:
    matches = []
    try:
        from serpapi import GoogleSearch
        results = GoogleSearch({
            "engine": "google_lens",
            "url": image_url,
            "api_key": api_key,
        }).get_dict()
        _check_serpapi_error(results)

        for item in results.get("visual_matches", [])[:max_results]:
            matches.append({
                "engine": "google_lens",
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "source": item.get("source", ""),
                "thumbnail": item.get("thumbnail", ""),
                "date": item.get("date", ""),
                "snippet": item.get("snippet", ""),
            })
    except RuntimeError:
        raise   # propagate quota/account errors up
    except Exception as e:
        logger.warning("Google Lens search failed: %s", e)
    return matches


def _search_yandex(api_key: str, image_url: str, max_results: int = 50) -> List[Dict]:
    matches = []
    try:
        from serpapi import GoogleSearch
        results = GoogleSearch({
            "engine": "yandex_images",
            "url": image_url,
            "api_key": api_key,
        }).get_dict()

        def _yandex_thumb(item):
            """
            Extract a usable thumbnail URL from a Yandex result item.
            SerpAPI sometimes returns thumbnail as a dict {url, width, height}
            rather than a plain string — handle both forms.
            """
            def _extract(val):
                if isinstance(val, str) and val.startswith("http"):
                    return val
                if isinstance(val, dict):
                    return val.get("url") or val.get("src") or val.get("link") or ""
                return ""

            for key in ("thumbnail", "original", "image", "preview", "url", "image_url"):
                url = _extract(item.get(key))
                if url:
                    return url

            logger.info("Yandex item has no thumbnail — fields: %s",
                        {k: str(v)[:80] for k, v in item.items()})
            return ""

        # Yandex returns similar_images
        for item in results.get("similar_images", [])[:max_results]:
            matches.append({
                "engine": "yandex",
                "title": item.get("title", "Yandex match"),
                "link": item.get("link", "") or item.get("source", ""),
                "source": item.get("source", "") or item.get("domain", "") or item.get("website", ""),
                "thumbnail": _yandex_thumb(item),
                "snippet": item.get("snippet", ""),
            })

        # Also include visual matches if present
        for item in results.get("visual_matches", [])[:max_results]:
            matches.append({
                "engine": "yandex",
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "source": item.get("source", "") or item.get("domain", ""),
                "thumbnail": _yandex_thumb(item),
            })
    except Exception as e:
        logger.warning("Yandex search failed: %s", e)
    return matches


def _search_bing_visual(api_key: str, image_url: str, max_results: int = 50) -> List[Dict]:
    """Bing Visual Search via SerpAPI."""
    matches = []
    try:
        from serpapi import GoogleSearch
        results = GoogleSearch({
            "engine": "bing_images",
            "q": image_url,
            "api_key": api_key,
        }).get_dict()
        for item in results.get("inline_images", results.get("images_results", []))[:max_results]:
            matches.append({
                "engine": "bing",
                "title": item.get("title", ""),
                "link": item.get("link", "") or item.get("source", ""),
                "source": item.get("source", ""),
                "thumbnail": item.get("thumbnail", ""),
            })
    except Exception as e:
        logger.debug("Bing search failed: %s", e)
    return matches


def search_image(
    image_path: str,
    api_key: str,
    engines: List[str] = None,
    max_results: int = 50,
) -> Dict[str, Any]:
    """
    Run reverse image search on a local file via SerpAPI.
    Uploads to Catbox.moe first to get a public URL.
    """
    if engines is None:
        engines = ["google_lens", "yandex"]

    if not os.path.exists(image_path):
        return {"success": False, "error": f"File not found: {image_path}"}

    # Upload to get a public URL
    public_url = upload_to_catbox(image_path)
    if not public_url:
        return {"success": False, "error": "Failed to upload image to Catbox.moe for public hosting"}

    all_matches = []
    errors = []

    if "google_lens" in engines:
        m = _search_google_lens(api_key, public_url, max_results)
        all_matches.extend(m)
        if not m:
            errors.append("Google Lens: no results")

    if "yandex" in engines:
        m = _search_yandex(api_key, public_url, max_results)
        all_matches.extend(m)
        if not m:
            errors.append("Yandex: no results")

    if "bing" in engines:
        m = _search_bing_visual(api_key, public_url, max_results)
        all_matches.extend(m)

    # Find earliest result by date if available
    earliest = None
    for match in all_matches:
        if match.get("date"):
            if earliest is None or match["date"] < earliest["date"]:
                earliest = match

    return {
        "success": True,
        "public_url": public_url,
        "image_path": image_path,
        "matches": all_matches,
        "earliest_match": earliest,
        "engines_used": engines,
        "errors": errors,
    }


# ── Main entry point for pipeline ────────────────────────────────────────────

def run_reverse_search_for_media(
    media_file,
    api_key: str,
    case_dir: str,
    max_results: int = 50,
) -> Dict[str, Any]:
    """
    Run reverse search for a media file (image or video).
    For video: extracts keyframes first, searches each one.
    Returns aggregated results.
    """
    mf = media_file if isinstance(media_file, dict) else media_file.model_dump()
    local_path = mf.get("local_path", "")
    media_type = mf.get("media_type", "unknown")
    filename = mf.get("filename", "")

    result = {
        "filename": filename,
        "media_type": media_type,
        "frames_searched": [],
        "all_matches": [],
        "earliest_match": None,
        "error": None,
        "manual_links": {
            "google_lens": f"https://lens.google.com/",
            "yandex": f"https://yandex.com/images/",
            "tineye": f"https://tineye.com/",
        },
    }

    if not api_key:
        result["error"] = "No SerpAPI key configured — set serpapi_api_key in config.yaml"
        return result

    if not local_path or not os.path.exists(local_path):
        result["error"] = f"Media file not found: {local_path}"
        return result

    search_paths = []

    if media_type == "image":
        search_paths = [local_path]

    elif media_type == "video":
        kf_dir = os.path.join(case_dir, "keyframes", Path(filename).stem)
        frames = extract_keyframes(local_path, kf_dir, n_frames=5)
        if not frames:
            result["error"] = "No keyframes extracted (ffmpeg required for video)"
            return result
        search_paths = frames

    elif media_type == "audio":
        result["error"] = "Reverse search not applicable to audio files"
        return result

    # Search each frame/image
    all_matches = []
    for path in search_paths:
        frame_result = search_image(path, api_key, max_results=max_results)
        frame_info = {
            "path": path,
            "filename": os.path.basename(path),
            "success": frame_result.get("success"),
            "public_url": frame_result.get("public_url"),
            "matches": frame_result.get("matches", []),
            "error": frame_result.get("error"),
        }
        result["frames_searched"].append(frame_info)
        all_matches.extend(frame_result.get("matches", []))

    result["all_matches"] = all_matches

    # Find overall earliest match
    for m in all_matches:
        if m.get("date"):
            if result["earliest_match"] is None or m["date"] < result["earliest_match"]["date"]:
                result["earliest_match"] = m

    return result


def run_all_reverse_searches(case, api_key: str, case_dir: str) -> Dict[str, Any]:
    """Run reverse search for all media files in a case."""
    results = {}
    for mf in (case.media_files or []):
        mf_dict = mf if isinstance(mf, dict) else mf.model_dump()
        filename = mf_dict.get("filename", "unknown")
        media_type = mf_dict.get("media_type", "")
        if media_type not in ("image", "video"):
            continue
        logger.info("Running reverse search for %s (%s)", filename, media_type)
        results[filename] = run_reverse_search_for_media(mf, api_key, case_dir)
    return results
