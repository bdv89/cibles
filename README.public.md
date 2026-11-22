# Inventaire cibles

Web app d'inventaire des cibles de sputtering/dépôt (FastAPI + SQLite + FTS5 + UI statique).
Une ligne = une cible physique. Voir [docs/inventaire-cibles.md](docs/inventaire-cibles.md) et [CLAUDE.md](CLAUDE.md).

```bash
cd server_display
python -m venv venv && venv/bin/pip install -r requirements-server.txt pytest httpx
venv/bin/python run.py --db inventory.sqlite --port 8001
venv/bin/python -m pytest tests/
```

Les données (base SQLite, SDS/COA/bons de commande, listing xlsx) ne font pas partie du dépôt.
Scripts d'exploitation spécifiques au site : `server_display/scripts/mirror_documents.example.cmd`.
