import os
import shutil
import threading
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
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
    case = manager.create(req.url, req.notes, req.name)
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
def get_keyframes(case_id: str, filename: str):
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
def get_keyframe_image(case_id: str, video_filename: str, frame_filename: str):
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
def run_reverse_search(case_id: str, req: ReverseSearchRequest):
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

    try:
        gl = _search_google_lens(api_key, public_url, max_results=10)
    except RuntimeError as e:
        raise HTTPException(402, str(e))
    try:
        yx = _search_yandex(api_key, public_url, max_results=10)
    except RuntimeError as e:
        raise HTTPException(402, str(e))
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


# ── Media-first investigation ─────────────────────────────────────────────────

def _run_media_pipeline(case_id: str, manager: CaseManager):
    """Background task: run full media-first investigation."""
    import mimetypes
    from datetime import datetime, timezone
    from backend.models import CaseStatus, ProgressStep, StepStatus, GuidanceItem

    case = manager.get(case_id)
    if not case:
        return

    def _now():
        return datetime.now(timezone.utc).isoformat()

    def step_start(name, label):
        for s in case.steps:
            if s.name == name:
                s.status = StepStatus.RUNNING
                s.started_at = _now()
                s.label = label
                break
        manager.save(case)

    def step_done(name, msg=""):
        for s in case.steps:
            if s.name == name:
                s.status = StepStatus.COMPLETED
                s.message = msg
                s.completed_at = _now()
                break
        manager.save(case)

    def step_fail(name, msg=""):
        for s in case.steps:
            if s.name == name:
                s.status = StepStatus.FAILED
                s.message = msg
                break
        manager.save(case)

    case.status = CaseStatus.RUNNING
    manager.save(case)

    api_key = config.get("serpapi_api_key", "")
    if not api_key:
        case.errors.append("serpapi_api_key not set in config.yaml")
        case.status = CaseStatus.FAILED
        manager.save(case)
        return

    case_dir = manager.case_dir(case_id)
    media_dir = case_dir / "media"
    media_files = list(media_dir.iterdir()) if media_dir.exists() else []

    if not media_files:
        case.errors.append("No media file found in case directory")
        case.status = CaseStatus.FAILED
        manager.save(case)
        return

    raw_file = media_files[0]
    file_path = str(raw_file)

    # Use selected keyframe if the user picked one (video flow)
    saved_inv = dict(case.media_investigation or {})   # preserve keyframes/frame metadata
    if saved_inv.get("selected_frame_path"):
        file_path = saved_inv["selected_frame_path"]

    # Extract source_url from notes field (stored as "source_url:URL\n...")
    source_url = None
    if case.notes and case.notes.startswith("source_url:"):
        lines = case.notes.split("\n", 1)
        source_url = lines[0][len("source_url:"):].strip()

    def _log(msg, level="info"):
        from datetime import datetime, timezone
        case.logs.append({"ts": datetime.now(timezone.utc).strftime("%H:%M:%S"), "msg": msg, "level": level})

    try:

        # Step 1: Reverse search + proactive known-account check
        anthropic_key = config.get("anthropic_api_key", "")
        step_start("reverse_search",
                   "Image Analysis + Reverse Search" if anthropic_key else "Reverse Image Search + Known Account Check")
        _log("Uploading to public host and running reverse image search…")
        manager.save(case)
        from backend.modules.media_pipeline import run_media_investigation, parse_social_url, scrape_account
        pipeline_result = run_media_investigation(file_path, api_key, max_results=15,
                                                  anthropic_api_key=anthropic_key)
        proactive = pipeline_result.get("proactive_check", {})
        n_proactive = len(proactive.get("confirmed_matches", []))
        # Merge pipeline results into saved_inv so keyframes/frame-selection metadata is preserved
        raw_matches = pipeline_result.get("raw_matches", [])
        saved_inv.update({
            "public_url": pipeline_result.get("public_url"),
            "raw_match_count": len(raw_matches),
            "reverse_search_matches": raw_matches,
            "source_url": source_url,
            "proactive_matches": n_proactive,
            "proactive_checked": len(proactive.get("checked", [])),
            "manual_links": proactive.get("manual_links", {}),
            "ocr_keywords": proactive.get("ocr_keywords", []),
            "llm_analysis": pipeline_result.get("llm_analysis"),
            "context_search": pipeline_result.get("context_search"),
        })
        case.media_investigation = saved_inv
        inv = pipeline_result   # alias for the rest of this function
        discovered = list(inv.get("discovered_accounts", []))

        # Inject user-provided source URL as the first account
        if source_url:
            parsed = parse_social_url(source_url)
            if parsed:
                src_acct = {**parsed, "match_engine": "user-provided source",
                            "match_title": "Original source URL provided by investigator",
                            "post_date": "", "source_domain": ""}
                try:
                    scraped = scrape_account(parsed)
                    src_acct = {**src_acct, **{k: v for k, v in scraped.items() if v is not None}}
                except Exception:
                    pass
                # Score it — it's the known origin so give it a strong base
                src_acct["severity_score"] = 90
                src_acct["severity_label"] = "CRITICAL"
                src_acct["score_reasons"] = ["user-identified source of this media"]
                # Apply known-account boost on top
                try:
                    from backend.modules.known_accounts import apply_known_account_scoring
                    apply_known_account_scoring(src_acct)
                except Exception:
                    pass
                src_acct["rank"] = 0
                # Re-rank everything else
                for a in discovered:
                    a["rank"] = a.get("rank", 0) + 1
                discovered.insert(0, src_acct)
            else:
                # Not a recognised social URL — store it as a generic source note
                case.media_investigation["source_url_note"] = f"Source URL provided: {source_url}"

        case.discovered_accounts = discovered
        step_done("reverse_search",
                  f"{len(inv.get('raw_matches',[]))} visual matches · "
                  f"{n_proactive} known-account proactive matches"
                  + (f" · source: {source_url}" if source_url else ""))

        # Step 2: Generate guidance from discovered accounts
        step_start("guidance", "Generating Investigation Leads")
        guidance = []
        for acct in discovered[:10]:
            sev = acct.get("severity_label", "LOW").lower()
            score = acct.get("severity_score", 0)
            platform = acct.get("platform", "web")
            username = acct.get("username") or acct.get("display_name") or "Unknown"
            url = acct.get("account_url") or acct.get("post_url", "")
            reasons = ", ".join(acct.get("score_reasons", [])) or "no specific flags"
            followers = acct.get("followers")
            follower_str = f" · {followers:,} followers" if followers else ""
            created = acct.get("created_at", "")[:10] if acct.get("created_at") else ""
            created_str = f" · account created {created}" if created else ""

            guidance.append(GuidanceItem(
                priority=acct.get("rank", 99),
                severity=sev if sev in ("critical","high","medium","low") else "medium",
                title=f"[{platform.upper()}] @{username} shared this content (score: {score})",
                detail=f"This account was found sharing the uploaded media via reverse image search.{follower_str}{created_str}. "
                       f"Flags: {reasons}.",
                action=f"Investigate this {platform} account fully — click 'Full Investigation' to run the complete pipeline.",
                pivot_url=url,
                pivot_label=f"Open {platform} profile",
                category="network",
                auto_result={"account": acct},
                auto_status="done",
            ))

        case.guidance = guidance
        step_done("guidance", f"{len(guidance)} leads generated")

        case.status = CaseStatus.COMPLETED
    except Exception as e:
        case.errors.append(f"Media pipeline error: {e}")
        case.status = CaseStatus.FAILED
        for s in case.steps:
            if s.status == StepStatus.RUNNING:
                s.status = StepStatus.FAILED
                s.message = str(e)

    manager.save(case)


VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.3gp'}


@router.post("/media-cases", response_model=Case, status_code=201)
async def create_media_case(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    notes: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
):
    """
    Upload image → pipeline runs immediately.
    Upload video → extract keyframes, wait for user to select one, then run pipeline.
    """
    import uuid as _uuid
    import re as _re
    from datetime import datetime, timezone
    from backend.models import CaseStatus, ProgressStep

    manager = get_manager()
    case_id = _uuid.uuid4().hex[:12]

    original_name = file.filename or "upload"
    suffix = Path(original_name).suffix.lower() or ".jpg"
    # Sanitize: keep alphanumerics, dots, hyphens, underscores
    base = Path(original_name).stem
    safe_base = _re.sub(r'[^\w.\-]', '_', base)[:64] or "upload"
    safe_name = safe_base + suffix
    is_video = suffix in VIDEO_EXTS

    case_dir = manager.case_dir(case_id)
    media_dir = case_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    dest = media_dir / safe_name
    content = await file.read()
    dest.write_bytes(content)

    from backend.models import Case as CaseModel
    now = datetime.now(timezone.utc).isoformat()
    combined_notes = notes or ""
    if source_url:
        combined_notes = f"source_url:{source_url}" + (f"\n{notes}" if notes else "")

    case = CaseModel(
        id=case_id,
        url=f"media:{safe_name}",
        name=name or None,
        notes=combined_notes or None,
        source_type="media_upload",
        status=CaseStatus.FRAME_SELECT if is_video else CaseStatus.PENDING,
        created_at=now,
        updated_at=now,
        steps=[
            ProgressStep(name="reverse_search", label="Reverse Image Search"),
            ProgressStep(name="guidance", label="Generating Leads"),
        ],
    )
    manager.save(case)

    if is_video:
        # Extract keyframes in background so UI can show them for selection
        background_tasks.add_task(_extract_keyframes_task, case_id, str(dest), manager)
    else:
        background_tasks.add_task(_run_media_pipeline, case_id, manager)

    return case


