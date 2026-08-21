"""
Rareté des noms : fréquence des mots de nom dans le corpus listé.

Le criblage garde TOUTES les correspondances au-dessus du seuil — c'est
l'exigence d'audit, et « Mohammed Ali » sans pays en produit 2 976 en
production. Aucune métrique de chaîne ne sépare ces fiches : leurs noms sont
identiques. Ce qui les sépare est ailleurs — « MOHAMMED » et « ALI » sont des
mots que des milliers de fiches portent, « TYURIN » non.

Ces tests fixent les propriétés sur lesquelles une règle anti-faux positifs
peut s'appuyer sans risque de faux négatif silencieux :
  1. la mesure est documentaire (une fiche compte une fois par mot) ;
  2. un mot INCONNU compte comme RARE, jamais comme répandu ;
  3. sans table, tous les drapeaux sont au repos — la règle ne clôture rien ;
  4. un seul mot rare partagé suffit à conserver l'alerte ;
  5. le signal n'a DÉPLACÉ AUCUN SCORE.
"""
import pytest

from fiskr import rarete
from fiskr.scoring import match_entities
from fiskr.settings import DEFAULT_SCORING_WEIGHTS


@pytest.fixture(autouse=True)
def table_au_repos():
    """Aucune table posée par défaut : chaque test installe la sienne."""
    precedente = rarete.table_courante()
    rarete.installer(None)
    yield
    rarete.installer(precedente)


def _corpus(*noms):
    return [{"primary_name": n} for n in noms]


# ------------------ MESURE ------------------

def test_frequence_documentaire_pas_frequence_de_terme():
    # « MOHAMMED MOHAMMED » est UNE fiche portant MOHAMMED, pas deux.
    table = rarete.construire(_corpus("MOHAMMED MOHAMMED", "MOHAMMED ALI"))
    assert table.total == 2
    assert table.df("MOHAMMED") == 2


def test_les_alias_comptent_comme_le_nom_principal():
    # Le moteur compare aussi les alias haute priorite : ils font partie du
    # corpus qui produit des correspondances.
    corpus = [{"primary_name": "VLADIMIR PUTIN",
               "aliases": {"high_priority": ["VLADIMIR POUTINE"]}}]
    table = rarete.construire(corpus * 5)
    assert table.df("POUTINE") == 5


def test_les_mots_d_un_caractere_sont_ignores():
    table = rarete.construire(_corpus("J SMITH", "J JONES", "J BROWN") * 5)
    assert table.df("J") == table.plancher - 1  # jamais indexe
    assert "J" not in [t["token"] for t in rarete.mots_les_plus_repandus(table, 10)]


def test_un_mot_inconnu_compte_comme_rare_jamais_comme_absent():
    table = rarete.construire(_corpus("MOHAMMED ALI") * 40)
    # Zero ferait une information INFINIE et un « repandu » faux : la table
    # rend une borne superieure, strictement sous le plancher.
    assert table.df("TYURIN") == table.plancher - 1
    assert table.df("TYURIN") >= 1
    assert not table.repandu("TYURIN")


def test_la_table_est_plafonnee_en_nombre_de_mots(monkeypatch):
    monkeypatch.setattr(rarete, "MAX_TOKENS_TABLE", 5)
    noms = [f"MOTCOMMUN MOT{i:03d}" for i in range(50) for _ in range(4)]
    table = rarete.construire(_corpus(*noms))
    assert len(table) == 5
    # Le plancher devient la frequence du dernier conserve : tout ce qui manque
    # est plus rare que lui.
    assert table.plancher >= rarete.DF_MIN_CONSERVE


# ------------------ PROFIL D'UN RAPPROCHEMENT ------------------

def test_nom_de_mots_repandus_est_signale_comme_tel():
    corpus = _corpus(*([f"MOHAMMED ALI {i}" for i in range(300)]
                       + [f"VLADIMIR TYURIN {i}" for i in range(2)]))
    table = rarete.construire(corpus)
    profil = table.profil("Mohammed Ali", "MOHAMMED ALI 7")
    assert profil["nom_repandu"] is True
    assert {t["token"] for t in profil["tokens"]} == {"MOHAMMED", "ALI"}
    assert profil["df_min"] == 300


def test_un_seul_mot_rare_partage_suffit_a_conserver_l_alerte():
    corpus = _corpus(*([f"VLADIMIR PERSONNE{i}" for i in range(300)]
                       + ["VLADIMIR TYURIN"]))
    table = rarete.construire(corpus)
    profil = table.profil("Vladimir Tyurin", "VLADIMIR TYURIN")
    # VLADIMIR est repandu, TYURIN non : le rapprochement identifie quelqu'un.
    assert profil["nom_repandu"] is False
    assert profil["df_max"] == 301


