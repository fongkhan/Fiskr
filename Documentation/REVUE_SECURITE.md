# Revue de sécurité — Fiskr

Revue défensive du code et de la documentation. Deux vulnérabilités
exploitables corrigées, plusieurs points durcis, et l'état des choix de
sécurité déjà en place. Chaque correctif est couvert par un test
(`tests/test_security.py`).

## Vulnérabilités corrigées

### 1. XSS stocké dans la modale d'audit — **corrigé**
- **Où** : `fiskr/static/app.js`, `viewAuditLogDetail` — le motif de hard
  match (`tree.hard_match_details`) et les descriptions d'ajustement
  (`tree.adjustments.*.description`) étaient interpolés **bruts** en
  `innerHTML`.
- **Pourquoi c'est exploitable** : ces libellés incorporent des champs des
  données **criblées** (n° et pays de passeport, type d'« autre identifiant »,
  libellés de pays), qui proviennent d'un CSV, d'un message ISO 20022 ou d'un
  webhook entrant — donc potentiellement forgés. Un profil piégé injecte du
  script qui s'exécute dans le navigateur d'un analyste (CSP en
  `script-src 'unsafe-inline'` : la charge s'exécute).
- **Correctif** : les trois interpolations passent par `escapeHtml`. Le jumeau
  côté serveur (dossier imprimable) échappait déjà via `html.escape` — vérifié.
  Garde-fou statique contre régression.

### 2. Traversée de chemin à l'ingestion — **corrigé**
- **Où** : `fiskr/api.py`, `POST /api/ingest` — `temp_dir / file.filename`
  avec le nom de téléversement **brut**.
- **Pourquoi c'est exploitable** : un nom `../../passenger_wsgi.py` ou
  `/etc/cron.d/x` fait écrire le fichier **hors** du répertoire prévu —
  primitive d'écriture arbitraire pour tout utilisateur authentifié
  (potentiellement une exécution de code en écrasant `passenger_wsgi.py`).
- **Correctif** : `safe_upload_filename` (helper partagé dans `database.py`)
  réduit le nom à un basename sûr (pas de séparateur, pas de `..`, pas de nom
  absolu, pas de fichier caché), préfixé d'un jeton unique. Les deux autres
  points d'upload (batch, pièce d'exclusion) sont unifiés sur ce helper.

## Durcissement

- **Secrets par défaut** : `SECRET_KEY` et `ADMIN_PASSWORD` ont une valeur par
  défaut dans le code (une clé de signature publique permet de forger un cookie
  de session admin). Le démarrage journalise désormais un **WARNING** appuyé
  quand l'une d'elles est restée au défaut, et le diagnostic
  (`/api/diagnostic/jobs`) liste les secrets non définis — **par nom
  seulement**, jamais la valeur. Action attendue à la mise en production :
  définir `SECRET_KEY` et `ADMIN_PASSWORD` en variables d'environnement.

## Choix de sécurité déjà en place (vérifiés, corrects)

- **Mots de passe** : PBKDF2-HMAC-SHA256, 100 000 itérations, sel de 16 octets,
  comparaison en temps constant.
- **Sessions** : JWT signé (HS256), cookie `HttpOnly`, `SameSite` et `Secure`
  réglables par le déploiement. Anti-brute-force : verrouillage temporaire
  après N échecs, tracé au journal d'administration.
- **En-têtes** : `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Permissions-Policy`, CSP avec `frame-ancestors 'none'`.
- **Rôles** : auditeur en lecture seule (toute écriture refusée), clés d'API
  au moindre privilège (rôle admin interdit), 4-yeux sur les décisions.
- **Moteur de règles anti-FP** (`fprules.py`) : exécute du Python via `exec`.
  C'est un **choix produit assumé et documenté** — pas un bac à sable —
  réservé au rôle `rules`/admin, avec validation 4-yeux et journal immuable
  des modifications. Le `__builtins__` est restreint mais ne constitue PAS un
  isolement : quiconque a le rôle `rules` peut exécuter du code arbitraire, par
  conception. À garder à l'esprit lors de l'attribution de ce rôle.

## Points d'attention résiduels (non bloquants)

- **CSP `script-src 'unsafe-inline'`** : nécessaire aux gestionnaires inline
  du tableau de bord ; affaiblit la défense en profondeur contre le XSS (d'où
  l'importance de l'échappement systématique, désormais tenu). Un durcissement
  futur (nonces) demanderait de retirer les handlers inline — chantier à part.
- **Webhooks sortants** (`notify.py`) : POST vers des URL configurées par un
  admin. Surface SSRF théorique mais réservée à l'admin ; à surveiller si la
  configuration des webhooks était un jour déléguée à un rôle moindre.
- **Clés d'API de production divulguées en conversation** : les deux premières
  clés d'auditeur communiquées lors du diagnostic à distance doivent être
  **révoquées** côté exploitation.
