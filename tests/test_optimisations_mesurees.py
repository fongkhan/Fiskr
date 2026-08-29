"""
Quatre constats à impact moyen — dont un qui ne tient pas la mesure.

Le lot précédent traitait les constats à impact fort. Ceux-ci coûtent moins,
et l'un d'eux a surtout coûté à celui qui l'avait écrit : la vérification a
montré que le défaut n'existait pas. Ce qui reste tient en une phrase — **on
n'optimise pas ce qu'on ne mesure pas, et on ne mesure rien en recopiant un
nombre**.

1. Le volume d'un passage de synchronisation. Deux passages « NO_CHANGE »
   portaient le même statut : l'un répondu 304 sans un octet, l'autre après
   avoir retéléchargé la liste entière pour la trouver identique. Et le
   compteur évident — les octets écrits sur le disque — n'est PAS le coût :
   ces sources sont servies en gzip, le fil porte quatre à six fois moins que
   le fichier décodé. Mesurer la mauvaise grandeur, c'est publier un chiffre
   faux avec l'autorité d'une mesure.
2. Le seuil de score. Le contrôle demandait de calibrer sur un référentiel
   clients vide — un travail que personne ne peut faire. Une consigne
   impossible s'ignore aussi vite qu'une alarme qui crie au loup.
3. La chaîne d'intégration jouait la suite deux fois sur le même commit.
4. Le compte de tests du README se corrigeait à la main, treize fois de suite.
   Le comptage vit maintenant dans un outil qui sait aussi écrire la phrase —
   et la garde IMPORTE cet outil, pour que les deux ne puissent pas diverger.
"""
import importlib
import os
import re
import uuid

import pytest

from fiskr import sync as sync_mod
from fiskr.database import get_db, ClientEntity, SyncReport
from fiskr.mise_en_service import _seuils

DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UID = uuid.uuid4().hex[:8].upper()


@pytest.fixture()
def db():
    session = next(get_db())
    yield session
    session.query(SyncReport).filter(SyncReport.source.like(f"T{UID}%")).delete(
        synchronize_session=False)
    session.commit()
    session.close()


# --------------------------------------------------------------------------
# 1. Le volume d'un passage : la bonne grandeur, ou rien
# --------------------------------------------------------------------------

class _ReponseFactice:
    """Réponse httpx en carton : du gzip sur le fil, du clair une fois décodé."""

    def __init__(self, status_code=200, headers=None, corps=b"", sur_le_fil=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._corps = corps
        self.num_bytes_downloaded = sur_le_fil if sur_le_fil is not None else len(corps)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self, chunk_size=None):
        yield self._corps


def _telecharge(monkeypatch, tmp_path, reponse, **kwargs):
    import httpx
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: reponse)
    return sync_mod.download_to_file(
        "https://exemple.test/liste.csv", tmp_path / "liste.csv", **kwargs)


def test_le_volume_rapporte_est_celui_du_fil_pas_du_fichier_decode(monkeypatch, tmp_path):
    """
    La garde qui fait tout le lot. Le fichier décodé pèse 43 Kio, le fil en a
    porté 9 : c'est 9 qu'on paie. Prendre l'autre nombre serait rapporter un
    coût quatre fois trop grand — la faute même que ce lot corrige ailleurs.
    """
    clair = b"x" * 43_000
    outcome = _telecharge(
        monkeypatch, tmp_path,
        _ReponseFactice(headers={"content-encoding": "gzip"}, corps=clair, sur_le_fil=9_000))

    assert outcome["bytes"] == 9_000, "le volume rapporté doit être celui du fil"
    assert outcome["bytes"] != len(clair)
    # Le fichier, lui, est bien écrit en clair : la mesure ne change pas ce
    # qu'on télécharge, seulement ce qu'on en dit.
    assert (tmp_path / "liste.csv").stat().st_size == len(clair)


def test_une_reponse_304_ne_coute_rien_et_le_dit(monkeypatch, tmp_path):
    """Zéro n'est pas « inconnu » : c'est la mesure exacte d'un 304."""
    outcome = _telecharge(monkeypatch, tmp_path, _ReponseFactice(status_code=304),
                          validators={"etag": 'W/"abc"'})

    assert outcome["not_modified"] is True
    assert outcome["bytes"] == 0
    assert not (tmp_path / "liste.csv").exists(), "un 304 n'écrit rien"


