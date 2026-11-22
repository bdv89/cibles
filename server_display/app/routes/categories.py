"""Category stock-level API — aggregated view for restock thresholds.

A "category" is Matériau + Forme + Dimension (key "materiau|forme|dimension").
The dimension is derived from the per-item columns (Ø + diamètre for round targets,
"longueur×largeur" for rectangular). Available stock counts only targets that are
NOT worn out: a "Finie" target no longer counts toward the category stock.
Thresholds are stored in `category_thresholds`; the "below threshold" flag is never persisted.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.categories_core import aggregate_categories
from app.dependencies import get_db, get_client_ip, check_read_auth
from app.main import limiter

router = APIRouter()

# All non-deleted rows; aggregation into categories happens in Python so the derived
# dimension (which can collapse several raw column values into one label) stays the
# single source of the category key.
INVENTORY_SQL = (
    "SELECT materiau, forme, diametre, longueur, largeur, usure "
    "FROM inventory WHERE deleted_at IS NULL"
)

CREATE_THRESHOLDS = (
    "CREATE TABLE IF NOT EXISTS category_thresholds ("
    "category_key TEXT PRIMARY KEY, min_count INTEGER NOT NULL DEFAULT 0)"
)


@router.get("/categories")
@limiter.limit("60/minute")
async def list_categories(
    request: Request,
    db=Depends(get_db),
    _auth: bool = Depends(check_read_auth),
):
    """List categories (Matériau+Forme+Dimension) with available stock, threshold and below flag.

    Available stock excludes "Finie" targets: they no longer count once worn out.
    """
    await db.execute(CREATE_THRESHOLDS)

    thresholds: dict[str, int] = {}
    async with db.execute("SELECT category_key, min_count FROM category_thresholds") as cursor:
        for row in await cursor.fetchall():
            thresholds[row["category_key"]] = row["min_count"]

    async with db.execute(INVENTORY_SQL) as cursor:
        rows = await cursor.fetchall()

    return {"categories": aggregate_categories(rows, thresholds)}


@router.put("/categories/{category_key:path}")
@limiter.limit("10/minute")
async def set_threshold(
    category_key: str,
    request: Request,
    db=Depends(get_db),
    client_ip: str | None = Depends(get_client_ip),
):
    """Set (or clear) the minimum-stock threshold for a category. No auth; rate-limited."""
    await db.execute(CREATE_THRESHOLDS)

    try:
        data = await request.json()
    except Exception:
        return {"success": False, "error": "Invalid JSON"}

    min_count = data.get("min_count")

    # Empty / null clears the threshold.
    if min_count in (None, ""):
        await db.execute(
            "DELETE FROM category_thresholds WHERE category_key = ?", (category_key,)
        )
        await db.commit()
        return {"success": True, "category": category_key, "threshold": None}

    try:
        mc = int(min_count)
    except (ValueError, TypeError):
        return {"success": False, "error": "min_count must be an integer"}
    if mc < 0:
        return {"success": False, "error": "min_count must be >= 0"}

    await db.execute(
        "INSERT INTO category_thresholds (category_key, min_count) VALUES (?, ?) "
        "ON CONFLICT(category_key) DO UPDATE SET min_count = excluded.min_count",
        (category_key, mc),
    )
    await db.commit()
    return {"success": True, "category": category_key, "threshold": mc}
