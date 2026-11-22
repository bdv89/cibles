# CLAUDE.md

> **Depuis le 2026-09-30, ce dossier ne contient plus que le serveur.** Le pipeline OCR
> (`main.py`, `INBOX/`...) et les outils ponctuels (audits, enrichissement, SDS) sont dans
> `../tools/` — voir `../tools/README.md`. Commandes du pipeline : depuis `tools/ocr_pipeline/` ;
> autres outils : depuis `04_picture_to_CAS/`, avec `--db inventaire_cibles/server_display/inventory.sqlite`.

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Inventaire Cibles** — inventory web app for physical **sputtering/deposition targets** (metal
"cibles"). This is a fork of a chemical-label OCR tool, pivoted to a target inventory: the chemistry
layer (CAS, PubChem, GHS, ICPE, chemical SDS pipeline) has been removed and the generic server stack
(FastAPI + SQLite + FTS5 + web UI) reused.

- **1 row = 1 physical target** (~1078 items since the 2026-07-13 reload), loaded from
  `Listing toutes cibles.xlsx` (sheet `test`).
- **Source of truth = the web app** after the initial import (the import is one-shot/destructive).
- No auth on writes (rate-limited only); optional read password. Fine on LAN — revisit if exposed.

> 📄 **Primary detailed doc: [docs/inventaire-cibles.md](docs/inventaire-cibles.md)** — schema, endpoints,
> search parser, UI features, document linking, commands. Read it first.

**Core scripts:**

| Script | Description |
|--------|-------------|
| `server_display/scripts/import_cibles_xlsx.py` | Import xlsx → SQLite (position mapping, normalize `usure`/`forme`, consolidate notes → `remarque`, `emplacement` from reference, FTS + triggers). One-shot destructive. |
| `server_display/scripts/link_documents.py` | Auto-link SDS (by material) and COA certificates (by reference) → `material_sds` / `coa_link`. Re-runnable; preserves `manual` links and skips `auto*` rows a `manual` row already covers (`drop_manual_duplicates`), otherwise an uploaded COA named after its reference would show up as `COA ×2`. Bons de commande are **never** auto-linked. |
| `server_display/scripts/mirror_documents.cmd` | Daily off-server duplicate of `SDS_cibles/`, `COA_cibles/`, `BC_cibles/` and `backups/` to `\\<serveur>\<partage>\inventaire_cibles` (robocopy `/MIR`). Scheduled task `CiblesDocsMirror`, 20:00. **One-way, server → share.** Verify SYSTEM can reach the UNC path *before* registering the task — see docs "Duplicata". |
| `server_display/scripts/send_low_stock_email.py` | Daily low-stock digest: e-mails categories below their `category_thresholds` (reuses `app.categories_core.aggregate_categories`). Read-only; SMTP via env vars. **Live since 2026-07-22**: scheduled task `CiblesLowStockAlert` (SYSTEM, daily 08:00, `LastTaskResult=0`), relay = M365 direct send, **no credentials**. See docs "Alerte stock bas". |
| `server_display/run.py` | FastAPI server entry point (uvicorn). |

**Directories:**

| Directory | Purpose |
|-----------|---------|
| `server_display/` | FastAPI app, static UI, SQLite DB, venv |
| `server_display/SDS_cibles/` | Safety data sheets (per material). Uploads land in `Fiche MSDS Cible/`. |
| `server_display/COA_cibles/` | Analysis certificates (per lot). The 10 subfolders match `equipement`, **not** `fournisseur` (empty on 34% of rows) — uploads land flat in `Certificat Analyse Cible/`, no auto-filing. |
| `server_display/BC_cibles/` | Purchase orders, keyed by `bon_commande`. Flat, upload-only. |
| `server_display/backups/` | Daily DB backups |

