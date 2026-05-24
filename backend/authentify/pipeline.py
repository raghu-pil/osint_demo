"""
Authentify deepfake detection pipeline.
Python port of authentify_api.sh — runs the full 8-step pipeline:
  1. Upload file to Veritas
  2. Metadata extraction + poll
  3. Inference (audio + video jobs)
  4. Create project with full file data
  5. Poll inference until COMPLETED
  6. Update file tampered status
  7. Request report generation
  8. Download PDF report

Job state is persisted to cases/{case_id}/authentify_job.json so the
frontend can poll for progress without holding a connection open.
"""
import json
import logging
import mimetypes
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from backend.config import config as _config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API endpoints (dev environment)
# ---------------------------------------------------------------------------
STORAGE_URL     = "https://platform-api.pi-labs.ai/veritas"
METADATA_URL    = "https://authentify-extract-api.pi-labs.ai/pi-extract/api/v1"
INFERENCE_URL   = "http://150.242.141.104:4096/inference/api/v1"
REPORT_API_URL  = "https://platform-api.pi-labs.ai/reports-d/api"
APP_URL         = "https://authentify-d.pi-labs.ai"

# Credentials re-read from config.yaml on each call — no restart needed after token refresh.
def _auth_token():        return _config.get("authentify", {}).get("auth_token", "")
def _owner_id():          return _config.get("authentify", {}).get("owner_id", "")
def _report_api_key():    return _config.get("authentify", {}).get("report_api_key", "")
def _inf_client_id():     return _config.get("authentify", {}).get("inference_client_id", "authentify-inference")
def _inf_client_secret(): return _config.get("authentify", {}).get("inference_client_secret", "")

POLL_INTERVAL   = 5   # seconds between polls
MAX_POLL        = 72  # max attempts (~6 minutes)

JOB_FILE = "authentify_job.json"
REPORT_FILE = "authentify_report.pdf"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif",  ".webp": "image/webp",
        ".mp4": "video/mp4",  ".mov": "video/quicktime", ".avi": "video/x-msvideo",
        ".webm": "video/webm",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",  ".aac": "audio/aac",
    }.get(ext, "application/octet-stream")


def _media_category(content_type: str) -> str:
    if content_type.startswith("image/"): return "image"
    if content_type.startswith("video/"): return "video"
    if content_type.startswith("audio/"): return "audio"
    return "unknown"


# ---------------------------------------------------------------------------
# Job state helpers
# ---------------------------------------------------------------------------

def _load_job(case_dir: Path) -> dict:
    p = case_dir / JOB_FILE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_job(case_dir: Path, job: dict):
    job["updated_at"] = _now()
    (case_dir / JOB_FILE).write_text(json.dumps(job, indent=2))


def _update(case_dir: Path, job: dict, **kwargs):
    job.update(kwargs)
    _save_job(case_dir, job)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def _upload(file_path: Path, content_type: str) -> dict:
    """Step 1: Upload to Veritas. Returns {file_id, hash}."""
    with open(file_path, "rb") as fh:
        r = requests.post(
            f"{STORAGE_URL}/v1/files",
            headers={"Authorization": f"Bearer {_auth_token()}"},
            files={"file": (file_path.name, fh, content_type)},
            data={"itemId": "temp", "sourceId": _owner_id(), "contentType": content_type},
            timeout=120,
        )
    r.raise_for_status()
    d = r.json().get("data", r.json())
    file_id = d.get("Fid") or d.get("id")
    if not file_id:
        raise ValueError(f"No file_id in upload response: {r.text[:200]}")
    return {"file_id": file_id, "hash": d.get("Sha256Hash", "")}


def _send_metadata(file_path: Path, file_id: str, content_type: str) -> str:
    """Step 2a: Send to metadata extractor. Returns extraction file_id."""
    fmt = content_type.split("/")[-1]
    with open(file_path, "rb") as fh:
        r = requests.post(
            f"{METADATA_URL}/analyze",
            headers={"Authorization": f"Bearer {_auth_token()}"},
            files={"file": (file_path.name, fh, content_type)},
            data={"file_id": file_id, "format_type": fmt,
                  "item_id": file_id, "source_id": _owner_id()},
            timeout=120,
        )
    r.raise_for_status()
    body = r.json()
    return body.get("data", {}).get("file_id") or body.get("file_id") or file_id


