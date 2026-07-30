#!/usr/bin/env python3
"""
Diagnostic des sources automatiques : quelles voies repondent DEPUIS CE SERVEUR.

A lancer sur le serveur de production (ou de recette) — le resultat depend de
l'IP sortante, du filtrage de l'hebergeur et des protections anti-robot, donc
il ne se deduit pas depuis un poste de developpement.

    python tools/diagnostic_sources.py            # toutes les voies
    python tools/diagnostic_sources.py --eurlex   # seulement les voies EUR-Lex
    python tools/diagnostic_sources.py --json     # sortie machine

Pour chaque voie : statut HTTP, taille, temps de reponse, verdict. Aucune
ecriture en base, aucun effet de bord : c'est une sonde, pas une importation.
"""
import argparse
import json
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import httpx

from fiskr.sync import get_sync_config

# La date d'hier : le JO du jour n'est pas toujours publie au moment du test
YESTERDAY = (date.today() - timedelta(days=1)).strftime("%d%m%Y")
CELEX_SAMPLE = "32014R0269"  # Reglement 269/2014 (mesures restrictives Ukraine)

# (cle, libelle, methode, url, en-tetes, ce qu'on attend dans le corps)
PROBES = [
    # --- Voie actuellement utilisee par Fiskr pour l'UE ---
    ("eurlex_html", "EUR-Lex — page JO du jour (scraping actuel)", "GET",
     f"https://eur-lex.europa.eu/oj/daily-view/L-series/default.html?ojDate={YESTERDAY}&locale=en",
     {}, "<html"),

    # --- Voies machine officielles de l'Office des publications (Cellar) ---
    ("cellar_sparql", "Cellar — endpoint SPARQL (Office des publications)", "GET",
     "http://publications.europa.eu/webapi/rdf/sparql"
     "?query=SELECT%20%3Fs%20WHERE%20%7B%3Fs%20%3Fp%20%3Fo%7D%20LIMIT%201"
     "&format=application%2Fsparql-results%2Bjson",
     {}, "results"),

    ("cellar_notice", "Cellar — notice REST d'un acte (negociation de contenu)", "GET",
     f"http://publications.europa.eu/resource/celex/{CELEX_SAMPLE}",
     {"Accept": "application/xml; notice=branch"}, "<"),

    ("cellar_formex", "Cellar — acte en FORMEX XML (format structure du JO)", "GET",
     f"http://publications.europa.eu/resource/celex/{CELEX_SAMPLE}",
     {"Accept": "application/xml;type=fmx4", "Accept-Language": "eng"}, "<"),

    ("eurlex_rss", "EUR-Lex — flux RSS du JO serie L", "GET",
     "https://eur-lex.europa.eu/EN/display-feed.rss?myRssId=oj-l",
     {}, "rss"),

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

EURLEX_KEYS = {"eurlex_html", "cellar_sparql", "cellar_notice", "cellar_formex",
               "eurlex_rss", "eu_fsf"}


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
    try:
        with httpx.Client(follow_redirects=True, timeout=45.0) as client:
            if method == "HEAD":
                r = client.head(url, headers=all_headers)
                # Certains portails refusent HEAD : repli GET en streaming
                if r.status_code in (403, 405, 501):
                    with client.stream("GET", url, headers=all_headers) as sr:
                        r = sr
                        body = sr.read(2048) if expect else b""
                else:
                    body = b""
            else:
                r = client.get(url, headers=all_headers)
                body = r.content[:4096]
    except Exception as e:
        return {"key": key, "label": label, "url": url, "verdict": "ECHEC RESEAU",
                "detail": f"{type(e).__name__}: {e}",
                "elapsed_s": round(time.monotonic() - started, 2)}

    elapsed = round(time.monotonic() - started, 2)
    size = int(r.headers.get("content-length") or len(body) or 0)
    text = body.decode("utf-8", "replace").lower() if body else ""

    if r.status_code == 202:
        verdict, detail = "ANTI-ROBOT", "HTTP 202 : interstitiel d'attente servi au client"
    elif r.status_code == 429:
        retry_after = r.headers.get("retry-after", "non precise")
        verdict, detail = "LIMITE DE DEBIT", f"HTTP 429, Retry-After: {retry_after}"
    elif r.status_code == 403:
        verdict, detail = "REFUSE", "HTTP 403 : client rejete (filtrage ou IP bloquee)"
    elif r.status_code >= 400:
        verdict, detail = "ERREUR", f"HTTP {r.status_code}"
    elif expect and expect.lower() not in text:
        verdict, detail = "CONTENU INATTENDU", f"HTTP 200 mais « {expect} » absent du debut du corps"
    else:
        verdict, detail = "OK", f"HTTP {r.status_code}"

    return {"key": key, "label": label, "url": url, "verdict": verdict,
            "detail": detail, "status": r.status_code, "size": size,
            "elapsed_s": elapsed,
            "content_type": r.headers.get("content-type", "")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eurlex", action="store_true", help="seulement les voies EUR-Lex / UE")
    ap.add_argument("--json", action="store_true", help="sortie JSON")
    args = ap.parse_args()

    probes = [p for p in PROBES if not args.eurlex or p[0] in EURLEX_KEYS]
    results = []
    for p in probes:
        res = probe(*p)
        results.append(res)
        if not args.json:
            mark = {"OK": "✅", "ANTI-ROBOT": "🤖", "LIMITE DE DEBIT": "⏳",
                    "REFUSE": "⛔", "NON CONFIGURE": "⚙️"}.get(res["verdict"], "❌")
            elapsed = f"{res.get('elapsed_s', 0):>5.2f}s"
            size = f"{res.get('size', 0):>10,} o" if res.get("size") else " " * 12
            print(f"{mark} {res['label']:<52} {elapsed} {size}  {res['detail']}")

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
    if "eurlex_html" not in ok:
        print("• Le scraping du JO EUR-Lex ne passe pas depuis ce serveur.")
        for alt, name in (("cellar_sparql", "SPARQL Cellar"),
                          ("cellar_formex", "FORMEX XML Cellar"),
                          ("eurlex_rss", "flux RSS du JO")):
            if alt in ok:
                print(f"  → voie machine officielle disponible en remplacement : {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
