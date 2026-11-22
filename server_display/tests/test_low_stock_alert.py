"""Tests for the low-stock e-mail alert: aggregation, digest body and SMTP send."""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from app.categories_core import aggregate_categories
from scripts import send_low_stock_email as alert


def _row(materiau="Au", forme="Circulaire", diametre="", longueur="",
         largeur="", usure="Neuve"):
    return {
        "materiau": materiau, "forme": forme, "diametre": diametre,
        "longueur": longueur, "largeur": largeur, "usure": usure,
    }


# --- aggregate_categories --------------------------------------------------

def test_count_excludes_finie():
    rows = [
        _row(usure="Neuve"),
        _row(usure="Utilisée"),
        _row(usure="Finie"),  # worn out -> not counted
    ]
    cats = aggregate_categories(rows, {})
    assert len(cats) == 1
    assert cats[0]["count"] == 2


def test_dimension_derivation_round_and_rect():
    rows = [
        _row(materiau="Au", forme="Circulaire", diametre="3"),
        _row(materiau="Ti", forme="Rectangulaire", longueur="450", largeur="150"),
    ]
    dims = {c["materiau"]: c["dimension"] for c in aggregate_categories(rows, {})}
    assert dims["Au"] == "Ø3"
    assert dims["Ti"] == "450×150"


def test_below_flag_only_when_threshold_and_count_lower():
    rows = [_row(diametre="3")]  # count = 1, category key "Au|Circulaire|Ø3"
    key = "Au|Circulaire|Ø3"

    # No threshold -> never below.
    assert aggregate_categories(rows, {})[0]["below"] is False
    # Threshold above count -> below.
    assert aggregate_categories(rows, {key: 2})[0]["below"] is True
    # Threshold met -> not below.
    assert aggregate_categories(rows, {key: 1})[0]["below"] is False


# --- load_below_categories (real in-memory DB) -----------------------------

def test_load_below_categories_reads_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE inventory (materiau TEXT, forme TEXT, diametre TEXT, "
        "longueur TEXT, largeur TEXT, usure TEXT, deleted_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE category_thresholds (category_key TEXT PRIMARY KEY, min_count INTEGER)"
    )
    conn.execute(
        "INSERT INTO inventory (materiau, forme, diametre, usure) VALUES ('Au', 'Circulaire', '3', 'Neuve')"
    )
    conn.execute(
        "INSERT INTO category_thresholds VALUES ('Au|Circulaire|Ø3', 5)"
    )
    conn.commit()

    below = alert.load_below_categories(conn)
    assert len(below) == 1
    assert below[0]["category"] == "Au|Circulaire|Ø3"
    assert below[0]["count"] == 1
    assert below[0]["threshold"] == 5


def test_load_below_categories_without_thresholds_table():
    """No category_thresholds table yet (UI never opened) -> nothing below, no crash."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE inventory (materiau TEXT, forme TEXT, diametre TEXT, "
        "longueur TEXT, largeur TEXT, usure TEXT, deleted_at TEXT)"
    )
    conn.execute(
        "INSERT INTO inventory (materiau, forme, diametre, usure) VALUES ('Au', 'Circulaire', '3', 'Neuve')"
    )
    conn.commit()

    assert alert.load_below_categories(conn) == []


# --- E-mail body -----------------------------------------------------------

def test_body_lists_only_below_categories():
    below = [{
        "category": "Au|Circulaire|Ø3", "materiau": "Au", "forme": "Circulaire",
        "dimension": "Ø3", "count": 1, "threshold": 5, "below": True,
    }]
    body = alert.build_email_body(below)
    assert "Au · Circulaire · Ø3" in body
    assert "1 dispo / seuil 5" in body


def test_body_empty_when_nothing_below():
    body = alert.build_email_body([])
    assert "Aucune catégorie" in body


# --- SMTP send (mocked) ----------------------------------------------------

def _below_one():
    return [{
        "category": "Au|Circulaire|Ø3", "materiau": "Au", "forme": "Circulaire",
        "dimension": "Ø3", "count": 1, "threshold": 5, "below": True,
    }]


def test_send_email_uses_starttls_and_login():
    cfg = alert.SmtpConfig(
        host="smtp.example.com", port=587, user="u@x", password="secret",
        starttls=True, sender="u@x", recipients=("dest@example.org",),
    )
    msg = alert.build_message(_below_one(), cfg.sender, list(cfg.recipients))

    with patch("scripts.send_low_stock_email.smtplib.SMTP") as smtp_cls:
        smtp = smtp_cls.return_value.__enter__.return_value
        alert.send_email(msg, cfg)

    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("u@x", "secret")
    smtp.send_message.assert_called_once_with(msg)


def test_send_email_no_login_without_user():
    cfg = alert.SmtpConfig(
        host="relay.local", port=25, user="", password="",
        starttls=False, sender="cibles@example.org", recipients=("dest@example.org",),
    )
    msg = alert.build_message(_below_one(), cfg.sender, list(cfg.recipients))

    with patch("scripts.send_low_stock_email.smtplib.SMTP") as smtp_cls:
        smtp = smtp_cls.return_value.__enter__.return_value
        alert.send_email(msg, cfg)

    smtp.starttls.assert_not_called()
    smtp.login.assert_not_called()
    smtp.send_message.assert_called_once_with(msg)


# --- main() decision: no send when nothing below and no --always -----------

def test_main_no_send_when_nothing_below(tmp_path):
    db = tmp_path / "inv.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE inventory (materiau TEXT, forme TEXT, diametre TEXT, "
        "longueur TEXT, largeur TEXT, usure TEXT, deleted_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE category_thresholds (category_key TEXT PRIMARY KEY, min_count INTEGER)"
    )
    conn.commit()
    conn.close()

    argv = ["send_low_stock_email.py", "--db", str(db)]
    with patch.object(alert.sys, "argv", argv), \
         patch("scripts.send_low_stock_email.send_email") as send:
        rc = alert.main()

    assert rc == 0
    send.assert_not_called()


def test_main_dry_run_does_not_send(tmp_path):
    db = tmp_path / "inv.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE inventory (materiau TEXT, forme TEXT, diametre TEXT, "
        "longueur TEXT, largeur TEXT, usure TEXT, deleted_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE category_thresholds (category_key TEXT PRIMARY KEY, min_count INTEGER)"
    )
    conn.execute(
        "INSERT INTO inventory (materiau, forme, diametre, usure) VALUES ('Au', 'Circulaire', '3', 'Neuve')"
    )
    conn.execute("INSERT INTO category_thresholds VALUES ('Au|Circulaire|Ø3', 5)")
    conn.commit()
    conn.close()

    argv = ["send_low_stock_email.py", "--db", str(db), "--dry-run"]
    with patch.object(alert.sys, "argv", argv), \
         patch("scripts.send_low_stock_email.send_email") as send:
        rc = alert.main()

    assert rc == 0
    send.assert_not_called()
