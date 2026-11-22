#!/usr/bin/env python3
"""Import 'Listing toutes cibles.xlsx' into SQLite with FTS5 for the cibles inventory.

One-shot destructive load: drops and rebuilds inventory / inventory_fts / edit_log,
then (re)creates the category_thresholds table. The xlsx is the source of truth only
for the initial load; afterwards edits happen in the web app.

Columns are mapped by POSITION (1-17) because the xlsx headers are cp1252 mojibake
when read via openpyxl. Junk columns (18+) are consolidated into `remarque`.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime
from pathlib import Path

# Internal column names, in the exact order of the first 17 xlsx columns.
INVENTORY_COLUMNS = [
    "materiau",       # 1  Matériau
    "reference",      # 2  Référence
    "date_achat",     # 3  Date
    "prix",           # 4  Prix
    "purete",         # 5  Pureté
    "equipement",     # 6  Equipement
    "usure",          # 7  Usure          (normalized)
    "particularite",  # 8  Particularité
    "fournisseur",    # 9  Fournisseur
    "hauteur_mm",     # 10 Hauteur (mm)
    "forme",          # 11 Forme          (normalized)
    "longueur",       # 12 Longueur
    "largeur",        # 13 Largeur
    "hauteur",        # 14 Hauteur
    "diametre",       # 15 Diamètre
    "epaisseur",      # 16 Epaisseur
    "bon_commande",   # 17 N°Bon de commande
    "emplacement",    #    (new, empty, indexed for search)
    "remarque",       #    (new, consolidates junk-column notes)
]

FTS_COLUMNS = ["materiau", "reference", "forme", "emplacement", "fournisseur", "remarque"]

# Number of real xlsx columns mapped by position; the rest fold into `remarque`.
N_REAL_COLS = 17

USURE_STATES = ("Neuve", "Utilisée", "Finie")


def cell_str(v) -> str:
    """Stringify an openpyxl cell value cleanly."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def normalize_usure(raw: str) -> tuple[str, str | None]:
    """Return (normalized_state, preserved_raw_or_None).

    Neuve/Utilisée/Finie recognized by prefix (accent/case/space tolerant).
    Anything else defaults to 'Neuve' and preserves the original value so nothing is lost.
    Empty stays 'Neuve' with no note.
    """
    s = (raw or "").strip()
    low = s.lower()
    if not low:
        return "Neuve", None
    if low.startswith("neuv"):
        return "Neuve", None
    if low.startswith("utilis"):
        return "Utilisée", None
    if low.startswith("fini"):
        return "Finie", None
    # Unexpected value (0, 150, Infime, ...): default + preserve raw
    return "Neuve", s


def normalize_forme(raw: str) -> str:
    """Normalize casing of the known shape vocabulary; keep unknowns verbatim."""
    s = (raw or "").strip()
    low = s.lower()
    if not low:
        return ""
    if low.startswith("circ"):
        return "Circulaire"
    if low.startswith("rect"):
        return "Rectangulaire"
    if low.startswith("cyl"):
        return "Cylindrique"
    return s


_REF_TAIL = re.compile(r"^(.*?)(\d+)$")


def emplacement_from_reference(ref: str) -> str:
    """Location code = the token immediately before the trailing number of the reference.

    "Ag-TM2-ABBE-005" -> "ABBE".  AGC family collapses to "AGC": "Ag-P.AGC_G_002" -> "AGC".
    No trailing number -> "".
    (Kept in sync with app.utils.emplacement_from_reference.)
    """
    m = _REF_TAIL.match(ref or "")
    if not m:
        return ""
    prefix = m.group(1).rstrip("-_. ")
    if not prefix:
        return ""
    tokens = re.split(r"[-_. ]+", prefix)
    if any(t.upper() == "AGC" for t in tokens):
        return "AGC"
    return tokens[-1] if tokens and tokens[-1] else ""


