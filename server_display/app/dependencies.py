"""FastAPI dependencies for database and authentication."""
from __future__ import annotations

import base64
from typing import AsyncGenerator

import aiosqlite
from fastapi import Depends, Header, HTTPException, Request

from app.config import get_settings


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async database connection dependency."""
    settings = get_settings()
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        yield db


async def get_metrics_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async metrics database connection dependency."""
    settings = get_settings()
    async with aiosqlite.connect(settings.metrics_db) as db:
        db.row_factory = aiosqlite.Row
        yield db


def check_auth(authorization: str = Header(None)) -> bool:
    """Check HTTP Basic Auth credentials.

    Raises HTTPException 401/403 if authentication fails.
    Returns True if authentication succeeds.
    """
    settings = get_settings()

    if not settings.editing_enabled:
        raise HTTPException(status_code=403, detail="Editing disabled")

    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Authorization required",
            headers={"WWW-Authenticate": 'Basic realm="Inventory Edit"'},
        )

    try:
        credentials = base64.b64decode(authorization[6:]).decode("utf-8")
        _, password = credentials.split(":", 1)
        if password != settings.edit_password:
            raise HTTPException(
                status_code=401,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": 'Basic realm="Inventory Edit"'},
            )
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization header",
            headers={"WWW-Authenticate": 'Basic realm="Inventory Edit"'},
        )

    return True


def check_read_auth(authorization: str = Header(None)) -> bool:
    """Check read authentication (optional based on config).

    If read_password is empty, access is public.
    If read_password is set, Basic Auth is required.
    """
    settings = get_settings()

    # Public access if no read password configured
    if not settings.read_protected:
        return True

    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Authorization required",
            headers={"WWW-Authenticate": 'Basic realm="Inventory Read"'},
        )

    try:
        credentials = base64.b64decode(authorization[6:]).decode("utf-8")
        _, password = credentials.split(":", 1)
        if password != settings.read_password:
            raise HTTPException(
                status_code=401,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": 'Basic realm="Inventory Read"'},
            )
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(
            status_code=401,
            detail="Invalid authorization header",
            headers={"WWW-Authenticate": 'Basic realm="Inventory Read"'},
        )

    return True


async def get_client_ip(request: Request) -> str | None:
    """Extract client IP from request."""
    # Check X-Forwarded-For header first (for proxies)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    # Fall back to client host
    if request.client:
        return request.client.host
    return None
