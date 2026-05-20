"""
Media downloader + EXIF/metadata extractor.
Downloads images, video, audio from URLs. Extracts metadata and GPS.
"""
import hashlib
import os
import uuid
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

import requests

from backend.models import MediaFileSummary

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".heic"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv", ".m4v", ".ts"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus"}

CONTENT_TYPE_MAP = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "video/mp4": ".mp4", "video/webm": ".webm",
    "video/quicktime": ".mov", "audio/mpeg": ".mp3", "audio/wav": ".wav",
    "audio/ogg": ".ogg", "audio/mp4": ".m4a",
}


def hash_file(path: str) -> Tuple[str, str]:
    sha256, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
            md5.update(chunk)
    return sha256.hexdigest(), md5.hexdigest()


def media_type_from_ext(ext: str) -> str:
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "unknown"


def ext_from_content_type(ct: str) -> str:
    ct = ct.split(";")[0].strip().lower()
    return CONTENT_TYPE_MAP.get(ct, "")


def extract_gps_from_exif(exif_data: dict) -> Optional[Tuple[float, float, Optional[float]]]:
    try:
        from PIL.ExifTags import GPSTAGS
        gps_info = exif_data.get("GPSInfo") or exif_data.get(34853)
        if not gps_info:
            return None

        def dms_to_dec(dms, ref):
            try:
                d = float(dms[0]) if not hasattr(dms[0], 'numerator') else dms[0].numerator / dms[0].denominator
                m = float(dms[1]) if not hasattr(dms[1], 'numerator') else dms[1].numerator / dms[1].denominator
                s = float(dms[2]) if not hasattr(dms[2], 'numerator') else dms[2].numerator / dms[2].denominator
            except Exception:
                d, m, s = float(dms[0]), float(dms[1]), float(dms[2])
            dec = d + m / 60 + s / 3600
            if ref in ("S", "W"):
                dec = -dec
            return dec

        # GPS tag IDs: 2=GPSLatitude, 1=GPSLatitudeRef, 4=GPSLongitude, 3=GPSLongitudeRef, 6=GPSAltitude
        lat_dms = gps_info.get(2)
        lat_ref = gps_info.get(1, "N")
        lon_dms = gps_info.get(4)
        lon_ref = gps_info.get(3, "E")
        alt_raw = gps_info.get(6)

        if not lat_dms or not lon_dms:
            return None

        lat = dms_to_dec(lat_dms, lat_ref)
        lon = dms_to_dec(lon_dms, lon_ref)
        alt = float(alt_raw) if alt_raw else None

        if lat == 0.0 and lon == 0.0:
            return None
        return lat, lon, alt
    except Exception as e:
        logger.debug("GPS extraction failed: %s", e)
        return None


def extract_image_metadata(path: str) -> Tuple[Dict[str, Any], Optional[Tuple[float, float, Optional[float]]]]:
    meta = {}
    gps = None
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        img = Image.open(path)
        meta["format"] = img.format
        meta["size"] = f"{img.width}x{img.height}"
        meta["mode"] = img.mode

        raw_exif = img._getexif() or {}
        useful = ["Make", "Model", "Software", "DateTime", "DateTimeOriginal",
                  "DateTimeDigitized", "Flash", "FocalLength", "ExposureTime",
                  "FNumber", "ISOSpeedRatings", "Artist", "Copyright"]
        decoded = {}
        for tag_id, val in raw_exif.items():
            tag = TAGS.get(tag_id, str(tag_id))
            if tag == "GPSInfo":
                decoded["GPSInfo"] = val
            elif tag in useful:
                try:
                    decoded[tag] = str(val) if not isinstance(val, (str, int, float)) else val
                except Exception:
                    pass
        meta.update({k: v for k, v in decoded.items() if k != "GPSInfo"})
        gps = extract_gps_from_exif(decoded)
    except ImportError:
        logger.debug("Pillow not installed, skipping image metadata")
    except Exception as e:
        logger.debug("Image metadata failed for %s: %s", path, e)
    return meta, gps


def extract_video_metadata(path: str) -> Tuple[Dict[str, Any], Optional[Tuple[float, float, Optional[float]]]]:
    meta = {}
    gps = None
    try:
        import subprocess
        import json
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            fmt = data.get("format", {})
            tags = fmt.get("tags", {})
            meta["duration_s"] = fmt.get("duration")
            meta["format_name"] = fmt.get("format_name")
            meta["bit_rate"] = fmt.get("bit_rate")
            for k in ["creation_time", "encoder", "title", "artist", "major_brand"]:
                if k in tags:
                    meta[k] = tags[k]
            loc = tags.get("location") or tags.get("com.apple.quicktime.location.ISO6709")
            if loc:
                m = re.match(r'([+-]\d+\.\d+)([+-]\d+\.\d+)', loc)
                if m:
                    gps = (float(m.group(1)), float(m.group(2)), None)
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    meta["video_codec"] = stream.get("codec_name")
                    meta["resolution"] = f"{stream.get('width')}x{stream.get('height')}"
                elif stream.get("codec_type") == "audio":
                    meta["audio_codec"] = stream.get("codec_name")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug("Video metadata failed: %s", e)
    return meta, gps


def extract_audio_metadata(path: str) -> Tuple[Dict[str, Any], None]:
    meta = {}
    try:
        from mutagen import File as MFile
        audio = MFile(path)
        if audio:
            if hasattr(audio.info, "length"):
                meta["duration_s"] = round(audio.info.length, 2)
            if hasattr(audio.info, "bitrate"):
                meta["bitrate"] = audio.info.bitrate
            if audio.tags:
                for k, v in list(audio.tags.items())[:10]:
                    try:
                        meta[str(k)] = str(v)[:200]
                    except Exception:
                        pass
    except ImportError:
        pass
    except Exception as e:
        logger.debug("Audio metadata failed: %s", e)
    return meta, None