Legacy chemistry scripts (`find_sds-master/`, `validate_cas.py`, OCR pipeline…) were moved out on
2026-09-30 (shared copies in `../tools/`, this folder's duplicates in `../_archive/inventaire_cibles_racine/`).
What remains here is **inert**: PubChem helpers and SDS PDF parsing in `app/utils.py`.

## Quick Start

```bash
# venv (already created): server_display/venv
cd server_display && venv/Scripts/python.exe -m pip install -r requirements-server.txt pytest httpx

# --- Run the next two FROM THE REPO ROOT: their default paths are relative to it. ---
# link_documents.py launched from server_display/ finds 0 files and wipes every auto link.

# Import inventory from xlsx (one-shot, destructive — see "Re-import" below)
python server_display/scripts/import_cibles_xlsx.py \
    --xlsx "Listing toutes cibles.xlsx" --sheet test --db server_display/inventory.sqlite

# Link SDS / COA documents (re-runnable; keeps manual links)
python server_display/scripts/link_documents.py

# Start server for local dev (pick a free port — 8000/8001 taken in prod)
cd server_display && venv/Scripts/python.exe run.py --db inventory.sqlite --port 8042
# Production: NSSM service `InventoryCibles` on port 8001, served at http://<serveur>/cibles/
# (dashboard tile in serveur_automate/dashboard/services.yaml; see docs/inventaire-cibles.md "Déploiement")

# Read-protected (writes stay open):
cd server_display && venv/Scripts/python.exe run.py --db inventory.sqlite --read-password "lecteur"

# Tests
cd server_display && venv/Scripts/python.exe -m pytest tests/
```

## Server CLI (run.py)

| Argument | Default | Description |
|----------|---------|-------------|
| `--db` | inventory.sqlite | Inventory database path |
| `--host` | 0.0.0.0 | Server host |
| `--port` | 8000 | Server port |
| `--sds-root` | server_display/ | Document files root (serves SDS_cibles/, COA_cibles/) |
| `--metrics-db` | server_display/metrics.sqlite | Metrics database |
| `--read-password` | (empty) | Password for reading (empty = public). Writing is always open. |
| `--rate-limit` | 60 | Requests/minute per IP |
| `--backup-dir` | server_display/backups | Backup directory |
| `--backup-keep` | 7 | Backups to retain |

## Database Schema (server_display/inventory.sqlite)

| Table | Purpose |
|-------|---------|
| `inventory` | Main data, 1 row/target (+ `version`, `deleted_at`) |
| `inventory_fts` | FTS5 external-content index (triggers keep it in sync) |
| `edit_log` | Audit log (rowid, action, field_changes JSON, user_ip, timestamp) |
| `category_thresholds` | Min-stock threshold per `materiau|forme|dimension` category (3-segment key) |
| `material_sds` | SDS links by material (1→N) |
| `coa_link` | Certificate links by reference (N↔N) |
| `bc_link` | Purchase-order links by `bon_commande` (1→N, up to 20 targets per order) |

**`inventory` columns:** `materiau, reference, date_achat, prix, purete, equipement, usure,
particularite, fournisseur, hauteur_mm, forme, longueur, largeur, hauteur, diametre, epaisseur,
bon_commande, emplacement, remarque`.

- `usure` ∈ {Neuve, Utilisée, Finie}; `forme` ∈ {Circulaire, Rectangulaire, Cylindrique, ""}.
- FTS-indexed: `materiau, reference, forme, emplacement, fournisseur, remarque`.
- Document link `source`: `auto`, `auto_element`, `auto_ref`, `auto_range`, `manual`.
- `emplacement` is **derived from the reference**: the token right before the trailing number
  (`Ag-TM2-ABBE-005` → `ABBE`). Special case: the **AGC family always collapses to `AGC`** whatever
  the sub-code (`Ag-P.AGC_P_002`, `Ag-P.AGC_G_002` → `AGC`, not `P`/`G`).
  ⚠️ This rule is duplicated in **3 copies that must stay in sync**:
  `app/utils.py::emplacement_from_reference`, `scripts/import_cibles_xlsx.py` (same function), and
  `static/edit.js::emplacementFromRef` (live form fill). Test: `tests/test_cibles.py`.

## Key Technical Details

- **Smart search** (`app/utils.py::build_match_query` + `load_search_vocab`): routes known material
  tokens → `materiau:"X"` and shapes → `forme:"Y"` (exact), unknown → multi-column prefix; bm25 weights
  materiau×10/forme×5. Accent-insensitive, shape abbreviations (rect/circ/cyl).
- **Document linking**: SDS are per-material (alias table + per-element fallback for alloys → 300 links);
  COA are per-reference (exact + range expansion → 64 auto, rest manual); BC are per-`bon_commande`
  and **never auto-linked**. Measured 2026-08-21: 997/1079 rows have an SDS, 61 have a COA, and
  **122 files on disk are linked to nothing** (114 COA + 8 SDS) — most COA are named by material+date
  rather than by reference, which no matching rule can exploit. See docs.
- **Document upload** (`POST /api/document-upload`): multipart, `kind` ∈ `sds|coa|bc` + `key` + `file`.
  Writes the file **then** the link (the reverse order would leave a dead link); on link failure the
  file is removed. Filename reduced to a basename and sanitised, extension whitelisted **and
  recomposed** from the whitelist, size counted in streaming with abort on the first excess byte,
  never overwrites (` (2)`). ⚠️ The effective cap is **nginx's `client_max_body_size 100m`** on
  `/cibles/`, which answers 413 before FastAPI — the front handles that status separately because the
  body is not JSON.
- **`bon_commande` is not in the FTS index**, so `?q=<n° de bon>` finds nothing. The Documents page
  works around it by linking each of the order's references instead. Only 11.5% of rows carry one.
- **Re-import safety**: `import_cibles_xlsx.py` drops only `inventory`/`inventory_fts`/`edit_log`.
  Document links live in `material_sds`/`coa_link`, keyed by **business keys** (`materiau`/`reference`),
  never by `rowid` — so they survive a reload untouched, `manual` ones included.
  ⚠️ **Never delete `inventory.sqlite`**: "wiping the DB" means re-running the import *in place*.
  Deleting the file would destroy the link tables and force redoing all manual linking. What *is* lost
  on re-import: every in-app edit (usure, remarks, emplacement) and `edit_log`. Full procedure +
  orphan audit: docs/inventaire-cibles.md "Ré-import".
- **Serving**: all documents served via `GET /sds/{path}` (path-safe `safe_path`, root = `sds_root`).
- **Stock thresholds** (`app/routes/categories.py`): category = `materiau|forme|dimension` (dimension
  derived: `Ø{diametre}` for round, `{longueur}×{largeur}` for rectangular). Available `count` **excludes
  `Finie`** targets (a worn-out target no longer counts). Aggregation done in Python so the derived
  dimension is the single source of the key.
- **UI**: all columns toggleable (persisted in `localStorage` key `columnVisibilityCibles`); usure badge
  is a click-to-cycle button; inline remark editing; document badges (SDS/COA/BC) + `＋` manager modal
  with **Lier** *and* **Ajouter** per type; dark mode (`theme.js`); `/` focuses the search box,
  `Échap` clears it; Catégories page (Matériau + Forme + Dimension) for stock thresholds; **Documents
  page** (`documents.html`) auditing coverage — dead links, duplicates, orphan files, missing SDS/COA/BC
  — with deep links `./?q=<key>` back into the inventory (`app.js::init` reads `?q=` on boot).
- **Layout = 5 bands, one responsibility each** (`body.app-screen`, index page only — `styles.css` is
  shared with `categories.html`/`metrics.html`, which keep normal page scrolling):
  ① site header (identity + **app navigation**) ② `.controls` act ③ `.columns-bar` configure
  ④ `.table-wrap` data + sort ⑤ `.statusbar` locate + **paginate**. Everything fits one screen; only
  the table scrolls. Do not merge ① and ⑤: one changes *page*, the other changes *slice of data*.
- **Four CSS traps that cost real debugging time** — all documented in
  [docs/inventaire-cibles.md](docs/inventaire-cibles.md) § Interface:
  1. `position: sticky` on `th` **never engages** if the `overflow: auto` ancestor has no bounded
     height. Keep `.table-wrap` sized by flex (or `max-height`).
  2. **Never put a `transition` on a property fed by a theme variable** (`background: var(--bg-2)`):
     it freezes on the previous theme after a toggle (measured contrast 1.38 — invisible glyph).
     `theme.js` neutralises transitions during the switch via `.theme-switching`.
  3. **`table-layout: fixed` + per-column widths** decouple table width from data quality: otherwise
     one mistyped cell widens a column for all 200 rows (measured: 314 px for 3-character content).
  4. A flex item with **`margin: 0 auto` does not stretch** — it sizes to its content. On mobile the
     shell grew to 1225 px and `overflow: hidden` clipped 813 px of table *unreachably*. Needs an
     explicit `width: 100%`, not just `min-width: 0`.
- **Asset cache-busting**: JS/CSS are versioned (`app.js?v=32`, `edit.js?v=8`, `coa.js?v=3`,
  `styles.css?v=37`, `cibles.css?v=11`, `documents-app.js?v=1`, `theme.js?v=2`). **Bump the `?v=`
  whenever you edit one**, or browsers keep serving the cached copy and the fix looks like it did
  nothing. `styles.css` is referenced by the **four** pages (index, categories, documents, metrics) —
  bump all four, and keep `cibles.css` identical across the three that use it (it once sat at `?v=3`
  on categories vs `?v=10` on index, so Catégories served a stale sheet for seven versions).
- **Restart rule — `static/` yes, Python no.** FastAPI re-reads `static/` per request, so HTML/CSS/JS
  changes go live with no restart. **Any change under `app/` needs `Restart-Service InventoryCibles`
  (elevated).** The trap: ship a new page plus a new API route and the page appears immediately while
  its endpoint 404s, which reads as a front-end bug. That is exactly what happened when
  `documents.html` shipped ahead of `GET /api/documents-audit`.

## Development Context & Philosophy

### Default Approach
- Evolutionary POC: get end-to-end working ASAP
- Simple modules and standalone scripts
- Deterministic and traceable implementations
- Reasonable defaults; minimal configuration
- Clear logging over sophisticated error handling

### Solution Hierarchy
1. Remove complexity or unnecessary step
2. Repurpose an existing tool in the stack
3. Minimally modify existing code
4. Add new code (last resort)

### Forbidden Practices
- No premature abstractions or micro-services
- No heavy configuration for stable parameters
- No exotic error handling for rare edge cases
- No premature optimization before clear profiling

## Documentation

| Document | Content |
|----------|---------|
| [docs/inventaire-cibles.md](docs/inventaire-cibles.md) | **Primary** — schema, API, search, UI, document linking, commands |
| [docs/server-operation.md](docs/server-operation.md) | Server setup, scheduled task, backups (generic parts still apply) |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common errors and solutions |

Note: other `docs/*.md` (ocr-pipeline, sds-automation, database-enrichment…) describe the original
chemistry tool and are kept for lineage only — not applicable to the cibles inventory.