def test_sans_en_tete_de_compteur_le_volume_reste_celui_des_octets_lus(monkeypatch, tmp_path):
    """
    Repli : un client HTTP qui ne tient pas de compteur de fil laisse la
    mesure des octets lus. Approximatif, mais jamais absent — et jamais zéro,
    qui se lirait comme « ce passage n'a rien coûté ».
    """
    reponse = _ReponseFactice(corps=b"y" * 2048)
    del reponse.num_bytes_downloaded
    outcome = _telecharge(monkeypatch, tmp_path, reponse)
    assert outcome["bytes"] == 2048


@pytest.mark.parametrize("octets,attendu", [
    (None, "volume non mesure"),
    (0, "0 o"),
    (812, "812 o"),
    (24_576, "24 Kio"),
    (3_976_396, "3,8 Mio"),
])
def test_le_volume_s_ecrit_pour_un_humain(octets, attendu):
    assert sync_mod._volume_lisible(octets) == attendu


def test_le_volume_non_mesure_n_est_jamais_ecrit_comme_un_zero():
    """
    Un téléchargement confié à l'appelant n'est pas mesuré. « 0 octet » et
    « pas mesuré » ne disent pas la même chose : le premier affirme que le
    passage était gratuit.
    """
    assert "0" not in sync_mod._volume_lisible(None)


def test_le_rapport_porte_le_volume_du_passage(db):
    """La colonne existe et accepte la mesure — sinon rien n'est conservé."""
    report = SyncReport(source=f"T{UID}V", status="NO_CHANGE", trigger="MANUAL",
                        message="essai", bytes_downloaded=9_000)
    db.add(report)
    db.commit()
    db.refresh(report)
    assert report.bytes_downloaded == 9_000


def test_un_passage_inchange_mais_paye_le_dit_dans_son_message(db, monkeypatch):
    """
    Le cœur du constat : « identique à la version active » ne doit pas se lire
    pareil selon qu'il a coûté 0 ou 3,8 Mio. Le message porte la différence.
    """
    source = f"T{UID}W"
    monkeypatch.setattr(sync_mod, "_existing_snapshot_with_hash",
                        lambda db_, file_type, fhash: type("S", (), {
                            "status": "ACTIVE", "snapshot_id": "snap-1"})())
    monkeypatch.setattr(sync_mod, "_latest_ready_snapshot", lambda db_, ft: None)
    monkeypatch.setattr(sync_mod, "download_to_file",
                        lambda url, dest, **k: (dest.write_bytes(b"z" * 10),
                                                {"not_modified": False, "bytes": 3_976_396,
                                                 "etag": None, "last_modified": None})[1])

    report = sync_mod._run_list_replacement_sync(
        db, source=source, file_type="WATCHLIST_OFAC",
        url="https://exemple.test/l.csv", parser=lambda p: [],
        file_label="essai", temp_suffix=".csv", trigger="MANUAL")

    assert report.status == "NO_CHANGE"
    assert report.bytes_downloaded == 3_976_396
    assert "3,8 Mio" in report.message and "pour rien" in report.message


# --------------------------------------------------------------------------
# 2. Le seuil : l'ordre des gestes, dit là où on le cherche
# --------------------------------------------------------------------------

def test_sans_referentiel_le_controle_des_seuils_dit_par_quoi_commencer(db, monkeypatch):
    """
    Demander de calibrer un seuil sans portefeuille, c'est demander un travail
    que personne ne peut faire. Le contrôle nomme le premier geste et pointe
    vers lui.
    """
    monkeypatch.setattr("fiskr.settings.score_thresholds",
                        lambda db_: {"source": "config", "cut_off_threshold": 75.0})

    class _Vide:
        def query(self, model):
            return self

        def count(self):
            return 0

    controle = _seuils(_Vide())
    assert controle["etat"] == "A_FAIRE"
    assert "référentiel clients" in controle["constat"]
    assert "importez d'abord" in controle["remede"].lower()
    assert controle["lien"].endswith("watchlist-import"), (
        "le lien doit mener au premier geste, pas à l'écran des seuils")


def test_avec_un_referentiel_le_controle_renvoie_a_l_ecran_des_seuils(monkeypatch):
    """Le portefeuille est là : la calibration devient possible, et le lien change."""
    monkeypatch.setattr("fiskr.settings.score_thresholds",
                        lambda db_: {"source": "config", "cut_off_threshold": 75.0})

    class _Peuple:
        def query(self, model):
            return self

        def count(self):
            return 4_000

    controle = _seuils(_Peuple())
    assert controle["etat"] == "A_FAIRE"
    assert "alerts-blocking" in controle["lien"]
    assert "importez d'abord" not in controle["remede"].lower()


