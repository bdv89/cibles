"""Document management: upload, linking (SDS / COA / BC) and audit.

All tests run on the ``client_docs`` fixture, which isolates ``sds_root`` in a
tmp_path. Do NOT switch them to ``client_public``: uploads would land in the
real ``server_display/SDS_cibles/``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


SDS_DIR = "SDS_cibles/Fiche MSDS Cible"
COA_DIR = "COA_cibles/Certificat Analyse Cible"
BC_DIR = "BC_cibles"

BC_REF = "Au-TEST-001"
BC_VALUE = "S24/0481/FONPROCHI"

PDF = b"%PDF-1.4 test payload"


def upload(client, kind, key, filename, content=PDF):
    return client.post(
        "/api/document-upload",
        data={"kind": kind, "key": key},
        files={"file": (filename, content, "application/pdf")},
    )


def root_of(client):
    from app.config import get_settings

    return get_settings().sds_root


def docs_of(client, reference, field):
    rows = client.get("/api/search", params={"q": reference}).json()["rows"]
    row = next(r for r in rows if r["reference"] == reference)
    return row[field]


# --- Upload: nominal cases -----------------------------------------------------

def test_upload_sds_nominal(client_docs):
    res = upload(client_docs, "sds", "Au", "Au fiche.pdf")
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["path"] == SDS_DIR + "/Au fiche.pdf"
    assert (root_of(client_docs) / body["path"]).read_bytes() == PDF

    # SDS are shared by every row of the material
    rows = client_docs.get("/api/search", params={"q": "Au"}).json()["rows"]
    assert all(
        any(d["path"] == body["path"] and d["source"] == "manual" for d in r["sds_docs"])
        for r in rows
    )


def test_upload_coa_nominal(client_docs):
    body = upload(client_docs, "coa", BC_REF, "Au-TEST-001.pdf").json()
    assert body["success"] is True
    assert body["path"] == COA_DIR + "/Au-TEST-001.pdf"
    assert (root_of(client_docs) / body["path"]).exists()
    assert any(d["path"] == body["path"] for d in docs_of(client_docs, BC_REF, "coa_docs"))


def test_upload_bc_nominal(client_docs):
    body = upload(client_docs, "bc", BC_VALUE, "commande.pdf").json()
    assert body["success"] is True
    assert body["path"] == BC_DIR + "/commande.pdf"
    assert (root_of(client_docs) / body["path"]).exists()
    assert any(d["path"] == body["path"] for d in docs_of(client_docs, BC_REF, "bc_docs"))


# --- Upload: rejections --------------------------------------------------------

def test_upload_rejects_extension(client_docs):
    root = root_of(client_docs)
    before = {p for p in root.rglob("*") if p.is_file()}

    for name in ("evil.exe", "archive.zip", "sneaky.pdf.exe", "noext"):
        body = upload(client_docs, "sds", "Au", name).json()
        assert body["success"] is False, name
        assert "extension" in body["error"].lower(), name

    assert {p for p in root.rglob("*") if p.is_file()} == before
    assert docs_of(client_docs, BC_REF, "sds_docs") == []


def test_upload_rejects_oversize(client_docs):
    from app.config import get_settings

    settings = get_settings()
    settings.max_sds_size = 1024
    try:
        body = upload(client_docs, "sds", "Au", "gros.pdf", b"x" * 4096).json()
    finally:
        settings.max_sds_size = 100 * 1024 * 1024

    assert body["success"] is False
    assert "volumineux" in body["error"].lower()

    folder = root_of(client_docs) / SDS_DIR
    assert not (folder / "gros.pdf").exists()
    # no half-written temporary file left behind
    assert list(folder.glob("*.part")) == []
    assert docs_of(client_docs, BC_REF, "sds_docs") == []


def test_upload_filename_path_traversal(client_docs):
    root = root_of(client_docs)
    outside_before = set(root.parent.iterdir())

    for name in ("../../evil.pdf", "..\\..\\evil.pdf", "C:\\Windows\\evil.pdf", "foo/bar.pdf"):
        body = upload(client_docs, "sds", "Au", name).json()
        assert body["success"] is True, name
        # always flattened into the target folder, never above it
        assert body["path"].startswith(SDS_DIR + "/"), name
        assert ".." not in body["path"], name

    # nothing created outside the isolated root
    assert set(root.parent.iterdir()) == outside_before
    assert not (root.parent / "evil.pdf").exists()


def test_upload_collision_preserves_both(client_docs):
    first = upload(client_docs, "coa", BC_REF, "certificat.pdf", b"%PDF first").json()
    second = upload(client_docs, "coa", BC_REF, "certificat.pdf", b"%PDF second").json()

    assert first["success"] is True and first["renamed"] is False
    assert second["success"] is True and second["renamed"] is True
    assert first["path"] != second["path"]

    root = root_of(client_docs)
    assert (root / first["path"]).read_bytes() == b"%PDF first"
    assert (root / second["path"]).read_bytes() == b"%PDF second"

    paths = {d["path"] for d in docs_of(client_docs, BC_REF, "coa_docs")}
    assert {first["path"], second["path"]} <= paths


def test_upload_missing_key(client_docs):
    root = root_of(client_docs)
    before = {p for p in root.rglob("*") if p.is_file()}

    for kind in ("sds", "coa", "bc"):
        body = upload(client_docs, kind, "   ", "doc.pdf").json()
        assert body["success"] is False, kind

    assert {p for p in root.rglob("*") if p.is_file()} == before


def test_upload_missing_fields_use_our_error_shape(client_docs):
    """A missing form field must not surface as a FastAPI 422 the front cannot render."""
    res = client_docs.post(
        "/api/document-upload", files={"file": ("doc.pdf", PDF, "application/pdf")}
    )
    body = res.json()
    assert "detail" not in body
    assert body["success"] is False


def test_upload_unknown_kind(client_docs):
    body = upload(client_docs, "xxx", "Au", "doc.pdf").json()
    assert body["success"] is False


# --- Upload: end-to-end plumbing ----------------------------------------------

def test_upload_link_visible_in_search(client_docs):
    path = upload(client_docs, "coa", BC_REF, "visible.pdf").json()["path"]
    doc = next(d for d in docs_of(client_docs, BC_REF, "coa_docs") if d["path"] == path)

    assert doc["source"] == "manual"
    # reverse-proxy constraint: URLs must stay relative
    assert doc["url"].startswith("sds/")
    assert not doc["url"].startswith("/")


def test_uploaded_file_is_served(client_docs):
    payload = b"%PDF-1.4 served bytes"
    doc_path = upload(client_docs, "sds", "Au", "servi.pdf", payload).json()["path"]

    res = client_docs.get("/sds/" + doc_path)
    assert res.status_code == 200
    assert res.content == payload


# --- Linking: existence + traversal guards ------------------------------------

def test_link_rejects_nonexistent_path(client_docs):
    """Regression: coa.js used to POST raw user input, creating dead links."""
    body = client_docs.post(
        "/api/coa-link", json={"reference": BC_REF, "path": COA_DIR + "/jamais-vu.pdf"}
    ).json()
    assert body["success"] is False
    assert docs_of(client_docs, BC_REF, "coa_docs") == []


def test_link_rejects_path_traversal(client_docs):
    for bad in ("../../../etc/passwd", "..\\..\\secret.pdf"):
        body = client_docs.post(
            "/api/material-sds", json={"materiau": "Au", "path": bad}
        ).json()
        assert body["success"] is False, bad
    assert docs_of(client_docs, BC_REF, "sds_docs") == []


def test_legacy_upload_routes_removed(client_docs):
    for url in ("/api/upload", "/api/upload-sds"):
        assert client_docs.post(url).status_code in (404, 405)


# --- Linking: migrated from test_cibles.py ------------------------------------

def test_search_rows_include_document_fields(client_docs):
    rows = client_docs.get("/api/search").json()["rows"]
    assert rows
    assert all("sds_docs" in r and "coa_docs" in r and "bc_docs" in r for r in rows)


def test_coa_link_add_and_remove(client_docs):
    path = COA_DIR + "/demo.pdf"

    add = client_docs.post("/api/coa-link", json={"reference": BC_REF, "path": path})
    assert add.json()["success"] is True
    assert any(
        d["path"] == path and d["source"] == "manual"
        for d in docs_of(client_docs, BC_REF, "coa_docs")
    )

    rm = client_docs.request("DELETE", "/api/coa-link", json={"reference": BC_REF, "path": path})
    assert rm.json()["success"] is True
    assert all(d["path"] != path for d in docs_of(client_docs, BC_REF, "coa_docs"))


def test_coa_link_requires_reference_and_path(client_docs):
    body = client_docs.post("/api/coa-link", json={"reference": "", "path": ""}).json()
    assert body["success"] is False


def test_material_sds_add_and_remove(client_docs):
    path = SDS_DIR + "/demo.pdf"

    add = client_docs.post("/api/material-sds", json={"materiau": "Au", "path": path})
    assert add.json()["success"] is True

    rows = client_docs.get("/api/search", params={"q": "Au"}).json()["rows"]
    assert all(any(d["path"] == path for d in r["sds_docs"]) for r in rows)

    rm = client_docs.request("DELETE", "/api/material-sds", json={"materiau": "Au", "path": path})
    assert rm.json()["success"] is True
    rows2 = client_docs.get("/api/search", params={"q": "Au"}).json()["rows"]
    assert all(all(d["path"] != path for d in r["sds_docs"]) for r in rows2)


def test_bc_link_add_and_remove(client_docs):
    path = BC_DIR + "/demo.pdf"

    add = client_docs.post("/api/bc-link", json={"bon_commande": BC_VALUE, "path": path})
    assert add.json()["success"] is True
    assert any(d["path"] == path for d in docs_of(client_docs, BC_REF, "bc_docs"))

    # rows without a purchase order never carry a BC document
    rows = client_docs.get("/api/search", params={"q": "Au"}).json()["rows"]
    other = next(r for r in rows if r["reference"] == "Au-TEST-002")
    assert other["bc_docs"] == []

    rm = client_docs.request(
        "DELETE", "/api/bc-link", json={"bon_commande": BC_VALUE, "path": path}
    )
    assert rm.json()["success"] is True
    assert docs_of(client_docs, BC_REF, "bc_docs") == []


# --- Audit ---------------------------------------------------------------------

def audit(client):
    res = client.get("/api/documents-audit")
    assert res.status_code == 200
    return res.json()


def test_audit_lists_material_without_sds(client_docs):
    by_mat = {e["materiau"]: e for e in audit(client_docs)["sans_sds"]}
    assert by_mat["Au"]["cibles"] == 2
    assert by_mat["Ti"]["cibles"] == 1

    upload(client_docs, "sds", "Au", "Au.pdf")
    assert "Au" not in {e["materiau"] for e in audit(client_docs)["sans_sds"]}


def test_audit_lists_orphan_file(client_docs):
    orphans = {o["path"] for o in audit(client_docs)["orphelins"]}
    assert COA_DIR + "/demo.pdf" in orphans
    assert SDS_DIR + "/demo.pdf" in orphans

    client_docs.post(
        "/api/material-sds", json={"materiau": "Au", "path": SDS_DIR + "/demo.pdf"}
    )
    assert SDS_DIR + "/demo.pdf" not in {o["path"] for o in audit(client_docs)["orphelins"]}


def test_audit_lists_dead_link(client_docs):
    path = upload(client_docs, "coa", BC_REF, "ephemere.pdf").json()["path"]
    assert audit(client_docs)["morts"] == []

    (root_of(client_docs) / path).unlink()
    morts = audit(client_docs)["morts"]
    assert any(m["path"] == path and m["kind"] == "coa" for m in morts)


def test_audit_lists_duplicate_link(client_docs):
    """No table has a UNIQUE constraint - duplicates are physically possible."""
    from app.config import get_settings

    path = COA_DIR + "/demo.pdf"
    conn = sqlite3.connect(get_settings().db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coa_link (reference TEXT, path TEXT, source TEXT)"
    )
    conn.executemany(
        "INSERT INTO coa_link (reference, path, source) VALUES (?, ?, ?)",
        [(BC_REF, path, "manual"), (BC_REF, path, "auto_ref")],
    )
    conn.commit()
    conn.close()

    dup = audit(client_docs)["doublons"]
    assert any(d["cle"] == BC_REF and d["path"] == path and d["n"] == 2 for d in dup)


def test_audit_dead_link_removable_via_existing_delete(client_docs):
    path = upload(client_docs, "sds", "Au", "adieu.pdf").json()["path"]
    (root_of(client_docs) / path).unlink()
    assert any(m["path"] == path for m in audit(client_docs)["morts"])

    client_docs.request("DELETE", "/api/material-sds", json={"materiau": "Au", "path": path})
    assert audit(client_docs)["morts"] == []


# --- link_documents.py: manual links must not be duplicated -------------------

def test_link_documents_skips_rows_already_manual(tmp_path):
    """A COA uploaded under its reference also matches tier A of the script.

    Without the filter, the next run would add an `auto_ref` row next to our
    `manual` one on the same (reference, path) and the UI would show COA x2.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "link_documents", Path(__file__).resolve().parents[1] / "scripts" / "link_documents.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    conn = sqlite3.connect(tmp_path / "t.sqlite")
    conn.execute("CREATE TABLE coa_link (reference TEXT, path TEXT, source TEXT)")
    conn.execute(
        "INSERT INTO coa_link VALUES (?, ?, 'manual')", ("Au-TEST-001", "COA/a.pdf")
    )
    conn.commit()

    rows = [
        ("Au-TEST-001", "COA/a.pdf", "auto_ref"),   # already covered by the manual row
        ("Au-TEST-002", "COA/b.pdf", "auto_ref"),   # genuinely new
    ]
    kept = mod.drop_manual_duplicates(conn, "coa_link", "reference", rows)
    conn.close()

    assert kept == [("Au-TEST-002", "COA/b.pdf", "auto_ref")]


# --- Backups must never be polluted by the test suite -------------------------

def test_tests_never_write_into_the_real_backup_dir(client_docs, tmp_path):
    """Regression: the fixtures used to leave a 3-row DB in server_display/backups/.

    ensure_daily_backup guards on `if not backup_path.exists()`, so that file
    permanently occupied the day's slot and the real service never overwrote it.
    """
    from app.config import get_settings

    backup_dir = get_settings().backup_dir
    assert tmp_path in backup_dir.parents or backup_dir == tmp_path / "backups"

    real = Path(__file__).resolve().parents[1] / "backups"
    written_today = list(backup_dir.glob("inventory_*.sqlite"))
    assert all(real not in b.parents for b in written_today)