def _extract_keyframes_task(case_id: str, video_path: str, manager: CaseManager):
    """Extract keyframes from uploaded video and store paths on the case."""
    from backend.modules.reverse_search import extract_keyframes
    from pathlib import Path as P
    case = manager.get(case_id)
    if not case:
        return
    video_stem = P(video_path).stem
    kf_dir = str(manager.case_dir(case_id) / "keyframes" / video_stem)
    frames = extract_keyframes(video_path, kf_dir, n_frames=8)
    # Store frame paths in media_investigation so frontend can display them
    case.media_investigation = {
        "keyframes": [
            {
                "index": i,
                "url": f"/api/cases/{case_id}/keyframes/{P(video_path).name}/{P(f).name}",
                "path": f,
            }
            for i, f in enumerate(frames)
        ],
        "video_path": video_path,
        "awaiting_frame_selection": True,
    }
    manager.save(case)


class FrameSelectRequest(BaseModel):
    frame_index: int


@router.post("/media-cases/{case_id}/select-frame")
async def select_frame(case_id: str, req: FrameSelectRequest,
                       background_tasks: BackgroundTasks):
    """User selected a keyframe — run the full analysis pipeline on it."""
    from backend.models import CaseStatus
    manager = get_manager()
    case = manager.get(case_id)
    if not case:
        raise HTTPException(404, "Case not found")

    inv = case.media_investigation or {}
    frames = inv.get("keyframes", [])
    if req.frame_index < 0 or req.frame_index >= len(frames):
        raise HTTPException(400, f"Invalid frame index {req.frame_index}")

    selected = frames[req.frame_index]
    # Store selected frame path so _run_media_pipeline can use it
    inv["selected_frame_path"] = selected["path"]
    inv["selected_frame_index"] = req.frame_index
    inv["awaiting_frame_selection"] = False
    case.media_investigation = inv
    case.status = CaseStatus.PENDING
    manager.save(case)

    background_tasks.add_task(_run_media_pipeline, case_id, manager)
    return {"started": True, "frame_index": req.frame_index}