def test_rapprochement_sans_mot_commun_ne_dit_rien():
    table = rarete.construire(_corpus(*[f"MOHAMMED ALI {i}" for i in range(300)]))
    profil = table.profil("Schmidt", "SCHMITT")
    assert profil["sans_token_commun"] is True
    # Et surtout : il ne doit RIEN faire cloturer.
    assert profil["nom_repandu"] is False


def test_couverture_dit_quelle_part_du_nom_liste_a_ete_rapprochee():
    table = rarete.construire(_corpus("MARIA CARMEN LOPEZ HERNANDEZ") * 5)
    complet = table.profil("Maria Carmen Lopez Hernandez", "MARIA CARMEN LOPEZ HERNANDEZ")
    partiel = table.profil("Maria Lopez", "MARIA CARMEN LOPEZ HERNANDEZ")
    assert complet["couverture"] == 1.0
    assert 0.0 < partiel["couverture"] < 1.0


def test_rarete_est_bornee_entre_zero_et_cent():
    table = rarete.construire(_corpus(*[f"MOT{i}" for i in range(200)]))
    for nom in ("MOT1", "INCONNU", ""):
        valeur = table.profil(nom, nom)["rarete"]
        assert 0.0 <= valeur <= 100.0


def test_le_seuil_de_repandu_a_un_plancher_absolu():
    # Sur un petit corpus, une part relative rendrait « repandu » un mot porte
    # par trois fiches. Trois confusions se lisent : ce n'est pas une
    # volumetrie.
    petit = rarete.construire(_corpus("JEAN DUPONT") * 5)
    assert petit.seuil_repandu == rarete.SEUIL_REPANDU_MIN
    assert petit.profil("Jean Dupont", "JEAN DUPONT")["nom_repandu"] is False


# ------------------ SANS TABLE : TOUT AU REPOS ------------------

def test_sans_table_le_profil_ne_fait_rien_cloturer():
    profil = rarete.profil("Mohammed Ali", "MOHAMMED ALI")
    assert profil["disponible"] is False
    assert profil["nom_repandu"] is False
    assert profil["sans_token_commun"] is True


def test_corpus_vide_ne_leve_pas():
    rarete.installer(rarete.construire([]))
    profil = rarete.profil("Mohammed Ali", "MOHAMMED ALI")
    assert profil["disponible"] is False


# ------------------ AUCUN SCORE N'A BOUGE ------------------

def _config():
    return {"scoring": {"weights": dict(DEFAULT_SCORING_WEIGHTS),
                        "contextual_rules": {}},
            "cut_off_threshold": 75}


def test_le_signal_ne_deplace_aucun_score():
    """
    Ajouter un terme au score deplacerait d'un coup tous les seuils calibres,
    toutes les regles ecrites contre eux et tous les cahiers homologues. La
    rarete s'AJOUTE a l'arbre de decision, elle n'entre pas dans le calcul.
    """
    client = {"client_type": "PP", "client_first_name": "Mohammed", "client_last_name": "Ali"}
    entite = {"entity_id": "X1", "primary_name": "MOHAMMED ALI", "entity_type": "I"}

    sans = match_entities(client, entite, _config())
    rarete.installer(rarete.construire(_corpus(*[f"MOHAMMED ALI {i}" for i in range(300)])))
    avec = match_entities(client, entite, _config())

    assert avec["final_score"] == sans["final_score"]
    assert avec["base_score"] == sans["base_score"]
    assert avec["status"] == sans["status"]
    assert avec["adjustments"] == sans["adjustments"]


def test_la_rarete_est_ecrite_dans_l_arbre_de_decision_des_alertes():
    rarete.installer(rarete.construire(_corpus(*[f"MOHAMMED ALI {i}" for i in range(300)])))
    resultat = match_entities(
        {"client_type": "PP", "client_first_name": "Mohammed", "client_last_name": "Ali"},
        {"entity_id": "X1", "primary_name": "MOHAMMED ALI", "entity_type": "I"},
        _config())
    assert resultat["status"] == "ALERT"
    # Ecrite, pas seulement affichee : une rarete se relit des mois plus tard,
    # en controle, avec le corpus qui l'a produite.
    assert resultat["name_rarity"]["nom_repandu"] is True


