"""Serves the admin page (static files). Local-only, with strict browser security headers."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.admin.api import check_local
from app.config import settings

ROOT = Path(__file__).parent / "ui"
FILES = {"admin.js": "text/javascript", "admin.css": "text/css"}
HEADERS = {
    # Everything comes from this origin: no inline script or style, no framing, no external loads.
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                                "form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

router = APIRouter(include_in_schema=False)


def _serve(request: Request, name: str, media: str) -> FileResponse:
    check_local(request, settings.admin_allow_remote, settings.admin_allow_remote and settings.admin_require_https)
    return FileResponse(ROOT / name, media_type=media, headers=HEADERS)


@router.get("/admin")
@router.get("/admin/")
def page(request: Request):
    return _serve(request, "index.html", "text/html")


@router.get("/admin/static/{name}")
def static(name: str, request: Request):
    if name not in FILES:                                   # allowlist: no path traversal
        raise HTTPException(404)
    return _serve(request, name, FILES[name])