def test_un_seuil_regle_depuis_l_application_reste_un_controle_vert(monkeypatch):
    """La dépendance ajoutée ne doit pas ressusciter une alarme déjà éteinte."""
    monkeypatch.setattr("fiskr.settings.score_thresholds",
                        lambda db_: {"source": "database", "cut_off_threshold": 82.0})

    class _Vide:
        def query(self, model):
            return self

        def count(self):
            return 0

    assert _seuils(_Vide())["etat"] == "OK"


# --------------------------------------------------------------------------
# 3. La chaîne d'intégration : un résultat par commit
# --------------------------------------------------------------------------

def _workflow():
    with open(os.path.join(DEPOT, ".github", "workflows", "ci.yml"),
              encoding="utf-8") as f:
        return f.read()


def test_le_declencheur_push_ne_couvre_plus_les_branches_de_travail():
    """
    Un commit poussé sur une branche portant une PR déclenchait deux runs de
    la suite complète sur le même SHA. Le `push` ne garde que master.
    """
    texte = _workflow()
    bloc_push = re.search(r"\n  push:\n    branches: \[([^\]]*)\]", texte)
    assert bloc_push, "le déclencheur push a changé de forme"
    branches = [b.strip().strip('"') for b in bloc_push.group(1).split(",")]
    assert branches == ["master"], f"push déclenché sur {branches}"


def test_les_branches_gardent_une_voie_de_verification():
    """
    Retirer `claude/**` du push prive une branche sans PR de vérification
    automatique. Deux filets restent, et les retirer serait une régression :
    le déclencheur `pull_request`, et le run à la demande.
    """
    texte = _workflow()
    assert "pull_request:" in texte
    assert "workflow_dispatch:" in texte


# --------------------------------------------------------------------------
# 4. Le compte du README : compter une fois, à un seul endroit
# --------------------------------------------------------------------------

@pytest.fixture()
def outil():
    return importlib.import_module("tools.compte_de_tests")


def test_l_outil_compte_la_suite_reelle(outil):
    fonctions, fichiers = outil.compter()
    assert fonctions > 500 and fichiers > 100


def test_l_outil_corrige_un_compte_faux(outil, tmp_path):
    avant = ("Exécutez la suite complète — **1 234 fonctions de test** réparties sur 99\n"
             "fichiers — avec pytest :\n")
    apres, change = outil.texte_corrige(avant, 1699, 167)
    assert change
    assert "1699" in apres.replace(" ", "").replace(" ", "")
    assert "167\nfichiers" in apres, "la coupure de ligne d'origine doit survivre"


def test_l_outil_corrige_mais_n_uniformise_pas(outil):
    """
    Les trois typographies du séparateur sont acceptées par la garde. Un outil
    qui les normaliserait produirait des diffs gratuits — et se mettrait à
    réécrire un README que la garde tient pour juste.
    """
    for separateur in (" ", " ", " "):
        texte = (f"**1{separateur}699 fonctions de test** réparties sur 167\n"
                 f"fichiers — avec pytest")
        _, change = outil.texte_corrige(texte, 1699, 167)
        assert not change, f"séparateur {separateur!r} : rien à corriger"


def test_l_outil_refuse_d_ecrire_un_compte_absurde(outil, monkeypatch):
    """
    Un README périmé vaut mieux qu'un README affirmant un chiffre absurde avec
    l'autorité d'un outil : si l'analyse rate les fichiers, on n'écrit rien.
    """
    monkeypatch.setattr(outil, "compter", lambda dossier_tests=None: (3, 1))
    with pytest.raises(SystemExit) as echec:
        outil.main(["--corriger"])
    assert "suspect" in str(echec.value)


def test_l_outil_signale_une_phrase_qui_a_change_de_forme(outil):
    """Introuvable ≠ juste : un README remanié doit être dit, pas laissé passer."""
    with pytest.raises(SystemExit):
        outil.texte_corrige("Le README ne parle plus de tests.\n", 1699, 167)


def test_la_garde_du_readme_derive_son_compte_de_l_outil():
    """
    Recopier le comptage dans la garde, c'est deux sources de vérité — et le
    jour vient où l'outil écrit ce que la garde refuse. La garde importe.
    """
    chemin = os.path.join(DEPOT, "tests", "test_documentation_exacte.py")
    with open(chemin, encoding="utf-8") as f:
        source = f.read()
    corps = source[source.index("def test_le_compte_de_tests_annonce_par_le_readme_est_juste"):]
    corps = corps[:corps.index("\ndef ")]
    assert "tools.compte_de_tests" in corps
    assert "ast.walk" not in corps, "le comptage ne doit plus être recopié ici"
