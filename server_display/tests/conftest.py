"""Pytest fixtures for server_display tests."""
from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database with full schema."""
    import sqlite3

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE inventory (
            materiau TEXT,
            reference TEXT,
            date_achat TEXT,
            prix TEXT,
            purete TEXT,
            equipement TEXT,
            usure TEXT,
            particularite TEXT,
            fournisseur TEXT,
            hauteur_mm TEXT,
            forme TEXT,
            longueur TEXT,
            largeur TEXT,
            hauteur TEXT,
            diametre TEXT,
            epaisseur TEXT,
            bon_commande TEXT,
            emplacement TEXT,
            remarque TEXT,
            version INTEGER DEFAULT 1,
            deleted_at TEXT
        )
    """)
    # Create FTS5 virtual table
    conn.execute("""
        CREATE VIRTUAL TABLE inventory_fts USING fts5(
            materiau, reference, forme, emplacement, fournisseur, remarque,
            content='inventory', content_rowid='rowid'
        )
    """)
    # Triggers keep the external-content FTS index in sync (canonical FTS5 pattern).
    _fts = "materiau, reference, forme, emplacement, fournisseur, remarque"
    _new = ", ".join("new." + c for c in _fts.replace(" ", "").split(","))
    _old = ", ".join("old." + c for c in _fts.replace(" ", "").split(","))
    conn.execute(
        f"CREATE TRIGGER inventory_ai AFTER INSERT ON inventory BEGIN "
        f"INSERT INTO inventory_fts(rowid, {_fts}) VALUES (new.rowid, {_new}); END"
    )
    conn.execute(
        f"CREATE TRIGGER inventory_ad AFTER DELETE ON inventory BEGIN "
        f"INSERT INTO inventory_fts(inventory_fts, rowid, {_fts}) VALUES ('delete', old.rowid, {_old}); END"
    )
    conn.execute(
        f"CREATE TRIGGER inventory_au AFTER UPDATE ON inventory BEGIN "
        f"INSERT INTO inventory_fts(inventory_fts, rowid, {_fts}) VALUES ('delete', old.rowid, {_old}); "
        f"INSERT INTO inventory_fts(rowid, {_fts}) VALUES (new.rowid, {_new}); END"
    )
    conn.execute("""
        CREATE TABLE category_thresholds (
            category_key TEXT PRIMARY KEY,
            min_count    INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE edit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_rowid INTEGER NOT NULL,
            action TEXT NOT NULL,
            field_changes TEXT,
            user_ip TEXT,
            timestamp TEXT DEFAULT (datetime('now'))
        )
    """)
    rows = [
        ("Au", "Au-TEST-001", "Circulaire", "Neuve"),
        ("Au", "Au-TEST-002", "Circulaire", "Utilisée"),
        ("Ti", "Ti-TEST-001", "Rectangulaire", "Neuve"),
    ]
    for (mat, ref, forme, usure) in rows:
        # The AFTER INSERT trigger populates inventory_fts automatically.
        conn.execute(
            "INSERT INTO inventory (materiau, reference, forme, usure) VALUES (?, ?, ?, ?)",
            (mat, ref, forme, usure),
        )
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    db_path.unlink(missing_ok=True)


# Every client fixture MUST override backup_dir. app startup runs
# ensure_daily_backup (app/utils.py:230), which copies db_path into
# settings.backup_dir as inventory_<today>.sqlite — and it guards on
# `if not backup_path.exists()`, so a 3-row test database written there first
# permanently occupies the day's slot: the real service will never overwrite it.
@pytest.fixture
def client_public(temp_db, tmp_path):
    """Test client with no read password (public access)."""
    from app.config import init_settings
    from app.main import app, limiter

    init_settings(
        db_path=temp_db,
        backup_dir=tmp_path / "backups",
        edit_password="",
        read_password="",
    )

    # Reset rate limiter for test isolation
    limiter.reset()

    with TestClient(app) as client:
        yield client


@pytest.fixture
def client_read_protected(temp_db, tmp_path):
    """Test client with read password set."""
    from app.config import init_settings
    from app.main import app, limiter

    init_settings(
        db_path=temp_db,
        backup_dir=tmp_path / "backups",
        edit_password="editpwd",
        read_password="readpwd",
    )

    # Reset rate limiter for test isolation
    limiter.reset()

    with TestClient(app) as client:
        yield client


@pytest.fixture
def client_docs(temp_db, tmp_path):
    """Test client with an isolated sds_root and a ready-made document tree.

    Without ``sds_root=tmp_path`` an upload test would write into the real
    ``server_display/SDS_cibles/``: the other client fixtures only override
    db_path/passwords, so sds_root keeps its production default.
    """
    import sqlite3

    from app.config import init_settings
    from app.main import app, limiter

    for rel in (
        "SDS_cibles/Fiche MSDS Cible",
        "COA_cibles/Certificat Analyse Cible",
        "BC_cibles",
    ):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    (tmp_path / "SDS_cibles/Fiche MSDS Cible/demo.pdf").write_bytes(b"%PDF-1.4 sds")
    (tmp_path / "COA_cibles/Certificat Analyse Cible/demo.pdf").write_bytes(b"%PDF-1.4 coa")
    (tmp_path / "BC_cibles/demo.pdf").write_bytes(b"%PDF-1.4 bc")

    # Only 11.5% of real rows carry a purchase order; give one to the seed data
    # so the BC section can be exercised (and the other two rows cover the
    # "no purchase order" branch).
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "UPDATE inventory SET bon_commande = ? WHERE reference = ?",
        ("S24/0481/FONPROCHI", "Au-TEST-001"),
    )
    conn.commit()
    conn.close()

    init_settings(
        db_path=temp_db,
        sds_root=tmp_path,
        backup_dir=tmp_path / "backups",
        edit_password="",
        read_password="",
    )

    # Reset rate limiter for test isolation
    limiter.reset()

    with TestClient(app) as client:
        yield client


@pytest.fixture
def read_auth_header():
    """Valid Basic Auth header for read password 'readpwd'."""
    credentials = base64.b64encode(b":readpwd").decode("utf-8")
    return {"Authorization": f"Basic {credentials}"}


@pytest.fixture
def invalid_auth_header():
    """Invalid Basic Auth header."""
    credentials = base64.b64encode(b":wrongpassword").decode("utf-8")
    return {"Authorization": f"Basic {credentials}"}
