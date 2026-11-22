#!/usr/bin/env python3
"""Auto-link SDS (by material) and COA certificates (by reference) to the inventory.

- SDS are per-substance -> table `material_sds(materiau, path, source)`, one row per (material, file).
  Alloys with no dedicated SDS get a per-element fallback (source='auto_element').
- COA are per-lot/item -> table `coa_link(reference, path, source)`. Auto links only when a
  filename contains an exact inventory reference (source='auto_ref') or an expanded range
  (source='auto_range'). Everything else stays for manual assignment in the UI.

Idempotent: rebuilds all 'auto*' rows from disk, PRESERVES 'manual' rows.
Paths are stored relative to server_display/ so the existing /sds/{path} route can serve them.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import unicodedata
from pathlib import Path

# --- Vocabulary ------------------------------------------------------------

ELEMENTS = {
    "H", "Li", "Be", "B", "C", "N", "O", "F", "Na", "Mg", "Al", "Si", "P", "S",
    "Cl", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Ga", "Ge", "As", "Se", "Sr", "Y", "Zr", "Nb", "Mo", "Ru", "Rh", "Pd", "Ag",
    "Cd", "In", "Sn", "Sb", "Te", "Ba", "La", "Ce", "Pr", "Nd", "Sm", "Eu", "Gd",
    "Tb", "Dy", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Pb", "Bi",
    "Th", "U", "SST",  # SST = stainless steel pseudo-symbol
}

ALIAS = {
    "silver": "Ag", "argent": "Ag", "gold": "Au", "copper": "Cu", "cuivre": "Cu",
    "kupfer": "Cu", "aluminium": "Al", "aluminum": "Al", "chrome": "Cr",
    "chromium": "Cr", "molybdenum": "Mo", "molybdene": "Mo", "molybden": "Mo",
    "zirconium": "Zr", "titanium": "Ti", "titane": "Ti", "tungsten": "W",
    "tungstene": "W", "wolfram": "W", "niobium": "Nb", "nickel": "Ni",
    "tantalum": "Ta", "tantale": "Ta", "magnesium": "Mg", "zinc": "Zn",
    "platinum": "Pt", "platine": "Pt", "palladium": "Pd", "carbon": "C",
    "carbone": "C", "graphite": "C", "cobalt": "Co", "indium": "In",
    "vanadium": "V", "yttrium": "Y", "yittrium": "Y", "antimony": "Sb",
    "antimoine": "Sb", "cadmium": "Cd", "hafnium": "Hf", "iron": "Fe", "fer": "Fe",
    "lithium": "Li", "ruthenium": "Ru", "cerium": "Ce", "gadolinium": "Gd",
    "silicon": "Si", "silicium": "Si", "boron": "B", "bore": "B",
    "germanium": "Ge", "europium": "Eu", "tin": "Sn", "etain": "Sn",
    "manganese": "Mn", "lanthanum": "La", "lead": "Pb", "plomb": "Pb",
    "bismuth": "Bi", "selenium": "Se", "tellurium": "Te", "tellure": "Te",
    "rhenium": "Re", "gallium": "Ga", "arsenic": "As", "barium": "Ba",
    "strontium": "Sr", "calcium": "Ca", "inox": "SST",
    "stainless": "SST", "sst": "SST",
}

COMPOUND = {
    "tib2": ["Ti", "B"], "ws2": ["W", "S"], "mos2": ["Mo", "S"], "tic": ["Ti", "C"],
    "linbo3": ["Li", "Nb", "O"], "kmno4": ["K", "Mn", "O"], "yb6": ["Y", "B"],
    "lab6": ["La", "B"], "ceb6": ["Ce", "B"], "tzm": ["Ti", "Zr", "Mo"],
    "li3po4": ["Li", "P", "O"], "licoo2": ["Li", "Co", "O"],
    "li4ti5o12": ["Li", "Ti", "O"], "mo2c": ["Mo", "C"], "ceo2gd2o3": ["Ce", "O", "Gd"],
    "cegd": ["Ce", "Gd"], "ti6al4v": ["Ti", "Al", "V"], "azo": ["Al", "Zn", "O"],
    "alcrfemnmo": ["Al", "Cr", "Fe", "Mn", "Mo"], "nicr": ["Ni", "Cr"],
    "inox": ["SST"], "stainless": ["SST"],
}


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def decompose(tok: str):
    """Greedily split a lowercase token into element symbols. Returns (elements, leftover_bool)."""
    els = set()
    i = 0
    leftover = False
    while i < len(tok):
        two = tok[i:i + 2].capitalize()
        one = tok[i:i + 1].upper()
        if len(tok[i:i + 2]) == 2 and two in ELEMENTS:
            els.add(two); i += 2
        elif one in ELEMENTS:
            els.add(one); i += 1
        elif tok[i].isdigit():
            i += 1
        else:
            leftover = True; i += 1
    return els, leftover


def material_elements(mat: str):
    """Element set of an inventory material value, or None if it looks like prose/junk."""
    low = strip_accents(mat).strip().lower()
    if not low:
        return None
    if low in COMPOUND:
        return set(COMPOUND[low])
    els = set()
    for tok in re.split(r"[^a-z0-9]+", low):
        if not tok or tok.isdigit():
            continue
        if tok in ALIAS:
            els.add(ALIAS[tok]); continue
        if tok in COMPOUND:
            els.update(COMPOUND[tok]); continue
        got, leftover = decompose(tok)
        if leftover or not got:  # prose / unknown word -> reject the whole material
            return None
        els.update(got)
    return els or None


def file_elements(fname: str):
    """Element set a document filename covers (lenient: aliases, compounds, symbol/formula tokens)."""
    low = strip_accents(fname).lower()
    els = set()
    for key, syms in COMPOUND.items():
        if key in low:
            els.update(syms)
    for alias, sym in ALIAS.items():
        if re.search(r"(?<![a-z])" + re.escape(alias.strip()) + r"(?![a-z])", low):
            els.add(sym)
    for tok in re.split(r"[^A-Za-z0-9]+", fname):
        if tok in ELEMENTS:                       # exact symbol token, e.g. "Cr", "Au"
            els.add(tok)
        elif tok and any(ch.isdigit() for ch in tok):  # formula token, e.g. "Ti50Al50", "Cu3P"
            got, _ = decompose(tok.lower())
            els.update(got)
    return els


# --- Linking ---------------------------------------------------------------

def rel_path(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS material_sds "
        "(materiau TEXT, path TEXT, source TEXT DEFAULT 'auto')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coa_link "
        "(reference TEXT, path TEXT, source TEXT DEFAULT 'auto')"
    )


def drop_manual_duplicates(conn, table: str, key_col: str, rows: list) -> list:
    """Skip auto rows that a 'manual' row already covers.

    Uploads create a 'manual' link, and a COA named after its reference also
    matches tier A here — without this filter the same file would show up twice
    on the row after the next run.
    """
    existing = {
        (k, path)
        for k, path in conn.execute(
            f"SELECT {key_col}, path FROM {table} WHERE source = 'manual'"
        )
    }
    return [r for r in rows if (r[0], r[1]) not in existing]


def link_sds(conn, root: Path, sds_dir: Path):
    files = [p for p in sds_dir.rglob("*")
             if p.is_file() and p.suffix.lower() in (".pdf", ".docx", ".doc")]
    file_elems = {p: file_elements(p.name) for p in files}

    materials = [r[0] for r in conn.execute(
        "SELECT DISTINCT materiau FROM inventory WHERE materiau IS NOT NULL AND materiau != ''"
    )]

    rows, covered_materials = [], 0
    for mat in materials:
        elems = material_elements(mat)
        if not elems:
            continue
        exact = [p for p, fe in file_elems.items() if fe and fe == elems]
        if exact:
            for p in exact:
                rows.append((mat, rel_path(root, p), "auto"))
            covered_materials += 1
        elif len(elems) >= 2:  # alloy fallback: per-element SDS
            got_any = False
            for e in elems:
                singles = [p for p, fe in file_elems.items() if fe == {e}]
                for p in singles:
                    rows.append((mat, rel_path(root, p), "auto_element"))
                    got_any = True
            if got_any:
                covered_materials += 1

    rows = drop_manual_duplicates(conn, "material_sds", "materiau", rows)
    conn.execute("DELETE FROM material_sds WHERE source != 'manual'")
    conn.executemany(
        "INSERT INTO material_sds (materiau, path, source) VALUES (?, ?, ?)", rows
    )
    return len(rows), covered_materials, len(materials)


def link_coa(conn, root: Path, coa_dir: Path):
    files = [p for p in coa_dir.rglob("*")
             if p.is_file() and p.suffix.lower() in (".pdf", ".xlsx", ".jpg", ".jpeg", ".png", ".docx")]

    refs = [r[0] for r in conn.execute(
        "SELECT DISTINCT reference FROM inventory WHERE reference IS NOT NULL AND reference != ''"
    )]
    ref_set = set(refs)

    # Families for range expansion: prefix (no trailing digits) -> (width, {ref})
    fam = {}
    tail_re = re.compile(r"^(.*?)(\d+)$")
    for ref in refs:
        m = tail_re.match(ref)
        if m and m.group(1):
            prefix, num = m.group(1), m.group(2)
            fam.setdefault(prefix, {"width": len(num), "refs": set()})["refs"].add(ref)

    rows = []
    for p in files:
        name = p.name
        low = strip_accents(name).lower()
        matched = {}  # ref -> source

        # Tier A: exact reference substring
        for ref in refs:
            if len(ref) >= 5 and strip_accents(ref).lower() in low:
                matched[ref] = "auto_ref"

        # Tier B: range expansion within a known family prefix present in the name
        for prefix, info in fam.items():
            if strip_accents(prefix).lower() in low:
                tail = low.split(strip_accents(prefix).lower(), 1)[1][:50]
                nums = set(int(x) for x in re.findall(r"\d+", tail))
                for a, b in re.findall(r"(\d+)\s*(?:a|à)\s*(\d+)", tail):
                    nums.update(range(int(a), int(b) + 1))
                for n in nums:
                    cand = f"{prefix}{str(n).zfill(info['width'])}"
                    if cand in ref_set and cand not in matched:
                        matched[cand] = "auto_range"

        for ref, src in matched.items():
            rows.append((ref, rel_path(root, p), src))

    rows = drop_manual_duplicates(conn, "coa_link", "reference", rows)
    conn.execute("DELETE FROM coa_link WHERE source != 'manual'")
    conn.executemany(
        "INSERT INTO coa_link (reference, path, source) VALUES (?, ?, ?)", rows
    )
    linked_refs = len({r[0] for r in rows})
    return len(rows), linked_refs, len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description="Link SDS & COA documents to inventory")
    parser.add_argument("--db", default="server_display/inventory.sqlite")
    parser.add_argument("--root", default="server_display", help="Serving root (sds_root)")
    parser.add_argument("--sds-dir", default="server_display/SDS_cibles")
    parser.add_argument("--coa-dir", default="server_display/COA_cibles")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    conn = sqlite3.connect(args.db)
    try:
        ensure_tables(conn)
        n_sds, mats_cov, mats_tot = link_sds(conn, root, Path(args.sds_dir).resolve())
        n_coa, refs_cov, coa_files = link_coa(conn, root, Path(args.coa_dir).resolve())
        conn.commit()

        rows_with_sds = conn.execute(
            "SELECT COUNT(*) FROM inventory WHERE deleted_at IS NULL AND materiau IN "
            "(SELECT DISTINCT materiau FROM material_sds)"
        ).fetchone()[0]
        rows_with_coa = conn.execute(
            "SELECT COUNT(*) FROM inventory WHERE deleted_at IS NULL AND reference IN "
            "(SELECT DISTINCT reference FROM coa_link)"
        ).fetchone()[0]
        total_rows = conn.execute(
            "SELECT COUNT(*) FROM inventory WHERE deleted_at IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()

    print("=== SDS ===")
    print(f"  {n_sds} links, {mats_cov}/{mats_tot} materials covered")
    print(f"  {rows_with_sds}/{total_rows} rows have an SDS ({100*rows_with_sds//total_rows}%)")
    print("=== COA ===")
    print(f"  {n_coa} links across {refs_cov} references (from {coa_files} files)")
    print(f"  {rows_with_coa}/{total_rows} rows have a COA ({100*rows_with_coa//total_rows}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
