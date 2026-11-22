"""File serving endpoints (documents are served from sds_root)."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.config import get_settings
from app.dependencies import get_client_ip, check_read_auth
from app.utils import safe_path

router = APIRouter()


@router.get("/sds/{path:path}")
async def serve_sds(
    path: str,
    client_ip: str | None = Depends(get_client_ip),
    _auth: bool = Depends(check_read_auth),
):
    """Serve SDS PDF files."""
    from app.routes.metrics import log_request_async, log_sds_access_async

    await log_request_async("/sds", client_ip)

    settings = get_settings()

    if not path:
        raise HTTPException(status_code=404, detail="File not found")

    # Extract CAS number from filename for metrics
    filename = Path(path).stem
    if filename.endswith("-SDS"):
        cas_number = filename[:-4]
        await log_sds_access_async(cas_number)

    try:
        target = safe_path(settings.sds_root, path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")

    ctype, _ = mimetypes.guess_type(str(target))
    if not ctype:
        ctype = "application/octet-stream"

    return FileResponse(
        path=target,
        media_type=ctype,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
