# Politique d'acces — Inventaire chimique

> Outils : les commandes `tools/...` se lancent depuis `04_picture_to_CAS/` avec `tools/.venv` ;
> celles du pipeline OCR (`main.py`, `INBOX/`, `DONE/`...) depuis `tools/ocr_pipeline/`. Voir `../../tools/README.md`.

> ⚠️ **Document hérité de l'outil chimie d'origine.** Ce dépôt est le fork
> **inventaire de cibles** : les fonctions chimie (CAS, PubChem, phrases H/P, ICPE) en ont été
> retirées, et le serveur tourne sur le port 8001. Vérifier chaque affirmation avant de
> l'appliquer ici. Référence à jour : [inventaire-cibles.md](inventaire-cibles.md).


> Version : 1.1
> Date : 2026-06-19
> Reference ISO 9001 : 7.5.3 (Maitrise des informations documentees)

---

## 1. Niveaux d'acces

Le systeme distingue la **lecture** et l'**ecriture**, configurables au demarrage du serveur.

| Niveau | Portee | Authentification | Configuration |
|--------|--------|-----------------|---------------|
| **Lecture publique** | Consultation inventaire, recherche, FDS | Aucune | `--read-password` non defini (defaut) |
| **Lecture protegee** | Idem lecture publique | HTTP Basic Auth (mot de passe lecture) | `--read-password "motdepasse"` |
| **Ecriture (edition)** | Ajout, modification, suppression de fiches, upload FDS/videos | **Aucune** — ouverte | Choix metier intranet (voir ci-dessous) |

### Ecriture ouverte (decision metier)

Par choix metier, l'ecriture est **ouverte sans mot de passe** sur l'intranet : n'importe
quel poste peut ajouter, modifier, supprimer une fiche ou uploader une FDS. `--edit-password`
n'a plus d'effet de blocage et `GET /api/edit-enabled` renvoie toujours `true`. **Risque
accepte** (intranet de confiance). Garde-fous conserves : rate-limit (10 req/min/IP sur
create/update/delete), journal d'audit `edit_log`, soft-delete, backups quotidiens.

> **Re-verrouiller plus tard** : restaurer `Depends(check_auth)` sur les routes d'ecriture
> (`app/routes/inventory.py`, `app/routes/files.py`) et faire renvoyer `settings.editing_enabled`
> a `/api/edit-enabled`. La fonction `check_auth` reste presente dans `app/dependencies.py`.

### Mode recommande (production)

```bash
python run.py --db inventory.sqlite --rate-limit 60
```

---

## 2. Mecanisme d'authentification

L'authentification ne concerne plus que la **lecture protegee** (optionnelle). L'ecriture
est ouverte (voir section 1).

- **Protocole** : HTTP Basic Authentication (RFC 7617)
- **Transmission** : Header `Authorization: Basic <base64(user:password)>`
- **Realm lecture** : `Inventory Read`
- **Nom d'utilisateur** : ignore (seul le mot de passe est verifie)
- **Implementation** : `server_display/app/dependencies.py`

### Endpoints et niveau d'acces

| Endpoint | Methode | Niveau requis |
|----------|---------|---------------|
| `/api/search` | GET | Lecture (publique sauf `--read-password`) |
| `/api/columns` | GET | Lecture |
| `/api/export` | GET | Lecture |
| `/sds/*` | GET | Lecture |
| `/api/inventory` | POST | Aucune (ouvert) |
| `/api/inventory/<id>` | PUT | Aucune (ouvert) |
| `/api/inventory/<id>` | DELETE | Aucune (ouvert) |
| `/api/upload/*` | POST | Aucune (ouvert) |

### Reponses d'erreur

| Code HTTP | Situation |
|-----------|-----------|
| 401 Unauthorized | Lecture protegee : mot de passe manquant/incorrect (si `--read-password` defini) |
| 429 Too Many Requests | Limite de debit depassee |

---

## 3. Limitation de debit (Rate Limiting)