def create_fts_triggers(conn: sqlite3.Connection) -> None:
    """Triggers that keep the external-content FTS index in sync on every write.

    This is the canonical FTS5 external-content pattern (using the special 'delete'
    command with OLD.* values), which — unlike a plain DELETE by rowid — never corrupts
    the index. Column names in FTS_COLUMNS are plain identifiers, so no quoting needed.
    """
    cols = ", ".join(FTS_COLUMNS)
    newv = ", ".join(f"new.{c}" for c in FTS_COLUMNS)
    oldv = ", ".join(f"old.{c}" for c in FTS_COLUMNS)
    for name in ("inventory_ai", "inventory_au", "inventory_ad"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute(
        f"CREATE TRIGGER inventory_ai AFTER INSERT ON inventory BEGIN "
        f"INSERT INTO inventory_fts(rowid, {cols}) VALUES (new.rowid, {newv}); END"
    )
    conn.execute(
        f"CREATE TRIGGER inventory_ad AFTER DELETE ON inventory BEGIN "
        f"INSERT INTO inventory_fts(inventory_fts, rowid, {cols}) "
        f"VALUES ('delete', old.rowid, {oldv}); END"
    )
    conn.execute(
        f"CREATE TRIGGER inventory_au AFTER UPDATE ON inventory BEGIN "
        f"INSERT INTO inventory_fts(inventory_fts, rowid, {cols}) "
        f"VALUES ('delete', old.rowid, {oldv}); "
        f"INSERT INTO inventory_fts(rowid, {cols}) VALUES (new.rowid, {newv}); END"
    )


def create_schema(conn: sqlite3.Connection) -> None:
    col_defs = ", ".join(f'"{c}" TEXT' for c in INVENTORY_COLUMNS)
    conn.execute("DROP TABLE IF EXISTS inventory")
    conn.execute(
        f"CREATE TABLE inventory ({col_defs}, version INTEGER DEFAULT 1, deleted_at TEXT)"
    )
    conn.execute("CREATE INDEX idx_emplacement ON inventory(emplacement)")

    conn.execute("DROP TABLE IF EXISTS inventory_fts")
    fts_cols = ", ".join(f'"{c}"' for c in FTS_COLUMNS)
    conn.execute(
        "CREATE VIRTUAL TABLE inventory_fts USING fts5("
        f"{fts_cols}, content='inventory', content_rowid='rowid')"
    )
    create_fts_triggers(conn)

    conn.execute("DROP TABLE IF EXISTS edit_log")
    conn.execute(
        """
        CREATE TABLE edit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_rowid INTEGER NOT NULL,
            action TEXT NOT NULL,
            field_changes TEXT,
            user_ip TEXT,
            timestamp TEXT DEFAULT (datetime('now'))
        )
        """
    )

    # Stock thresholds per category ("materiau|forme|dimension", cf. app.categories_core._key);
    # empty by default. A stale 2-segment key matches no category and is silently ignored.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS category_thresholds (
            category_key TEXT PRIMARY KEY,
            min_count    INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def build_rows(ws) -> list[list[str]]:
    rows: list[list[str]] = []
    for i, raw_row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue  # header (mojibake, ignored — we map by position)
        cells = list(raw_row)
        # Pad short rows
        while len(cells) < N_REAL_COLS:
            cells.append(None)

        real = [cell_str(cells[j]) for j in range(N_REAL_COLS)]

        # Consolidate every non-empty junk cell (index >= 17) into remarque.
        notes = [cell_str(c) for c in cells[N_REAL_COLS:]]
        notes = [n for n in notes if n]

        usure, usure_raw = normalize_usure(real[6])
        real[6] = usure
        if usure_raw:
            notes.append(f"usure source: {usure_raw}")

        real[10] = normalize_forme(real[10])

        # Skip fully-empty rows (no material and no reference)
        if not real[0] and not real[1]:
            continue

        emplacement = emplacement_from_reference(real[1])
        remarque = " | ".join(notes)
        rows.append(real + [emplacement, remarque])
    return rows


def insert_rows(conn: sqlite3.Connection, rows: list[list[str]]) -> None:
    placeholders = ", ".join(["?"] * len(INVENTORY_COLUMNS))
    col_list = ", ".join(f'"{c}"' for c in INVENTORY_COLUMNS)
    # The AFTER INSERT trigger populates inventory_fts for each row.
    conn.executemany(
        f"INSERT INTO inventory ({col_list}) VALUES ({placeholders})", rows
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Import cibles xlsx into SQLite")
    parser.add_argument("--xlsx", default="Listing toutes cibles.xlsx")
    parser.add_argument("--sheet", default="test")
    parser.add_argument("--db", default="inventory.sqlite")
    args = parser.parse_args()

    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        raise SystemExit(f"XLSX not found: {xlsx_path}")

    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    if args.sheet not in wb.sheetnames:
        raise SystemExit(f"Sheet '{args.sheet}' not found. Sheets: {wb.sheetnames}")
    ws = wb[args.sheet]
    rows = build_rows(ws)

    db_path = Path(args.db)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        create_schema(conn)
        insert_rows(conn, rows)
        conn.commit()
    finally:
        conn.close()

    print(f"OK: {db_path} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
