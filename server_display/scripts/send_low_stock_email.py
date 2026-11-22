#!/usr/bin/env python3
"""Daily low-stock digest: e-mail the categories whose available stock is below threshold.

Reads the inventory + category_thresholds from SQLite, reuses the exact aggregation of the
/categories endpoint (app.categories_core.aggregate_categories), and — if any category is
below its threshold — sends a single recap e-mail via smtplib (stdlib, no extra dependency).

Read-only on the database. Meant to be run once a day by the Windows Task Scheduler.

SMTP is configured through environment variables (secrets never live in the code):
    SMTP_HOST       required to actually send (e.g. company relay, or smtp.gmail.com)
    SMTP_PORT       default 587
    SMTP_USER       optional (login user); if empty, no authentication is attempted
    SMTP_PASSWORD   optional (login password) — never logged
    SMTP_STARTTLS   "true"/"false", default "true"
    ALERT_TO        recipient(s), comma-separated, required to send (comma-separated)
    ALERT_FROM      sender, default = SMTP_USER or ALERT_TO

Usage:
    python scripts/send_low_stock_email.py --db inventory.sqlite
    python scripts/send_low_stock_email.py --dry-run     # print the mail, don't send
    python scripts/send_low_stock_email.py --always      # send even when nothing is low
"""
from __future__ import annotations

import argparse
import os
import smtplib
import sqlite3
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

# Make `app` importable when run as a standalone script from server_display/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.categories_core import aggregate_categories  # noqa: E402


# --- Data access -----------------------------------------------------------

def load_below_categories(conn: sqlite3.Connection) -> list[dict]:
    """Return the categories currently below their threshold (empty list if none)."""
    conn.row_factory = sqlite3.Row
    # The UI creates category_thresholds lazily; with no thresholds set nothing is below.
    # Stay read-only (unlike the route, we don't CREATE the table).
    has_thresholds = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='category_thresholds'"
    ).fetchone()
    thresholds = {
        r["category_key"]: r["min_count"]
        for r in conn.execute("SELECT category_key, min_count FROM category_thresholds")
    } if has_thresholds else {}
    rows = conn.execute(
        "SELECT materiau, forme, diametre, longueur, largeur, usure "
        "FROM inventory WHERE deleted_at IS NULL"
    ).fetchall()
    return [c for c in aggregate_categories(rows, thresholds) if c["below"]]


# --- E-mail building -------------------------------------------------------

def _label(cat: dict) -> str:
    """Human label 'Matériau · Forme · Dimension' (drops empty parts)."""
    parts = [p for p in (cat["materiau"], cat["forme"], cat["dimension"]) if p]
    return " · ".join(parts) or "(sans catégorie)"


def build_email_body(below: list[dict]) -> str:
    """Plain-text digest body listing every below-threshold category."""
    if not below:
        return "Aucune catégorie sous le seuil. Tout est OK.\n"
    lines = [
        f"{len(below)} catégorie(s) de cibles sous le seuil de stock :",
        "",
    ]
    for cat in below:
        lines.append(
            f"  - {_label(cat)} : {cat['count']} dispo / seuil {cat['threshold']}"
        )
    lines += ["", "— Inventaire cibles (alerte automatique quotidienne)"]
    return "\n".join(lines) + "\n"


def build_message(below: list[dict], sender: str, recipients: list[str]) -> EmailMessage:
    msg = EmailMessage()
    subject = (
        f"[Cibles] {len(below)} catégorie(s) sous le seuil"
        if below else "[Cibles] Stock OK"
    )
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(build_email_body(below))
    return msg


# --- SMTP ------------------------------------------------------------------

@dataclass
class SmtpConfig:
    host: str
    port: int = 587
    user: str = ""
    password: str = ""
    starttls: bool = True
    sender: str = ""
    recipients: tuple[str, ...] = ()


def smtp_config_from_env() -> SmtpConfig:
    recipients = tuple(
        addr.strip()
        for addr in os.environ.get("ALERT_TO", "").split(",")
        if addr.strip()
    )
    user = os.environ.get("SMTP_USER", "")
    return SmtpConfig(
        host=os.environ.get("SMTP_HOST", ""),
        port=int(os.environ.get("SMTP_PORT", "587")),
        user=user,
        password=os.environ.get("SMTP_PASSWORD", ""),
        starttls=os.environ.get("SMTP_STARTTLS", "true").strip().lower() != "false",
        sender=os.environ.get("ALERT_FROM", "") or user or (recipients[0] if recipients else ""),
        recipients=recipients,
    )


def send_email(msg: EmailMessage, cfg: SmtpConfig) -> None:
    """Send the message via SMTP. STARTTLS and login only when configured."""
    with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as smtp:
        if cfg.starttls:
            smtp.starttls()
        if cfg.user:
            smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)


# --- Entry point -----------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="E-mail the low-stock categories digest")
    parser.add_argument("--db", default="inventory.sqlite", help="Inventory SQLite path")
    parser.add_argument("--always", action="store_true",
                        help="Send a mail even when nothing is below threshold")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the e-mail instead of sending it")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        below = load_below_categories(conn)
    finally:
        conn.close()

    print(f"{len(below)} catégorie(s) sous le seuil.")

    if not below and not args.always:
        print("RAS — aucun mail envoyé.")
        return 0

    cfg = smtp_config_from_env()
    msg = build_message(below, cfg.sender, list(cfg.recipients))

    if args.dry_run:
        print("--- DRY RUN — mail non envoyé ---")
        print(f"To: {msg['To']}")
        print(f"Subject: {msg['Subject']}")
        print(msg.get_content())
        return 0

    if not cfg.host:
        print("ERREUR : SMTP_HOST non défini — impossible d'envoyer. "
              "Utilise --dry-run pour tester sans SMTP.", file=sys.stderr)
        return 1

    try:
        send_email(msg, cfg)
    except Exception as exc:  # clear log over sophisticated handling
        print(f"ERREUR envoi mail : {exc}", file=sys.stderr)
        return 1

    print(f"Mail envoyé à {msg['To']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
