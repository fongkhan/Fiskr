"""
EUR-Lex : traiter le refus plutôt que s'en accommoder.

Le portail EUR-Lex rendait un `202` à corps vide à l'installation de
production, deux fois par jour depuis des semaines. Le lot précédent avait
supprimé l'attente inutile entre deux refus et laissé le choix à
l'exploitant : réparer, ou suspendre la source. **Ce n'était pas suffisant, et
le diagnostic tenait mal** : un portail qui protège ses pages HTML contre un
client qui les racle ne fait rien d'anormal. L'anormal était de les lui
demander ainsi.

L'Office des publications expose la même matière par une porte faite pour les
machines, publique et sans inscription : le point SPARQL de CELLAR pour la
liste du jour, l'adresse CELEX pour le texte de chaque acte.

Ce que ces tests tiennent :
- changer de porte ne change **ni le périmètre, ni le filtre, ni les fiches** ;
- l'adresse **citable** (EUR-Lex) et l'adresse de **lecture** (CELLAR) ne se
  confondent jamais — l'une part dans un dossier d'audit, l'autre non ;
- le mot-clé, qui vient d'un réglage, entre dans la requête comme **donnée** ;
- aucune voie ne se rabat en silence sur l'autre ;
- une réponse illisible n'est **pas** un Journal Officiel sans publication.
"""
import json
from datetime import date

import pytest

from fiskr import sync as sync_mod
from fiskr.sync import (DEFAULT_CELLAR_CONTENT_URL, DEFAULT_EURLEX_SPARQL_URL,
                        _OJ_SERIE_L, eurlex_act_url, fetch_eurlex_acts,
                        fetch_eurlex_acts_cellar, fetch_eurlex_entities,
                        requete_actes_du_jour)


def _reponse_sparql(*couples):
    return json.dumps({
        "head": {"vars": ["celex", "title"]},
        "results": {"bindings": [
            {"celex": {"value": celex}, "title": {"value": titre}}
            for celex, titre in couples]},
    })


ACTE_XHTML = """
<html><body>
<table>
 <tr><td>Name</td><td>Identifying information</td><td>Reasons</td><td>Date of listing</td></tr>
 <tr><td>Ivan Petrovitch VOLKOV</td><td>DOB: 12.3.1970</td><td>Motif</td><td>7.8.2026</td></tr>
</table>
</body></html>
"""


# --------------------------------------------------------------------------
# La requête : même périmètre, et le réglage reste une donnée
# --------------------------------------------------------------------------

def test_la_requete_garde_le_perimetre_de_la_voie_portail():
    """
    Série L, titre anglais, mot-clé : les trois filtres de la page du jour.
    Élargir le périmètre en changeant de porte reviendrait à changer la
    source sous les pieds de l'analyste.
    """
    requete = requete_actes_du_jour(date(2026, 8, 7), "restrictive measures")
    assert f"<{_OJ_SERIE_L}>" in requete, "la série L doit rester le périmètre"
    assert '"2026-08-07"' in requete
    assert "lang:ENG" in requete
    assert '"restrictive measures"' in requete
    assert "LIMIT" in requete, "une réponse doit rester bornée"


def test_le_mot_cle_du_reglage_entre_comme_donnee():
    """
    Le mot-clé est configurable. Un guillemet non échappé fermerait le
    littéral et la suite du réglage deviendrait de la requête.
    """
    requete = requete_actes_du_jour(date(2026, 8, 7), 'mesures "ciblées"')
    assert '\\"ciblées\\"' in requete
    # Le littéral reste unique et fermé : le compte de guillemets non échappés
    # dans le FILTER ne peut pas déborder.
    filtre = requete[requete.index("FILTER"):]
    assert filtre.count('"') - filtre.count('\\"') == 2


def test_le_mot_cle_est_compare_en_minuscules():
    """Le filtre du portail ne distinguait pas la casse : celui-ci non plus."""
    requete = requete_actes_du_jour(date(2026, 8, 7), "Restrictive Measures")
    assert '"restrictive measures"' in requete
    assert "LCASE" in requete


# --------------------------------------------------------------------------
# Les deux adresses d'un acte
# --------------------------------------------------------------------------

