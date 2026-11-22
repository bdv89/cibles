"""Document domain — resolve, link, upload and audit SDS / COA / BC for inventory rows.

Three document types, one shape each (see DOC_KINDS):
- material_sds(materiau, path, source)     — SDS shared by all rows of a material
- coa_link(reference, path, source)        — certificates keyed by reference
- bc_link(bon_commande, path, source)      — purchase orders keyed by order number

All three are keyed on **business keys**, never on rowid, so they survive a
re-import of `inventory` untouched. Files are served through /sds/{path}
(path is relative to sds_root).
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import aiofiles
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app.config import get_settings
from app.dependencies import get_db, get_client_ip, check_read_auth
from app.main import limiter
from app.utils import safe_path

router = APIRouter()

COA_EXT = (".pdf", ".xlsx", ".jpg", ".jpeg", ".png", ".docx")
SDS_EXT = (".pdf", ".docx", ".doc")

# One entry per document type. Adding a fourth type is an entry here plus a folder.
#   dir       destination of uploads (files may also live in sub-folders below root)
#   root      folder scanned when listing/auditing (paths are relative to sds_root)
#   key       inventory column carrying the business key
#   field     name of the list injected into each search row
DOC_KINDS = {
    "sds": {
        "dir": "SDS_cibles/Fiche MSDS Cible",
        "root": "SDS_cibles",
        "ext": SDS_EXT,
        "table": "material_sds",
        "key": "materiau",
        "field": "sds_docs",
        "label": "SDS",
    },
    "coa": {
        "dir": "COA_cibles/Certificat Analyse Cible",
        "root": "COA_cibles",
        "ext": COA_EXT,
        "table": "coa_link",
        "key": "reference",
        "field": "coa_docs",
        "label": "certificat",
    },
    "bc": {
        "dir": "BC_cibles",
        "root": "BC_cibles",
        "ext": COA_EXT,
        "table": "bc_link",
        "key": "bon_commande",
        "field": "bc_docs",
        "label": "bon de commande",
    },
}

CHUNK = 1024 * 1024
MAX_FILENAME_STEM = 120

_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def nfc(value: str) -> str:
    """Normalise to NFC before comparing paths.

    Filenames carry accents ("Fiche Conformite.pdf") and macOS uploads arrive in
    NFD: without this, disk and database would disagree on identical names and
    every link would look dead.
    """
    return unicodedata.normalize("NFC", value or "")


def _list_files(base, exts):
    out = []
    if base.exists():
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix.lower() in exts:
                out.append({"path": p.relative_to(base.parent).as_posix(), "name": p.name})
    return out


def kind_files(kind: str) -> list[dict]:
    cfg = DOC_KINDS[kind]
    return _list_files(get_settings().sds_root / cfg["root"], cfg["ext"])


async def ensure_doc_tables(db) -> None:
    for cfg in DOC_KINDS.values():
        await db.execute(
            f"CREATE TABLE IF NOT EXISTS {cfg['table']} "
            f"({cfg['key']} TEXT, path TEXT, source TEXT DEFAULT 'auto')"
        )


def _doc(path: str, source: str) -> dict:
    # URL stays relative (no leading slash): the app is served under /cibles/.
    return {"path": path, "url": "sds/" + quote(path), "source": source}


async def attach_documents(db, rows: list[dict]) -> None:
    """Enrich row dicts in place with `sds_docs`, `coa_docs` and `bc_docs`."""
    if not rows:
        return
    await ensure_doc_tables(db)

    for kind, cfg in DOC_KINDS.items():
        keys = {r.get(cfg["key"]) for r in rows if r.get(cfg["key"])}
        found: dict[str, list] = defaultdict(list)
        if keys:
            ph = ",".join("?" * len(keys))
            async with db.execute(
                f"SELECT {cfg['key']}, path, source FROM {cfg['table']} "
                f"WHERE {cfg['key']} IN ({ph})",
                tuple(keys),
            ) as cur:
                for key, path, source in await cur.fetchall():
                    found[key].append(_doc(path, source))
        for r in rows:
            r[cfg["field"]] = found.get(r.get(cfg["key"]), [])


# --- Filesystem helpers --------------------------------------------------------

def safe_filename(raw: str, allowed_ext) -> str | None:
    """Turn a client-supplied filename into a bare, safe basename.

    Returns None when the extension is not whitelisted. The extension is
    *recomposed* from the whitelist, never reused verbatim, so ".PDF" lands as
    ".pdf" and "sneaky.pdf.exe" is rejected on its real suffix.
    """
    # Strip any directory component: handles ../x, ..\x and C:\dir\x alike.
    # Path().name alone would not cut backslashes off Windows.
    name = nfc((raw or "").replace("\\", "/").rsplit("/", 1)[-1])

    ext = Path(name).suffix.lower()
    if ext not in allowed_ext:
        return None

    stem = name[: len(name) - len(Path(name).suffix)]
    stem = _BAD_FILENAME_CHARS.sub("_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if stem.upper() in _WIN_RESERVED:
        stem = "_" + stem
    stem = stem[:MAX_FILENAME_STEM].strip(" .")
    if not stem:
        stem = "document_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    return stem + ext


def unique_dest(folder: Path, name: str) -> tuple[Path, bool]:
    """Never overwrite: a certificate crushed by a same-named upload is lost."""
    dest = folder / name
    if not dest.exists():
        return dest, False
    stem, ext = Path(name).stem, Path(name).suffix
    for i in range(2, 100):
        candidate = folder / f"{stem} ({i}){ext}"
        if not candidate.exists():
            return candidate, True
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return folder / f"{stem} {stamp}{ext}", True


class TooLarge(Exception):
    pass


async def save_streaming(upload: UploadFile, dest: Path, max_bytes: int) -> int:
    """Write in chunks, aborting on the first byte past the cap.

    Reading the whole body first (the legacy route did) buffers up to the cap in
    RAM before any check happens.
    """
    tmp = dest.with_name(dest.name + ".part")
    total = 0
    try:
        async with aiofiles.open(tmp, "wb") as out:
            while True:
                chunk = await upload.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise TooLarge()
                await out.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)
    return total


# --- Link helpers (shared by the 6 link routes and by upload) ------------------

async def _log(db, action: str, payload: dict, client_ip: str | None) -> None:
    # Documents are keyed on business keys, not rowid, hence rowid 0.
    await db.execute(
        "INSERT INTO edit_log (inventory_rowid, action, field_changes, user_ip) "
        "VALUES (0, ?, ?, ?)",
        (action, json.dumps(payload, ensure_ascii=False), client_ip),
    )


async def link_add(
    db, kind: str, key: str, path: str, client_ip: str | None = None, action: str = "doc_link"
) -> dict:
    cfg = DOC_KINDS[kind]
    key = (key or "").strip()
    path = nfc((path or "").strip())
    if not key or not path:
        return {"success": False, "error": f"{cfg['key']} et path requis"}

    settings = get_settings()
    try:
        if not safe_path(settings.sds_root, path).exists():
            return {"success": False, "error": "Fichier introuvable sur le serveur"}
    except ValueError:
        return {"success": False, "error": "Chemin invalide"}

    async with db.execute(
        f"SELECT 1 FROM {cfg['table']} WHERE {cfg['key']} = ? AND path = ?", (key, path)
    ) as cur:
        if await cur.fetchone():
            return {"success": True, "message": "Déjà lié"}

    await db.execute(
        f"INSERT INTO {cfg['table']} ({cfg['key']}, path, source) VALUES (?, ?, 'manual')",
        (key, path),
    )
    await _log(db, action, {"kind": kind, "key": key, "path": path}, client_ip)
    await db.commit()
    return {"success": True}


async def link_del(db, kind: str, key: str, path: str, client_ip: str | None = None) -> dict:
    cfg = DOC_KINDS[kind]
    key = (key or "").strip()
    path = (path or "").strip()
    if not key or not path:
        return {"success": False, "error": f"{cfg['key']} et path requis"}

    await db.execute(
        f"DELETE FROM {cfg['table']} WHERE {cfg['key']} = ? AND path = ?", (key, path)
    )
    await _log(db, "doc_unlink", {"kind": kind, "key": key, "path": path}, client_ip)
    await db.commit()
    return {"success": True}


async def _payload(request: Request) -> dict | None:
    try:
        return await request.json()
    except Exception:
        return None


async def _link_route(request: Request, db, client_ip, kind: str, add: bool) -> dict:
    await ensure_doc_tables(db)
    data = await _payload(request)
    if data is None:
        return {"success": False, "error": "Invalid JSON"}
    cfg = DOC_KINDS[kind]
    key = data.get(cfg["key"]) or ""
    path = data.get("path") or ""
    if add:
        return await link_add(db, kind, key, path, client_ip)
    return await link_del(db, kind, key, path, client_ip)


# --- File listing --------------------------------------------------------------

@router.get("/coa-files")
@limiter.limit("60/minute")
async def list_coa_files(request: Request, _auth: bool = Depends(check_read_auth)):
    """List available certificate files (for the manual assignment picker)."""
    return {"files": kind_files("coa")}


@router.get("/sds-files")
@limiter.limit("60/minute")
async def list_sds_files(request: Request, _auth: bool = Depends(check_read_auth)):
    """List available SDS files (for the manual assignment picker)."""
    return {"files": kind_files("sds")}


@router.get("/bc-files")
@limiter.limit("60/minute")
async def list_bc_files(request: Request, _auth: bool = Depends(check_read_auth)):
    """List available purchase-order files (for the manual assignment picker)."""
    return {"files": kind_files("bc")}


# --- Linking routes (thin wrappers over link_add / link_del) -------------------

@router.post("/material-sds")
@limiter.limit("30/minute")
async def add_material_sds(
    request: Request, db=Depends(get_db), client_ip: str | None = Depends(get_client_ip)
):
    """Attach an SDS to a material (shared by all rows of that material)."""
    return await _link_route(request, db, client_ip, "sds", add=True)


@router.delete("/material-sds")
@limiter.limit("30/minute")
async def remove_material_sds(
    request: Request, db=Depends(get_db), client_ip: str | None = Depends(get_client_ip)
):
    """Remove an SDS link from a material (affects all rows of that material)."""
    return await _link_route(request, db, client_ip, "sds", add=False)


@router.post("/coa-link")
@limiter.limit("30/minute")
async def add_coa_link(
    request: Request, db=Depends(get_db), client_ip: str | None = Depends(get_client_ip)
):
    """Attach a certificate to a reference."""
    return await _link_route(request, db, client_ip, "coa", add=True)


@router.delete("/coa-link")
@limiter.limit("30/minute")
async def remove_coa_link(
    request: Request, db=Depends(get_db), client_ip: str | None = Depends(get_client_ip)
):
    """Remove a certificate link from a reference."""
    return await _link_route(request, db, client_ip, "coa", add=False)


@router.post("/bc-link")
@limiter.limit("30/minute")
async def add_bc_link(
    request: Request, db=Depends(get_db), client_ip: str | None = Depends(get_client_ip)
):
    """Attach a purchase order to an order number (shared by all its targets)."""
    return await _link_route(request, db, client_ip, "bc", add=True)


@router.delete("/bc-link")
@limiter.limit("30/minute")
async def remove_bc_link(
    request: Request, db=Depends(get_db), client_ip: str | None = Depends(get_client_ip)
):
    """Remove a purchase-order link."""
    return await _link_route(request, db, client_ip, "bc", add=False)


# --- Upload --------------------------------------------------------------------

@router.post("/document-upload")
@limiter.limit("10/minute")
async def upload_document(
    request: Request,
    # Defaults instead of Form(...): a missing field must come back in our own
    # {"success": false, "error": ...} shape, not as a FastAPI 422 the front
    # cannot render.
    kind: str = Form(""),
    key: str = Form(""),
    file: UploadFile = File(...),
    db=Depends(get_db),
    client_ip: str | None = Depends(get_client_ip),
):
    """Store a document on the server and link it to the row's business key.

    Order matters: folder, then file, then link. If the link fails the file is
    removed — the reverse order would leave a dead link, the very defect this
    endpoint exists to avoid. Worst case is an orphan file, which the audit page
    reports.
    """
    await ensure_doc_tables(db)

    cfg = DOC_KINDS.get(kind)
    if cfg is None:
        return {"success": False, "error": "Type de document inconnu"}

    key = (key or "").strip()
    if not key:
        return {"success": False, "error": f"{cfg['key']} requis"}

    name = safe_filename(file.filename, cfg["ext"])
    if name is None:
        suffix = Path((file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]).suffix
        allowed = ", ".join(cfg["ext"])
        return {
            "success": False,
            "error": f"Extension non autorisée : {suffix or '(aucune)'} — attendu : {allowed}",
        }

    settings = get_settings()
    folder = (settings.sds_root / cfg["dir"]).resolve()
    folder.mkdir(parents=True, exist_ok=True)

    dest, renamed = unique_dest(folder, name)

    # Defence in depth: safe_path allows any depth below sds_root, the parent
    # check pins the destination to this exact folder.
    try:
        checked = safe_path(settings.sds_root, f"{cfg['dir']}/{dest.name}")
    except ValueError:
        return {"success": False, "error": "Chemin invalide"}
    if checked.parent != folder:
        return {"success": False, "error": "Chemin invalide"}

    max_bytes = settings.max_sds_size
    try:
        await save_streaming(file, dest, max_bytes)
    except TooLarge:
        mo = max_bytes // (1024 * 1024)
        return {"success": False, "error": f"Fichier trop volumineux (max {mo} Mo)"}
    except OSError as exc:
        return {"success": False, "error": f"Écriture impossible : {exc}"}

    rel = dest.relative_to(settings.sds_root).as_posix()
    result = await link_add(db, kind, key, rel, client_ip, action="doc_upload")
    if not result.get("success"):
        dest.unlink(missing_ok=True)
        return result

    return {"success": True, "path": rel, "renamed": renamed}


# --- Audit ---------------------------------------------------------------------

@router.get("/documents-audit")
@limiter.limit("30/minute")
async def documents_audit(
    request: Request, db=Depends(get_db), _auth: bool = Depends(check_read_auth)
):
    """Coverage and consistency of the document store.

    Aggregated in Python (like categories.py) so the derived data stays in one
    place. Volumes are small: ~1000 rows, ~250 files.
    """
    await ensure_doc_tables(db)
    settings = get_settings()

    async with db.execute(
        "SELECT materiau, reference, bon_commande FROM inventory WHERE deleted_at IS NULL"
    ) as cur:
        rows = [
            {"materiau": m or "", "reference": r or "", "bon_commande": b or ""}
            for m, r, b in await cur.fetchall()
        ]

    links: dict[str, list[tuple[str, str, str]]] = {}
    for kind, cfg in DOC_KINDS.items():
        async with db.execute(
            f"SELECT {cfg['key']}, path, source FROM {cfg['table']}"
        ) as cur:
            links[kind] = [
                (nfc(k or ""), nfc(p or ""), s or "") for k, p, s in await cur.fetchall()
            ]

    morts, doublons, orphelins = [], [], []
    linked_keys: dict[str, set[str]] = {}

    for kind, cfg in DOC_KINDS.items():
        linked_keys[kind] = {k for k, _, _ in links[kind] if k}

        # Dead links: the row points at a file the disk no longer holds.
        for key, path, source in links[kind]:
            try:
                exists = safe_path(settings.sds_root, path).exists()
            except ValueError:
                exists = False
            if not exists:
                morts.append(
                    {"kind": kind, "cle": key, "path": path, "source": source,
                     "label": cfg["label"]}
                )

        # Duplicates: none of the tables carries a UNIQUE constraint.
        for (key, path), n in Counter((k, p) for k, p, _ in links[kind]).items():
            if n > 1:
                doublons.append({"kind": kind, "cle": key, "path": path, "n": n})

        # Orphan files: present on disk, referenced by nothing.
        used = {p for _, p, _ in links[kind]}
        for f in kind_files(kind):
            path = nfc(f["path"])
            if path not in used:
                orphelins.append(
                    {"kind": kind, "path": path, "name": f["name"],
                     "url": "sds/" + quote(path)}
                )

    # Missing SDS: grouped by material, worst offenders first.
    per_material = Counter(r["materiau"] for r in rows if r["materiau"])
    sans_sds = sorted(
        (
            {"materiau": m, "cibles": n}
            for m, n in per_material.items()
            if m not in linked_keys["sds"]
        ),
        key=lambda e: (-e["cibles"], e["materiau"]),
    )

    sans_coa = sorted(
        (
            {"reference": r["reference"], "materiau": r["materiau"]}
            for r in rows
            if r["reference"] and r["reference"] not in linked_keys["coa"]
        ),
        key=lambda e: (e["materiau"], e["reference"]),
    )

    # bon_commande is not FTS-indexed, so the audit exposes the references
    # instead: they are what the deep link into the inventory can search for.
    bc_refs: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["bon_commande"]:
            bc_refs[r["bon_commande"]].append(r["reference"])
    sans_bc = sorted(
        (
            {"bon_commande": bc, "cibles": len(refs), "references": sorted(refs)}
            for bc, refs in bc_refs.items()
            if bc not in linked_keys["bc"]
        ),
        key=lambda e: (-e["cibles"], e["bon_commande"]),
    )

    def covered(kind, field):
        return sum(1 for r in rows if r[field] and r[field] in linked_keys[kind])

    return {
        "sans_sds": sans_sds,
        "sans_coa": sans_coa,
        "sans_bc": sans_bc,
        "morts": sorted(morts, key=lambda e: (e["kind"], e["path"])),
        "doublons": sorted(doublons, key=lambda e: (e["kind"], e["path"])),
        "orphelins": sorted(orphelins, key=lambda e: (e["kind"], e["path"])),
        "stats": {
            "cibles": len(rows),
            "cibles_avec_sds": covered("sds", "materiau"),
            "cibles_avec_coa": covered("coa", "reference"),
            "cibles_avec_bc": covered("bc", "bon_commande"),
            "cibles_avec_bon": sum(1 for r in rows if r["bon_commande"]),
            "fichiers": {k: len(kind_files(k)) for k in DOC_KINDS},
            "orphelins": len(orphelins),
            "morts": len(morts),
            "doublons": len(doublons),
        },
    }
