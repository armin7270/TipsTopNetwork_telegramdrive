from __future__ import annotations

from pathlib import Path
from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["emulator"])
_BASE_DIR = Path(__file__).resolve().parent
_INDEX_HTML_PATH = _BASE_DIR / "index.html"
_MANIFEST_PATH = _BASE_DIR / "manifest.json"
_SW_PATH = _BASE_DIR / "sw.js"
_ICONS_DIR = _BASE_DIR / "icons"


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/emulator", response_class=HTMLResponse, tags=["emulator"], summary="TeleDrive Web & Mobile PWA")
async def get_emulator() -> HTMLResponse:
    """Serve the interactive Telegram Mini App and PWA interface."""
    if _INDEX_HTML_PATH.exists():
        content = _INDEX_HTML_PATH.read_text(encoding="utf-8")
        return HTMLResponse(content=content)
    return HTMLResponse("<h1>TeleDrive Emulator not found</h1>", status_code=404)


@router.get("/manifest.json", include_in_schema=False)
async def get_manifest() -> Response:
    if _MANIFEST_PATH.exists():
        return Response(content=_MANIFEST_PATH.read_bytes(), media_type="application/manifest+json")
    return Response("Not found", status_code=404)


@router.get("/sw.js", include_in_schema=False)
async def get_service_worker() -> Response:
    if _SW_PATH.exists():
        return Response(content=_SW_PATH.read_bytes(), media_type="application/javascript")
    return Response("Not found", status_code=404)


@router.get("/icons/{icon_name}", include_in_schema=False)
async def get_icon(icon_name: str) -> Response:
    icon_path = _ICONS_DIR / icon_name
    if icon_path.exists() and icon_path.is_file():
        return Response(content=icon_path.read_bytes(), media_type="image/png")
    return Response("Not found", status_code=404)