def test_l_acte_porte_l_adresse_citable_et_l_adresse_de_lecture():
    """
    Le cœur du changement. `url` est l'adresse qu'un auditeur ouvrira ;
    `url_lecture` est celle par laquelle le produit va chercher le texte.
    Les confondre reviendrait à citer une API technique dans une pièce
    opposable — ou à aller lire là où l'on se fait refuser.
    """
    actes = fetch_eurlex_acts_cellar(
        date(2026, 8, 7),
        lambda url: _reponse_sparql(("32026R1940", "Council Implementing Regulation")))

    assert len(actes) == 1
    acte = actes[0]
    assert acte["url"] == "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32026R1940"
    assert acte["url_lecture"] == "http://publications.europa.eu/resource/celex/32026R1940"
    assert acte["celex"] == "32026R1940"
    assert "publications.europa.eu" not in acte["url"], (
        "l'adresse citée dans un dossier reste celle d'EUR-Lex")


def test_le_pdf_probant_se_derive_toujours_de_l_adresse_citable():
    """
    CELLAR ne détient pas de PDF pour les actes récents : la pièce probante
    reste servie par EUR-Lex. La dérivation ne doit donc pas partir de
    l'adresse de lecture.
    """
    citable = eurlex_act_url("32026R1940")
    assert sync_mod._act_pdf_url(citable) == (
        "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:32026R1940")


def test_une_ligne_sans_titre_ou_sans_celex_est_ecartee():
    """Un acte sans adresse ni titre n'est pas un acte : il ne remonte pas."""
    actes = fetch_eurlex_acts_cellar(
        date(2026, 8, 7),
        lambda url: _reponse_sparql(("32026R1940", "Un titre"), ("", "Sans celex"),
                                    ("32026R9999", "   ")))
    assert [a["celex"] for a in actes] == ["32026R1940"]


# --------------------------------------------------------------------------
# L'aiguillage : deux portes, aucun repli silencieux
# --------------------------------------------------------------------------

def test_la_voie_cellar_ne_touche_jamais_au_portail():
    """
    La garde qui fait tout ce lot : la raison d'être de la voie machine est
    de ne plus dépendre du portail qui refuse.
    """
    vues = []

    def _getter(url, **kw):
        vues.append(url)
        return _reponse_sparql(("32026R1940", "Council Regulation"))

    fetch_eurlex_acts(date(2026, 8, 7), _getter,
                      "https://eur-lex.europa.eu/oj/daily-view/{date}", "restrictive",
                      voie="cellar")
    assert vues and all("eur-lex.europa.eu" not in u for u in vues), vues
    assert all(u.startswith(DEFAULT_EURLEX_SPARQL_URL) for u in vues)


def test_la_voie_portail_reste_disponible_telle_quelle():
    """La voie historique n'est pas retirée : elle est nommée."""
    page = ('<html><body><a href="./legal-content/EN/TXT/?uri=OJ:L_2026001">'
            'Council Regulation on restrictive measures</a></body></html>')
    actes = fetch_eurlex_acts(
        date(2026, 8, 7), lambda url, **kw: page,
        "https://eur-lex.europa.eu/oj/daily-view/L-series/default.html?ojDate={date}",
        "restrictive measures", voie="portail")
    assert len(actes) == 1
    assert "eur-lex.europa.eu" in actes[0]["url"]
    assert "url_lecture" not in actes[0], (
        "la voie portail lit là où elle cite : pas de seconde adresse")


def test_une_voie_qui_echoue_ne_se_rabat_pas_sur_l_autre():
    """
    Se rabattre en silence sur la porte refusée produirait une erreur qui ne
    parle pas de la voie choisie — le repli qui masque le défaut.
    """
    def _casse(url, **kw):
        raise RuntimeError("point SPARQL injoignable")

    with pytest.raises(RuntimeError, match="SPARQL injoignable"):
        fetch_eurlex_acts(date(2026, 8, 7), _casse, "https://eur-lex.europa.eu/{date}",
                          "restrictive", voie="cellar")


def test_une_reponse_illisible_n_est_pas_un_jour_sans_publication():
    """
    La confusion qui coûterait le plus cher : un point d'accès cassé rendu
    comme « aucun acte au JO », c'est-à-dire comme une bonne nouvelle.
    """
    with pytest.raises(RuntimeError) as echec:
        fetch_eurlex_acts_cellar(date(2026, 8, 7), lambda url: "<html>maintenance</html>")
    assert "n'a PAS ete lu" in str(echec.value)
    assert "07/08/2026" in str(echec.value)


# --------------------------------------------------------------------------
# La lecture du texte : sans l'en-tête, CELLAR répond à côté
# --------------------------------------------------------------------------