def reverse_image_search_urls(image_url: str) -> Dict[str, str]:
    from urllib.parse import quote_plus
    enc = quote_plus(image_url)
    return {
        "google_lens": f"https://lens.google.com/uploadbyurl?url={enc}",
        "yandex": f"https://yandex.com/images/search?url={enc}&rpt=imageview",
        "tineye": f"https://tineye.com/search?url={enc}",
        "bing": f"https://www.bing.com/images/search?q=imgurl:{enc}&view=detailv2&iss=sbi",
    }


def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    try:
        from geopy.geocoders import Nominatim
        geo = Nominatim(user_agent="osint-forensic-tool/1.0")
        loc = geo.reverse(f"{lat}, {lon}", timeout=10)
        return loc.address if loc else None
    except ImportError:
        return None
    except Exception:
        return None


def download_with_ytdlp(url: str, output_dir: str) -> List[MediaFileSummary]:
    results = []
    try:
        import yt_dlp
        ydl_opts = {
            "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "writethumbnail": False,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        for fname in os.listdir(output_dir):
            fpath = os.path.join(output_dir, fname)
            if not os.path.isfile(fpath):
                continue
            ext = Path(fname).suffix.lower()
            mtype = media_type_from_ext(ext)
            if mtype == "unknown":
                continue
            sha256, md5 = hash_file(fpath)
            meta = {}
            if info:
                meta["title"] = info.get("title", "")
                meta["uploader"] = info.get("uploader", "")
                meta["upload_date"] = info.get("upload_date", "")
                meta["duration"] = info.get("duration")
                meta["view_count"] = info.get("view_count")
                meta["like_count"] = info.get("like_count")

            results.append(MediaFileSummary(
                filename=fname,
                media_type=mtype,
                file_size=os.path.getsize(fpath),
                hash_sha256=sha256,
                hash_md5=md5,
                source_url=url,
                local_path=fpath,
                metadata=meta,
                reverse_search_urls=reverse_image_search_urls(url) if mtype == "image" else {},
            ))
    except ImportError:
        logger.warning("yt-dlp not installed")
    except Exception as e:
        logger.debug("yt-dlp failed for %s: %s", url, e)
    return results


def download_direct(url: str, output_dir: str) -> Optional[MediaFileSummary]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30, stream=True)
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        ext = ext_from_content_type(ct)
        if not ext:
            path_ext = Path(url.split("?")[0]).suffix.lower()
            ext = path_ext if path_ext in IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS else ".bin"
        mtype = media_type_from_ext(ext)
        fname = f"{uuid.uuid4().hex[:8]}{ext}"
        fpath = os.path.join(output_dir, fname)
        with open(fpath, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        sha256, md5 = hash_file(fpath)
        return MediaFileSummary(
            filename=fname,
            media_type=mtype,
            file_size=os.path.getsize(fpath),
            hash_sha256=sha256,
            hash_md5=md5,
            source_url=url,
            local_path=fpath,
            reverse_search_urls=reverse_image_search_urls(url) if mtype == "image" else {},
        )
    except Exception as e:
        logger.debug("Direct download failed for %s: %s", url, e)
        return None


def enrich_metadata(mf: MediaFileSummary) -> MediaFileSummary:
    path = mf.local_path
    if not os.path.exists(path):
        return mf
    if mf.media_type == "image":
        meta, gps = extract_image_metadata(path)
    elif mf.media_type == "video":
        meta, gps = extract_video_metadata(path)
    elif mf.media_type == "audio":
        meta, gps = extract_audio_metadata(path)
    else:
        return mf
    mf.metadata = {**mf.metadata, **meta}
    if gps:
        lat, lon, alt = gps
        mf.gps_lat = lat
        mf.gps_lon = lon
        mf.gps_address = reverse_geocode(lat, lon)
    return mf


def run_ocr(mf: MediaFileSummary, keyframes_dir: str) -> Optional[str]:
    try:
        import pytesseract
        from PIL import Image
        if mf.media_type == "image":
            img = Image.open(mf.local_path)
            text = pytesseract.image_to_string(img)
            return text.strip() or None
        elif mf.media_type == "video":
            import subprocess
            os.makedirs(keyframes_dir, exist_ok=True)
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", mf.local_path],
                capture_output=True, text=True, timeout=10
            )
            duration = float(r.stdout.strip()) if r.stdout.strip() else 30
            texts = []
            for i in range(min(5, int(duration // 10) + 1)):
                ts = i * max(1, int(duration / 5))
                fpath = os.path.join(keyframes_dir, f"frame_{i:03d}.jpg")
                subprocess.run(
                    ["ffmpeg", "-ss", str(ts), "-i", mf.local_path,
                     "-frames:v", "1", "-q:v", "2", fpath, "-y"],
                    capture_output=True, timeout=15
                )
                if os.path.exists(fpath):
                    t = pytesseract.image_to_string(Image.open(fpath))
                    if t.strip():
                        texts.append(t.strip())
            return "\n---\n".join(texts) if texts else None
    except ImportError:
        pass
    except Exception as e:
        logger.debug("OCR failed: %s", e)
    return None


def download_post_media(post_data: dict, media_dir: str, max_files: int = 10) -> List[MediaFileSummary]:
    os.makedirs(media_dir, exist_ok=True)
    results = []
    media_items = post_data.get("media") or []

    for item in media_items[:max_files]:
        url = item.get("url") or item.get("media_url_https") or item.get("preview_image_url")
        if not url:
            continue
        mf = download_direct(url, media_dir)
        if mf:
            mf = enrich_metadata(mf)
            results.append(mf)
        if len(results) >= max_files:
            break

    return results
