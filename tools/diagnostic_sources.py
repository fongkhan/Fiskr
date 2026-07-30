#!/usr/bin/env python3
"""
Diagnostic des sources automatiques : quelles voies repondent DEPUIS CE SERVEUR.

A lancer sur le serveur de production (ou de recette) — le resultat depend de
l'IP sortante, du filtrage de l'hebergeur et des protections anti-robot, donc
il ne se deduit pas depuis un poste de developpement.

    python tools/diagnostic_sources.py            # toutes les voies
    python tools/diagnostic_sources.py --eurlex   # seulement les voies EUR-Lex
    python tools/diagnostic_sources.py --json     # sortie machine
    python tools/diagnostic_sources.py --bodies   # + extrait des corps recus

Pour chaque voie : statut HTTP, taille, temps de reponse, verdict. Aucune
ecriture en base, aucun effet de bord : c'est une sonde, pas une importation.

Un mot sur la severite des verdicts, parce que la v1 s'y est laissee prendre :
un HTTP 300 (« Multiple Choices ») n'est PAS un succes — Cellar renvoie la
liste des representations disponibles, pas le document. Un corps de 234 octets
annonce comme un flux RSS n'en est pas un. La sonde tranche donc sur le
CONTENU (--bodies pour le voir), pas sur le seul code de statut.
"""
import argparse
import json
import re
import sys
import time
from datetime import date, timedelta
from urllib.parse import quote

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import httpx

from fiskr.sync import get_sync_config

# La date d'hier : le JO du jour n'est pas toujours publie au moment du test
YESTERDAY = (date.today() - timedelta(days=1)).strftime("%d%m%Y")
CELEX_SAMPLE = "32014R0269"  # Reglement 269/2014 (mesures restrictives Ukraine)