def test_le_texte_d_un_acte_se_demande_avec_l_en_tete_qui_convient():
    """
    Sans `Accept`, CELLAR rend la fiche RDF de l'acte — du metadata là où on
    attend le texte, donc une extraction qui ne trouve rien et ne s'en plaint
    pas. Le pire des échecs : silencieux.
    """
    vues = []

    def _getter(url, headers=None):
        vues.append((url, headers))
        if "sparql" in url:
            return _reponse_sparql(("32026R1940", "Regulation on restrictive measures"))
        return ACTE_XHTML

    actes, fiches, echecs = fetch_eurlex_entities(
        date(2026, 8, 7), _getter, "https://eur-lex.europa.eu/{date}",
        "restrictive measures", voie="cellar")

    lecture = [(u, h) for u, h in vues if "sparql" not in u]
    assert lecture, "le texte de l'acte doit être lu"
    url_lue, entetes = lecture[0]
    assert url_lue.startswith("http://publications.europa.eu/resource/celex/")
    assert entetes and entetes.get("Accept") == "application/xhtml+xml"
    assert entetes.get("Accept-Language") == "eng"
    assert not echecs
    assert len(fiches) == 1 and fiches[0]["primary_name"] == "Ivan Petrovitch VOLKOV"


def test_la_fiche_extraite_cite_l_acte_par_son_adresse_opposable():
    """
    L'origine d'une fiche part dans un dossier d'audit. C'est l'adresse
    EUR-Lex qui doit s'y trouver, jamais celle du service technique.
    """
    def _getter(url, headers=None):
        if "sparql" in url:
            return _reponse_sparql(("32026R1940", "Regulation on restrictive measures"))
        return ACTE_XHTML

    _, fiches, _ = fetch_eurlex_entities(
        date(2026, 8, 7), _getter, "https://eur-lex.europa.eu/{date}",
        "restrictive measures", voie="cellar")

    trace = json.dumps(fiches[0], default=str)
    assert "legal-content" in trace
    assert "publications.europa.eu" not in trace


def test_un_lecteur_a_un_seul_argument_reste_utilisable():
    """
    Un double de test rend ce qu'on lui a dit de rendre : lui imposer un
    en-tête le ferait échouer pour rien.
    """
    def _getter_simple(url):
        return (_reponse_sparql(("32026R1940", "restrictive measures act"))
                if "sparql" in url else ACTE_XHTML)

    actes, fiches, echecs = fetch_eurlex_entities(
        date(2026, 8, 7), _getter_simple, "https://eur-lex.europa.eu/{date}",
        "restrictive measures", voie="cellar")
    assert len(actes) == 1 and len(fiches) == 1 and not echecs


def test_un_acte_illisible_est_compte_avec_son_adresse_citable():
    """
    Un échec de lecture remonte au rapport : il ne doit jamais présenter une
    liste amputée comme complète. Et il nomme l'acte par son adresse
    publique, celle que l'exploitant peut ouvrir.
    """
    def _getter(url, headers=None):
        if "sparql" in url:
            return _reponse_sparql(("32026R1940", "restrictive measures act"))
        raise RuntimeError("503")

    actes, fiches, echecs = fetch_eurlex_entities(
        date(2026, 8, 7), _getter, "https://eur-lex.europa.eu/{date}",
        "restrictive measures", voie="cellar")
    assert len(actes) == 1 and not fiches
    assert len(echecs) == 1
    assert echecs[0]["url"] == eurlex_act_url("32026R1940")


# --------------------------------------------------------------------------
# Le réglage
# --------------------------------------------------------------------------

def test_la_voie_machine_est_celle_par_defaut():
    """
    C'est le sens du lot : l'installation ne doit plus racler une page que le
    portail protège.
    """
    assert sync_mod.get_sync_config()["eurlex"]["voie"] == "cellar"


def test_l_argument_de_passage_prime_sur_le_reglage(db, monkeypatch):
    """Comme `mode` : forcer une passe par l'autre porte sans toucher au réglage."""
    vues = []

    def _getter(url, timeout=None, **kw):
        vues.append(url)
        return "<html><body>rien</body></html>"

    sync_mod.run_eurlex_sync(db, for_date=date(2026, 8, 7), mode="alert",
                             http_get=_getter, voie="portail")
    assert vues and any("eur-lex.europa.eu" in u for u in vues)


@pytest.fixture()
def db():
    from fiskr.database import get_db
    session = next(get_db())
    yield session
    session.close()
