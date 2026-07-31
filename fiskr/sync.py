"""
Synchronisation automatique des sources de sanctions.

Deux collecteurs sont fournis, executables manuellement (dashboard / API) ou
chaque matin par le planificateur :

1. OFAC  : telechargement du fichier SDN_ADVANCED.XML officiel, ingestion en
           snapshot, delta par rapport a la liste active, puis remplacement
           (les anciens snapshots OFAC passent en SUPERSEDED : les ajouts,
           modifications et suppressions du delta sont appliques au cache).
2. EURLEX: lecture du Journal Officiel de l'UE du jour, detection des actes
           mentionnant "mesures restrictives", scraping heuristique des listes
           (Individus, Entites, Navires, Aeronefs), puis fusion incrementale
           avec la liste EU active (le JO du jour amende la liste, il ne la
           remplace pas) et delta.

Chaque execution produit un rapport de suivi (table sync_reports) affiche dans
l'application et envoye par email si un serveur SMTP est configure (.env).
"""
import os
import re
import uuid
import hashlib
import logging
import smtplib
import unicodedata
from datetime import datetime, date
from email.mime.text import MIMEText
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from fiskr.config import config, PROJECT_ROOT
from fiskr.quality import evaluate_and_clean
from fiskr.delta import calculate_delta
from fiskr.ingest import (
    parse_ofac_advanced_xml, parse_dgt_gels_json, parse_eu_fsf_xml, parse_un_consolidated_xml,
    parse_pep_targets_csv, parse_ofsi_conlist_csv, parse_seco_xml, parse_seco_opensanctions_csv,
    parse_opensanctions_simple_csv,
    parse_ofac_consolidated_xml, parse_csl_json, CSL_DEFAULT_EXCLUDED_SOURCES,
    parse_canada_sema_csv, parse_dfat_consolidated,
    parse_hk_sfc_alert_list, parse_amf_blacklist, parse_worldbank_debarred_json
)
from fiskr.names import parse_individual_name, ensure_parsed_name
from fiskr.database import Snapshot, WatchlistEntity, SyncReport, compute_checksum
from fiskr.sources import OPENSANCTIONS_BY_KEY, opensanctions_default_url
from fiskr.settings import require_approval_enabled

logger = logging.getLogger("fiskr.sync")

DEFAULT_OFAC_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML"
# Second fichier publie par l'OFAC, meme format « Advanced » : la liste
# consolidee Non-SDN. Elle porte les regimes SANS gel total des avoirs, donc
# absents du fichier SDN — sanctions sectorielles (SSI), FSE, NS-MBS, PLC,
# MEU, CMIC. Publique, sans authentification.
DEFAULT_OFAC_NONSDN_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_ADVANCED.XML"
# Version anglaise du Journal Officiel : c'est la reference reglementaire retenue
DEFAULT_EURLEX_DAILY_URL = "https://eur-lex.europa.eu/oj/daily-view/L-series/default.html?ojDate={date}&locale=en"
DEFAULT_EURLEX_KEYWORD = "restrictive measures"

# Registre national des gels des avoirs (Direction generale du Tresor, API
# publique ENGEL sans authentification) : criblage obligatoire pour les
# etablissements assujettis francais (lignes directrices ACPR/DGT).
DEFAULT_DGT_URL = "https://gels-avoirs.dgtresor.gouv.fr/ApiPublic/api/v1/publication/derniere-publication-fichier-json"

# Liste consolidee officielle des sanctions financieres de l'UE (fichiers FSF,
# webgate FSD). {token} est le nom d'utilisateur cree lors de l'inscription
# gratuite sur le webgate de la Commission — a renseigner dans config.yaml.
DEFAULT_EU_FSF_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token={token}"

# Liste consolidee du Conseil de securite de l'ONU (publique, sans token)
DEFAULT_UN_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"

# Dataset PEP OpenSanctions (usage non commercial libre ; licence requise pour
# un usage commercial — opensanctions.org/licensing)
DEFAULT_PEP_URL = "https://data.opensanctions.org/datasets/latest/peps/targets.simple.csv"

# Liste consolidee UK OFSI (HM Treasury, publique, format 2022)
DEFAULT_OFSI_URL = "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv"

# Liste consolidee suisse (SECO). Deux voies au choix, reglees par
# `sync.seco.format` :
#   - "xml" (defaut) : export officiel SESAM de la Confederation. Source qui
#     fait foi, gratuite, sans licence, et qui porte la base legale suisse
#     (ordonnance RS) ainsi que les dates d'inscription.
#   - "opensanctions" : jeu `ch_seco_sanctions` agrege par OpenSanctions, format
#     plat targets.simple.csv. Utile si l'export officiel est indisponible, au
#     prix de la base legale et des dates d'acte — et sous licence
#     OpenSanctions pour un usage commercial.
DEFAULT_SECO_URL = (
    "https://www.sesam.search.admin.ch/sesam-search-web/pages/"
    "downloadXmlGesamtliste.xhtml?lang=en&action=downloadXmlGesamtlisteAction"
)
DEFAULT_SECO_OPENSANCTIONS_URL = (
    "https://data.opensanctions.org/datasets/latest/ch_seco_sanctions/targets.simple.csv"
)

# Consolidated Screening List du gouvernement americain (International Trade
# Administration). Agregat public et sans cle : son apport propre est le
# CONTROLE DES EXPORTATIONS (BIS Entity List, Denied Persons, Unverified,
# Military End User ; ITAR Debarred et Nonproliferation du Departement d'Etat).
DEFAULT_CSL_URL = "https://api.trade.gov/static/consolidated_screening_list/consolidated.json"

# Liste consolidee des sanctions autonomes canadiennes (SEMA), publiee en CSV
# par Affaires mondiales Canada. Le Canada designe de facon autonome, avec un
# perimetre qui ne recoupe ni celui de l'UE ni celui de l'OFAC.
DEFAULT_CANADA_URL = (
    "https://www.international.gc.ca/world-monde/assets/office_docs/international_relations-relations_internationales/sanctions/sema-lmes.csv"
)

# Liste consolidee australienne (DFAT) : sanctions onusiennes transposees ET
# sanctions autonomes australiennes. Publiee en XLSX et en CSV — l'extension
# de l'URL choisit le lecteur, la voie XLSX demandant le paquet openpyxl.
DEFAULT_DFAT_URL = "https://www.dfat.gov.au/sites/default/files/regulation8_consolidated.csv"

# LISTES D'ALERTE DE REGULATEURS. Ce ne sont PAS des listes de sanctions :
# une touche n'emporte aucune obligation de gel, c'est un signal de risque a
# instruire. Chacune a donc son propre type de liste, donc son propre seuil.
# Hong Kong n'ayant pas de regime de sanctions autonome, c'est cette liste-la
# qui apporte quelque chose qu'aucune autre source branchee ne porte.
DEFAULT_HK_SFC_URL = "https://www.sfc.hk/en/alert-list"
# Listes noires de l'AMF : marche domestique de l'etablissement.
DEFAULT_AMF_URL = "https://www.amf-france.org/fr/listes-noires-et-mises-en-garde"

# Exclusions de la Banque mondiale : ni gel, ni mise en garde — une exclusion
# des marches finances, prononcee pour fraude ou corruption averee. Criblee
# au titre du risque de contrepartie sur le financement de projets.
DEFAULT_WORLDBANK_URL = (
    "https://apigwext.worldbank.org/dvsvc/v1.0/json/APPLICATION/"
    "ADOBE_EXPERIENCE_MANAGER/FIRM/SANCTIONED_FIRM"
)

# Archivage probant : les PDF officiels des actes EUR-Lex font foi en audit
EURLEX_ARCHIVE_DIR = PROJECT_ROOT / "eurlex_archives"

# Le snapshot des ajouts manuels reste toujours actif : il n'est ni fusionne
# ni remplace par les synchronisations automatiques.
MANUAL_SNAPSHOT_ID = "manual-watchlist"
# Les ajouts manuels par liste vivent dans des snapshots dedies
# (manual-watchlist-ofac, ...) : tout ce qui epargnait le snapshot manuel
# generique doit epargner la famille entiere.
MANUAL_SNAPSHOT_LIKE = "manual-watchlist%"

# Taille maximale des listes de details conservees dans le rapport stocke
MAX_REPORT_DETAILS = 100


# Budget reseau par defaut d'une source qui en demande davantage. EUR-Lex
# repond HTTP 202 « page en preparation » (anti-robot) : 4 tentatives sur 18 s
# ne suffisent pas a franchir son interstitiel.
_SOURCE_NETWORK_DEFAULTS = {
    "eurlex": {"retries": 6, "backoff_seconds": 5},
}


# Hote -> source, pour appliquer le bon budget reseau sans que chaque appelant
# ait a le preciser (les requetes par acte EUR-Lex en beneficient aussi)
_HOST_TO_SOURCE = {"eur-lex.europa.eu": "eurlex"}


def network_for_url(url: str) -> Dict[str, Any]:
    """Parametres reseau applicables a une URL, deduits de son hote."""
    from urllib.parse import urlsplit
    host = urlsplit(url).netloc.lower()
    source = _HOST_TO_SOURCE.get(host)
    return source_network_config(source) if source else get_sync_config()["network"]


def source_network_config(source: str) -> Dict[str, Any]:
    """
    Parametres reseau applicables a UNE source : `sync.network` surcharge par
    `sync.<source>.network`, elle-meme surchargee par le defaut plus patient
    de la source si l'exploitant n'a rien precise. Une source lente ne
    ralentit ainsi jamais les autres.
    """
    sync_cfg = config.get("sync", {}) or {}
    network = dict(get_sync_config()["network"])
    key = source.lower()
    network.update(_SOURCE_NETWORK_DEFAULTS.get(key, {}))
    override = (sync_cfg.get(key) or {}).get("network") or {}
    for field in ("timeout_seconds", "download_timeout_seconds", "backoff_seconds"):
        if override.get(field) is not None:
            network[field] = float(override[field])
    if override.get("retries") is not None:
        network["retries"] = int(override["retries"])
    if override.get("user_agent"):
        network["user_agent"] = str(override["user_agent"])
    return network


