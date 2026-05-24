import logging
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# Ensure project root is on path so `osint` package is importable
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.routes.cases import router as cases_router
from backend.authentify.routes import router as authentify_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Quiet noisy libs
for _noisy in ("urllib3.connectionpool", "urllib3.util.retry"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

app = FastAPI(
    title="OSINT Investigation Tool",
    description="Forensic OSINT tool for social media content analysis",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(cases_router, prefix="/api")
app.include_router(authentify_router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


# Serve frontend static assets (logo, etc.)
FRONTEND = ROOT / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    p = FRONTEND / "index.html"
    if p.exists():
        return HTMLResponse(p.read_text(), headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        })
    return HTMLResponse("<h1>OSINT Tool — frontend not found</h1>", status_code=500)


if __name__ == "__main__":
    import uvicorn
    from backend.config import config
    host = config.get("server", {}).get("host", "0.0.0.0")
    port = config.get("server", {}).get("port", 8000)
    uvicorn.run("backend.main:app", host=host, port=port, reload=False)
