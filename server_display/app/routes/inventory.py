"""Inventory CRUD API endpoints."""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from app.dependencies import get_db, get_client_ip
from app.config import get_settings
from app.main import limiter
from app.utils import load_columns_async, ensure_daily_backup, emplacement_from_reference

router = APIRouter()

# Wear states, in advancing order. The "→" button moves to the next state.
USURE_STATES = ["Neuve", "Utilisée", "Finie"]


async def _log_edit(db, rowid: int, action: str, changes: dict, client_ip: str | None) -> None:
    """Log an edit action to the edit_log table."""
    await db.execute(
        "INSERT INTO edit_log (inventory_rowid, action, field_changes, user_ip) VALUES (?, ?, ?, ?)",
        (rowid, action, json.dumps(changes, ensure_ascii=False), client_ip),
    )


# FTS is kept in sync automatically by SQLite triggers (inv_ai/inv_au/inv_ad) — see
# the schema in scripts/import_cibles_xlsx.py. No manual FTS maintenance here.


@router.get("/edit-enabled")
async def edit_enabled():
    """Editing is open (no password required); always enabled."""
    return {"enabled": True}


@router.get("/inventory/{rowid}")
async def get_inventory_row(
    rowid: int,
    db=Depends(get_db),
):
    """Get a single inventory row by rowid."""
    async with db.execute(
        "SELECT rowid, * FROM inventory WHERE rowid = ? AND deleted_at IS NULL",
        (rowid,),
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Row not found")

    return {"row": dict(row)}


@router.post("/inventory")
@limiter.limit("10/minute")
async def create_inventory(
    request: Request,
    db=Depends(get_db),
    client_ip: str | None = Depends(get_client_ip),
):
    """Create a new inventory row (no auth; rate-limited)."""
    settings = get_settings()

    # Ensure daily backup
    ensure_daily_backup(settings.db_path, settings.backup_dir, settings.backup_keep)

    try:
        data = await request.json()
    except Exception:
        return {"success": False, "error": "Invalid JSON"}

    # Reject a duplicate reference (keeps references unique; near-perfect today).
    ref = (data.get("reference") or "").strip()
    if ref:
        async with db.execute(
            "SELECT rowid FROM inventory WHERE reference = ? AND deleted_at IS NULL",
            (ref,),
        ) as cursor:
            if await cursor.fetchone():
                return {"success": False, "error": f"Référence déjà utilisée : {ref}"}

    # Auto-derive emplacement from the reference (unless one was explicitly provided).
    if ref and not (data.get("emplacement") or "").strip():
        data["emplacement"] = emplacement_from_reference(ref)

    columns = await load_columns_async(db)
    editable_cols = [c for c in columns if c not in ("version", "deleted_at")]
    insert_cols = [c for c in data.keys() if c in editable_cols]

    if not insert_cols:
        return {"success": False, "error": "No valid fields"}

    col_list = ", ".join(f'"{c}"' for c in insert_cols)
    placeholders = ", ".join(["?"] * len(insert_cols))
    values = [data.get(c, "") for c in insert_cols]

    cursor = await db.execute(
        f"INSERT INTO inventory ({col_list}, version) VALUES ({placeholders}, 1)",
        values,
    )
    new_rowid = cursor.lastrowid

    # Log the creation
    await _log_edit(db, new_rowid, "create", data, client_ip)

    await db.commit()

    return {"success": True, "rowid": new_rowid}


@router.put("/inventory/{rowid}")
@limiter.limit("10/minute")
async def update_inventory(
    rowid: int,
    request: Request,
    db=Depends(get_db),
    client_ip: str | None = Depends(get_client_ip),
):
    """Update an inventory row (no auth; rate-limited)."""
    settings = get_settings()

    # Ensure daily backup
    ensure_daily_backup(settings.db_path, settings.backup_dir, settings.backup_keep)

    try:
        data = await request.json()
    except Exception:
        return {"success": False, "error": "Invalid JSON"}

    expected_version = data.pop("_version", None)

    # Check row exists and version matches
    async with db.execute(
        "SELECT rowid, * FROM inventory WHERE rowid = ? AND deleted_at IS NULL",
        (rowid,),
    ) as cursor:
        current = await cursor.fetchone()

    if not current:
        return {"success": False, "error": "Row not found"}

    current_dict = dict(current)
    current_version = current_dict.get("version") or 1

    if expected_version is not None and current_version != expected_version:
        return {
            "success": False,
            "error": "Version conflict - row was modified",
            "current_version": current_version,
        }

    # If the reference changed and no explicit emplacement was sent, re-derive it.
    new_ref = (data.get("reference") or "").strip()
    if "reference" in data and new_ref and not (data.get("emplacement") or "").strip():
        data["emplacement"] = emplacement_from_reference(new_ref)

    # Filter to valid editable columns
    columns = await load_columns_async(db)
    editable_cols = [c for c in columns if c not in ("version", "deleted_at")]
    update_cols = [c for c in data.keys() if c in editable_cols]

    if not update_cols:
        return {"success": False, "error": "No valid fields to update"}

    # Build changes dict (old -> new)
    changes = {}
    for col in update_cols:
        old_val = current_dict.get(col)
        new_val = data[col]
        if old_val != new_val:
            changes[col] = [old_val, new_val]

    if not changes:
        return {"success": True, "message": "No changes", "rowid": rowid}

    # Build UPDATE statement
    set_clause = ", ".join(f'"{c}" = ?' for c in changes.keys())
    values = [data[c] for c in changes.keys()]
    values.append(rowid)

    await db.execute(
        f"UPDATE inventory SET {set_clause}, version = version + 1 WHERE rowid = ?",
        values,
    )

    # Log the update
    await _log_edit(db, rowid, "update", changes, client_ip)

    await db.commit()

    return {
        "success": True,
        "rowid": rowid,
        "updated_fields": list(changes.keys()),
        "new_version": current_version + 1,
    }


@router.post("/inventory/{rowid}/advance-usure")
@limiter.limit("10/minute")
async def advance_usure(
    rowid: int,
    request: Request,
    db=Depends(get_db),
    client_ip: str | None = Depends(get_client_ip),
):
    """Cycle the wear state Neuve → Utilisée → Finie → Neuve → … No auth; rate-limited."""
    settings = get_settings()
    ensure_daily_backup(settings.db_path, settings.backup_dir, settings.backup_keep)

    async with db.execute(
        "SELECT usure, version FROM inventory WHERE rowid = ? AND deleted_at IS NULL",
        (rowid,),
    ) as cursor:
        current = await cursor.fetchone()

    if not current:
        return {"success": False, "error": "Row not found"}

    old_state = (current["usure"] or "Neuve").strip()
    try:
        idx = USURE_STATES.index(old_state)
    except ValueError:
        idx = -1  # unknown value cycles into Neuve first
    new_state = USURE_STATES[(idx + 1) % len(USURE_STATES)]

    await db.execute(
        "UPDATE inventory SET usure = ?, version = version + 1 WHERE rowid = ?",
        (new_state, rowid),
    )
    await _log_edit(db, rowid, "advance-usure", {"usure": [old_state, new_state]}, client_ip)
    await db.commit()

    return {"success": True, "rowid": rowid, "usure": new_state}


@router.delete("/inventory/{rowid}")
@limiter.limit("10/minute")
async def delete_inventory(
    rowid: int,
    request: Request,
    db=Depends(get_db),
    client_ip: str | None = Depends(get_client_ip),
):
    """Soft delete an inventory row (no auth; rate-limited)."""
    settings = get_settings()

    # Ensure daily backup
    ensure_daily_backup(settings.db_path, settings.backup_dir, settings.backup_keep)

    # Check row exists
    async with db.execute(
        "SELECT rowid FROM inventory WHERE rowid = ? AND deleted_at IS NULL",
        (rowid,),
    ) as cursor:
        current = await cursor.fetchone()

    if not current:
        return {"success": False, "error": "Row not found"}

    # Soft delete
    now = datetime.now().isoformat()
    await db.execute(
        "UPDATE inventory SET deleted_at = ? WHERE rowid = ?",
        (now, rowid),
    )

    # Log the deletion
    await _log_edit(db, rowid, "delete", {"deleted_at": now}, client_ip)

    # FTS stays in sync via the AFTER UPDATE trigger; search filters deleted_at IS NULL.
    await db.commit()

    return {"success": True, "rowid": rowid}
