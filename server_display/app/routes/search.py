"""Search and columns API endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.dependencies import get_db, get_client_ip, check_read_auth
from app.main import limiter
from app.utils import (
    build_match_query,
    load_columns_async,
    has_fts,
    load_search_vocab,
    load_field_values,
    reference_prefix,
    next_reference,
    SUGGEST_FIELDS,
)

router = APIRouter()


@router.get("/search")
@limiter.limit("60/minute")
async def search(
    request: Request,
    q: str = Query(""),
    sort: str = Query(""),
    dir: str = Query("asc"),
    limit: int = Query(200),
    offset: int = Query(0),
    db=Depends(get_db),
    client_ip: str | None = Depends(get_client_ip),
    _auth: bool = Depends(check_read_auth),
):
    """Search inventory with FTS5 or LIKE fallback."""
    from app.routes.metrics import log_request_async, log_search_async

    await log_request_async("/api/search", client_ip)

    columns = await load_columns_async(db)
    use_fts = await has_fts(db)

    order_dir = "DESC" if dir.lower() == "desc" else "ASC"
    order_sql = ""
    if sort and sort in columns:
        order_sql = f' ORDER BY "{sort}" COLLATE NOCASE {order_dir}'

    deleted_filter = "deleted_at IS NULL"

    if q.strip():
        # Log search query
        await log_search_async(q.strip())

        materials, shapes = await load_search_vocab(db)
        match = build_match_query(q.strip(), materials, shapes)
        if use_fts and match:
            base_sql = (
                " FROM inventory_fts "
                " JOIN inventory ON inventory_fts.rowid = inventory.rowid "
                f" WHERE inventory_fts MATCH ? AND inventory.{deleted_filter}"
            )
            async with db.execute(f"SELECT COUNT(1){base_sql}", (match,)) as cursor:
                total = (await cursor.fetchone())[0]

            sql = "SELECT inventory.rowid, inventory.*" + base_sql
            if order_sql:
                sql += order_sql
            else:
                # Weight matches: materiau & forme rank above reference/supplier/remarque.
                # Column order: materiau, reference, forme, emplacement, fournisseur, remarque.
                sql += " ORDER BY bm25(inventory_fts, 10.0, 2.0, 5.0, 3.0, 1.0, 1.0)"
            sql += " LIMIT ? OFFSET ?"

            async with db.execute(sql, (match, limit, offset)) as cursor:
                rows = await cursor.fetchall()
        else:
            # Fallback: LIKE on all text columns
            like = f"%{q.strip()}%"
            where = " OR ".join([f'"{c}" LIKE ?' for c in columns])

            async with db.execute(
                f"SELECT COUNT(1) FROM inventory WHERE ({where}) AND {deleted_filter}",
                [like] * len(columns),
            ) as cursor:
                total = (await cursor.fetchone())[0]

            sql = f"SELECT rowid, * FROM inventory WHERE ({where}) AND {deleted_filter}"
            sql += order_sql if order_sql else ""
            sql += " LIMIT ? OFFSET ?"

            async with db.execute(sql, [like] * len(columns) + [limit, offset]) as cursor:
                rows = await cursor.fetchall()
    else:
        async with db.execute(f"SELECT COUNT(1) FROM inventory WHERE {deleted_filter}") as cursor:
            total = (await cursor.fetchone())[0]

        sql = f"SELECT rowid, * FROM inventory WHERE {deleted_filter}"
        if order_sql:
            sql += order_sql
        sql += " LIMIT ? OFFSET ?"

        async with db.execute(sql, (limit, offset)) as cursor:
            rows = await cursor.fetchall()

    # Convert rows to dicts and attach linked documents (SDS by material, COA by reference)
    data = [dict(row) for row in rows]
    from app.routes.documents import attach_documents
    await attach_documents(db, data)

    # Global counts (target inventory: total + wear-state breakdown)
    async with db.execute(f"SELECT COUNT(1) FROM inventory WHERE {deleted_filter}") as cursor:
        total_all = (await cursor.fetchone())[0]

    async with db.execute(
        f"SELECT COUNT(1) FROM inventory WHERE {deleted_filter} AND usure = 'Neuve'"
    ) as cursor:
        count_neuve = (await cursor.fetchone())[0]

    async with db.execute(
        f"SELECT COUNT(1) FROM inventory WHERE {deleted_filter} AND usure = 'Finie'"
    ) as cursor:
        count_finie = (await cursor.fetchone())[0]

    return {
        "total": total,
        "returned": len(data),
        "total_all": total_all,
        "count_neuve": count_neuve,
        "count_finie": count_finie,
        "rows": data,
    }


@router.get("/columns")
@limiter.limit("60/minute")
async def get_columns(
    request: Request,
    db=Depends(get_db),
    client_ip: str | None = Depends(get_client_ip),
    _auth: bool = Depends(check_read_auth),
):
    """Get list of inventory columns."""
    from app.routes.metrics import log_request_async

    await log_request_async("/api/columns", client_ip)
    columns = await load_columns_async(db)
    return {"columns": columns}


@router.get("/suggestions")
@limiter.limit("60/minute")
async def get_suggestions(
    request: Request,
    db=Depends(get_db),
    _auth: bool = Depends(check_read_auth),
):
    """Distinct existing values for small-vocab fields, for <datalist> autocompletion."""
    return await load_field_values(db, SUGGEST_FIELDS)


@router.get("/next-reference")
@limiter.limit("60/minute")
async def get_next_reference(
    request: Request,
    prefix: str = Query(""),
    db=Depends(get_db),
    _auth: bool = Depends(check_read_auth),
):
    """Suggest the next reference for a family (max trailing number + 1, 3-digit padded).

    `prefix` may be a full reference (its trailing number is stripped) or a bare family prefix.
    """
    base = reference_prefix(prefix.strip())
    if not base:
        return {"next": "", "prefix": ""}
    async with db.execute(
        "SELECT reference FROM inventory WHERE reference LIKE ? AND deleted_at IS NULL",
        (base + "%",),
    ) as cursor:
        refs = [row[0] for row in await cursor.fetchall()]
    return {"next": next_reference(refs, base), "prefix": base}
