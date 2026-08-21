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

### 3. Injection de formules dans tous les exports CSV — **corrigé**
- **Où** : `fiskr/api.py`, `_csv_response` — tous les exports (alertes, journal
  d'audit, résultats de campagne, fiches listées) écrivaient les valeurs
  **telles quelles**.
- **Pourquoi c'est exploitable** : une cellule commençant par `=`, `+`, `-`,
  `@`, une tabulation ou un retour chariot est interprétée comme une **formule**
  par Excel et LibreOffice. Un nom de client ou un commentaire de décision
  valant `=cmd|'/c calc'!A1` s'exécute sur le poste de l'auditeur qui ouvre
  l'export. Le contenu vient des listes ingérées, des fichiers clients importés,
  des messages de paiement et des commentaires d'analystes — rien de tout cela
  n'est sous le contrôle du produit.
- **Correctif** : `_csv_neutralise` préfixe d'une apostrophe toute cellule
  commençant par un caractère dangereux, en-têtes compris, sur **tous** les
  exports. 22 tests.

### 4. Oracle temporel sur l'existence d'un compte — **corrigé**
- **Où** : `fiskr/api.py`, `POST /api/auth/login` — l'empreinte du mot de passe
  n'était calculée que si le compte existait.
- **Pourquoi c'est exploitable** : les deux chemins étaient séparés par le coût
  d'une dérivation de clé (PBKDF2, 100 000 itérations), mesurable de
  l'extérieur — de quoi énumérer les identifiants valides sans jamais
  s'authentifier.
- **Correctif** : le chemin « compte inexistant » vérifie contre une empreinte
  **factice**, calculée une fois au démarrage à partir d'un mot de passe
  aléatoire. Les deux chemins paient le même calcul.

### 5. Aucun plafond de taille sur les téléversements — **corrigé**
- **Où** : les six endpoints acceptant un fichier. Le filtrage transactionnel
  lisait le message **entier en mémoire** (`await file.read()`) ; les cinq
  autres recopiaient sur le disque avec `shutil.copyfileobj`, sans limite.
- **Pourquoi c'est exploitable** : un seul fichier suffisait à épuiser la
  mémoire du worker ou le disque de l'instance. La boîte CFT surveillée est
  alimentée par un système amont : l'entrée n'est pas toujours celle d'un
  opérateur attentif.
- **Correctif** : trois helpers bornés, avec des plafonds différenciés par
  nature de dépôt — une liste officielle est volumineuse par construction
  (`SDN_ADVANCED.XML` de l'OFAC pèse 126 Mo, mesuré), une pièce jointe d'alerte
  non : **liste 512 Mo, clients 64 Mo, pièce 32 Mo, message 8 Mo**. Au-delà, un
  413, et rien de partiel ne reste sur le disque. Un test AST vérifie que
  **toute** fonction d'endpoint prenant un `UploadFile` passe par l'un des
  helpers.

### 6. Amplification d'entrée du filtrage transactionnel — **corrigé**
- **Où** : `fiskr/transactions.py`, `screen_payment_message` — le nombre de
  parties criblées dans un message n'était pas borné, ni la longueur de leurs
  noms.
- **Pourquoi c'est exploitable** : un message accepté jusqu'au plafond de
  téléversement porte **56 678 transactions** (mesuré sur un pain.001 minimal
  réel, 148 o par transaction), donc autant de parties. À ~75 ms par partie
  (415 candidats en moyenne par seau phonétique de production, ~180 µs la
  comparaison), c'est **plus d'une heure** de calcul pour un message, dans une
  requête synchrone, avec autant de lignes écrites au journal d'audit. La
  requête échouait déjà sur le délai d'attente — **après** avoir brûlé ce temps.
  Par ailleurs, Damerau-Levenshtein étant linéaire en la longueur du nom criblé,
  un seul nom de 20 000 caractères valait 34 s de calcul.
- **Correctif** : refus immédiat (413) au-delà de 500 parties, **avant** toute
  lecture de réglage et tout calcul, avec le compte et la marche à suivre ; et
  les noms sont bornés à 1 000 caractères — exactement ce que la base sait
  stocker, sept fois le plafond `Max140Text` d'ISO 20022, et trois fois le plus
  long nom réel du corpus de production (310 caractères, mesuré).

### 7. Le plafond de dépôt ne bornait pas la mémoire des connecteurs — **corrigé**
- **Où** : `fiskr/ingest.py` — trois connecteurs officiels au format JSON (DGT,
  Consolidated Screening List, Banque mondiale) lisaient le fichier d'un seul
  bloc (`json.load`), et deux lecteurs matérialisaient tout le contenu
  (`f.read().splitlines()` pour le ConList britannique, `parser.feed(f.read())`
  pour les pages HTML d'alerte).
- **Pourquoi c'est exploitable** : le plafond de téléversement (512 Mo pour une
  liste) borne le FICHIER, pas l'arbre d'objets Python qu'il produit — et
  celui-ci pèse plus, d'un facteur qui dépend du contenu. Mesuré avec
  `tracemalloc` : ×4,0 sur des entrées CSL réalistes, ×6,0 sur des chaînes
  courtes distinctes, ×15,1 sur des objets minuscules `{"a":1}`, ×16,1 sur des
  listes vides. Soit **2 à 8 Go** pour un seul import. Sur un hébergement
  mutualisé le processus meurt ; sous Passenger, le worker web entier tombe.
- **Correctif** : `TAILLE_MAX_LECTURE_BLOC` (64 Mo) borne ce qu'un connecteur
  **sans lecture en flux** accepte — trois fois la marge du plus gros fichier
  réel (la CSL pèse une vingtaine de mégaoctets), et le pire cas adverse tombe
  à environ un gigaoctet. Le refus dit quoi faire. Les deux autres lecteurs
  sont passés en flux plutôt que bornés.

### 8. Aucune borne sur ce qu'une source distante peut faire lire — **corrigé**
- **Où** : `fiskr/sync.py` — `http_get_text` rendait `response.text` sans
  aucune borne (flux RSS de presse négative, scraping EUR-Lex), et
  `download_to_file` écrivait sur le disque tant que l'hôte envoyait.
- **Pourquoi c'est exploitable** : les points 5 et 7 ont plafonné tout ce qui
  entre par **téléversement**. Or une liste entre par deux portes — l'écran
  d'import, ou l'URL d'une source configurée — et c'est le même artefact, lu
  par le même analyseur, écrit dans le même répertoire de travail. La seconde
  porte n'avait rien. La taille de ce qui est lu était décidée **entièrement
  par le bout d'en face** : un portail officiel détourné, un flux RSS servi par
  un tiers, ou simplement un hôte qui déraille, suffisait à épuiser la mémoire
  du processus de synchronisation ou à remplir le disque. Sur un hébergement
  mutualisé, un disque plein est une interruption de service.
- **Correctif** : `fiskr/limites.py` tient les plafonds en **un seul endroit**,
  et celui du téléchargement est *dérivé* de celui du téléversement des listes
  — pas recopié à côté : deux nombres qui devraient être égaux et qu'on
  maintient à la main finissent toujours par diverger. Une page de texte a son
  propre plafond, bien plus bas (32 Mo, contre 12 Ko mesurés pour un vrai flux
  Google News sur un nom).
- **Ce que le correctif a demandé de plus qu'un test de taille** : `client.get()`
  met **tout le corps en mémoire avant de rendre la main**. Un contrôle posé
  après coup ne protège de rien — quand on peut mesurer, la mémoire est déjà
  dépensée. `http_get_text` lit désormais par blocs et refuse au premier bloc de
  trop ; un test vérifie que le flux s'arrête bien là, et pas après. Un refus de
  téléchargement efface ce qui était déjà sur le disque (même règle que
  `copier_televersement`) et n'est **jamais** retenté : la réponse ne
  rétrécira pas, et la rejouer paierait deux fois ce qu'on vient de refuser.

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

## Balayage refait, sans nouveau défaut trouvé

- **Interpolations non échappées du frontal** : balayage systématique des
  gabarits de `app.js`. Tout ce qui vient des données passe par `escapeHtml`,
  par `textContent`, ou provient d'un catalogue statique du code. `showToast`
  échappe son message.
- **XXE et bombe entités XML** : `ElementTree` refuse une entité externe
  (entité indéfinie), et expat plafonne l'expansion d'entités — la « billion
  laughs » est refusée dès le septième niveau.
- **Parseur ISO 20022** : quinze messages malformés (vide, tronqué, encodage
  invalide, éléments manquants, nom de 100 Ko) — refus propres en `ValueError`,
  aucun plantage.
- **File de travaux** : `SELECT ... FOR UPDATE SKIP LOCKED` sous PostgreSQL,
  garde `WHERE status='QUEUED'` et arbitrage par `rowcount` sur le chemin de
  repli. Deux démons ne peuvent pas prendre le même travail.
