"""
Authentify API routes.

POST /api/cases/{case_id}/authentify          — start pipeline
GET  /api/cases/{case_id}/authentify/status   — poll job progress
GET  /api/cases/{case_id}/authentify/report   — serve PDF when done
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from backend.config import config
from backend.authentify.pipeline import (
    JOB_FILE, REPORT_FILE, _load_job, start_pipeline,
)

router = APIRouter()

_CASES_DIR = str(
    Path(__file__).parent.parent.parent
    / config.get("storage", {}).get("cases_dir", "cases")
)


class StartRequest(BaseModel):
    filename: str


@router.post("/cases/{case_id}/authentify")
def start_authentify(case_id: str, req: StartRequest):
    case_dir = Path(_CASES_DIR) / case_id
    if not case_dir.exists():
        raise HTTPException(404, "Case not found")

    file_path = case_dir / "media" / req.filename
    if not file_path.exists():
        raise HTTPException(404, f"Media file not found: {req.filename}")

    # If a job is already running or completed, return its existing state
    existing = _load_job(case_dir)
    if existing.get("status") in ("pending", "uploading", "extracting_metadata",
                                   "running_inference", "creating_project",
                                   "polling_inference", "updating_status",
                                   "generating_report", "downloading_report",
                                   "completed"):
        return JSONResponse(existing)

    job = start_pipeline(case_id, req.filename, _CASES_DIR)
    return JSONResponse(job)


@router.get("/cases/{case_id}/authentify/status")
def get_authentify_status(case_id: str):
    case_dir = Path(_CASES_DIR) / case_id
    if not case_dir.exists():
        raise HTTPException(404, "Case not found")

    job = _load_job(case_dir)
    if not job:
        raise HTTPException(404, "No authentify job found for this case")
    return JSONResponse(job)


@router.get("/cases/{case_id}/authentify/report")
def get_authentify_report(case_id: str):
    case_dir = Path(_CASES_DIR) / case_id
    report_path = case_dir / REPORT_FILE
    if not report_path.exists():
        raise HTTPException(404, "Report not ready yet")
    content = report_path.read_bytes()
    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )
