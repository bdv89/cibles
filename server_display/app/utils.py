"""Utility functions for the FastAPI app."""
from __future__ import annotations

import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


def safe_path(base: Path, rel: str) -> Path:
    """Validate and resolve a relative path within a base directory.

    Raises ValueError if the path would escape the base directory.
    """
    rel = rel.lstrip("/\\")
    candidate = (base / rel).resolve()
    if not str(candidate).startswith(str(base.resolve())):
        raise ValueError("Unsafe path")
    return candidate


def validate_cas_checksum(cas: str) -> bool:
    """Validate CAS number checksum (last digit = sum(digit*pos) mod 10)."""
    clean = cas.replace("-", "")
    if not clean.isdigit() or len(clean) < 5:
        return False
    check_digit = int(clean[-1])
    total = sum(int(d) * (i + 1) for i, d in enumerate(reversed(clean[:-1])))
    return (total % 10) == check_digit


def parse_number(val: str | None) -> float:
    """Parse a number from a string, handling various formats."""
    if not val:
        return 0.0
    try:
        cleaned = re.sub(r"[^\d.,\-]", "", str(val))
        cleaned = cleaned.replace(",", ".")
        return float(cleaned) if cleaned else 0.0
    except (ValueError, TypeError):
        return 0.0


def _strip_accents(s: str) -> str:
    """Lowercase and drop accents for vocabulary matching (é -> e)."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower())
        if unicodedata.category(c) != "Mn"
    )


# Common shape abbreviations mapped to their canonical (accent-stripped) key.
SHAPE_ABBREV = {"circ": "circulaire", "rect": "rectangulaire", "cyl": "cylindrique"}


async def load_search_vocab(db: "aiosqlite.Connection") -> tuple[dict, dict]:
    """Load the controlled vocabularies (materials, shapes) from the DB.

    Returns (materials, shapes): dicts mapping accent-stripped-lowercase key -> canonical value.
    Cheap (two SELECT DISTINCT on a small table); loaded per request so new values are
    immediately routable.
    """
    materials: dict[str, str] = {}
    async with db.execute(
        "SELECT DISTINCT materiau FROM inventory WHERE materiau IS NOT NULL AND materiau != ''"
    ) as cursor:
        for row in await cursor.fetchall():
            v = (row[0] or "").strip()
            if v:
                materials[_strip_accents(v)] = v

    shapes: dict[str, str] = {}
    async with db.execute(
        "SELECT DISTINCT forme FROM inventory WHERE forme IS NOT NULL AND forme != ''"
    ) as cursor:
        for row in await cursor.fetchall():
            v = (row[0] or "").strip()
            if v:
                shapes[_strip_accents(v)] = v

    # Map abbreviations onto existing canonical shapes (e.g. "rect" -> "Rectangulaire").
    for abbr, full_key in SHAPE_ABBREV.items():
        if full_key in shapes:
            shapes[abbr] = shapes[full_key]

    return materials, shapes


def build_match_query(q: str, materials: dict | None = None, shapes: dict | None = None) -> str:
    """Build an FTS5 MATCH query from a search string.

    Smart routing: a token that matches a known material is scoped to `materiau:` and a
    known shape to `forme:` (both exact, no prefix) so e.g. "Ag rectangulaire" becomes
    `materiau:"Ag" AND forme:"Rectangulaire"` instead of a loose prefix bleeding across all
    columns. Unknown tokens keep the multi-column prefix behavior (`"token"*`).

    Materials take precedence if a token were ever in both vocabularies.
    """
    tokens = [t for t in re.split(r"\s+", q.strip()) if t]
    if not tokens:
        return ""

    materials = materials or {}
    shapes = shapes or {}

    parts = []
    for t in tokens:
        key = _strip_accents(t)
        if key in materials:
            canon = materials[key].replace('"', '""')
            parts.append(f'materiau:"{canon}"')
        elif key in shapes:
            canon = shapes[key].replace('"', '""')
            parts.append(f'forme:"{canon}"')
        else:
            safe = t.replace('"', '""')
            parts.append(f'"{safe}"*')
    return " AND ".join(parts)


# Fields worth suggesting from existing values (small controlled vocabularies).
SUGGEST_FIELDS = ["materiau", "forme", "fournisseur", "purete", "emplacement"]


async def load_field_values(db: "aiosqlite.Connection", fields: list[str]) -> dict:
    """Return distinct non-empty values per field, ordered by frequency (desc).

    Used to populate <datalist> autocompletion in the add/edit form. Field names come
    from a fixed allow-list (SUGGEST_FIELDS), never from user input.
    """
    result: dict[str, list[str]] = {}
    for f in fields:
        async with db.execute(
            f'SELECT "{f}" FROM inventory '
            f'WHERE "{f}" IS NOT NULL AND "{f}" != \'\' AND deleted_at IS NULL '
            f'GROUP BY "{f}" ORDER BY COUNT(*) DESC'
        ) as cursor:
            rows = await cursor.fetchall()
        result[f] = [row[0] for row in rows]
    return result


# Split a reference into its family prefix and trailing sequence number.
_REF_TAIL = re.compile(r"^(.*?)(\d+)$")


def reference_prefix(ref: str) -> str:
    """Return the family prefix of a reference (everything before the trailing number).

    "Ag-TM2-ABBE-005" -> "Ag-TM2-ABBE-".  "Ag-TM2-ABBE-" -> unchanged (no trailing digits).
    A reference with no trailing number (e.g. a supplier name) is returned as-is.
    """
    m = _REF_TAIL.match(ref or "")
    return m.group(1) if m else (ref or "")


def next_reference(refs, prefix: str) -> str:
    """Suggest the next reference for a family: prefix + (max existing suffix + 1), 3-digit padded.

    Only references matching exactly `prefix + digits` count. If none exist, start at 001.
    (98% of the data uses 3-digit zero-padded suffixes; max+1 has zero collisions across families.)
    """
    if not prefix:
        return ""
    pattern = re.compile(r"^" + re.escape(prefix) + r"(\d+)$")
    nums = []
    for r in refs:
        if not r:
            continue
        m = pattern.match(r)
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}{n:03d}"


def emplacement_from_reference(ref: str) -> str:
    """Derive the location code from a reference: the token immediately before the
    trailing number.

    "Ag-TM2-ABBE-005" -> "ABBE";  "Au-TM2-SUN-001" -> "SUN".
    AGC family collapses to "AGC" whatever the sub-code: "Ag-P.AGC_G_002" -> "AGC".
    A reference with no trailing number returns "".
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