Le systeme impose des limites de debit par adresse IP pour prevenir les abus.

| Endpoint | Limite | Implementation |
|----------|--------|---------------|
| `/api/search` | 60 requetes/minute | slowapi |
| `/api/export` | 10 requetes/minute | slowapi |
| `/api/inventory` (POST/PUT/DELETE) | 10 requetes/minute | slowapi |

- **Cle de limitation** : adresse IP du client (via `X-Forwarded-For` ou `request.client.host`)
- **Configuration** : `--rate-limit N` (defaut : 60 req/min)
- **Bibliotheque** : slowapi (wrapper FastAPI autour de Limits)

---

## 4. Tracabilite des acces

### 4.1 Journal des modifications (edit_log)

Chaque operation d'edition est enregistree dans la table `edit_log` :

| Champ | Contenu |
|-------|---------|
| `id` | Identifiant auto-increment |
| `inventory_rowid` | Ligne inventaire concernee |
| `action` | Type : `create`, `update`, `delete` |
| `field_changes` | JSON des modifications (`{champ: [ancienne_valeur, nouvelle_valeur]}`) |
| `user_ip` | Adresse IP de l'editeur |
| `timestamp` | Horodatage automatique (UTC) |

### 4.2 Metriques de consultation

Quatre tables dans `metrics.sqlite` tracent l'activite :

| Table | Donnees tracees |
|-------|----------------|
| `metrics_daily` | Nombre de requetes par endpoint et par jour |
| `metrics_searches` | Termes de recherche avec frequence |
| `metrics_sds` | Acces aux FDS par numero CAS |
| `metrics_ips` | Adresses IP uniques par jour |

### 4.3 Detection IP client

L'adresse IP est extraite par ordre de priorite :
1. Header `X-Forwarded-For` (premier IP, pour les proxies)
2. `request.client.host` (connexion directe)

---

## 5. Protection des donnees

### 5.1 Integrite

- **Versioning optimiste** : chaque fiche a un champ `version` incremente a chaque modification, evitant les ecrasements concurrents
- **Soft delete** : la suppression marque `deleted_at` sans effacer les donnees, permettant la recuperation

### 5.2 Sauvegardes

- **Frequence** : quotidienne, declenchee automatiquement avant la premiere edition du jour
- **Retention** : 7 derniers backups conserves (configurable via `--backup-keep`)
- **Emplacement** : `server_display/backups/` (configurable via `--backup-dir`)
- **Format** : copie integrale de `inventory.sqlite`
- **Test** : `tools/.venv/Scripts/python tools/audit/test_backup_restore.py --production-db inventaire_cibles/server_display/inventory.sqlite` (verification integrite + simulation restauration)

### 5.3 En-tetes de securite

Le serveur ajoute automatiquement les en-tetes HTTP suivants :

| En-tete | Valeur | Protection |
|---------|--------|-----------|
| `X-Content-Type-Options` | `nosniff` | MIME sniffing |
| `X-Frame-Options` | `DENY` | Clickjacking |
| `X-XSS-Protection` | `1; mode=block` | XSS reflechis |

---

## 6. Limites connues et recommandations

| Limite actuelle | Risque | Recommandation |
|-----------------|--------|----------------|
| Ecriture ouverte sans authentification | Tout poste intranet peut modifier | Acceptable (intranet de confiance) ; re-verrouiller via `check_auth` si exposition externe |
| Pas de HTTPS natif | MITM sur reseau non securise | Aligne sur la pratique IT ; cert CA d'entreprise (AD CS) si besoin futur |
| Pas de gestion multi-utilisateurs | Pas de distinction entre editeurs | Tracabilite par IP via `edit_log` ; suffisant pour equipe reduite |

---

## 7. Historique des revisions

| Version | Date | Modification |
|---------|------|-------------|
| 1.0 | 2026-04-13 | Creation initiale pour audit ISO 9001 |
| 1.1 | 2026-06-19 | Ecriture ouverte sans mot de passe (choix metier intranet) ; TLS non retenu (aligne IT) |
