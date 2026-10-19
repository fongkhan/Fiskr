"""
« Aucune correspondance » quand il n'y avait rien à comparer.

Suite de la chasse par classe. Le défaut le plus grave rencontré jusqu'ici,
parce qu'il ne demande aucune panne pour se produire et qu'il produit une
**fausse pièce de conformité**.

Sans liste en production, le moteur ne trouve aucun candidat et rend « aucune
correspondance ». Rien ne casse, rien n'alerte : le client repart avec un
quitus, et le journal de criblage — la pièce produite en inspection — écrit
qu'il a bien été criblé. Une installation neuve, une liste retirée de la
production, ou une simple restriction de périmètre visant une liste absente
suffisent.

Le contrôle de nom existant ne posait pas la bonne question : il vérifie qu'un
**nom** de liste est connu du produit, jamais qu'elle est **en production**.
Les deux sont différentes, et c'est la seconde qui décide de ce qui est
criblé.

Le refus est franc, sur les quatre voies : criblage réglementaire, criblage à
blanc, filtrage de paiement, campagne de masse. Rendre une décision serait
écrire une fausse pièce, et une fausse pièce de conformité est pire que pas de
pièce du tout.
"""
import uuid

import pytest
from fastapi import HTTPException

from fiskr import api as api_mod

UID = uuid.uuid4().hex[:8]


@pytest.fixture()
def univers(monkeypatch):
    """Pose l'univers chargé dans ce processus, sans toucher à la base."""
    def _poser(*types):
        monkeypatch.setattr(api_mod, "watchlist_types", set(types))
    return _poser


# --------------------------------------------------------------------------
# Le refus
# --------------------------------------------------------------------------

def test_sans_aucune_liste_le_criblage_est_refuse(univers):
    """
    La garde du lot. Le silence est ici la pire des réponses : il ressemble
    trait pour trait à une bonne nouvelle.
    """
    univers()
    with pytest.raises(HTTPException) as refus:
        api_mod.exiger_un_univers()
    assert refus.value.status_code == 409
    assert "Aucune liste n'est en production" in refus.value.detail


def test_le_refus_explique_ce_qu_une_reponse_aurait_valu(univers):
    """
    Un message qui dit seulement « impossible » laisse croire à une panne
    passagère, et l'utilisateur réessaie. Celui-ci dit ce qu'une réponse
    aurait signifié.
    """
    univers()
    with pytest.raises(HTTPException) as refus:
        api_mod.exiger_un_univers()
    assert "aucune correspondance" in refus.value.detail
    assert "sans avoir comparé" in refus.value.detail


def test_une_restriction_a_une_liste_absente_est_refusee(univers):
    univers("WATCHLIST_OFAC", "WATCHLIST_EU")
    with pytest.raises(HTTPException) as refus:
        api_mod.exiger_un_univers(["WATCHLIST_PEP"])
    assert refus.value.status_code == 409
    assert "WATCHLIST_PEP" in refus.value.detail


def test_le_refus_nomme_ce_qui_est_reellement_disponible(univers):
    """
    Nommer ce qui manque sans nommer ce qu'on a laisse l'exploitant deviner.
    Le message porte les deux.
    """
    univers("WATCHLIST_OFAC", "WATCHLIST_EU")
    with pytest.raises(HTTPException) as refus:
        api_mod.exiger_un_univers(["WATCHLIST_PEP"])
    assert "WATCHLIST_OFAC" in refus.value.detail
    assert "WATCHLIST_EU" in refus.value.detail


# --------------------------------------------------------------------------
# Ce que le refus ne doit PAS attraper
# --------------------------------------------------------------------------

def test_une_restriction_partiellement_disponible_passe(univers):
    """
    Une liste demandée absente parmi d'autres présentes n'annule pas le
    criblage : il reste un univers, et il est rendu tel qu'il est.
    """
    univers("WATCHLIST_OFAC", "WATCHLIST_EU")
    retenues = api_mod.exiger_un_univers(["WATCHLIST_OFAC", "WATCHLIST_PEP"])
    assert retenues == ["WATCHLIST_OFAC"]


def test_sans_restriction_l_univers_est_celui_de_la_production(univers):
    univers("WATCHLIST_EU", "WATCHLIST_OFAC")
    assert api_mod.exiger_un_univers() == ["WATCHLIST_EU", "WATCHLIST_OFAC"]
    assert api_mod.exiger_un_univers(None) == ["WATCHLIST_EU", "WATCHLIST_OFAC"]


def test_un_univers_present_ne_declenche_aucun_refus(univers):
    """Un refus qui se déclenche en fonctionnement normal serait pire que rien."""
    univers("WATCHLIST_OFAC")
    assert api_mod.exiger_un_univers(["WATCHLIST_OFAC"]) == ["WATCHLIST_OFAC"]


# --------------------------------------------------------------------------
# L'univers se DÉRIVE du cache, il ne se tient pas à la main
# --------------------------------------------------------------------------

def test_l_univers_se_derive_du_cache_charge():
    """
    Deux inventaires tenus séparément finissent par diverger, et celui qui
    ment est toujours celui qu'on lit. `watchlist_types` sort du même corpus
    que `watchlist_index` — c'est la seule façon qu'il dise la même chose que
    ce contre quoi on crible.
    """
    import inspect
    source = inspect.getsource(api_mod.load_watchlist_cache)
    assert 'watchlist_types = {e["_list_type"] for e in temp_store' in source


def test_un_cache_sans_snapshot_vide_l_univers():
    """
    Le chemin qui sort tôt doit remettre l'univers à zéro : garder celui du
    chargement précédent laisserait croire à une production qui n'est plus là.
    """
    import inspect
    source = inspect.getsource(api_mod.load_watchlist_cache)
    debut = source.index("No watchlist snapshots found")
    assert "watchlist_types = set()" in source[debut:debut + 200]


# --------------------------------------------------------------------------
# Les quatre voies sont gardées
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fonction,marqueur", [
    ("screen_client_profile", "exiger_un_univers(requested_lists)"),
    ("screen_preview", "exiger_un_univers(requested_lists)"),
    ("screen_transaction_message", "exiger_un_univers(requested_lists)"),
])
def test_chaque_voie_de_criblage_exige_un_univers(fonction, marqueur):
    import inspect
    source = inspect.getsource(getattr(api_mod, fonction))
    assert marqueur in source, f"{fonction} ne vérifie pas son univers"


def test_la_campagne_verifie_une_fois_et_non_par_ligne():
    """
    `screen_client_profile` refuserait de toute façon, mais ligne par ligne :
    dix mille refus identiques noieraient la cause dans le détail de chaque
    client, alors que le défaut ne concerne aucun d'eux.
    """
    import inspect
    source = inspect.getsource(api_mod)
    debut = source.index("Execute desormais dans le demon travailleur")
    extrait = source[debut:debut + 900]
    assert "exiger_un_univers(requested_lists)" in extrait
    assert extrait.index("exiger_un_univers") < extrait.index("for profile in profiles")


def test_le_refus_vient_apres_le_quality_gate():
    """
    Un profil inexploitable se refuse pour ce qu'il est, pas au nom de l'état
    de l'installation : sinon l'utilisateur corrige la mauvaise chose.
    """
    import inspect
    source = inspect.getsource(api_mod.screen_client_profile)
    assert source.index("report[\"is_valid\"]") < source.index("exiger_un_univers")
