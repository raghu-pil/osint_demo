import os
import shutil
import threading
from pathlib import Path
from typing import List

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse

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
