import os
import shutil
import threading
from pathlib import Path
from typing import List

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.config import config
from backend.models import Case, InvestigateRequest
from backend.pipeline import CaseManager, run_pipeline

router = APIRouter()

_CASES_DIR = str(Path(__file__).parent.parent.parent / config.get("storage", {}).get("cases_dir", "cases"))


def get_manager() -> CaseManager:
    return CaseManager(_CASES_DIR)


@router.post("/cases", response_model=Case, status_code=201)
async def create_case(req: InvestigateRequest, background_tasks: BackgroundTasks):
    manager = get_manager()
    case = manager.create(req.url, req.notes)
    background_tasks.add_task(run_pipeline, case.id, manager)
    return case


@router.get("/cases", response_model=List[Case])
async def list_cases():
    return get_manager().list_all()


@router.get("/cases/{case_id}", response_model=Case)
async def get_case(case_id: str):
    case = get_manager().get(case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    return case


@router.delete("/cases/{case_id}")
async def delete_case(case_id: str):
    manager = get_manager()
    if not manager.get(case_id):
        raise HTTPException(404, "Case not found")
    shutil.rmtree(Path(_CASES_DIR) / case_id, ignore_errors=True)
    return {"deleted": case_id}


@router.get("/cases/{case_id}/report")
async def get_report(case_id: str):
    manager = get_manager()
    case = manager.get(case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    report_path = Path(_CASES_DIR) / case_id / "case.json"
    return FileResponse(report_path, media_type="application/json",
                        filename=f"osint_case_{case_id}.json")


@router.get("/cases/{case_id}/media/{filename}")
async def get_media(case_id: str, filename: str):
    import mimetypes
    fpath = Path(_CASES_DIR) / case_id / "media" / filename
    if not fpath.exists():
        raise HTTPException(404, "File not found")
    mt = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(str(fpath), media_type=mt)


@router.get("/cases/{case_id}/keyframes/{filename}")
async def get_keyframes(case_id: str, filename: str):
    """Extract keyframes from a video and return their paths/timestamps."""
    import subprocess, tempfile, json as _json
    case_dir = Path(_CASES_DIR) / case_id
    video_path = case_dir / "media" / filename
    if not video_path.exists():
        raise HTTPException(404, "File not found")

    kf_dir = case_dir / "keyframes" / video_path.stem
    kf_dir.mkdir(parents=True, exist_ok=True)

    # Get duration
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, timeout=15
    )
    try:
        duration = float(r.stdout.strip())
    except ValueError:
        duration = 30.0

    n = 8
    frames = []
    for i in range(n):
        ts = round((i + 0.5) * duration / n, 2)
        frame_file = kf_dir / f"kf_{i:03d}.jpg"
        if not frame_file.exists():
            subprocess.run(
                ["ffmpeg", "-ss", str(ts), "-i", str(video_path),
                 "-frames:v", "1", "-q:v", "2", str(frame_file), "-y"],
                capture_output=True, timeout=20
            )
        if frame_file.exists():
            frames.append({
                "index": i,
                "timestamp": ts,
                "filename": frame_file.name,
                "url": f"/api/cases/{case_id}/keyframes/{filename}/{frame_file.name}",
            })

    return {"duration": duration, "frames": frames}


@router.get("/cases/{case_id}/keyframes/{video_filename}/{frame_filename}")
async def get_keyframe_image(case_id: str, video_filename: str, frame_filename: str):
    """Serve a specific keyframe image."""
    from pathlib import Path as P
    stem = P(video_filename).stem
    fpath = Path(_CASES_DIR) / case_id / "keyframes" / stem / frame_filename
    if not fpath.exists():
        raise HTTPException(404, "Keyframe not found")
    return FileResponse(str(fpath), media_type="image/jpeg")


class ReverseSearchRequest(BaseModel):
    filename: str
    timestamp: float | None = None


@router.post("/cases/{case_id}/reverse-search")
async def run_reverse_search(case_id: str, req: ReverseSearchRequest):
    """Run reverse image search on a media file or specific video frame."""
    import subprocess, tempfile as _tmpfile, os as _os
    case_dir = Path(_CASES_DIR) / case_id
    media_path = case_dir / "media" / req.filename
    if not media_path.exists():
        raise HTTPException(404, "Media file not found")

    api_key = config.get("serpapi_api_key", "")
    if not api_key:
        raise HTTPException(400, "serpapi_api_key not configured in config.yaml")

    search_path = str(media_path)

    # For video with timestamp: extract a specific frame
    if req.timestamp is not None:
        kf_dir = case_dir / "keyframes" / media_path.stem
        kf_dir.mkdir(parents=True, exist_ok=True)
        frame_path = kf_dir / f"custom_{req.timestamp:.2f}.jpg"
        result = subprocess.run(
            ["ffmpeg", "-ss", str(req.timestamp), "-i", str(media_path),
             "-frames:v", "1", "-q:v", "2", str(frame_path), "-y"],
            capture_output=True, timeout=20
        )
        if result.returncode != 0 or not frame_path.exists():
            raise HTTPException(500, "Failed to extract frame")
        search_path = str(frame_path)
    elif str(media_path).endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm')):
        # Video with no timestamp: use first keyframe
        kf_dir = case_dir / "keyframes" / media_path.stem
        kf_dir.mkdir(parents=True, exist_ok=True)
        frame_path = kf_dir / "kf_000.jpg"
        if not frame_path.exists():
            subprocess.run(
                ["ffmpeg", "-ss", "1", "-i", str(media_path),
                 "-frames:v", "1", "-q:v", "2", str(frame_path), "-y"],
                capture_output=True, timeout=20
            )
        if frame_path.exists():
            search_path = str(frame_path)

    from backend.modules.reverse_search import upload_to_catbox, _search_google_lens, _search_yandex
    public_url = upload_to_catbox(search_path)
    if not public_url:
        raise HTTPException(502, "Failed to upload to Catbox.moe")

    gl = _search_google_lens(api_key, public_url, max_results=10)
    yx = _search_yandex(api_key, public_url, max_results=10)
    all_matches = gl + yx

    earliest = None
    for m in all_matches:
        if m.get("date"):
            if earliest is None or m["date"] < earliest["date"]:
                earliest = m

    return {
        "success": True,
        "search_path": search_path,
        "public_url": public_url,
        "matches": all_matches,
        "earliest_match": earliest,
        "google_lens_count": len(gl),
        "yandex_count": len(yx),
    }