def _apply_hot_sync_settings(db, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Applique les reglages a chaud (base) par-dessus config.yaml : interrupteur
    general des synchronisations planifiees et activation par source. Ce sont
    les valeurs que le planificateur doit lire — l'exploitant coupe ou relance
    une source depuis l'application, sans editer de fichier ni redemarrer.
    """
    from fiskr.settings import sync_auto_enabled, sync_sources_enabled

    cfg["auto_enabled"] = sync_auto_enabled(db)
    for source, enabled in sync_sources_enabled(db).items():
        if isinstance(cfg.get(source), dict):
            cfg[source]["enabled"] = enabled
    return cfg


def get_sync_config(db=None) -> Dict[str, Any]:
    """
    Configuration de synchronisation (config.yaml, section sync) avec defauts.

    Avec une session `db`, les reglages a chaud de la synchronisation
    automatique (interrupteur general, sources actives) sont appliques
    par-dessus : c'est la forme que doivent lire les planificateurs et l'ecran
    de reglages. Sans session, seule la configuration fichier est rendue — les
    appels internes (parametres reseau, URL d'une source) n'ont pas besoin de
    la base et ne doivent pas la solliciter.
    """
    from fiskr.settings import sync_network_settings

    sync_cfg = config.get("sync", {}) or {}
    cfg = {
        "auto_enabled": bool(sync_cfg.get("auto_enabled", False)),
        "schedule_time": sync_cfg.get("schedule_time", "06:00"),
        # Parametres reseau partages par toutes les sources (repris par les
        # helpers HTTP), reglables a chaud — lecture standalone sans session :
        # les surcharges PAR SOURCE (sync.<source>.network) continuent de primer
        "network": sync_network_settings(db),
        "ofac": {
            "enabled": bool((sync_cfg.get("ofac") or {}).get("enabled", True)),
            "url": (sync_cfg.get("ofac") or {}).get("url", DEFAULT_OFAC_URL),
        },
        # `mode` choisit ce que EUR-Lex EST pour le produit :
        #   "alert"   (defaut) : un signal d'alerte precoce — « un acte de
        #             mesures restrictives est paru aujourd'hui ». Les
        #             designations viennent de la liste consolidee officielle
        #             (EUFSF), qui fait autorite et porte les radiations.
        #   "extract" : comportement historique — extraction heuristique des
        #             noms depuis le HTML des annexes. Conserve pour ne pas
        #             priver de source une installation sans token FSF, mais
        #             les designations qui en sortent sont des SUPPOSITIONS.
        "eurlex": {
            "enabled": bool((sync_cfg.get("eurlex") or {}).get("enabled", True)),
            "mode": str((sync_cfg.get("eurlex") or {}).get("mode", "alert") or "alert").lower(),
            "daily_journal_url": (sync_cfg.get("eurlex") or {}).get("daily_journal_url", DEFAULT_EURLEX_DAILY_URL),
            "keyword": (sync_cfg.get("eurlex") or {}).get("keyword", DEFAULT_EURLEX_KEYWORD),
        },
        "dgt": {
            "enabled": bool((sync_cfg.get("dgt") or {}).get("enabled", True)),
            "url": (sync_cfg.get("dgt") or {}).get("url", DEFAULT_DGT_URL),
        },
        # Desactive par defaut : necessite un token (inscription gratuite au webgate FSD)
        "eu_fsf": {
            "enabled": bool((sync_cfg.get("eu_fsf") or {}).get("enabled", False)),
            "url": (sync_cfg.get("eu_fsf") or {}).get("url", DEFAULT_EU_FSF_URL),
            "token": str((sync_cfg.get("eu_fsf") or {}).get("token", "") or ""),
        },
        "un": {
            "enabled": bool((sync_cfg.get("un") or {}).get("enabled", True)),
            "url": (sync_cfg.get("un") or {}).get("url", DEFAULT_UN_URL),
        },
        # Desactives par defaut : PEP (volumetrie + licence commerciale
        # OpenSanctions) et OFSI (liste UK, opt-in selon l'exposition)
        "pep": {
            "enabled": bool((sync_cfg.get("pep") or {}).get("enabled", False)),
            "url": (sync_cfg.get("pep") or {}).get("url", DEFAULT_PEP_URL),
        },
        "ofsi": {
            "enabled": bool((sync_cfg.get("ofsi") or {}).get("enabled", False)),
            "url": (sync_cfg.get("ofsi") or {}).get("url", DEFAULT_OFSI_URL),
        },
        # Liste consolidee Non-SDN de l'OFAC (sanctions sectorielles et
        # regimes sans gel total). Opt-in : elle elargit le perimetre d'alertes.
        "ofac_nonsdn": {
            "enabled": bool((sync_cfg.get("ofac_nonsdn") or {}).get("enabled", False)),
            "url": (sync_cfg.get("ofac_nonsdn") or {}).get("url", DEFAULT_OFAC_NONSDN_URL),
        },
        # Consolidated Screening List (trade.gov). `exclude_sources` evite de
        # dupliquer une liste deja recuperee a sa source : par defaut la SDN.
        "csl": _csl_source_config(sync_cfg.get("csl") or {}),
        # Sanctions autonomes canadiennes (SEMA) et liste consolidee
        # australienne (DFAT) : opt-in selon l'exposition geographique.
        "canada": {
            "enabled": bool((sync_cfg.get("canada") or {}).get("enabled", False)),
            "url": (sync_cfg.get("canada") or {}).get("url", DEFAULT_CANADA_URL),
        },
        "dfat": {
            "enabled": bool((sync_cfg.get("dfat") or {}).get("enabled", False)),
            "url": (sync_cfg.get("dfat") or {}).get("url", DEFAULT_DFAT_URL),
        },
        # Listes d'alerte de regulateurs et exclusions de bailleurs : opt-in.
        "hk_sfc": {
            "enabled": bool((sync_cfg.get("hk_sfc") or {}).get("enabled", False)),
            "url": (sync_cfg.get("hk_sfc") or {}).get("url", DEFAULT_HK_SFC_URL),
        },
        "amf": {
            "enabled": bool((sync_cfg.get("amf") or {}).get("enabled", False)),
            "url": (sync_cfg.get("amf") or {}).get("url", DEFAULT_AMF_URL),
        },
        "worldbank": {
            "enabled": bool((sync_cfg.get("worldbank") or {}).get("enabled", False)),
            "url": (sync_cfg.get("worldbank") or {}).get("url", DEFAULT_WORLDBANK_URL),
        },
        # Liste suisse SECO, opt-in selon l'exposition CH. `format` choisit la
        # voie : "xml" (export officiel SESAM) ou "opensanctions" (CSV agrege).
        # Une URL laissee vide prend le defaut correspondant au format, pour
        # qu'un simple basculement de format suffise a changer de source.
        "seco": _seco_source_config(sync_cfg.get("seco") or {}),
        # Sources du registre OpenSanctions (fiskr/sources.py) : une entree
        # generee par source — opt-in, URL vide = defaut derive du dataset
        # (meme logique que SECO : un slug se corrige dans config.yaml).
        **{
            key: {
                "enabled": bool((sync_cfg.get(key) or {}).get("enabled", False)),
                "url": str((sync_cfg.get(key) or {}).get("url", "") or "")
                       or opensanctions_default_url(src.dataset),
                # En-tetes d'authentification optionnels (fournisseur a cle) ;
                # valeurs ${VAR} interpolees depuis .env par fiskr/config.py
                "auth_headers": dict((sync_cfg.get(key) or {}).get("auth_headers") or {}),
            }
            for key, src in OPENSANCTIONS_BY_KEY.items()
        },
    }
    return _apply_hot_sync_settings(db, cfg) if db is not None else cfg


def _csl_source_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Configuration CSL. `exclude_sources` absent (et non pas vide) reprend le
    defaut : une liste explicitement vide signifie « tout charger »."""
    raw_excluded = raw.get("exclude_sources", None)
    if raw_excluded is None:
        excluded = tuple(CSL_DEFAULT_EXCLUDED_SOURCES)
    elif isinstance(raw_excluded, str):
        excluded = tuple(v.strip() for v in raw_excluded.split(";") if v.strip())
    else:
        excluded = tuple(str(v).strip() for v in raw_excluded if str(v).strip())
    return {
        "enabled": bool(raw.get("enabled", False)),
        "url": str(raw.get("url", "") or "").strip() or DEFAULT_CSL_URL,
        "exclude_sources": excluded,
    }


def _seco_source_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    fmt = str(raw.get("format", "xml") or "xml").strip().lower()
    if fmt not in ("xml", "opensanctions"):
        fmt = "xml"
    url = str(raw.get("url", "") or "").strip()
    if not url:
        url = DEFAULT_SECO_OPENSANCTIONS_URL if fmt == "opensanctions" else DEFAULT_SECO_URL
    return {
        "enabled": bool(raw.get("enabled", False)),
        "format": fmt,
        "url": url,
    }


# ------------------ RECUPERATION HTTP ------------------

# Statuts qui valent la peine d'etre retentes : 202 = anti-robot EUR-Lex
# (reponse differree a corps vide), 408/429 = throttling, 5xx = incident
# serveur transitoire. Les 403/404 echouent immediatement (deterministes).
_RETRYABLE_STATUS = {202, 408, 429, 500, 502, 503, 504}

_shared_http_client = None
_shared_client_lock = None


def _browser_headers() -> Dict[str, str]:
    return {
        "User-Agent": get_sync_config()["network"]["user_agent"],
        "Accept-Language": "en, fr;q=0.8",
    }


def _get_shared_client():
    """Client httpx module-level (keep-alive) : evite un handshake TCP+TLS
    par requete — la sync EUR-Lex en fait 2N+1 vers le meme hote."""
    global _shared_http_client, _shared_client_lock
    import threading
    import httpx
    if _shared_client_lock is None:
        _shared_client_lock = threading.Lock()
    with _shared_client_lock:
        if _shared_http_client is None or _shared_http_client.is_closed:
            _shared_http_client = httpx.Client(follow_redirects=True, headers=_browser_headers())
    return _shared_http_client


class _RetryableHTTP(RuntimeError):
    """
    Reponse HTTP recue mais a retenter (statut transitoire ou corps vide).

    Porte le delai demande par le serveur (`Retry-After`) quand il en a
    fourni un : c'est le serveur qui sait quand il acceptera de nouveau, pas
    nous. L'ignorer, c'est se faire bloquer plus durement et plus longtemps.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


# Plafond de l'attente imposee par un serveur : un Retry-After de plusieurs
# heures ne doit pas immobiliser un job de synchronisation. Au-dela, mieux
# vaut echouer proprement et laisser la planification reprendre plus tard.
MAX_RETRY_AFTER_SECONDS = 300.0


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """
    Interprete l'en-tete `Retry-After` (RFC 9110) : soit un nombre de
    secondes, soit une date HTTP. Retourne des secondes, ou None si absent
    ou illisible. Jamais negatif (une date deja passee vaut 0).
    """
    if not value:
        return None
    raw = value.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(raw)
        if when is None:
            return None
        from datetime import timezone
        now = datetime.now(timezone.utc) if when.tzinfo else datetime.utcnow()
        return max(0.0, (when - now).total_seconds())
    except (TypeError, ValueError):
        return None


def _retryable_from_response(response) -> _RetryableHTTP:
    """Construit l'exception de reprise d'une reponse transitoire, en
    conservant le delai que le serveur a demande."""
    delay = parse_retry_after(response.headers.get("retry-after"))
    suffix = f", Retry-After: {delay:.0f}s" if delay is not None else ""
    return _RetryableHTTP(f"HTTP {response.status_code}{suffix}", retry_after=delay)


def _with_retries(operation, url: str, retries: int, backoff: float):
    """
    Execute `operation` avec reprises sur les erreurs TRANSPORT
    (httpx.TransportError : ConnectError, timeouts, coupure TLS/proxy...)
    ET sur les reponses transitoires (_RetryableHTTP levee par l'operation).
    C'est le correctif du « probleme de connexions » : la boucle historique
    ne couvrait que les statuts HTTP, jamais les exceptions de transport.

    Quand le serveur a precise `Retry-After`, c'est LUI qui fixe l'attente
    (plafonnee) : un client qui respecte ce delai se fait nettement moins
    bloquer qu'un client qui rejoue selon son propre calendrier.
    """
    import time
    import httpx
    last_error: Exception = RuntimeError(f"Echec inconnu sur {url}")
    for attempt in range(retries + 1):
        try:
            return operation()
        except (_RetryableHTTP, httpx.TransportError) as e:
            last_error = e
            logger.warning(f"{url}: {e} — tentative {attempt + 1}/{retries + 1}")
            if attempt < retries:
                wait = backoff * (attempt + 1)
                asked = getattr(e, "retry_after", None)
                if asked is not None:
                    wait = min(max(wait, asked), MAX_RETRY_AFTER_SECONDS)
                time.sleep(wait)
    hint = ""
    if isinstance(last_error, _RetryableHTTP) and "HTTP 202" in str(last_error):
        # 202 persistant = interstitiel anti-robot jamais franchi : l'exploitant
        # doit savoir quoi regler plutot que de lire un simple compte d'echecs
        hint = (" — le portail sert sa page d'attente anti-robot : augmentez "
                "sync.<source>.network.retries / backoff_seconds, ou relancez "
                "la source plus tard")
    raise RuntimeError(
        f"Echec apres {retries + 1} tentatives sur {url}: {last_error}{hint}")


def download_to_file(url: str, dest_path: Path, timeout: Optional[float] = None,
                     retries: Optional[int] = None, progress=None,
                     validators: Optional[Dict[str, str]] = None,
                     headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Telecharge un fichier volumineux en streaming vers dest_path, avec
    reprises sur erreurs de transport, User-Agent navigateur (les portails
    officiels filtrent l'UA httpx par defaut) et timeouts granulaires : le
    timeout de lecture s'applique PAR CHUNK, pas au telechargement entier.
    `progress(octets_recus, taille_totale_ou_None)` est appele ~tous les Mo.

    `validators` : {"etag": ..., "last_modified": ...} du dernier
    telechargement reussi. Fournis, ils partent en requete CONDITIONNELLE
    (If-None-Match / If-Modified-Since) : si la source n'a pas change, elle
    repond 304 sans corps — rien n'est retelecharge ni analyse. C'est autant
    de bande passante economisee ET autant de sollicitations en moins, donc
    moins de risque de se faire limiter par un portail officiel.

    `headers` : en-tetes supplementaires (fusionnes apres ceux du navigateur,
    donc prioritaires). C'est la porte d'entree des fournisseurs a cle d'API
    (Authorization: Bearer...) — cf. sync.<source>.auth_headers et
    Documentation/SOURCES_PREMIUM.md ; les valeurs ${VAR} de config.yaml sont
    interpolees depuis .env par fiskr/config.py, le secret ne vit jamais
    dans un fichier versionne.

    Retourne {"not_modified": bool, "etag": ..., "last_modified": ...} :
    dest_path n'est PAS ecrit quand `not_modified` est vrai.
    """
    import httpx
    network = get_sync_config()["network"]
    read_timeout = timeout if timeout is not None else network["download_timeout_seconds"]
    max_retries = retries if retries is not None else network["retries"]
    granular_timeout = httpx.Timeout(connect=10.0, read=read_timeout, write=30.0, pool=10.0)

    request_headers = _browser_headers()
    if headers:
        request_headers.update(headers)
    if validators:
        if validators.get("etag"):
            request_headers["If-None-Match"] = validators["etag"]
        if validators.get("last_modified"):
            request_headers["If-Modified-Since"] = validators["last_modified"]

    def _attempt():
        with httpx.stream("GET", url, timeout=granular_timeout, follow_redirects=True,
                          headers=request_headers) as response:
            if response.status_code in _RETRYABLE_STATUS:
                raise _retryable_from_response(response)
            if response.status_code == 304:
                # Source inchangee depuis le dernier passage : rien a lire
                return {"not_modified": True,
                        "etag": (validators or {}).get("etag"),
                        "last_modified": (validators or {}).get("last_modified")}
            response.raise_for_status()
            total = None
            try:
                total = int(response.headers.get("content-length", "") or 0) or None
            except ValueError:
                pass
            received, last_reported = 0, 0
            # Nouveau fichier a chaque tentative : jamais de contenu partiel concatene
            with open(dest_path, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=1024 * 256):
                    f.write(chunk)
                    received += len(chunk)
                    if progress and received - last_reported >= 1024 * 1024:
                        last_reported = received
                        try:
                            progress(received, total)
                        except Exception:
                            pass
            if progress:
                try:
                    progress(received, total)
                except Exception:
                    pass
            return {"not_modified": False,
                    "etag": response.headers.get("etag"),
                    "last_modified": response.headers.get("last-modified")}

    return _with_retries(_attempt, url, max_retries, network["backoff_seconds"])


def _portal_root(url: str) -> str:
    """Racine du portail (schema + hote) d'une URL, pour le prechauffage."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def warm_up_session(base_url: str) -> None:
    """
    Requete de prechauffage : recupere la page d'accueil du portail avec le
    client partage (qui conserve ses cookies) avant d'attaquer la page utile.

    EUR-Lex sert un interstitiel HTTP 202 aux clients sans cookie de session ;
    passer d'abord par l'accueil donne au client de quoi etre reconnu. Un
    echec de prechauffage n'est jamais bloquant : la requete utile suivra.
    """
    try:
        _get_shared_client().get(base_url, timeout=get_sync_config()["network"]["timeout_seconds"])
    except Exception as e:
        logger.debug(f"Prechauffage de session ignore sur {base_url}: {e}")


def http_get_text(url: str, timeout: Optional[float] = None,
                  retries: Optional[int] = None,
                  headers: Optional[Dict[str, str]] = None) -> str:
    """
    Recupere le contenu textuel d'une page web avec reprises couvrant les
    erreurs de transport ET les reponses transitoires. EUR-Lex repond parfois
    HTTP 202 avec un corps vide (anti-robot) : on reessaie apres un delai, et
    on echoue franchement plutot que de traiter une page vide comme un
    Journal Officiel sans publication.

    Le budget de reprises est deduit de l'hote : EUR-Lex est plus patient que
    les autres sources (cf. network_for_url / source_network_config), sans que
    l'appelant ait a le savoir.
    """
    network = network_for_url(url)
    page_timeout = timeout if timeout is not None else network["timeout_seconds"]
    max_retries = retries if retries is not None else network["retries"]

    def _attempt():
        # En-tetes par requete : httpx les fusionne avec ceux du client
        # partage (la requete prime cle par cle). Passes seulement s'ils
        # existent — les doubles de test du client n'exposent pas ce kwarg.
        kwargs = {"timeout": page_timeout}
        if headers:
            kwargs["headers"] = headers
        response = _get_shared_client().get(url, **kwargs)
        if response.status_code in _RETRYABLE_STATUS:
            raise _retryable_from_response(response)
        if response.status_code == 200 and not response.text.strip():
            raise _RetryableHTTP(f"HTTP {response.status_code} (corps vide)")
        if response.status_code != 200:
            raise RuntimeError(f"Reponse invalide de {url} (HTTP {response.status_code})")
        return response.text

    return _with_retries(_attempt, url, max_retries, network["backoff_seconds"])


# ------------------ PERSISTANCE DES SNAPSHOTS ------------------

# ------------------ VALIDATEURS DE CACHE HTTP (par source) ------------------
# Stockes en base plutot qu'en memoire : le demon travailleur, les processus
# API et une relance manuelle doivent partager le meme etat de fraicheur.

SETTING_HTTP_VALIDATORS = "sync.http_validators"


def stored_validators(db, source: str) -> Dict[str, str]:
    """ETag / Last-Modified du dernier telechargement reussi de cette source."""
    from fiskr.settings import get_setting
    try:
        all_validators = get_setting(db, SETTING_HTTP_VALIDATORS, {}) or {}
        return dict(all_validators.get(source.upper(), {}) or {})
    except Exception as e:  # un cache illisible ne bloque jamais une sync
        logger.debug(f"Validateurs HTTP illisibles pour {source}: {e}")
        return {}


def remember_validators(db, source: str, etag: Optional[str],
                        last_modified: Optional[str]) -> None:
    """Memorise les validateurs pour la prochaine requete conditionnelle.
    Sans etag ni date, l'entree est effacee : mieux vaut retelecharger que
    conditionner sur un validateur perime."""
    from fiskr.settings import get_setting, set_setting
    try:
        all_validators = dict(get_setting(db, SETTING_HTTP_VALIDATORS, {}) or {})
        key = source.upper()
        if etag or last_modified:
            entry = {}
            if etag:
                entry["etag"] = etag
            if last_modified:
                entry["last_modified"] = last_modified
            all_validators[key] = entry
        else:
            all_validators.pop(key, None)
        set_setting(db, SETTING_HTTP_VALIDATORS, all_validators)
    except Exception as e:
        logger.debug(f"Validateurs HTTP non memorises pour {source}: {e}")


def _clamp_to_column_lengths(values: Dict[str, Any]) -> Dict[str, Any]:
    """
    Tronque les valeurs textuelles aux longueurs maximales des colonnes
    (VARCHAR(n)) : les donnees scrapees (titres d'actes EUR-Lex, adresses...)
    peuvent depasser les capacites du schema et faire echouer l'INSERT
    (StringDataRightTruncation sous PostgreSQL).
    """
    for column in WatchlistEntity.__table__.columns:
        max_length = getattr(column.type, "length", None)
        value = values.get(column.name)
        if max_length and isinstance(value, str) and len(value) > max_length:
            values[column.name] = value[:max_length]
    return values


# Colonnes etendues extraites par les parseurs officiels (26 champs AML). Elles
# vivent ici et non dans l'API parce que les DEUX chemins d'ecriture en ont
# besoin : l'upload manuel et la synchronisation automatique.
EXTENDED_ENTITY_FIELDS = (
    "crypto_wallets", "bic_swift", "tax_id", "duns_number",
    "vessel_call_sign", "vessel_mmsi", "vessel_flag", "vessel_type",
    "vessel_tonnage", "vessel_owner",
    "aircraft_model", "aircraft_operator", "aircraft_construction_number",
    "sanction_programs", "listed_on", "delisted_on", "name_original_script",
    "title", "pep_role", "secondary_sanctions_risk", "designating_state",
    "organization_established_date", "organization_type",
    "phone_numbers", "email_addresses", "websites",
)

_EXTENDED_LIST_FIELDS = ("sanction_programs", "phone_numbers", "email_addresses", "websites")


def extended_entity_kwargs(item: Dict[str, Any]) -> Dict[str, Any]:
    """Champs etendus normalises : les colonnes CSV texte des champs liste
    sont decoupees sur « ; » (parite avec les parseurs officiels)."""
    out: Dict[str, Any] = {}
    for field in EXTENDED_ENTITY_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and field in _EXTENDED_LIST_FIELDS:
            value = [v.strip() for v in value.split(";") if v.strip()] or None
        elif isinstance(value, str) and field == "crypto_wallets":
            value = [{"currency": "", "address": v.strip()} for v in value.split(";") if v.strip()] or None
        elif isinstance(value, str):
            value = value.strip() or None
        out[field] = value
    return out


def build_watchlist_entity(snap_id: str, item: Dict[str, Any], report: Dict[str, Any]) -> WatchlistEntity:
    """Construit une ligne WatchlistEntity depuis un enregistrement au schema pivot."""
    parsed_name = item.get("individual_name_parsed") or {}
    alt_addrs = item.get("alternative_addresses")
    if isinstance(alt_addrs, str):
        alt_addrs = [a.strip() for a in alt_addrs.split(";")]
    return WatchlistEntity(**_clamp_to_column_lengths(dict(
        snapshot_id=snap_id,
        entity_id=item.get("entity_id"),
        entity_type=item.get("entity_type"),
        primary_name=report["cleansed_name"],
        individual_name_parsed={
            "first_name": parsed_name.get("first_name", ""),
            "last_name": parsed_name.get("last_name", ""),
            "maiden_name": report["cleansed_maiden_name"]
        },
        aliases=report["cleansed_aliases"],
        dates_of_birth=item.get("dates_of_birth", []),
        date_of_death=item.get("date_of_death"),
        is_deceased=item.get("is_deceased", False),
        gender=report["resolved_gender"],
        countries=item.get("countries", {}),
        place_of_birth=item.get("place_of_birth"),
        address=item.get("address") or item.get("adress"),
        city=item.get("city"),
        state=item.get("state"),
        country=item.get("country"),
        origin=item.get("origin"),
        designation=item.get("designation"),
        designation_reasons=item.get("designation_reasons"),
        additional_informations=item.get("additional_informations") or item.get("additional_info"),
        official_reference=item.get("official_reference"),
        alternative_addresses=alt_addrs or [],
        imo_number=item.get("imo_number"),
        aircraft_tail_number=item.get("aircraft_tail_number"),
        lei_number=item.get("lei_number"),
        national_registry_ids=item.get("national_registry_ids"),
        other_registration_ids=item.get("other_registration_ids"),
        passport_documents=item.get("passport_documents"),
        national_id_documents=item.get("national_id_documents"),
        other_id_documents=item.get("other_id_documents"),
        entity_checksum=item.get("entity_checksum") or compute_checksum(item),
        **extended_entity_kwargs(item)
    )))


class SyncProgress:
    """
    Publication de la progression d'une synchronisation de source.

    Partage par les QUATRE implementations (OFAC, DGT, EUR-Lex et le cycle
    generique) : avant, seul le cycle generique publiait ses phases et les
    autres sources n'apparaissaient qu'en barre indeterminee. Le jeton
    `sync:<source>` est le meme que celui interroge par le tableau de bord.
    """

    def __init__(self, source: str, started_by: str = "système"):
        from fiskr import progress as progress_registry
        self._registry = progress_registry
        self.token = f"sync:{source.lower()}"
        self.source = source
        # `started_by` n'ecrase jamais une valeur deja posee : un declenchement
        # manuel inscrit le nom de l'utilisateur avant d'appeler le cycle
        self._registry.update(self.token, phase="DOWNLOAD", kind="sync",
                              label=f"Synchronisation {source}",
                              started_by=started_by)

    def phase(self, phase: str, processed: int = 0, total: Optional[int] = None,
              snapshot_id: Optional[str] = None) -> None:
        self._registry.update(self.token, phase=phase, processed=processed,
                              total=total, snapshot_id=snapshot_id)

    def downloading(self):
        """Callback octets recus / taille annoncee pour download_to_file."""
        return lambda done, total: self.phase("DOWNLOAD", processed=done, total=total)

    def persisting(self, db, snap_id: str):
        """
        Callback de `persist_pivot_items` : publie le compteur ET persiste la
        progression sur le snapshot (le suivi survit a un redemarrage).
        """
        def _tick(count: int) -> None:
            self.phase("PERSIST", processed=count, snapshot_id=snap_id)
            row = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
            if row:
                row.processed_count = count
                row.phase = "PERSIST"
                db.commit()
        return _tick

    def done(self) -> None:
        self._registry.finish(self.token)

    def failed(self, error: Exception) -> None:
        self._registry.finish(self.token, status="ERROR", error=str(error))


def _release_persisted_entities(db) -> None:
    """
    Libere de l'identity map les SEULES entites de liste ecrites par la boucle
    de persistance.

    `db.expunge_all()` bornait bien la RAM mais detachait AUSSI les objets que
    l'appelant garde en main pendant toute la boucle — le snapshot en cours et
    le snapshot precedent. Lire ensuite `previous.snapshot_id` levait alors
    « Instance <Snapshot> is not bound to a Session », et les ecritures sur le
    snapshot n'etaient plus persistees du tout (liste jamais mise en
    production, sans erreur visible). On n'expulse donc que ce qui s'accumule.
    """
    for obj in list(db.identity_map.values()):
        if isinstance(obj, WatchlistEntity):
            db.expunge(obj)


def persist_pivot_items(db, snap_id: str, items, commit_every: int = 1000,
                        progress: Optional[Callable[[int], None]] = None) -> int:
    """
    Valide (Quality Gate) et persiste des enregistrements pivots. Retourne le
    nombre insere. Commits periodiques : la progression devient visible par
    polling ET les entites deja ecrites sont liberees de l'identity map (un
    dataset PEP de 750 000 fiches n'accumule plus les objets en RAM), sans
    jamais detacher les objets de l'appelant.
    """
    count = 0
    for item in items:
        # Complete le decoupage prenoms / nom de famille des individus quand la
        # source ne le fournit pas (moteur de detection fiskr.names)
        item = ensure_parsed_name(item)
        report = evaluate_and_clean(item)
        if not report["is_valid"]:
            continue
        # Un nom qui ne survit pas au nettoyage (ex: uniquement des caracteres
        # speciaux ou cyrilliques) ne peut pas etre crible : fiche ecartee
        if len([c for c in report["cleansed_name"] if c.isalnum()]) < 2:
            continue
        db.add(build_watchlist_entity(snap_id, item, report))
        count += 1
        if commit_every and count % commit_every == 0:
            db.commit()
            _release_persisted_entities(db)
            if progress:
                try:
                    progress(count)
                except Exception:
                    pass
    return count


def _clone_entity_row(snap_id: str, ent: WatchlistEntity) -> WatchlistEntity:
    """Copie une ligne d'entite existante vers un nouveau snapshot (checksum conserve)."""
    values = {c.name: getattr(ent, c.name) for c in ent.__table__.columns if c.name != "id"}
    values["snapshot_id"] = snap_id
    return WatchlistEntity(**values)


def _latest_ready_snapshot(db, file_type: str) -> Optional[Snapshot]:
    return db.query(Snapshot).filter(
        Snapshot.file_type == file_type,
        Snapshot.status == "READY",
        Snapshot.snapshot_id.notlike(MANUAL_SNAPSHOT_LIKE)
    ).order_by(Snapshot.uploaded_at.desc()).first()


def _latest_reviewable_snapshot(db, file_type: str) -> Optional[Snapshot]:
    """
    Base de fusion incrementale en mode homologation : le snapshot le plus
    recent encore vivant (en production OU en attente de pointage). Sans cela,
    un JO du jour 2 fusionne sur la production perdrait les entites du pending
    du jour 1 lors de son approbation.
    """
    return db.query(Snapshot).filter(
        Snapshot.file_type == file_type,
        Snapshot.status.in_(["READY", "PENDING_REVIEW"]),
        Snapshot.snapshot_id.notlike(MANUAL_SNAPSHOT_LIKE)
    ).order_by(Snapshot.uploaded_at.desc()).first()


def _existing_snapshot_with_hash(db, file_type: str, fhash: str) -> Optional[Snapshot]:
    """
    Deduplication par hash etendue aux snapshots en attente d'homologation :
    sans cela, la sync quotidienne recreerait chaque matin un doublon pending
    du meme fichier tant que le pointage n'a pas eu lieu.
    """
    return db.query(Snapshot).filter(
        Snapshot.file_type == file_type,
        Snapshot.file_hash == fhash,
        Snapshot.status.in_(["READY", "PENDING_REVIEW"])
    ).first()


def _snapshot_entity_dicts(db, snapshot_id: str) -> List[Dict[str, Any]]:
    # yield_per : streaming ORM — reduit fortement le pic memoire du calcul
    # de delta sur les tres grandes listes (les dicts restent en RAM, pas
    # les objets ORM ni l'identity map)
    query = db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == snapshot_id).yield_per(2000)
    return [{c.name: getattr(e, c.name) for c in WatchlistEntity.__table__.columns} for e in query]


def _supersede_previous_snapshots(db, file_type: str, keep_snapshot_id: str) -> None:
    """
    Applique le delta au referentiel actif : les anciens snapshots READY du meme
    type passent en SUPERSEDED, seul le snapshot le plus recent reste charge en
    cache (les ajouts manuels a la volee sont preserves).
    """
    db.query(Snapshot).filter(
        Snapshot.file_type == file_type,
        Snapshot.status == "READY",
        Snapshot.snapshot_id != keep_snapshot_id,
        Snapshot.snapshot_id.notlike(MANUAL_SNAPSHOT_LIKE)
    ).update({"status": "SUPERSEDED"}, synchronize_session=False)


def _truncate_delta_details(delta: Dict[str, Any]) -> Dict[str, Any]:
    """Limite la taille des details stockes dans le rapport (les compteurs restent exacts)."""
    details = delta.get("details", {})
    truncated = {}
    for key in ("added", "removed", "modified"):
        rows = details.get(key, [])
        truncated[key] = rows[:MAX_REPORT_DETAILS]
        if len(rows) > MAX_REPORT_DETAILS:
            truncated[f"{key}_truncated"] = len(rows) - MAX_REPORT_DETAILS
    return {"summary": delta.get("summary", {}), "details": truncated}


def _save_report(db, **kwargs) -> SyncReport:
    report = SyncReport(**kwargs)
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


# ------------------ NOTIFICATION EMAIL ------------------

def send_report_email(report: SyncReport) -> bool:
    """
    Envoie le rapport de synchronisation par email si un serveur SMTP est
    configure (variables SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD /
    SMTP_FROM / SYNC_EMAIL_TO). Retourne False sans erreur si non configure.
    """
    host = os.getenv("SMTP_HOST")
    recipients = [r.strip() for r in os.getenv("SYNC_EMAIL_TO", "").split(",") if r.strip()]
    if not host or not recipients:
        return False

    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM", user or "fiskr@localhost")

    body = (
        f"Rapport de synchronisation Fiskr\n"
        f"--------------------------------\n"
        f"Source        : {report.source}\n"
        f"Execution     : {report.executed_at} ({report.trigger})\n"
        f"Statut        : {report.status}\n"
        f"Message       : {report.message or '-'}\n"
        f"Snapshot      : {report.snapshot_id or '-'}\n"
        f"Ajouts        : {report.added_count}\n"
        f"Modifications : {report.modified_count}\n"
        f"Suppressions  : {report.removed_count}\n"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[Fiskr] Sync {report.source} - {report.status} (+{report.added_count} ~{report.modified_count} -{report.removed_count})"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPNotSupportedError:
                pass
            if user and password:
                server.login(user, password)
            server.sendmail(sender, recipients, msg.as_string())
        return True
    except Exception as e:
        logger.error(f"Echec de l'envoi de l'email de rapport: {e}")
        return False


def _finalize_report(db, **kwargs) -> SyncReport:
    """Persiste le rapport, tente l'envoi email et memorise le resultat."""
    report = _save_report(db, **kwargs)
    if send_report_email(report):
        report.email_sent = True
        db.commit()
    # Notification metier de l'etape : chaque fin de synchronisation est
    # notifiee — panne reseau et snapshot a homologuer immediatement, fins
    # nominales dans le recapitulatif periodique.
    from fiskr.notifier import emit
    delta = {"Ajouts": report.added_count, "Modifications": report.modified_count,
             "Suppressions": report.removed_count}
    if report.status == "ERROR":
        emit(db, "sync_error", {
            "Source": report.source, "Message": report.message or "—",
            "Déclencheur": report.trigger,
        })
    elif report.status == "PENDING_REVIEW":
        emit(db, "snapshot_pending_review", {
            "Source": report.source, "Snapshot": report.snapshot_id,
            "Déclencheur": report.trigger, **delta,
        })
    else:
        emit(db, "sync_completed", {
            "Source": report.source, "Statut": report.status,
            "Message": report.message or "—", "Déclencheur": report.trigger,
            "Snapshot": report.snapshot_id or "—", **delta,
        })
    return report


# ------------------ SYNCHRONISATION OFAC ------------------

def run_ofac_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge le fichier OFAC SDN_ADVANCED.XML, l'ingere en snapshot, calcule
    le delta par rapport a la liste OFAC active et applique le remplacement.
    """
    cfg = get_sync_config()["ofac"]
    url = cfg["url"]
    fetch = fetcher or download_to_file

    temp_dir = PROJECT_ROOT / "temp_ingestion"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f"ofac_sync_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.xml"

    previous = _latest_ready_snapshot(db, "WATCHLIST_OFAC")
    # Scalaire capture AVANT la boucle de persistance : le code ne depend plus
    # de la survie d'un objet ORM a un traitement long (cf. panne detachement)
    previous_id = previous.snapshot_id if previous else None
    snap_id = None
    tracker = SyncProgress("OFAC")
    try:
        logger.info(f"Sync OFAC: telechargement de {url}")
        if fetcher is None:
            download_to_file(url, temp_file, progress=tracker.downloading())
        else:
            fetch(url, temp_file)

        tracker.phase("HASH")
        hasher = hashlib.sha256()
        with open(temp_file, "rb") as f:
            while chunk := f.read(1024 * 1024):
                hasher.update(chunk)
        fhash = hasher.hexdigest()

        duplicate = _existing_snapshot_with_hash(db, "WATCHLIST_OFAC", fhash)
        if duplicate:
            if duplicate.status == "PENDING_REVIEW":
                message = "Le fichier OFAC est identique a un snapshot deja en attente d'homologation."
            else:
                message = "Le fichier OFAC est identique a la version active (hash inchange)."
            return _finalize_report(
                db, source="OFAC", trigger=trigger, status="NO_CHANGE",
                message=message,
                previous_snapshot_id=duplicate.snapshot_id
            )

        # Ingestion du nouveau snapshot
        snap_id = f"ofac-sync-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        snap = Snapshot(
            snapshot_id=snap_id,
            file_type="WATCHLIST_OFAC",
            file_name=f"SDN_ADVANCED_{datetime.utcnow().strftime('%Y-%m-%d')}.xml",
            file_hash=fhash,
            record_count=0,
            status="PROCESSING"
        )
        db.add(snap)
        db.commit()

        ofac_relations: list = []
        record_count = persist_pivot_items(
            db, snap_id, parse_ofac_advanced_xml(str(temp_file), relations_out=ofac_relations),
            progress=tracker.persisting(db, snap_id),
        )
        # Graphe de relations entre profils (ownership) rafraichi avec la liste
        if ofac_relations:
            from fiskr.database import refresh_source_relationships
            refresh_source_relationships(db, "OFAC", ofac_relations)
        # Le snapshot a pu etre expire par les commits periodiques : on le
        # relit avant d'ecrire, sinon statut et compteur ne seraient pas
        # persistes (liste jamais mise en production, sans erreur visible)
        snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
        # Mode homologation : le snapshot attend un pointage humain, l'ancienne
        # liste READY reste en production jusqu'a l'approbation.
        staging = require_approval_enabled(db)
        snap.status = "PENDING_REVIEW" if staging else "READY"
        snap.record_count = record_count
        db.commit()

        # Delta par rapport a la liste active (= production, non supersedee)
        tracker.phase("DELTA", processed=record_count, snapshot_id=snap_id)
        old_entities = _snapshot_entity_dicts(db, previous_id) if previous_id else []
        new_entities = _snapshot_entity_dicts(db, snap_id)
        delta = calculate_delta(old_entities, new_entities, "entity_id")

        if not staging:
            # Application immediate (remplacement de la liste OFAC active)
            _supersede_previous_snapshots(db, "WATCHLIST_OFAC", snap_id)
            db.commit()
            if reload_cache:
                tracker.phase("RELOAD", processed=record_count, snapshot_id=snap_id)
                reload_cache()

        summary = delta["summary"]
        if staging:
            message = (
                f"{record_count} fiches importees depuis le fichier OFAC officiel, "
                "snapshot en attente d'homologation (pointage humain requis)."
            )
        else:
            message = f"{record_count} fiches importees depuis le fichier OFAC officiel."
        return _finalize_report(
            db, source="OFAC", trigger=trigger, status="PENDING_REVIEW" if staging else "SUCCESS",
            message=message,
            snapshot_id=snap_id,
            previous_snapshot_id=previous_id,
            added_count=summary["added_count"],
            modified_count=summary["modified_count"],
            removed_count=summary["removed_count"],
            delta_report=_truncate_delta_details(delta)
        )
    except Exception as e:
        db.rollback()
        tracker.failed(e)
        logger.error(f"Echec de la synchronisation OFAC: {e}")
        if snap_id:
            error_snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
            if error_snap:
                error_snap.status = "ERROR"
                db.commit()
        return _finalize_report(
            db, source="OFAC", trigger=trigger, status="ERROR",
            message=f"Echec: {e}"
        )
    finally:
        tracker.done()
        if temp_file.exists():
            os.remove(temp_file)


def run_dgt_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge le registre national des gels des avoirs (DGT, JSON officiel),
    l'ingere en snapshot, calcule le delta par rapport a la liste active et
    applique le remplacement (ou attend l'homologation si le mode est actif).
    """
    cfg = get_sync_config()["dgt"]
    url = cfg["url"]
    fetch = fetcher or download_to_file

    temp_dir = PROJECT_ROOT / "temp_ingestion"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f"dgt_sync_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.json"

    previous = _latest_ready_snapshot(db, "WATCHLIST_DGT")
    previous_id = previous.snapshot_id if previous else None
    snap_id = None
    tracker = SyncProgress("DGT")
    try:
        logger.info(f"Sync DGT: telechargement de {url}")
        if fetcher is None:
            download_to_file(url, temp_file, progress=tracker.downloading())
        else:
            fetch(url, temp_file)

        tracker.phase("HASH")
        hasher = hashlib.sha256()
        with open(temp_file, "rb") as f:
            while chunk := f.read(1024 * 1024):
                hasher.update(chunk)
        fhash = hasher.hexdigest()

        duplicate = _existing_snapshot_with_hash(db, "WATCHLIST_DGT", fhash)
        if duplicate:
            if duplicate.status == "PENDING_REVIEW":
                message = "Le registre DGT est identique a un snapshot deja en attente d'homologation."
            else:
                message = "Le registre DGT est identique a la version active (hash inchange)."
            return _finalize_report(
                db, source="DGT", trigger=trigger, status="NO_CHANGE",
                message=message,
                previous_snapshot_id=duplicate.snapshot_id
            )

        snap_id = f"dgt-sync-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        snap = Snapshot(
            snapshot_id=snap_id,
            file_type="WATCHLIST_DGT",
            file_name=f"Registre_gels_DGT_{datetime.utcnow().strftime('%Y-%m-%d')}.json",
            file_hash=fhash,
            record_count=0,
            status="PROCESSING"
        )
        db.add(snap)
        db.commit()

        record_count = persist_pivot_items(db, snap_id, parse_dgt_gels_json(str(temp_file)),
                                           progress=tracker.persisting(db, snap_id))
        # Relecture avant ecriture (cf. commentaire OFAC)
        snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
        staging = require_approval_enabled(db)
        snap.status = "PENDING_REVIEW" if staging else "READY"
        snap.record_count = record_count
        db.commit()

        # Delta par rapport a la liste active (= production, non supersedee)
        tracker.phase("DELTA", processed=record_count, snapshot_id=snap_id)
        old_entities = _snapshot_entity_dicts(db, previous_id) if previous_id else []
        new_entities = _snapshot_entity_dicts(db, snap_id)
        delta = calculate_delta(old_entities, new_entities, "entity_id")

        if not staging:
            _supersede_previous_snapshots(db, "WATCHLIST_DGT", snap_id)
            db.commit()
            if reload_cache:
                tracker.phase("RELOAD", processed=record_count, snapshot_id=snap_id)
                reload_cache()

        summary = delta["summary"]
        if staging:
            message = (
                f"{record_count} fiches importees depuis le registre national des gels (DGT), "
                "snapshot en attente d'homologation (pointage humain requis)."
            )
        else:
            message = f"{record_count} fiches importees depuis le registre national des gels (DGT)."
        return _finalize_report(
            db, source="DGT", trigger=trigger, status="PENDING_REVIEW" if staging else "SUCCESS",
            message=message,
            snapshot_id=snap_id,
            previous_snapshot_id=previous_id,
            added_count=summary["added_count"],
            modified_count=summary["modified_count"],
            removed_count=summary["removed_count"],
            delta_report=_truncate_delta_details(delta)
        )
    except Exception as e:
        db.rollback()
        tracker.failed(e)
        logger.error(f"Echec de la synchronisation DGT: {e}")
        if snap_id:
            error_snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
            if error_snap:
                error_snap.status = "ERROR"
                db.commit()
        return _finalize_report(
            db, source="DGT", trigger=trigger, status="ERROR",
            message=f"Echec: {e}"
        )
    finally:
        tracker.done()
        if temp_file.exists():
            os.remove(temp_file)


def _run_list_replacement_sync(
    db,
    source: str,
    file_type: str,
    url: str,
    parser: Callable[[str], Any],
    file_label: str,
    temp_suffix: str,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
    auth_headers: Optional[Dict[str, str]] = None,
) -> SyncReport:
    """
    Cycle generique de synchronisation d'une liste officielle a remplacement
    complet : telechargement, deduplication par hash (y compris snapshots en
    attente d'homologation), ingestion, delta par rapport a la liste active,
    puis application (supersede + rechargement du cache) ou attente de
    pointage humain si le mode homologation est actif.

    `auth_headers` : en-tetes d'authentification d'un flux sous cle
    (sync.<source>.auth_headers) — transmis au telechargement uniquement.
    """
    tracker = SyncProgress(source)
    fetch = fetcher or download_to_file
    temp_dir = PROJECT_ROOT / "temp_ingestion"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f"{source.lower()}_sync_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{temp_suffix}"

    previous = _latest_ready_snapshot(db, file_type)
    previous_id = previous.snapshot_id if previous else None
    snap_id = None
    try:
        logger.info(f"Sync {source}: telechargement de {url}")
        if fetcher is None:
            # Telechargement instrumente (octets recus / taille annoncee) et
            # CONDITIONNEL : si la source n'a pas bouge, elle repond 304 et
            # rien n'est retelecharge.
            outcome = download_to_file(url, temp_file, progress=tracker.downloading(),
                                       validators=stored_validators(db, source),
                                       headers=auth_headers) or {}
            if outcome.get("not_modified"):
                logger.info(f"Sync {source}: source inchangee (HTTP 304), aucun telechargement.")
                return _finalize_report(
                    db, source=source, trigger=trigger, status="NO_CHANGE",
                    message=f"Source {source} inchangee depuis le dernier passage "
                            f"(réponse 304 du serveur, aucun téléchargement).",
                    previous_snapshot_id=previous_id
                )
            remember_validators(db, source, outcome.get("etag"), outcome.get("last_modified"))
        else:
            fetch(url, temp_file)

        tracker.phase("HASH")
        hasher = hashlib.sha256()
        with open(temp_file, "rb") as f:
            while chunk := f.read(1024 * 1024):
                hasher.update(chunk)
        fhash = hasher.hexdigest()

        duplicate = _existing_snapshot_with_hash(db, file_type, fhash)
        if duplicate:
            if duplicate.status == "PENDING_REVIEW":
                message = f"Le fichier {source} est identique a un snapshot deja en attente d'homologation."
            else:
                message = f"Le fichier {source} est identique a la version active (hash inchange)."
            return _finalize_report(
                db, source=source, trigger=trigger, status="NO_CHANGE",
                message=message,
                previous_snapshot_id=duplicate.snapshot_id
            )

        snap_id = f"{source.lower()}-sync-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        snap = Snapshot(
            snapshot_id=snap_id,
            file_type=file_type,
            file_name=f"{file_label}_{datetime.utcnow().strftime('%Y-%m-%d')}{temp_suffix}",
            file_hash=fhash,
            record_count=0,
            status="PROCESSING"
        )
        db.add(snap)
        db.commit()

        record_count = persist_pivot_items(db, snap_id, parser(str(temp_file)),
                                           progress=tracker.persisting(db, snap_id))
        # Le snapshot a pu etre detache par les commits periodiques
        snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
        staging = require_approval_enabled(db)
        snap.status = "PENDING_REVIEW" if staging else "READY"
        snap.record_count = record_count
        snap.processed_count = record_count
        snap.phase = "DELTA"
        db.commit()

        tracker.phase("DELTA", processed=record_count, snapshot_id=snap_id)
        old_entities = _snapshot_entity_dicts(db, previous_id) if previous_id else []
        new_entities = _snapshot_entity_dicts(db, snap_id)
        delta = calculate_delta(old_entities, new_entities, "entity_id")

        if not staging:
            _supersede_previous_snapshots(db, file_type, snap_id)
            db.commit()
            if reload_cache:
                tracker.phase("RELOAD", processed=record_count, snapshot_id=snap_id)
                reload_cache()
        snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
        if snap:
            snap.phase = "DONE"
            db.commit()

        summary = delta["summary"]
        message = f"{record_count} fiches importees depuis la source {source}."
        if staging:
            message += " Snapshot en attente d'homologation (pointage humain requis)."
        return _finalize_report(
            db, source=source, trigger=trigger, status="PENDING_REVIEW" if staging else "SUCCESS",
            message=message,
            snapshot_id=snap_id,
            previous_snapshot_id=previous_id,
            added_count=summary["added_count"],
            modified_count=summary["modified_count"],
            removed_count=summary["removed_count"],
            delta_report=_truncate_delta_details(delta)
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Echec de la synchronisation {source}: {e}")
        tracker.failed(e)
        if snap_id:
            error_snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
            if error_snap:
                error_snap.status = "ERROR"
                db.commit()
        return _finalize_report(
            db, source=source, trigger=trigger, status="ERROR",
            message=f"Echec: {e}"
        )
    finally:
        tracker.done()
        if temp_file.exists():
            os.remove(temp_file)


def run_eu_fsf_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste consolidee officielle des sanctions financieres de
    l'UE (fichiers FSF du webgate FSD) et remplace la liste EU active. Fait
    autorite sur le scraping du Journal Officiel : les radiations y sont
    fiables. Necessite un token (inscription gratuite au webgate).
    """
    cfg = get_sync_config()["eu_fsf"]
    url = cfg["url"]
    if "{token}" in url:
        if not cfg["token"]:
            return _finalize_report(
                db, source="EUFSF", trigger=trigger, status="ERROR",
                message=(
                    "Token FSF non configure : creez un compte gratuit sur le webgate FSD "
                    "de la Commission europeenne puis renseignez sync.eu_fsf.token dans config.yaml."
                )
            )
        url = url.replace("{token}", cfg["token"])
    return _run_list_replacement_sync(
        db, source="EUFSF", file_type="WATCHLIST_EU", url=url,
        parser=parse_eu_fsf_xml, file_label="EU_FSF_Consolidated",
        temp_suffix=".xml", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


def run_un_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste consolidee du Conseil de securite de l'ONU (XML
    public officiel) et remplace la liste ONU active.
    """
    cfg = get_sync_config()["un"]
    return _run_list_replacement_sync(
        db, source="UN", file_type="WATCHLIST_UN", url=cfg["url"],
        parser=parse_un_consolidated_xml, file_label="UN_Consolidated",
        temp_suffix=".xml", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


def run_pep_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge le dataset PEP OpenSanctions (targets.simple.csv) et remplace
    la liste PEP active. Usage non commercial libre ; licence OpenSanctions
    requise pour un usage commercial.
    """
    cfg = get_sync_config()["pep"]
    return _run_list_replacement_sync(
        db, source="PEP", file_type="WATCHLIST_PEP", url=cfg["url"],
        parser=parse_pep_targets_csv, file_label="OpenSanctions_PEP",
        temp_suffix=".csv", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


def run_ofsi_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste consolidee UK OFSI (ConList.csv, format 2022) et
    remplace la liste OFSI active.
    """
    cfg = get_sync_config()["ofsi"]
    return _run_list_replacement_sync(
        db, source="OFSI", file_type="WATCHLIST_OFSI", url=cfg["url"],
        parser=parse_ofsi_conlist_csv, file_label="UK_OFSI_ConList",
        temp_suffix=".csv", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


def run_ofac_nonsdn_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste consolidee Non-SDN de l'OFAC (CONS_ADVANCED.XML) et
    remplace la liste Non-SDN active.

    C'est le pendant de la SDN : memes obligations de criblage, mais des
    regimes qui n'emportent pas de gel total des avoirs (sanctions
    sectorielles SSI, FSE, NS-MBS, PLC, MEU, CMIC) et qui sont pour cette
    raison absents du fichier SDN. Liste separee — `WATCHLIST_OFAC_NONSDN` —
    parce que la consequence operationnelle d'une touche n'y est pas la meme
    et qu'un etablissement doit pouvoir la seuiller a part.
    """
    cfg = get_sync_config()["ofac_nonsdn"]
    return _run_list_replacement_sync(
        db, source="OFACNONSDN", file_type="WATCHLIST_OFAC_NONSDN", url=cfg["url"],
        parser=parse_ofac_consolidated_xml, file_label="OFAC_Consolidated_NonSDN",
        temp_suffix=".xml", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


def run_csl_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la Consolidated Screening List americaine (trade.gov) et
    remplace la liste CSL active.

    L'agregat contient la SDN, que Fiskr recupere deja aupres de l'OFAC : les
    libelles de `sync.csl.exclude_sources` sont ecartes a la lecture pour ne
    pas doubler les alertes. Ce qui reste est l'apport propre de la CSL — les
    listes de controle des exportations (BIS, Departement d'Etat), absentes de
    toutes les autres sources branchees.
    """
    cfg = get_sync_config()["csl"]
    excluded = cfg["exclude_sources"]
    return _run_list_replacement_sync(
        db, source="CSL", file_type="WATCHLIST_CSL", url=cfg["url"],
        parser=lambda path: parse_csl_json(path, excluded_sources=excluded),
        file_label="US_Consolidated_Screening_List",
        temp_suffix=".json", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


def _alert_list_suffix(url: str) -> str:
    """Extension a donner au fichier telecharge : elle commande le lecteur.

    Les regulateurs servent leur liste tantot en page web, tantot en CSV ou en
    JSON, parfois sans extension dans l'URL. Le defaut est donc HTML, qui est
    la forme la plus repandue de ces listes.
    """
    path = url.lower().split("?")[0]
    for suffix in (".json", ".csv", ".xlsx", ".xlsm", ".html", ".htm"):
        if path.endswith(suffix):
            return suffix
    return ".html"


def run_hk_sfc_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste d'alerte de la Securities and Futures Commission de
    Hong Kong et remplace la liste HK-SFC active.

    Ce n'est PAS une liste de sanctions : la SFC y recense les entites non
    autorisees, les sites frauduleux et les usurpations d'identite
    d'intermediaires agrees. Une touche est un signal de risque a instruire,
    pas une obligation de gel — d'ou un type de liste distinct, seuillable a
    part via `scoring.cut_off_overrides`.
    """
    cfg = get_sync_config()["hk_sfc"]
    return _run_list_replacement_sync(
        db, source="HKSFC", file_type="WATCHLIST_HK_SFC", url=cfg["url"],
        parser=parse_hk_sfc_alert_list, file_label="HK_SFC_Alert_List",
        temp_suffix=_alert_list_suffix(cfg["url"]), trigger=trigger,
        fetcher=fetcher, reload_cache=reload_cache
    )


def run_amf_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge les listes noires de l'Autorite des marches financiers et
    remplace la liste AMF active. Meme nature que HK-SFC : mise en garde, pas
    gel des avoirs.
    """
    cfg = get_sync_config()["amf"]
    return _run_list_replacement_sync(
        db, source="AMF", file_type="WATCHLIST_AMF", url=cfg["url"],
        parser=parse_amf_blacklist, file_label="AMF_Listes_Noires",
        temp_suffix=_alert_list_suffix(cfg["url"]), trigger=trigger,
        fetcher=fetcher, reload_cache=reload_cache
    )


def run_worldbank_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste des fournisseurs et personnes exclus des marches
    finances par le Groupe de la Banque mondiale.

    Troisieme nature encore : ni gel, ni mise en garde — une exclusion
    prononcee pour fraude ou corruption averee. La date de fin d'exclusion est
    portee par `delisted_on`, la colonne prevue pour cela.
    """
    cfg = get_sync_config()["worldbank"]
    return _run_list_replacement_sync(
        db, source="WORLDBANK", file_type="WATCHLIST_WORLDBANK", url=cfg["url"],
        parser=parse_worldbank_debarred_json, file_label="WorldBank_Debarred",
        temp_suffix=".json", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


# ------------------ SOURCES DU REGISTRE OPENSANCTIONS ------------------
# Onze listes publiques (exclusions des banques multilaterales, Asie-
# Pacifique, gels terrorisme nationaux, Ukraine) branchees sur le lecteur
# `parse_opensanctions_simple_csv` deja utilise par PEP et SECO : UN chemin
# de code, teste, au lieu de onze parseurs de formats officiels exotiques.
# Le registre (fiskr/sources.py) est la seule source de verite ; ici, une
# fabrique produit les onze runners — pas onze copies.

def make_opensanctions_runner(run_key: str) -> Callable:
    """Fabrique le runner de synchronisation d'une source du registre."""
    import functools
    src = OPENSANCTIONS_BY_KEY[run_key]
    parser = functools.partial(
        parse_opensanctions_simple_csv,
        id_prefix=src.id_prefix, origin=src.origin,
        designation_reasons=src.designation_reasons,
    )

    def runner(db, trigger: str = "MANUAL",
               fetcher: Optional[Callable[[str, Path], None]] = None,
               reload_cache: Optional[Callable[[], None]] = None) -> SyncReport:
        cfg = get_sync_config()[run_key]
        return _run_list_replacement_sync(
            db, source=src.source, file_type=src.file_type, url=cfg["url"],
            parser=parser, file_label=f"{src.source}_OpenSanctions",
            temp_suffix=".csv", trigger=trigger, fetcher=fetcher,
            reload_cache=reload_cache, auth_headers=cfg.get("auth_headers") or None,
        )

    runner.__name__ = f"run_{run_key}_sync"
    runner.__doc__ = (f"{src.label} — dataset OpenSanctions `{src.dataset}` "
                      f"(format plat ; source officielle native : {src.official_url}).")
    return runner


OPENSANCTIONS_RUNNERS: Dict[str, Callable] = {
    key: make_opensanctions_runner(key) for key in OPENSANCTIONS_BY_KEY
}


def run_canada_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste consolidee des sanctions autonomes canadiennes (SEMA)
    et remplace la liste canadienne active.

    Le fichier existe en anglais et en francais ; le lecteur accepte les deux
    jeux d'intitules de colonnes, pour qu'un telechargement depuis la page
    francophone ne produise pas une liste vide.
    """
    cfg = get_sync_config()["canada"]
    return _run_list_replacement_sync(
        db, source="CANADA", file_type="WATCHLIST_CANADA", url=cfg["url"],
        parser=parse_canada_sema_csv, file_label="Canada_SEMA_Consolidated",
        temp_suffix=".csv", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


def run_dfat_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste consolidee australienne (DFAT) et remplace la liste
    australienne active.

    Le format suit l'extension de l'URL configuree : `.csv` par defaut, `.xlsx`
    si l'etablissement prefere le classeur publie — ce dernier demande le
    paquet optionnel openpyxl, dont l'absence produit un rapport d'erreur
    explicite plutot qu'une pile d'appels.
    """
    cfg = get_sync_config()["dfat"]
    suffix = ".xlsx" if cfg["url"].lower().split("?")[0].endswith((".xlsx", ".xlsm")) else ".csv"
    return _run_list_replacement_sync(
        db, source="DFAT", file_type="WATCHLIST_DFAT", url=cfg["url"],
        parser=parse_dfat_consolidated, file_label="Australia_DFAT_Consolidated",
        temp_suffix=suffix, trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


def run_seco_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste consolidee suisse (SECO) et remplace la liste SECO
    active.

    Deux voies au choix (`sync.seco.format`), qui produisent le meme schema
    pivot et donc le meme criblage :
      - `xml`           : export officiel SESAM de la Confederation. Voie qui
                          fait foi ; elle seule porte la base legale suisse
                          (ordonnance RS) et les dates d'inscription.
      - `opensanctions` : jeu `ch_seco_sanctions` au format targets.simple.csv.
                          Voie de secours a format plat, soumise a la licence
                          OpenSanctions pour un usage commercial.
    """
    cfg = get_sync_config()["seco"]
    if cfg["format"] == "opensanctions":
        parser, label, suffix = parse_seco_opensanctions_csv, "SECO_OpenSanctions", ".csv"
    else:
        parser, label, suffix = parse_seco_xml, "SECO_Gesamtliste", ".xml"
    return _run_list_replacement_sync(
        db, source="SECO", file_type="WATCHLIST_SECO", url=cfg["url"],
        parser=parser, file_label=label,
        temp_suffix=suffix, trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


# ------------------ SCRAPING EUR-LEX ------------------

class _HTMLDocumentExtractor(HTMLParser):
    """Extrait les liens (href, texte) et les tableaux (lignes de cellules) d'une page HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: List[Tuple[str, str]] = []
        self.tables: List[List[List[str]]] = []
        self._link_href = None
        self._link_text: List[str] = []
        self._table_stack: List[List[List[str]]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._link_href = dict(attrs).get("href")
            self._link_text = []
        elif tag == "table":
            self._table_stack.append([])
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "a" and self._link_href is not None:
            text = re.sub(r"\s+", " ", " ".join(self._link_text)).strip()
            if text:
                self.links.append((self._link_href, text))
            self._link_href = None
            self._link_text = []
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", " ".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table_stack:
            if any(c for c in self._row):
                self._table_stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._table_stack:
            table = self._table_stack.pop()
            if table:
                self.tables.append(table)

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        if self._link_href is not None:
            self._link_text.append(data)


def _strip_accents_lower(text: str) -> str:
    normalized = unicodedata.normalize("NFD", (text or "").replace("’", "'"))
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn").lower()


def extract_daily_acts(html: str, base_url: str, keyword: str = DEFAULT_EURLEX_KEYWORD) -> List[Dict[str, str]]:
    """
    Extrait de la page du Journal Officiel du jour les actes dont le titre
    mentionne le mot-cle (par defaut "mesures restrictives").
    """
    parser = _HTMLDocumentExtractor()
    parser.feed(html)

    keyword_norm = _strip_accents_lower(keyword)
    acts = []
    seen = set()
    for href, text in parser.links:
        if keyword_norm not in _strip_accents_lower(text):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        acts.append({"title": text, "url": url})
    return acts


_TYPE_KEYWORDS = [
    ("V", ["navire", "vessel", "ship", "imo", "tanker", "petrolier", "pétrolier",
           "flotte fantome", "shadow fleet", "mmsi", "pavillon", "flag of"]),
    ("O", ["aeronef", "aéronef", "aircraft", "immatriculation de l'aeronef", "tail number"]),
    ("E", ["entite", "entité", "entity", "societe", "société", "organisation", "organisme",
           "company", "corporation", "enterprise", "subsidiary", "filiale", "holding",
           "incorporated", "registered in", "immatriculee", "enregistree", "state-owned",
           "joint stock", "sarl", "llc", "ltd", "gmbh", "fze"]),
]


def _detect_entity_type(context_text: str) -> str:
    """
    Determine le type du liste (I/E/V/O) a partir de toute la ligne d'annexe :
    informations d'identification ET motifs de la designation. Les indices
    personnels (date/lieu de naissance, pronoms, fonctions) priment sur les
    mots-cles d'entites ou de navires cites dans les motifs.
    """
    ctx = _strip_accents_lower(context_text)
    if _PERSONAL_INDICATORS.search(ctx):
        return "I"
    for etype, keywords in _TYPE_KEYWORDS:
        for kw in keywords:
            # Correspondance sur mot entier ("ship" ne doit pas matcher "SHIPPING")
            if re.search(rf"\b{re.escape(_strip_accents_lower(kw))}\b", ctx):
                return etype
    return "I"


def _stable_eu_entity_id(name: str) -> str:
    digest = hashlib.sha1(_strip_accents_lower(name).encode("utf-8")).hexdigest()
    return f"EU-{digest[:12].upper()}"


_DOB_PATTERN = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{4})|(\d{4})-(\d{2})-(\d{2})")
_IMO_PATTERN = re.compile(r"IMO\D{0,3}(\d{7})", re.IGNORECASE)
_TAIL_PATTERN = re.compile(r"immatriculation\D{0,10}([A-Z0-9\-]{4,10})", re.IGNORECASE)


def _extract_dob(text: str) -> Optional[str]:
    m = _DOB_PATTERN.search(text)
    if not m:
        return None
    if m.group(4):
        return f"{m.group(4)}-{m.group(5)}-{m.group(6)}"
    return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"


# Tournures editoriales des actes (considerants, references, mise en page)
# a ne pas confondre avec des noms
_NON_NAME_PATTERNS = (
    # Francais
    "journal officiel", "union europeenne", "il y a ", "vu le ", "vu la ", "considerant",
    "le conseil ", "la commission ", "conformement ", "annex", "serie l",
    "informations d'identification", "translitteration", "caracteres latins",
    # Anglais (version de reference du JO)
    "official journal", "european union", "having regard", "whereas",
    "the council", "the commission", "in accordance", "identifying information",
    "transliteration", "latin characters", "latin script", "l series",
    "should therefore", "as follows",
)

# En-tete de la colonne des motifs dans les annexes (FR/EN)
_MOTIFS_HEADER = re.compile(r"motif|reasons|grounds", re.IGNORECASE)

# Instructions d'amendement citant du texte de liste ("la mention suivante est
# remplacee par...") : leurs lignes ne decrivent pas un liste
_AMENDMENT_CONTEXT = re.compile(
    r"(est|sont) (remplace|ajoute|supprime|modifie)|texte suivant|mention suivante|"
    r"rubrique suivante|entree suivante|(is|are) (replaced|added|deleted|amended)",
    re.IGNORECASE
)

# Indices d'attributs personnels, y compris dans les motifs de la designation
# (pronoms, fonctions, professions) : ils priment sur les mots-cles d'entites
# presents dans la meme ligne (ex: "dirigeant d'une entite")
_PERSONAL_INDICATORS = re.compile(
    r"date de naissance|lieu de naissance|date of birth|place of birth|"
    r"\bn[ée]e? le\b|\bborn\b|nationalit[ey]|sexe\s*:|gender\s*:|"
    r"\b(he|she) (is|was|has)\b|\bil est\b|\belle est\b|"
    r"minist(re|er)|oligar(que|ch)|homme d'affaires|femme d'affaires|"
    r"business(man|woman)|propagandist|ressortissant",
    re.IGNORECASE
)


def _looks_like_name(value: str) -> bool:
    v = value.strip()
    if len(v) < 3 or len(v) > 120:
        return False
    if not re.search(r"[A-Za-zÀ-ÿ]{2}", v):
        return False
    # Exclut les numeros d'ordre, dates et libelles de colonnes
    if re.fullmatch(r"[\d\s./-]+", v):
        return False
    normalized = _strip_accents_lower(v)
    if normalized in ("nom", "noms", "name", "names", "nom complet", "full name", "designation",
                      "type", "identite", "identity", "reasons", "grounds",
                      "limited liability company"):
        return False
    # Libelles de colonnes, references d'actes et formules juridiques (FR + EN)
    if normalized.startswith(("motifs", "date d", "date de", "lieu d", "informations",
                              "sont ", "tous les", "les fonds", "en russe", "en anglais",
                              "reasons", "grounds", "date of", "place of", "identifying",
                              "statement of", "in russian", "in english", "all funds",
                              "funds and", "name of", "regulation", "decision", "directive",
                              "reglement", "implementing")):
        return False
    # Les phrases longues sont du texte editorial, pas des identites
    if len(v.split()) > 8:
        return False
    if any(p in normalized for p in _NON_NAME_PATTERNS):
        return False
    return True


def scrape_act_entities(html: str, act_title: str = "", act_url: str = "") -> List[Dict[str, Any]]:
    """
    Analyse heuristique d'un acte EUR-Lex (annexes de reglements de mesures
    restrictives) pour en extraire les listes au schema pivot Fiskr.
    Couvre les annexes tabulaires (Nom / Informations / Motifs / Date) et les
    listes numerotees en texte.
    """
    parser = _HTMLDocumentExtractor()
    parser.feed(html)

    entities: Dict[str, Dict[str, Any]] = {}

    def register(name: str, context: str, reasons: Optional[str] = None):
        # Les lignes d'instructions d'amendement citent du texte de liste
        # entre guillemets : ce ne sont pas des listes
        if _AMENDMENT_CONTEXT.search(_strip_accents_lower(context)):
            return
        # Retire les guillemets (typographiques inclus) avant analyse
        name = re.sub(r"[«»“”\"]", " ", name)
        # Ne conserve que le segment latin du nom (les translitterations
        # cyrilliques/arabes accolees dans la meme cellule sont ecartees)
        name = name.strip()
        latin = re.match(r"^[A-Za-zÀ-ÿ0-9\s'’.,()\-/]+", name)
        if latin:
            name = latin.group(0)
        # Retire les mentions de langue tronquees par la coupe ci-dessus, avec ou
        # sans parenthese et dans les deux syntaxes : "Anton USOV en russe : ..."
        # (FR) et "Maria DUDKO (Russian: ...)" (EN)
        name = re.sub(
            r"\s*[\(«\"]?\s*\b((en|in)\s+)?(russe|anglais|ukrainien|bielorusse|arabe|persan|farsi|"
            r"russian|english|ukrainian|belarusian|arabic|persian)\b(\s*[:)].*)?$",
            "", name, flags=re.IGNORECASE
        )
        name = re.sub(
            r"\s*[\(«\"]?\s*\b(en|in)\s+(russe|anglais|ukrainien|bielorusse|arabe|persan|farsi|"
            r"russian|english|ukrainian|belarusian|arabic|persian)\b.*$",
            "", name, flags=re.IGNORECASE
        )
        name = re.sub(r"\s*\(\s*(en|in)\s+[^)]*\)?\s*$", "", name, flags=re.IGNORECASE)
        name = re.sub(r"\s+", " ", name).strip().strip("«»\"").rstrip(".;,(")
        if not _looks_like_name(name):
            return
        etype = _detect_entity_type(context)
        dob = _extract_dob(context) if etype == "I" else None
        imo = None
        tail = None
        if etype == "V":
            imo_match = _IMO_PATTERN.search(context)
            imo = imo_match.group(1) if imo_match else None
        if etype == "O":
            tail_match = _TAIL_PATTERN.search(context)
            tail = tail_match.group(1) if tail_match else None

        entity_id = _stable_eu_entity_id(name)
        # Une fiche deja enrichie (DOB connue) n'est pas ecrasee par une occurrence plus pauvre
        existing = entities.get(entity_id)
        if existing and existing.get("dates_of_birth") and not dob:
            return
        item = {
            "entity_id": entity_id,
            "entity_type": etype,
            "primary_name": name,
            "individual_name_parsed": parse_individual_name(name) if etype == "I" else {"first_name": "", "last_name": "", "maiden_name": ""},
            "aliases": {"high_priority": [], "low_priority": []},
            "dates_of_birth": [dob] if dob else [],
            "date_of_death": None,
            "is_deceased": False,
            "gender": "U",
            "countries": {"citizenship": [], "residence": [], "birth_country": [], "jurisdiction_country": []},
            "imo_number": imo,
            "aircraft_tail_number": tail,
            "origin": f"EUR-Lex - {act_title}" if act_title else "EUR-Lex",
            "designation_reasons": reasons,
            "additional_informations": act_url or None,
        }
        entities[entity_id] = item

    # 1. Annexes tabulaires : la premiere colonne plausible porte le nom,
    #    le reste de la ligne sert de contexte (type, DOB, IMO...).
    #    La ligne d'en-tete sert a localiser la colonne "Motifs de la designation".
    for table in parser.tables:
        motifs_idx = None
        for row in table:
            norm_cells = [_strip_accents_lower(c) for c in row]
            if any(_MOTIFS_HEADER.search(c) for c in norm_cells) and \
               any(c in ("nom", "name", "identite", "identity") for c in norm_cells):
                motifs_idx = next(i for i, c in enumerate(norm_cells) if _MOTIFS_HEADER.search(c))
                break

        for row in table:
            if not row:
                continue
            row_context = " | ".join(row)
            # Ignore les lignes d'en-tete
            header_like = all(not _looks_like_name(c) or _strip_accents_lower(c) in
                              ("nom", "name", "informations d'identification", "motifs", "type")
                              for c in row)
            if header_like:
                continue
            name_cell = next((c for c in row if _looks_like_name(c)), None)
            if not name_cell:
                continue
            reasons = None
            if motifs_idx is not None and motifs_idx < len(row):
                candidate = row[motifs_idx].strip()
                if candidate and candidate != name_cell and not _MOTIFS_HEADER.search(_strip_accents_lower(candidate)):
                    reasons = candidate
            register(name_cell, row_context, reasons)

    # 2. Repli sur les listes numerotees en texte brut (ex: "12. DUPONT Jean (alias ...)"),
    #    uniquement si les annexes tabulaires n'ont rien donne : le corps des actes
    #    (considerants numerotes) genererait sinon des faux positifs.
    if not entities:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        for m in re.finditer(r"\b\d{1,3}\.\s+([A-ZÀ-Ý][A-Za-zÀ-ÿ'\-\. ]{2,80}?)(?:\s*\(([^)]{0,200})\)|[,;])", text):
            name, extra = m.group(1), m.group(2) or ""
            register(name, f"{name} {extra}")

    return list(entities.values())


def _act_pdf_url(act_url: str) -> str:
    """URL du PDF officiel d'un acte EUR-Lex (…/legal-content/EN/TXT/PDF/?uri=…)."""
    if "/TXT/PDF/" in act_url:
        return act_url
    return re.sub(r"/TXT(/HTML)?/", "/TXT/PDF/", act_url)


def _archive_act_pdf(act: Dict[str, str], pdf_fetcher: Callable[[str, Path], None],
                     archive_dir: Path) -> bool:
    """
    Telecharge et archive le PDF officiel de l'acte (version qui fait foi lors
    des audits), avec empreinte SHA-256 pour garantir son integrite probante.
    Un echec de telechargement n'interrompt pas la synchronisation.
    """
    match = re.search(r"uri=([^&]+)", act["url"])
    base_name = match.group(1) if match else hashlib.sha1(act["url"].encode()).hexdigest()[:12]
    filename = re.sub(r"[^A-Za-z0-9_.\-]", "_", base_name) + ".pdf"
    dest = archive_dir / filename
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            pdf_fetcher(_act_pdf_url(act["url"]), dest)
        with open(dest, "rb") as f:
            act["pdf_sha256"] = hashlib.sha256(f.read()).hexdigest()
        act["pdf_file"] = filename
        return True
    except Exception as e:
        logger.warning(f"Echec de l'archivage du PDF officiel {act['url']}: {e}")
        act["pdf_file"] = None
        if dest.exists():
            os.remove(dest)
        return False


def fetch_eurlex_acts(
    for_date: date,
    http_get: Callable[[str], str],
    daily_url_template: str,
    keyword: str,
) -> List[Dict[str, str]]:
    """
    Actes du Journal Officiel du jour dont le titre porte le mot-cle
    (« mesures restrictives »). UNE seule requete, sur la page du jour :
    c'est tout ce dont le mode « signal d'alerte precoce » a besoin.
    """
    daily_url = daily_url_template.format(date=for_date.strftime("%d%m%Y"))
    logger.info(f"Sync EUR-Lex: lecture du Journal Officiel {daily_url}")
    # Cookie de session avant la page du jour : sans lui, EUR-Lex sert son
    # interstitiel HTTP 202 et la lecture n'aboutit jamais
    warm_up_session(_portal_root(daily_url))
    daily_html = http_get(daily_url)
    return extract_daily_acts(daily_html, daily_url, keyword)


def fetch_eurlex_entities(
    for_date: date,
    http_get: Callable[[str], str],
    daily_url_template: str,
    keyword: str,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]], List[Dict[str, str]]]:
    """
    Recupere le JO du jour, filtre les actes "mesures restrictives" et scrape
    chacun d'eux. Retourne (actes retenus, entites extraites, actes en echec
    reseau) — les echecs ne sont plus avales : ils remontent au rapport de
    sync pour ne jamais presenter une liste amputee comme complete.

    `progress(actes_traites, total)` suit le scraping acte par acte : c'est la
    ou passe le temps d'une synchronisation EUR-Lex (une requete par acte),
    donc la seule progression qui ait du sens ici — un scraping n'annonce
    aucune taille de fichier.

    N'est utilise que par le mode `extract` (heuristique, cf. get_sync_config).
    """
    acts = fetch_eurlex_acts(for_date, http_get, daily_url_template, keyword)

    all_entities: Dict[str, Dict[str, Any]] = {}
    failed_acts: List[Dict[str, str]] = []
    for done, act in enumerate(acts, start=1):
        if progress:
            try:
                progress(done, len(acts))
            except Exception:
                pass  # la progression n'interrompt jamais un scraping
        try:
            act_html = http_get(act["url"])
        except Exception as e:
            logger.warning(f"Sync EUR-Lex: echec du chargement de l'acte {act['url']}: {e}")
            failed_acts.append({"url": act["url"], "title": act.get("title", ""), "error": str(e)})
            continue
        for ent in scrape_act_entities(act_html, act["title"], act["url"]):
            all_entities[ent["entity_id"]] = ent
    return acts, list(all_entities.values()), failed_acts


def _run_eurlex_alert(db, for_date: date, trigger: str, getter, pdf_getter,
                      archive_dir: Path, cfg: Dict[str, Any]) -> SyncReport:
    """
    Mode « signal d'alerte precoce » : detecte les actes de mesures
    restrictives parus au JO, archive leur PDF officiel, previent les
    homologateurs — et n'ecrit AUCUNE fiche listee.

    Le raisonnement : la liste consolidee (EUFSF) fait autorite sur les
    designations et porte les radiations ; le JO, lui, arrive en premier.
    Fiskr exploite donc chaque source pour ce qu'elle sait faire, au lieu de
    deduire des identites du texte juridique par expression reguliere.
    """
    tracker = SyncProgress("EURLEX")
    try:
        acts = fetch_eurlex_acts(for_date, getter, cfg["daily_journal_url"], cfg["keyword"])
        tracker.phase("DOWNLOAD", processed=0, total=len(acts))

        journal_day = for_date.strftime("%d/%m/%Y")
        if not acts:
            return _finalize_report(
                db, source="EURLEX", trigger=trigger, status="NO_PUBLICATION",
                message=f"Aucun acte mentionnant \"{cfg['keyword']}\" au JO du {journal_day}."
            )

        # Piece probante : le PDF officiel de chaque acte, empreinte SHA-256
        # a l'appui. Un echec d'archivage est une anomalie visible, jamais
        # silencieuse — c'est ce PDF qui fait foi devant un auditeur.
        pdf_failures = []
        for done, act in enumerate(acts, start=1):
            tracker.phase("DOWNLOAD", processed=done, total=len(acts))
            if not _archive_act_pdf(act, pdf_getter, archive_dir):
                pdf_failures.append(act["url"])

        # Sans source consolidee active, ce signal ne debouche sur rien : le
        # dire ICI, dans le rapport que l'exploitant lit, plutot que de
        # laisser la liste UE se perimer en silence.
        fsf_enabled = get_sync_config()["eu_fsf"]["enabled"]
        titles = "; ".join(a.get("title", "").strip()[:120] for a in acts[:3])
        message = (f"{len(acts)} acte(s) \"{cfg['keyword']}\" au JO du {journal_day} : {titles}"
                   + (" …" if len(acts) > 3 else "") + ".")
        if pdf_failures:
            message += f" ⚠ {len(pdf_failures)} PDF officiel(s) non archivé(s)."
        if not fsf_enabled:
            message += (" ⚠ La source consolidée EUFSF est désactivée : les désignations "
                        "de ces actes n'entreront donc dans aucune liste. Renseignez "
                        "sync.eu_fsf.token puis activez sync.eu_fsf.enabled.")

        from fiskr.notifier import emit
        emit(db, "eurlex_act_published", {
            "Journal Officiel": journal_day,
            "Actes": len(acts),
            "Titres": titles + (" …" if len(acts) > 3 else ""),
            "Liste consolidée EUFSF": "active" if fsf_enabled
                                      else "DÉSACTIVÉE — les désignations ne seront pas importées",
            "PDF archivés": f"{len(acts) - len(pdf_failures)} / {len(acts)}",
        })
        return _finalize_report(
            db, source="EURLEX", trigger=trigger, status="SUCCESS",
            message=message,
            delta_report={"mode": "alert", "acts": acts,
                          "pdf_failures": pdf_failures,
                          "eu_fsf_enabled": fsf_enabled}
        )
    except Exception as e:
        db.rollback()
        tracker.failed(e)
        logger.error(f"Echec de la surveillance EUR-Lex: {e}")
        return _finalize_report(
            db, source="EURLEX", trigger=trigger, status="ERROR",
            message=f"Echec: {e}"
        )
    finally:
        tracker.done()


def run_eurlex_sync(
    db,
    for_date: Optional[date] = None,
    trigger: str = "MANUAL",
    http_get: Optional[Callable[[str], str]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
    pdf_fetcher: Optional[Callable[[str, Path], None]] = None,
    archive_dir: Optional[Path] = None,
    mode: Optional[str] = None,
) -> SyncReport:
    """
    Surveillance du Journal Officiel de l'UE (version anglaise, qui fait
    reference), avec archivage des PDF officiels des actes retenus (valeur
    probante en audit). Deux modes, regles par `sync.eurlex.mode` (ou par
    l'argument `mode`, qui prime — utile pour forcer une passe ponctuelle) :

    - **alert** (defaut) : SIGNAL D'ALERTE PRECOCE. Signale qu'un acte de
      mesures restrictives est paru, et s'arrete la. Aucune designation n'est
      deduite du texte : elles viennent de la liste consolidee officielle
      (EUFSF), qui fait autorite et porte les radiations. Une seule requete
      HTTP pour la page du jour, plus une par PDF archive.

    - **extract** : comportement historique. Scrape les annexes de chaque
      acte et en deduit des listes par heuristique. Conserve pour ne pas
      priver de source une installation sans token FSF — mais ce qui en sort
      sont des SUPPOSITIONS : `_looks_like_name` decide par expression
      reguliere si une chaine est un nom, et les radiations ne sont jamais
      appliquees. A n'utiliser qu'en attendant le token FSF.
    """
    cfg = get_sync_config()["eurlex"]
    for_date = for_date or date.today()
    getter = http_get or http_get_text
    pdf_getter = pdf_fetcher or download_to_file
    archive_dir = archive_dir or EURLEX_ARCHIVE_DIR

    effective_mode = (mode or cfg.get("mode") or "alert").lower()
    if effective_mode != "extract":
        return _run_eurlex_alert(db, for_date, trigger, getter, pdf_getter, archive_dir, cfg)

    # Base de fusion : inclut un eventuel snapshot en attente d'homologation pour
    # que les amendements de jours successifs s'enchainent sans perte.
    previous = _latest_reviewable_snapshot(db, "WATCHLIST_EU")
    previous_id = previous.snapshot_id if previous else None
    snap_id = None
    tracker = SyncProgress("EURLEX")
    try:
        acts, scraped, failed_acts = fetch_eurlex_entities(
            for_date, getter, cfg["daily_journal_url"], cfg["keyword"],
            progress=lambda done, total: tracker.phase("DOWNLOAD", processed=done, total=total))

        # Archivage probant : le PDF officiel de chaque acte retenu fait foi.
        # Les echecs de telechargement sont restitues au rapport (piece
        # probante manquante = anomalie visible, plus jamais silencieuse).
        pdf_failures = []
        for act in acts:
            if not _archive_act_pdf(act, pdf_getter, archive_dir):
                pdf_failures.append(act["url"])

        if not acts:
            return _finalize_report(
                db, source="EURLEX", trigger=trigger, status="NO_PUBLICATION",
                message=f"Aucun acte mentionnant \"{cfg['keyword']}\" au JO du {for_date.strftime('%d/%m/%Y')}.",
                previous_snapshot_id=previous_id
            )
        if failed_acts and not scraped:
            # Tous les actes retenus sont inaccessibles : c'est une PANNE
            # reseau, pas un JO sans listes — rapport ERROR, pas NO_CHANGE
            return _finalize_report(
                db, source="EURLEX", trigger=trigger, status="ERROR",
                message=f"{len(failed_acts)} acte(s) inaccessibles au JO du "
                        f"{for_date.strftime('%d/%m/%Y')} (erreurs de connexion) : "
                        + " ; ".join(f["url"] for f in failed_acts[:3])
                        + (" …" if len(failed_acts) > 3 else ""),
                previous_snapshot_id=previous_id,
                delta_report={"acts": acts, "fetch_failures": failed_acts}
            )
        if not scraped:
            return _finalize_report(
                db, source="EURLEX", trigger=trigger, status="NO_CHANGE",
                message=f"{len(acts)} acte(s) trouve(s) au JO du {for_date.strftime('%d/%m/%Y')} mais aucun liste extrait.",
                previous_snapshot_id=previous_id,
                delta_report={"acts": acts}
            )

        # Fusion incrementale : les fiches actives sont reconduites, les fiches
        # scrapees du jour ajoutent ou remplacent (cle = entity_id stable)
        scraped_ids = {e["entity_id"] for e in scraped}
        snap_id = f"eurlex-sync-{for_date.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        content_hash = hashlib.sha256(
            "|".join(sorted(e["entity_id"] + (compute_checksum(e)) for e in scraped)).encode("utf-8")
        ).hexdigest()
        duplicate = _existing_snapshot_with_hash(db, "WATCHLIST_EU", content_hash)
        if duplicate:
            if duplicate.status == "PENDING_REVIEW":
                message = "Contenu identique a un snapshot EU deja en attente d'homologation."
            else:
                message = "Contenu identique a la liste EU active (hash inchange)."
            return _finalize_report(
                db, source="EURLEX", trigger=trigger, status="NO_CHANGE",
                message=message,
                previous_snapshot_id=duplicate.snapshot_id,
                delta_report={"acts": acts}
            )
        snap = Snapshot(
            snapshot_id=snap_id,
            file_type="WATCHLIST_EU",
            file_name=f"EUR-Lex JO {for_date.strftime('%Y-%m-%d')} ({len(acts)} acte(s))",
            file_hash=content_hash,
            record_count=0,
            status="PROCESSING"
        )
        db.add(snap)
        db.commit()

        record_count = persist_pivot_items(db, snap_id, scraped,
                                           progress=tracker.persisting(db, snap_id))
        carried = 0
        if previous_id:
            # Les entites exclues lors d'une revue ne sont pas reconduites
            prev_rows = db.query(WatchlistEntity).filter(
                WatchlistEntity.snapshot_id == previous_id,
                WatchlistEntity.excluded.isnot(True)
            ).all()
            for row in prev_rows:
                if row.entity_id not in scraped_ids:
                    db.add(_clone_entity_row(snap_id, row))
                    carried += 1
        # Relecture avant ecriture (cf. commentaire OFAC)
        snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
        staging = require_approval_enabled(db)
        snap.status = "PENDING_REVIEW" if staging else "READY"
        snap.record_count = record_count + carried
        db.commit()

        old_entities = _snapshot_entity_dicts(db, previous_id) if previous_id else []
        new_entities = _snapshot_entity_dicts(db, snap_id)
        delta = calculate_delta(old_entities, new_entities, "entity_id")

        if not staging:
            _supersede_previous_snapshots(db, "WATCHLIST_EU", snap_id)
            db.commit()
            if reload_cache:
                reload_cache()

        summary = delta["summary"]
        delta_stored = _truncate_delta_details(delta)
        delta_stored["acts"] = acts
        if failed_acts:
            delta_stored["fetch_failures"] = failed_acts
        if pdf_failures:
            delta_stored["pdf_failures"] = pdf_failures
        message = f"{len(acts)} acte(s) \"{cfg['keyword']}\" au JO du {for_date.strftime('%d/%m/%Y')} ; {len(scraped)} liste(s) extrait(s), {carried} fiche(s) reconduite(s)."
        if failed_acts:
            message += f" ⚠ {len(failed_acts)} acte(s) inaccessibles (repris au prochain run)."
        if staging:
            message += " Snapshot en attente d'homologation (pointage humain requis)."
        return _finalize_report(
            db, source="EURLEX", trigger=trigger, status="PENDING_REVIEW" if staging else "SUCCESS",
            message=message,
            snapshot_id=snap_id,
            previous_snapshot_id=previous_id,
            added_count=summary["added_count"],
            modified_count=summary["modified_count"],
            removed_count=summary["removed_count"],
            delta_report=delta_stored
        )
    except Exception as e:
        db.rollback()
        tracker.failed(e)
        logger.error(f"Echec de la synchronisation EUR-Lex: {e}")
        if snap_id:
            error_snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
            if error_snap:
                error_snap.status = "ERROR"
                db.commit()
        return _finalize_report(
            db, source="EURLEX", trigger=trigger, status="ERROR",
            message=f"Echec: {e}"
        )
    finally:
        tracker.done()