def load_columns(conn) -> list[str]:
    """Load column names from inventory table."""
    if hasattr(conn, 'execute'):
        # Sync sqlite3 connection
        return [row[1] for row in conn.execute("PRAGMA table_info(inventory)")]
    raise TypeError("Expected sqlite3 connection")


async def load_columns_async(db: "aiosqlite.Connection") -> list[str]:
    """Load column names from inventory table (async)."""
    async with db.execute("PRAGMA table_info(inventory)") as cursor:
        rows = await cursor.fetchall()
    return [row[1] for row in rows]


async def has_fts(db: "aiosqlite.Connection") -> bool:
    """Check if FTS5 index exists."""
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='inventory_fts'"
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None


# Backup tracking
_backup_done_today: set[str] = set()


def ensure_daily_backup(db_path: Path, backup_dir: Path | None, backup_keep: int = 7) -> None:
    """Create a daily backup if not already done today."""
    if not backup_dir:
        return

    today = datetime.now().strftime("%Y%m%d")
    if today in _backup_done_today:
        return

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"inventory_{today}.sqlite"

    if not backup_path.exists():
        shutil.copy2(db_path, backup_path)
        print(f"Backup created: {backup_path}")

        # Cleanup old backups
        backups = sorted(backup_dir.glob("inventory_*.sqlite"), reverse=True)
        for old_backup in backups[backup_keep:]:
            old_backup.unlink()
            print(f"Deleted old backup: {old_backup}")

    _backup_done_today.add(today)


def extract_sds_info(pdf_path: Path) -> dict:
    """Extract CAS number and product name from SDS PDF (Section 1).

    Returns dict with keys: cas_number, product_name, raw_text (first 2 pages)
    """
    result = {"cas_number": None, "product_name": None, "raw_text": ""}

    try:
        import pdfplumber
    except ImportError:
        print("Warning: pdfplumber not available, using filename-based extraction only")
        return result

    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Extract text from first 2 pages (Section 1 is typically on page 1)
            text = ""
            for page in pdf.pages[:2]:
                page_text = page.extract_text() or ""
                text += page_text + "\n"

            result["raw_text"] = text

            # Extract CAS numbers - look for patterns like "CAS: 7732-18-5" or just "7732-18-5"
            cas_patterns = [
                r'(?:CAS|CAS-Nr|CAS\s*No\.?|CAS\s*Number)\s*[:\-]?\s*(\d{2,7}-\d{2}-\d)',
                r'\b(\d{2,7}-\d{2}-\d)\b',
            ]

            cas_candidates = []
            for pattern in cas_patterns:
                matches = re.findall(pattern, text, re.IGNORECASE)
                for match in matches:
                    if validate_cas_checksum(match):
                        cas_candidates.append(match)

            if cas_candidates:
                # Prefer CAS from explicit CAS label
                result["cas_number"] = cas_candidates[0]

            # Extract product name - look for common SDS Section 1 patterns
            name_patterns = [
                r'(?:Product\s*(?:name|identifier)|Nom\s*(?:du\s*)?produit|Trade\s*name|Substance)\s*[:\-]?\s*([^\n]+)',
                r'(?:1\.1[.\s]*|SECTION\s*1[:\s]*)[^\n]*\n\s*(?:Product\s*(?:name|identifier)[:\s]*)?([^\n]+)',
            ]

            for pattern in name_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    name = match.group(1).strip()
                    # Clean up common noise
                    name = re.sub(r'\s+', ' ', name)
                    if len(name) > 3 and len(name) < 200:
                        result["product_name"] = name
                        break

    except Exception as e:
        print(f"Error parsing SDS PDF: {e}")

    return result