def test_aucune_rarete_sur_un_non_rapprochement():
    """Calculee uniquement sur les alertes : au criblage d'un univers entier,
    la calculer sur les candidats ecartes coûterait sur le chemin le plus
    chaud."""
    rarete.installer(rarete.construire(_corpus(*[f"MOHAMMED ALI {i}" for i in range(300)])))
    resultat = match_entities(
        {"client_type": "PP", "client_first_name": "Bjarne", "client_last_name": "Stroustrup"},
        {"entity_id": "X1", "primary_name": "MOHAMMED ALI", "entity_type": "I"},
        _config())
    assert resultat["status"] == "NO_MATCH"
    assert "name_rarity" not in resultat


# ------------------ CONTEXTE DES RÈGLES ------------------

def test_le_contexte_de_regle_porte_la_rarete():
    from fiskr.fprules import build_screening_ctx
    rarete.installer(rarete.construire(_corpus(*[f"MOHAMMED ALI {i}" for i in range(300)])))
    ctx = build_screening_ctx(
        {"client_id": "C1", "client_first_name": "Mohammed", "client_last_name": "Ali"},
        {"entity_id": "X1", "primary_name": "MOHAMMED ALI", "_list_type": "WATCHLIST_PEP"},
        {"final_score": 100.0, "base_score": 100.0, "adjustments": {},
         "best_client_name": "Mohammed Ali", "best_watchlist_name": "MOHAMMED ALI"})
    assert ctx["rarity"]["nom_repandu"] is True
    assert ctx["perimeter"] == "HORS_SANCTION"


def test_le_modele_de_regle_cloture_le_nom_repandu_et_garde_le_nom_rare():
    from fiskr.fprules import rule_templates, run_rule
    modele = next(m for m in rule_templates("SCREENING") if m["key"] == "common_name_tokens")
    base = {"hard_match": False, "corroboration": {"corroborated": False}}

    repandu = dict(base, rarity={"disponible": True, "sans_token_commun": False,
                                 "nom_repandu": True})
    rare = dict(base, rarity={"disponible": True, "sans_token_commun": False,
                              "nom_repandu": False})
    absente = dict(base, rarity=rarete.profil_indisponible())
    corrobore = dict(repandu, corroboration={"corroborated": True})
    identifiant = dict(repandu, hard_match=True)

    assert run_rule(modele["code"], repandu) == (True, None)
    assert run_rule(modele["code"], rare) == (False, None)
    assert run_rule(modele["code"], absente) == (False, None)
    assert run_rule(modele["code"], corrobore) == (False, None)
    assert run_rule(modele["code"], identifiant) == (False, None)


def test_le_modele_de_rarete_est_limite_au_hors_sanction():
    from fiskr.fprules import rule_templates
    modele = next(m for m in rule_templates("SCREENING") if m["key"] == "common_name_tokens")
    assert modele["perimeters"] == ["HORS_SANCTION"]


# ------------------ LA RÉPONSE RESTE BORNÉE ------------------

def test_la_liste_des_correspondances_ne_transporte_pas_le_detail():
    """
    La réponse de criblage NE GROSSIT PAS avec le périmètre : c'est un contrat,
    posé après les ~240 Mo mesurés en production sur un nom courant sans pays.
    Poser la rareté sur chaque ligne le rompait — 45 366 o pour 60 candidats
    contre 50 835 o pour 200, là où le contrat tolère 10 %. Elle est écrite
    dans le journal d'audit et rendue en entier sur `best_match`.
    """
    from fiskr.api import _match_allege
    complet = {
        "status": "ALERT", "final_score": 100.0,
        "name_rarity": {"disponible": True, "corpus": 832470, "tokens": [
            {"token": "MOHAMMED", "df": 24318, "part": 2.92, "repandu": True},
            {"token": "ALI", "df": 9887, "part": 1.19, "repandu": True}],
            "df_min": 9887, "df_max": 24318, "seuil_repandu": 2081,
            "plancher": 3, "information": 8.7, "information_nom_liste": 8.7,
            "couverture": 1.0, "rarete": 12.3, "nom_repandu": True,
            "sans_token_commun": False},
    }
    allege = _match_allege(complet)
    assert "name_rarity" not in allege
    # L'objet d'origine n'est pas touché : `best_match` le rend entier, et le
    # journal d'audit immuable le garde pour CHAQUE correspondance.
    assert len(complet["name_rarity"]["tokens"]) == 2


def test_un_resultat_sans_rarete_traverse_inchange():
    from fiskr.api import _match_allege
    brut = {"status": "NO_MATCH", "final_score": 10.0}
    assert _match_allege(brut) is brut