def _poll_metadata(meta_file_id: str) -> dict:
    """Step 2b: Poll until metadata extraction completes. Returns metadata dict."""
    url = f"{METADATA_URL}/status/{meta_file_id}"
    for _ in range(MAX_POLL):
        r = requests.get(url, headers={"Authorization": f"Bearer {_auth_token()}"}, timeout=30)
        if r.status_code == 200:
            body = r.json()
            status = body.get("status") or body.get("data", {}).get("status", "")
            if status.lower() in ("completed", "complete"):
                return body
            if status.lower() == "failed":
                raise RuntimeError(f"Metadata extraction failed: {body}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError("Metadata extraction timed out")


def _send_inference(file_path: Path, file_id: str, content_type: str,
                    category: str, has_audio: bool, has_video: bool) -> dict:
    """Step 3: Submit inference jobs. Returns {audio_id, video_id, versions}."""
    hdrs = {
        "X-Client-Id": _inf_client_id(),
        "X-Client-Secret": _inf_client_secret(),
    }
    result = {}

    def _post(endpoint: str, op: str) -> Optional[str]:
        with open(file_path, "rb") as fh:
            r = requests.post(
                f"{INFERENCE_URL}/{endpoint}",
                headers=hdrs,
                files={"file": (file_path.name, fh, content_type)},
                data={"op": op, "fileId": file_id, "sourceId": _owner_id(),
                      "itemId": file_id, "contentType": content_type},
                timeout=300,
            )
        if r.status_code in (200, 201):
            body = r.json()
            aid = body.get("id") or body.get("analysis_id")
            ver = body.get("model", {}).get("version", "unknown")
            return aid, ver
        logger.warning("Inference %s failed HTTP %s: %s", endpoint, r.status_code, r.text[:200])
        return None, "unknown"

    if category == "image":
        aid, ver = _post("image", "detect_fake,detect_language,detect_text")
        if aid:
            result["image_id"] = aid
            result["image_version"] = ver
    else:
        if has_audio:
            aid, ver = _post("audio", "detect_fake")
            if aid:
                result["audio_id"] = aid
                result["audio_version"] = ver
        if has_video:
            vid, ver = _post("video", "detect_fake")
            if vid:
                result["video_id"] = vid
                result["video_version"] = ver

    if not result:
        raise RuntimeError("No inference jobs were started")
    return result


def _create_project(case_name: str, file_id: str, filename: str,
                    content_type: str, file_size: int, file_hash: str,
                    audio_id: str, video_id: str, metadata_json: dict,
                    inferred_by: dict, duration: float) -> tuple[str, str]:
    """Step 4: Create project. Returns (project_id, db_file_id)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = {
        "name": case_name,
        "icon": "📁",
        "files": [{
            "source": file_id,
            "name": filename,
            "type": content_type,
            "size": file_size,
            "hash": file_hash,
            "audioId": audio_id or None,
            "videoId": video_id or None,
            "metadata": json.dumps(metadata_json),
            "inferedBy": json.dumps(inferred_by),
            "duration": duration,
        }],
        "attributes": json.dumps({
            "caseNumber": "",
            "title": case_name,
            "category": "assessment",
            "description": "",
            "files": [{"duration": duration}],
            "pairs": {},
            "reportedDate": ts,
            "reportedTime": ts,
            "reportedBy": "authintify",
            "selectedTitle": "",
            "selectedSource": None,
        }),
    }
    r = requests.post(
        f"{APP_URL}/api/projects",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_auth_token()}",
            "User-sub": _owner_id(),
        },
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    project_id = body.get("id")
    # The DB file id is returned nested under files[0].id
    db_file_id = None
    files = body.get("files") or []
    if files:
        db_file_id = files[0].get("id")
    if not project_id:
        raise ValueError(f"No project_id in response: {r.text[:200]}")
    return project_id, db_file_id or project_id


def _poll_inference(inference_ids: dict, job: dict, case_dir: Path) -> dict:
    """Step 5: Poll inference until all jobs complete. Returns result dict."""
    hdrs = {
        "X-Client-Id": _inf_client_id(),
        "X-Client-Secret": _inf_client_secret(),
    }
    pairs = {k.replace("_id", ""): v
             for k, v in inference_ids.items() if k.endswith("_id") and v}

    for attempt in range(MAX_POLL):
        all_done = True
        result = {"tampered": False, "fake_probability": None, "audio_decision": None}

        for media_type, analysis_id in pairs.items():
            r = requests.get(
                f"{INFERENCE_URL}/status?id={analysis_id}",
                headers=hdrs, timeout=30,
            )
            if r.status_code != 200:
                all_done = False
                continue

            body = r.json()
            op_status = (body.get("detect_fake", {}).get("op_status")
                         or body.get("status", "")).upper()

            if op_status in ("FAILED", "ERROR"):
                raise RuntimeError(f"{media_type} inference failed")

            if op_status not in ("COMPLETED", "COMPLETE", "SUCCESS"):
                all_done = False
                # Extract progress info and update job state for UI
                output = body.get("detect_fake", {}).get("output", {})
                chunks = output.get("chunks_processed") or output.get("frames_processed")
                total = output.get("total_chunks") or output.get("total_frames")
                _update(case_dir, job,
                        chunks_processed=chunks,
                        total_chunks=total,
                        inference_status=f"{media_type}: {op_status} (attempt {attempt+1})")
                continue

            # Completed — extract results
            output = body.get("detect_fake", {}).get("output", {})
            chunks = output.get("chunks_processed") or output.get("frames_processed")
            total  = output.get("total_chunks") or output.get("total_frames")
            _update(case_dir, job, chunks_processed=chunks, total_chunks=total)

            prob = output.get("fake_probability", 0)
            decision = output.get("decision", "")
            if media_type in ("video", "image"):
                result["fake_probability"] = prob
                if prob > 0.5:
                    result["tampered"] = True
            if media_type == "audio":
                result["audio_decision"] = decision
                if decision and decision.lower() in ("manipulated", "fake", "synthetic"):
                    result["tampered"] = True

        if all_done:
            return result
        time.sleep(POLL_INTERVAL)

    raise TimeoutError("Inference polling timed out")


def _update_file_status(project_id: str, db_file_id: str, tampered: bool, case_name: str,
                        source_file_id: str = "", filename: str = "", content_type: str = "",
                        file_size: int = 0, file_hash: str = "", audio_id: str = "",
                        video_id: str = "", metadata_json: dict = None,
                        inferred_by: dict = None, duration: float = 0):
    """Step 6: Mark file as tampered/authentic in project."""
    r = requests.put(
        f"{APP_URL}/api/projects/{project_id}",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_auth_token()}",
            "User-sub": _owner_id(),
        },
        json={
            "name": case_name,
            "icon": "📁",
            "files": [{
                "id": db_file_id,
                "source": source_file_id,
                "name": filename,
                "type": content_type,
                "size": file_size,
                "hash": file_hash,
                "audioId": audio_id or None,
                "videoId": video_id or None,
                "metadata": json.dumps(metadata_json or {}),
                "inferedBy": json.dumps(inferred_by or {}),
                "duration": duration,
                "tampered": tampered,
            }],
        },
        timeout=30,
    )
    r.raise_for_status()


def _request_report(record_id: str) -> str:
    """Step 7: Request report generation. Returns request_id."""
    r = requests.post(
        f"{REPORT_API_URL}/request",
        headers={"Content-Type": "application/json", "x-api-key": _report_api_key()},
        json={
            "recordId": record_id,
            "application": "authentify",
            "requestedBy": "authintify",
            "ownerId": _owner_id(),
            "options": {},
        },
        timeout=30,
    )
    r.raise_for_status()
    req_id = r.json().get("requestId") or r.json().get("id")
    if not req_id:
        raise ValueError(f"No requestId in report response: {r.text[:200]}")
    return req_id


def _poll_report(request_id: str, job: dict, case_dir: Path):
    """Step 7b: Poll until report generation completes."""
    for attempt in range(MAX_POLL):
        r = requests.get(
            f"{REPORT_API_URL}/status?requestId={request_id}",
            headers={"x-api-key": _report_api_key()},
            timeout=30,
        )
        if r.status_code == 200:
            status = r.json().get("status", "").upper()
            _update(case_dir, job, report_status=f"{status} (attempt {attempt+1})")
            if status in ("COMPLETED", "SUCCESS", "COMPLETE"):
                return
            if status in ("FAILED", "ERROR"):
                raise RuntimeError(f"Report generation failed: {r.text[:200]}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError("Report polling timed out")


def _download_report(request_id: str, out_path: Path):
    """Step 8: Download PDF to disk."""
    r = requests.get(
        f"{REPORT_API_URL}/download?requestId={request_id}",
        headers={"x-api-key": _report_api_key()},
        timeout=60,
        stream=True,
    )
    r.raise_for_status()
    with open(out_path, "wb") as fh:
        for chunk in r.iter_content(chunk_size=8192):
            fh.write(chunk)
    if out_path.stat().st_size < 1000:
        out_path.unlink(missing_ok=True)
        raise ValueError("Downloaded report is too small — likely an error response")


# ---------------------------------------------------------------------------
# Main pipeline orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(case_id: str, filename: str, cases_dir: str):
    """
    Run the full authentify pipeline in a background thread.
    Progress is written to cases/{case_id}/authentify_job.json.
    """
    case_dir = Path(cases_dir) / case_id
    file_path = case_dir / "media" / filename
    job = _load_job(case_dir)

    def _fail(msg: str):
        logger.error("Authentify pipeline failed: %s", msg)
        _update(case_dir, job, status="failed", error=msg, completed_at=_now())

    try:
        content_type = _content_type(filename)
        category = _media_category(content_type)
        file_size = file_path.stat().st_size
        has_audio = category in ("video", "audio")
        has_video = category == "video"

        # Step 1: Upload
        _update(case_dir, job, status="uploading",
                step=1, step_label="Uploading file to Veritas…")
        upload = _upload(file_path, content_type)
        file_id  = upload["file_id"]
        file_hash = upload["hash"]
        _update(case_dir, job, file_id=file_id, file_hash=file_hash)

        # Step 2: Metadata
        _update(case_dir, job, status="extracting_metadata",
                step=2, step_label="Extracting metadata…")
        meta_file_id = _send_metadata(file_path, file_id, content_type)
        metadata = _poll_metadata(meta_file_id)
        duration = (metadata.get("data", {}).get("duration")
                    or metadata.get("duration") or 0)
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = 0.0

        # Step 3: Inference
        _update(case_dir, job, status="running_inference",
                step=3, step_label="Submitting to deepfake detection models…")
        inf = _send_inference(file_path, file_id, content_type,
                              category, has_audio, has_video)
        audio_id = inf.get("audio_id", "")
        video_id = inf.get("video_id") or inf.get("image_id", "")
        inferred_by = {
            "server": INFERENCE_URL,
            "audioVersion": inf.get("audio_version", "unknown"),
            "videoVersion": inf.get("video_version", "unknown"),
            "imageVersion": inf.get("image_version", "unknown"),
        }

        # Step 4: Create project
        _update(case_dir, job, status="creating_project",
                step=4, step_label="Creating analysis project…")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        case_name = f"Case_{Path(filename).stem}_{ts}"
        project_id, db_file_id = _create_project(
            case_name, file_id, filename, content_type, file_size,
            file_hash, audio_id, video_id, metadata, inferred_by, duration,
        )
        _update(case_dir, job, project_id=project_id, db_file_id=db_file_id,
                case_name=case_name)

        # Step 5: Poll inference
        _update(case_dir, job, status="polling_inference",
                step=5, step_label="Running inference — analysing frames…")
        inf_result = _poll_inference(inf, job, case_dir)
        tampered   = inf_result["tampered"]
        fake_prob  = inf_result.get("fake_probability")
        audio_dec  = inf_result.get("audio_decision")
        _update(case_dir, job, tampered=tampered,
                fake_probability=fake_prob, audio_decision=audio_dec)

        # Step 6: Update status
        _update(case_dir, job, status="updating_status",
                step=6, step_label="Updating tamper status…")
        _update_file_status(
            project_id, db_file_id, tampered, case_name,
            source_file_id=file_id, filename=filename, content_type=content_type,
            file_size=file_size, file_hash=file_hash,
            audio_id=audio_id, video_id=video_id,
            metadata_json=metadata, inferred_by=inferred_by, duration=duration,
        )

        # Step 7: Request + poll report
        _update(case_dir, job, status="generating_report",
                step=7, step_label="Generating PDF report…")
        request_id = _request_report(db_file_id)
        _update(case_dir, job, request_id=request_id)
        _poll_report(request_id, job, case_dir)

        # Step 8: Download
        _update(case_dir, job, status="downloading_report",
                step=8, step_label="Downloading report…")
        report_path = case_dir / REPORT_FILE
        _download_report(request_id, report_path)

        _update(case_dir, job,
                status="completed",
                step=8,
                step_label="Done",
                report_filename=REPORT_FILE,
                completed_at=_now())
        logger.info("Authentify pipeline completed for case %s", case_id)

    except Exception as exc:
        _fail(str(exc))


def start_pipeline(case_id: str, filename: str, cases_dir: str) -> dict:
    """
    Initialise a job record and kick off the pipeline in a background thread.
    Returns the initial job state dict.
    """
    case_dir = Path(cases_dir) / case_id
    job = {
        "status": "pending",
        "step": 0,
        "step_label": "Starting…",
        "total_steps": 8,
        "filename": filename,
        "file_id": None,
        "file_hash": None,
        "project_id": None,
        "db_file_id": None,
        "request_id": None,
        "tampered": None,
        "fake_probability": None,
        "audio_decision": None,
        "chunks_processed": None,
        "total_chunks": None,
        "inference_status": None,
        "report_status": None,
        "report_filename": None,
        "error": None,
        "started_at": _now(),
        "completed_at": None,
        "updated_at": _now(),
    }
    _save_job(case_dir, job)

    t = threading.Thread(
        target=run_pipeline,
        args=(case_id, filename, cases_dir),
        daemon=True,
    )
    t.start()
    return job
