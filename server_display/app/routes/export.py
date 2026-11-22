"""Export API endpoint."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse, JSONResponse

from app.dependencies import get_db, get_client_ip, check_read_auth
from app.main import limiter
from app.utils import build_match_query, load_columns_async, has_fts, load_search_vocab

router = APIRouter()


@router.get("/export")
@limiter.limit("10/minute")
async def export_products(
    request: Request,
    q: str = Query(""),
    format: str = Query("txt"),
    db=Depends(get_db),
    client_ip: str | None = Depends(get_client_ip),
    _auth: bool = Depends(check_read_auth),
):
    """Export product names for LLM presentation.

    Returns plain text (txt) or JSON, with one product per line/entry.
    """
    from app.routes.metrics import log_request_async

    await log_request_async("/api/export", client_ip)

    columns = await load_columns_async(db)
    use_fts = await has_fts(db)
    deleted_filter = "deleted_at IS NULL"

    if q.strip():
        materials, shapes = await load_search_vocab(db)
        match = build_match_query(q.strip(), materials, shapes)
        if use_fts and match:
            sql = (
                "SELECT inventory.reference, inventory.materiau, inventory.forme "
                "FROM inventory_fts "
                "JOIN inventory ON inventory_fts.rowid = inventory.rowid "
                f"WHERE inventory_fts MATCH ? AND inventory.{deleted_filter} "
                "ORDER BY inventory.reference COLLATE NOCASE"
            )
            async with db.execute(sql, (match,)) as cursor:
                rows = await cursor.fetchall()
        else:
            like = f"%{q.strip()}%"
            where = " OR ".join([f'"{c}" LIKE ?' for c in columns])
            sql = (
                "SELECT reference, materiau, forme "
                f"FROM inventory WHERE ({where}) AND {deleted_filter} "
                "ORDER BY reference COLLATE NOCASE"
            )
            async with db.execute(sql, [like] * len(columns)) as cursor:
                rows = await cursor.fetchall()
    else:
        sql = (
            "SELECT reference, materiau, forme "
            f"FROM inventory WHERE {deleted_filter} "
            "ORDER BY reference COLLATE NOCASE"
        )
        async with db.execute(sql) as cursor:
            rows = await cursor.fetchall()

    # Build output
    lines = []
    for row in rows:
        reference = row["reference"] or ""
        materiau = row["materiau"] or ""
        forme = row["forme"] or ""

        name = reference or materiau
        if not name.strip():
            continue

        if format.lower() == "json":
            lines.append({
                "name": name,
                "reference": reference,
                "materiau": materiau,
                "forme": forme,
            })
        else:
            # txt format: clean, one name per line
            lines.append(name.strip())

    if format.lower() == "json":
        return JSONResponse(
            content={"products": lines, "count": len(lines)},
            headers={
                "Content-Disposition": 'attachment; filename="export_produits.json"',
            },
        )
    else:
        content = "\n".join(lines)
        return PlainTextResponse(
            content=content,
            headers={
                "Content-Disposition": 'attachment; filename="export_produits.txt"',
            },
        )
