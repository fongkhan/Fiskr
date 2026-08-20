"""
Un réglage que le moteur lit mais que la couche de réglages filtre est
injoignable.

C'est le défaut trouvé sur `token_set` : `scoring_weights()` reconstruit le
dictionnaire des poids sur les seules clés de `DEFAULT_SCORING_WEIGHTS`. La
métrique n'y figurait pas, donc poser `token_set: 0.4` dans les réglages
n'avait **aucun effet** — le moteur lisait bien `weights.get("token_set", 0.0)`,
mais la clé ne pouvait jamais lui parvenir. Rien n'échouait : la fonctionnalité
était simplement morte.

Ce test ferme la classe entière, pas seulement le cas trouvé. Il lit les clés
que le moteur demande et vérifie qu'elles survivent au filtrage. Ajouter une
métrique ou une règle contextuelle sans l'ajouter aux défauts échoue ici.
"""
import re
from pathlib import Path

import pytest

from fiskr.settings import (DEFAULT_SCORING_WEIGHTS, DEFAULT_CONTEXT_RULES,
                            scoring_weights, scoring_context_rules)

SCORING = Path("fiskr/scoring.py").read_text(encoding="utf-8")


def _lues(motif: str) -> set:
    return {m.group(1) for m in re.finditer(motif, SCORING)}


def test_l_analyse_trouve_bien_des_lectures():
    """Garde-fou : si le repérage ne trouve plus rien, le test passe à vide."""
    assert len(_lues(r'weights\.get\(\s*"([^"]+)"')) >= 3
    assert len(_lues(r'rules\.get\(\s*"([^"]+)"')) >= 5


def test_chaque_poids_lu_par_le_moteur_survit_au_reglage():
    lues = _lues(r'weights\.get\(\s*"([^"]+)"')
    manquantes = sorted(lues - set(DEFAULT_SCORING_WEIGHTS))
    assert not manquantes, (
        f"poids lus par le moteur mais absents des défauts : {manquantes}. "
        "Le réglage à chaud les filtrera : la métrique sera injoignable.")


def test_chaque_regle_contextuelle_lue_survit_au_reglage():
    lues = {c for c in _lues(r'rules\.get\(\s*"([^"]+)"')
            if any(t in c for t in ("dob", "gender", "geograph"))}
    manquantes = sorted(lues - set(DEFAULT_CONTEXT_RULES))
    assert not manquantes, (
        f"règles contextuelles lues mais absentes des défauts : {manquantes}")


def test_le_filtrage_est_bien_le_mecanisme_en_cause():
    """Le test précédent n'a de sens que si la couche de réglages filtre
    réellement. S'il cessait de filtrer, ces tests deviendraient décoratifs —
    et le vrai risque, une clé inventée qui passe, apparaîtrait."""
    from fiskr.settings import SETTING_SCORING_WEIGHTS
    from fiskr.database import get_db, AppSetting
    db = next(get_db())
    try:
        db.query(AppSetting).filter(
            AppSetting.key == SETTING_SCORING_WEIGHTS).delete()
        db.add(AppSetting(key=SETTING_SCORING_WEIGHTS, value={
            "jaro_winkler": 0.4, "damerau_levenshtein": 0.4, "token_sort": 0.2,
            "token_set": 0.0, "metrique_inventee": 0.9}))
        db.commit()
        obtenus = scoring_weights(db)
        assert "metrique_inventee" not in obtenus, (
            "une clé inconnue traverse : le moteur recevrait un réglage qu'il "
            "ne sait pas interpréter")
        assert set(obtenus) == set(DEFAULT_SCORING_WEIGHTS)
    finally:
        db.query(AppSetting).filter(
            AppSetting.key == SETTING_SCORING_WEIGHTS).delete()
        db.commit()
        db.close()


def test_les_defauts_livres_ne_deplacent_aucun_score():
    """Une métrique ajoutée doit arriver à poids nul : l'activer déplace tous
    les scores d'un coup, donc les seuils calibrés et les cahiers homologués."""
    assert DEFAULT_SCORING_WEIGHTS["token_set"] == 0.0
    somme_active = sum(v for k, v in DEFAULT_SCORING_WEIGHTS.items() if v > 0)
    assert abs(somme_active - 1.0) < 1e-9, (
        f"les poids actifs doivent sommer à 1 : {somme_active}")


def test_le_reglage_sans_valeur_rend_exactement_les_defauts():
    db_free = scoring_weights(None)
    assert db_free == DEFAULT_SCORING_WEIGHTS
    assert scoring_context_rules(None) == DEFAULT_CONTEXT_RULES
