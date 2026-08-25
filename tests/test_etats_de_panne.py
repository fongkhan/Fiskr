"""
Ce que montre un écran quand le serveur ne répond pas.

Sur un produit de conformité, « aucune alerte à instruire » et « le serveur
n'a pas répondu » ne doivent jamais se ressembler : la seconde lecture ferait
conclure à un analyste qu'il n'y a rien à faire. Le produit le savait — un
secours existait, `tableError`, avec ce raisonnement écrit au-dessus.

Mesuré dans un navigateur, API coupée avant le premier octet : **douze écrans
sur quatorze se taisaient**. Deux causes, et la première est la plus instructive.

1. `_tbodyOf` résolvait sa cible par `getElementById`. Or les appelants
   passent un SÉLECTEUR (« #ma-table »). `getElementById("#ma-table")` rend
   `null`, et les trois secours sortaient alors sans un mot : la fonction
   écrite CONTRE le tableau vide silencieux produisait elle-même un tableau
   vide silencieux. Sept des neuf appels à `tableError` étaient dans ce cas.
2. Dix chargeurs n'appelaient tout simplement pas de secours : leur `catch` se
   contentait d'un `console.error`, que personne ne lit.

Après correction, mesuré de nouveau : quatorze écrans sur quatorze annoncent
la panne, et aucun n'annonce d'erreur quand tout va bien.
"""
import os
import re

import pytest

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


@pytest.fixture(scope="module")
def app_js():
    with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def index_html():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return f.read()


def _corps(app_js, nom):
    debut = app_js.index(f"function {nom}(")
    return app_js[debut:app_js.index("\n}", debut)]


def _sans_commentaires(source):
    return "\n".join(re.sub(r"//.*$", "", ligne) for ligne in source.splitlines())


_HELPERS = ("tableError", "tableLoading", "tableEmpty")


def test_le_resolveur_comprend_ce_que_les_appelants_lui_passent(app_js):
    """
    `getElementById` sur « #ma-table » rend `null` — silencieusement. C'est
    tout le défaut : le secours ne secourait pas, et ne le disait pas.
    """
    corps = _sans_commentaires(_corps(app_js, "_tbodyOf"))
    assert "querySelector" in corps, (
        "un sélecteur « #… » doit être résolu par querySelector")
    assert "TABLE" in corps, (
        "recevoir la table plutôt que son tbody est le cas courant : il doit "
        "être traité, pas subi")


def test_chaque_cible_litterale_des_secours_existe_dans_le_balisage(app_js, index_html):
    """
    L'autre moitié du même défaut : une cible qui ne désigne plus rien. Le
    secours sortirait en silence, exactement comme avant — sauf que cette
    fois-ci le test le dit.
    """
    ids = set(re.findall(r'\bid="([^"\'{}$`]+)"', index_html))
    ids |= set(re.findall(r'\bid="([^"\'{}$`]+)"', app_js))
    introuvables = []
    for helper in _HELPERS:
        for cible in re.findall(rf'(?<![\w$.]){helper}\(\s*["\']([^"\']+)["\']', app_js):
            nom = cible[1:] if cible.startswith("#") else cible
            nom = nom.split()[0]  # « #ma-table tbody » → « ma-table »
            if nom not in ids:
                introuvables.append(f"{helper}(« {cible} »)")
    assert not introuvables, (
        "Cibles de secours absentes du balisage :\n  " + "\n  ".join(sorted(set(introuvables))))


# Chargeurs dont la panne DOIT se voir : ils remplissent un tableau que
# l'exploitant lit comme un état du dispositif.
CHARGEURS_A_SECOURS = (
    "fetchSyncReports", "fetchPendingReviews", "fetchBatchCampaigns",
    "fetchWhitelist", "fetchFpRules", "fetchKpis", "fetchUsersList",
    "fetchSyncConfig", "fetchLearnedEquivalences", "fetchClientQuality",
    "fetchSnapshots", "fetchAuditHistory",
)


@pytest.mark.parametrize("chargeur", CHARGEURS_A_SECOURS)
def test_un_chargeur_de_tableau_annonce_sa_panne(app_js, chargeur):
    """
    Un `catch` qui se contente d'un `console.error` laisse le tableau tel
    quel : vide, ou figé sur ses lignes squelettes. Les deux lectures sont
    fausses, et la seconde ressemble à un chargement qui n'en finit pas.
    """
    corps = _corps(app_js, chargeur)
    assert "catch" in corps, f"{chargeur} n'a pas de branche d'échec"
    assert "tableError" in corps or "color: var(--color-alert)" in corps, (
        f"{chargeur} ne dit rien à l'utilisateur quand la requête échoue")


def test_aucun_repli_ne_s_appelle_lui_meme(app_js):
    """
    `uiLocale` avait pour repli… `uiLocale()`. Sans `window.fiskrI18n` — i18n.js
    absent, lent ou bloqué — la récursion partait jusqu'à « Maximum call stack
    size exceeded », et cette fonction est traversée par tout affichage de
    date : une seule ressource manquante faisait tomber chaque date de chaque
    écran. Un repli qui ne peut pas replier n'est pas un repli.
    """
    source = _sans_commentaires(app_js)
    coupables = []
    for m in re.finditer(r"^function ([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", source, re.M):
        nom = m.group(1)
        corps = source[m.end():source.index("\n}", m.start())]
        if re.search(r"[?:]\s*" + re.escape(nom) + r"\(\s*\)", corps):
            coupables.append(nom)
    assert not coupables, (
        "Replis récursifs (récursion infinie garantie) : " + ", ".join(coupables))


def test_uiLocale_rend_une_locale_meme_sans_i18n(app_js):
    """La garde précédente interdit la forme ; celle-ci exige le fond."""
    corps = _sans_commentaires(_corps(app_js, "uiLocale"))
    assert "LOCALE_DE_REPLI" in corps, "le repli doit être une locale, pas un appel"
    # La signature contient « uiLocale() » : on cherche un APPEL, pas la
    # déclaration.
    sans_signature = corps.split("{", 1)[1]
    assert "uiLocale(" not in sans_signature