# Vraie requete SPARQL : les actes du JO serie L publies depuis 30 jours dont
# le titre porte « restrictive measures ». C'est CETTE requete qui remplacerait
# le scraping de la page quotidienne — la tester au lieu d'un SELECT trivial
# est la seule facon de savoir si la voie SPARQL tient debout.
SPARQL_REAL_QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT DISTINCT ?act ?title ?date WHERE {
  ?act cdm:work_date_document ?date .
  ?act cdm:expression_belongs_to_work/cdm:expression_title ?title .
  FILTER (?date >= "%s"^^<http://www.w3.org/2001/XMLSchema#date>)
  FILTER (CONTAINS(LCASE(STR(?title)), "restrictive measures"))
}
LIMIT 5
""" % (date.today() - timedelta(days=30)).isoformat()

# (cle, libelle, methode, url, en-tetes, ce qu'on attend dans le corps)
PROBES = [
    # --- Voie actuellement utilisee par Fiskr pour l'UE ---
    ("eurlex_html", "EUR-Lex — page JO du jour (scraping actuel)", "GET",
     f"https://eur-lex.europa.eu/oj/daily-view/L-series/default.html?ojDate={YESTERDAY}&locale=en",
     {}, "<html"),

    # --- Voies machine officielles de l'Office des publications (Cellar) ---
    # Vraie requete metier, pas un SELECT trivial : c'est elle qui remplacerait
    # le scraping de la page quotidienne, donc c'est elle qu'il faut prouver.
    ("cellar_sparql", "Cellar — SPARQL, vraie requête « mesures restrictives »", "GET",
     "http://publications.europa.eu/webapi/rdf/sparql?query="
     + quote(SPARQL_REAL_QUERY) + "&format=application%2Fsparql-results%2Bjson",
     {}, "sparql_json"),

    # v1 : Accept mal forme -> HTTP 400. Le parametre `notice` se passe sans
    # espace apres le point-virgule.
    ("cellar_notice", "Cellar — notice REST d'un acte (négociation de contenu)", "GET",
     f"http://publications.europa.eu/resource/celex/{CELEX_SAMPLE}",
     {"Accept": "application/xml;notice=branch"}, "xml"),

    # v1 : compte OK sur un HTTP 300 (« Multiple Choices ») qui ne livre PAS le
    # document. La sonde suit desormais le 300 jusqu'a une manifestation reelle.
    ("cellar_formex", "Cellar — acte en FORMEX XML (format structuré du JO)", "GET",
     f"http://publications.europa.eu/resource/celex/{CELEX_SAMPLE}",
     {"Accept": "application/xml;type=fmx4", "Accept-Language": "eng"}, "formex"),

    # v1 : 200 avec 234 octets compte OK parce que le mot « rss » figurait dans
    # le corps. Un vrai flux porte des <item> ou des <entry> : c'est ce qu'on
    # exige maintenant.
    ("eurlex_rss", "EUR-Lex — flux RSS du JO série L", "GET",
     "https://eur-lex.europa.eu/EN/display-feed.rss?myRssId=oj-l",
     {}, "feed"),

    # --- Liste consolidee officielle des sanctions financieres UE ---
    ("eu_fsf", "EU FSF — liste consolidee XML (webgate, token requis)", "GET",
     None,  # construite depuis la config (token)
     {}, "<"),

    # --- Autres sources deja branchees, pour un tableau de bord complet ---
    ("ofac_sdn", "OFAC — SDN Advanced XML", "HEAD",
     "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML",
     {}, None),
    ("un", "ONU — liste consolidee XML", "HEAD",
     "https://scsanctions.un.org/resources/xml/en/consolidated.xml", {}, None),
    ("dgt", "DGT — registre national des gels (JSON)", "HEAD",
     "https://gels-avoirs.dgtresor.gouv.fr/ApiPublic/api/v1/publication/derniere-publication-fichier-json",
     {}, None),
    ("ofsi", "UK OFSI — liste consolidee CSV", "HEAD",
     "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv", {}, None),
    ("seco", "Suisse SECO — export SESAM XML", "HEAD",
     "https://www.sesam.search.admin.ch/sesam-search-web/pages/"
     "downloadXmlGesamtliste.xhtml?lang=en&action=downloadXmlGesamtlisteAction", {}, None),
    ("csl", "US CSL — liste consolidee JSON", "HEAD",
     "https://api.trade.gov/static/consolidated_screening_list/consolidated.json", {}, None),
]

# Sources du registre OpenSanctions (fiskr/sources.py) : une sonde par
# dataset, generee depuis le registre. Le verdict porte sur le CONTENU
# (en-tete targets.simple.csv) : c'est ainsi qu'un slug faux se detecte —
# et il se corrige dans config.yaml (sync.<cle>.url), pas dans le code.
try:
    from fiskr.sources import OPENSANCTIONS_SOURCES, opensanctions_default_url
    for _src in OPENSANCTIONS_SOURCES:
        PROBES.append((
            f"os_{_src.run_key}",
            f"OpenSanctions — {_src.label} ({_src.dataset})",
            "GET", opensanctions_default_url(_src.dataset), {}, "os_csv"))
except Exception:  # registre absent (vieille version) : la sonde reste utilisable
    pass

EURLEX_KEYS = {"eurlex_html", "cellar_sparql", "cellar_notice", "cellar_formex",
               "eurlex_rss", "eu_fsf"}


def check_content(expect, text: str):
    """
    Verdict sur le CONTENU recu, et non sur le seul code de statut. Retourne
    (conforme, explication). `expect` None = aucune exigence de contenu.

    C'est la lecon de la v1 : un HTTP 200 (ou pire, un 300) ne dit rien de ce
    qu'on a vraiment recu. Sur une decision d'architecture, il faut la piece.
    """
    if expect is None:
        return True, ""
    low = text.lower()

    if expect == "sparql_json":
        try:
            parsed = json.loads(text)
        except ValueError:
            return False, "réponse SPARQL illisible (JSON attendu)"
        rows = (parsed.get("results") or {}).get("bindings")
        if rows is None:
            return False, "JSON sans results.bindings : ce n'est pas un résultat SPARQL"
        return True, f"{len(rows)} acte(s) retourné(s) par la requête réelle"

    if expect == "formex":
        # Un FORMEX porte ses balises propres ; du HTML ou une page de choix
        # n'en portent aucune.
        if any(tag in low for tag in ("<formex", "<general", "<act", "<bib.instance", "<content")):
            return True, "document FORMEX reçu"
        if "<html" in low:
            return False, "HTML reçu au lieu du FORMEX (négociation non aboutie)"
        return False, "ni FORMEX ni HTML identifiable"

    if expect == "xml":
        if low.lstrip().startswith(("<?xml", "<rdf", "<notice", "<work")):
            return True, "XML reçu"
        return False, "la réponse ne commence pas par du XML"

    if expect == "os_csv":
        # En-tete du format targets.simple.csv : les colonnes que le lecteur
        # de fiskr/ingest.py exige. Un slug faux renvoie du HTML (page 404).
        first_line = text.splitlines()[0].lower() if text.strip() else ""
        if "id" in first_line and "schema" in first_line and "name" in first_line:
            return True, "en-tête targets.simple.csv reconnu"
        if "<html" in low or "not found" in low:
            return False, "slug de dataset inconnu (page d'erreur reçue) — corriger sync.<clé>.url"
        return False, "la réponse n'a pas l'en-tête id/schema/name attendu"

    if expect == "feed":
        items = low.count("<item") + low.count("<entry")
        if items == 0:
            return False, "flux sans <item>/<entry> : vide ou ce n'est pas un flux"
        return True, f"{items} entrée(s) dans le flux"

    return (expect.lower() in low), ("" if expect.lower() in low
                                     else f"« {expect} » absent du début du corps")


def _follow_multiple_choices(client, body: str, headers):
    """
    Cellar repond 300 « Multiple Choices » en listant les representations
    disponibles : ce n'est PAS le document. On suit la premiere alternative
    plausible pour savoir ce qu'on obtient reellement.
    """
    candidates = re.findall(r'https?://[^\s"\'<>]+', body)
    for candidate in candidates[:5]:
        if candidate.rstrip("/").endswith(("/", ".css", ".js", ".png")):
            continue
        try:
            follow = client.get(candidate, headers=headers, timeout=45.0)
        except Exception:
            continue
        if follow.status_code == 200 and follow.content:
            return candidate, follow
    return None, None


def _user_agent() -> str:
    try:
        return get_sync_config()["network"]["user_agent"]
    except Exception:
        return "Mozilla/5.0 (compatible; Fiskr-Compliance; +https://github.com/fongkhan/Fiskr)"


def _fsf_url() -> str:
    """URL FSF avec le token de la configuration, si renseigne."""
    try:
        cfg = get_sync_config()["eu_fsf"]
        url, token = cfg["url"], cfg["token"]
        return url.replace("{token}", token) if token else ""
    except Exception:
        return ""


def probe(key, label, method, url, headers, expect):
    if key == "eu_fsf":
        url = _fsf_url()
        if not url:
            return {"key": key, "label": label, "verdict": "NON CONFIGURE",
                    "detail": "sync.eu_fsf.token vide — inscription gratuite au webgate FSD requise"}

    all_headers = {"User-Agent": _user_agent(), **headers}
    started = time.monotonic()
    body = b""
    note = ""
    try:
        with httpx.Client(follow_redirects=True, timeout=45.0) as client:
            if method == "HEAD":
                r = client.head(url, headers=all_headers)
                status, resp_headers = r.status_code, dict(r.headers)
                # Certains portails refusent HEAD : repli GET en streaming, en
                # ne lisant que le debut du corps (ces fichiers pesent des Mo)
                if status in (403, 405, 501):
                    with client.stream("GET", url, headers=all_headers) as sr:
                        status, resp_headers = sr.status_code, dict(sr.headers)
                        for chunk in sr.iter_bytes(chunk_size=2048):
                            body = chunk
                            break
            else:
                r = client.get(url, headers=all_headers)
                status, resp_headers = r.status_code, dict(r.headers)
                body = r.content[:8192]
                # 300 « Multiple Choices » : la reponse liste les
                # representations, elle ne LIVRE pas le document. On suit.
                if status == 300:
                    followed_url, followed = _follow_multiple_choices(
                        client, r.text, all_headers)
                    if followed is not None:
                        note = f"300 suivi vers {followed_url}"
                        status, resp_headers = followed.status_code, dict(followed.headers)
                        body = followed.content[:8192]
                    else:
                        note = "300 Multiple Choices — aucune alternative exploitable suivie"
    except Exception as e:
        return {"key": key, "label": label, "url": url, "verdict": "ECHEC RESEAU",
                "detail": f"{type(e).__name__}: {e}",
                "elapsed_s": round(time.monotonic() - started, 2)}

    elapsed = round(time.monotonic() - started, 2)
    size = int(resp_headers.get("content-length") or len(body) or 0)
    text = body.decode("utf-8", "replace") if body else ""

    if status == 202:
        verdict, detail = "ANTI-ROBOT", "HTTP 202 : interstitiel d'attente servi au client"
    elif status == 429:
        retry_after = resp_headers.get("retry-after", "non precise")
        verdict, detail = "LIMITE DE DEBIT", f"HTTP 429, Retry-After: {retry_after}"
    elif status == 403:
        verdict, detail = "REFUSE", "HTTP 403 : client rejete (filtrage ou IP bloquee)"
    elif status >= 400:
        verdict, detail = "ERREUR", f"HTTP {status}"
    elif 300 <= status < 400:
        # Un 3xx qu'on n'a pas su suivre n'est PAS un succes : rien n'a ete livre
        verdict, detail = "PAS DE DOCUMENT", f"HTTP {status} — aucune representation obtenue"
    else:
        conforme, why = check_content(expect, text)
        if conforme:
            verdict, detail = "OK", (why or f"HTTP {status}")
        else:
            verdict, detail = "CONTENU INATTENDU", f"HTTP {status} — {why}"
    if note:
        detail = f"{detail} [{note}]"

    return {"key": key, "label": label, "url": url, "verdict": verdict,
            "detail": detail, "status": status, "size": size,
            "elapsed_s": elapsed,
            "content_type": resp_headers.get("content-type", ""),
            "body_sample": text[:600]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eurlex", action="store_true", help="seulement les voies EUR-Lex / UE")
    ap.add_argument("--json", action="store_true", help="sortie JSON")
    ap.add_argument("--bodies", action="store_true",
                    help="affiche un extrait du corps recu (juger sur pièces)")
    args = ap.parse_args()

    probes = [p for p in PROBES if not args.eurlex or p[0] in EURLEX_KEYS]
    results = []
    for p in probes:
        res = probe(*p)
        results.append(res)
        if not args.json:
            mark = {"OK": "✅", "ANTI-ROBOT": "🤖", "LIMITE DE DEBIT": "⏳",
                    "REFUSE": "⛔", "NON CONFIGURE": "⚙️",
                    "PAS DE DOCUMENT": "🚫"}.get(res["verdict"], "❌")
            elapsed = f"{res.get('elapsed_s', 0):>5.2f}s"
            size = f"{res.get('size', 0):>10,} o" if res.get("size") else " " * 12
            print(f"{mark} {res['label']:<52} {elapsed} {size}  {res['detail']}")
            if args.bodies and res.get("body_sample"):
                extrait = " ".join(res["body_sample"].split())[:400]
                print(f"      ↳ {extrait}")

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0

    ok = {r["key"] for r in results if r["verdict"] == "OK"}
    print("\n--- Lecture ---")
    if "eu_fsf" in ok:
        print("• EU FSF repond : c'est LA source qui fait autorite pour les sanctions UE")
        print("  (structuree, radiations fiables). Activez sync.eu_fsf.enabled: true.")
    elif any(r["key"] == "eu_fsf" and r["verdict"] == "NON CONFIGURE" for r in results):
        print("• EU FSF n'est pas configure : c'est le principal manque. Inscription")
        print("  gratuite au webgate FSD de la Commission, puis sync.eu_fsf.token.")
    if "eurlex_html" in ok:
        print("• Le scraping du JO EUR-Lex PASSE depuis ce serveur : un échec de")
        print("  récupération EUR-Lex n'est donc pas un blocage anti-robot, mais")
        print("  une intermittence — ou un échec d'EXTRACTION (acte trouvé, aucun")
        print("  listé reconnu dans ses annexes).")
    else:
        print("• Le scraping du JO EUR-Lex ne passe pas depuis ce serveur.")

    # Verdict sur l'option B (réécriture EUR-Lex sur les voies machine) : elle
    # suppose SPARQL *et* FORMEX. Sans les deux, elle n'a pas de socle.
    socle_b = {"cellar_sparql", "cellar_formex"} <= ok
    print(f"• Socle de l'option B (SPARQL + FORMEX) : "
          f"{'DISPONIBLE' if socle_b else 'INCOMPLET'}.")
    for key, name in (("cellar_sparql", "SPARQL Cellar (découverte des actes)"),
                      ("cellar_formex", "FORMEX XML (contenu structuré)"),
                      ("eurlex_rss", "flux RSS du JO (découverte simple)")):
        row = next((r for r in results if r["key"] == key), None)
        if row:
            print(f"    - {name} : {row['verdict']} — {row['detail']}")
    if not socle_b:
        print("  → réécrire EUR-Lex sur Cellar n'est pas justifié en l'état :")
        print("    gardez le mode `alert` et faites porter les désignations par EUFSF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
