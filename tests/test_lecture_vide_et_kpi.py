"""
Une lecture vide n'est pas une liste vide ; un indicateur se définit une fois.

Deux défauts relevés en production le 1er octobre 2026.

1. **La liste canadienne vide depuis deux mois.** L'URL était passée au XML,
   le lecteur était resté CSV : zéro fiche lue chaque nuit, et le rapport
   disait « contenu identique à la liste active » — zéro comparé à zéro. Le
   premier de ces lots vides avait même été homologué. Pire : le XML officiel
   ne porte AUCUN nom (5 708 enregistrements, quatre balises mal étiquetées),
   aucun lecteur ne pouvait en tirer une liste.

2. **Le délai moyen de décision défini trois fois.** Tableau de bord, ligne par
   analyste et rapport d'activité le calculaient sur trois populations
   différentes ; une ligne annonçait « 1 décision » avec le délai d'une alerte
   rouverte. Et l'arrondi au dixième d'heure affichait « 0,0 h » pour deux
   décisions prises en deux minutes.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fiskr import kpi
from fiskr import sync as sync_mod


# --------------------------------------------------------------------------
# 1. Une lecture vide est un échec
# --------------------------------------------------------------------------

@pytest.fixture()
def db():
    from fiskr.database import get_db
    session = next(get_db())
    yield session
    session.close()


def _fetcher(contenu: bytes):
    def _ecrire(url, dest):
        Path(dest).write_bytes(contenu)
    return _ecrire


def test_un_fichier_dont_aucune_fiche_ne_sort_est_une_erreur(db):
    """
    La garde du lot, sur la situation exacte de la production : un fichier
    bien téléchargé, un lecteur qui n'y reconnaît rien.
    """
    from fiskr.database import Snapshot
    avant = db.query(Snapshot).filter(Snapshot.file_type == "WATCHLIST_MAS").count()
    rapport = sync_mod._run_list_replacement_sync(
        db, source="LECTVIDE", file_type="WATCHLIST_MAS", url="https://exemple.test/x.xml",
        parser=lambda chemin: iter(()), file_label="vide", temp_suffix=".csv",
        fetcher=_fetcher(b"<?xml version='1.0'?><data-set><record/></data-set>"))
    assert rapport.status == "ERROR"
    assert "aucune fiche n'a pu en être lue" in rapport.message
    assert "laissée intacte" in rapport.message
    # Aucun lot ne part en homologation : il n'y a rien à relire.
    restants = db.query(Snapshot).filter(Snapshot.file_type == "WATCHLIST_MAS",
                                         Snapshot.status.in_(("READY", "PENDING_REVIEW"))).count()
    assert restants <= avant


def test_l_erreur_ne_dit_plus_jamais_contenu_identique(db):
    """Le message qui a masqué le défaut deux mois durant ne peut plus sortir."""
    rapport = sync_mod._run_list_replacement_sync(
        db, source="LECTVIDE2", file_type="WATCHLIST_MAS", url="https://exemple.test/y.csv",
        parser=lambda chemin: iter(()), file_label="vide", temp_suffix=".csv",
        fetcher=_fetcher(b"a,b,c\n1,2,3\n"))
    assert "identique" not in (rapport.message or "")


def test_canada_passe_par_la_voie_qui_porte_des_noms():
    """Par défaut, le jeu OpenSanctions : le XML officiel ne porte aucun nom."""
    cfg = sync_mod._canada_source_config({"enabled": True})
    assert cfg["format"] == "opensanctions"
    assert "ca_dfatd_sema_sanctions" in cfg["url"]


def test_une_url_canada_en_xml_est_ignoree():
    """
    L'installation de production avait le XML renseigné : elle doit basculer
    d'elle-même sur la voie qui fonctionne plutôt qu'échouer chaque nuit.
    """
    xml = ("https://www.international.gc.ca/world-monde/assets/office_docs/"
           "international_relations-relations_internationales/sanctions/sema-lmes.xml")
    cfg = sync_mod._canada_source_config({"enabled": True, "url": xml})
    assert not cfg["url"].endswith(".xml")
    assert "opensanctions" in cfg["url"]


def test_le_csv_officiel_reste_une_voie_possible():
    cfg = sync_mod._canada_source_config({"format": "csv"})
    assert cfg["format"] == "csv" and cfg["url"].endswith(".csv")


def test_un_format_inconnu_retombe_sur_la_voie_qui_fonctionne():
    assert sync_mod._canada_source_config({"format": "xml"})["format"] == "opensanctions"


def test_le_lecteur_opensanctions_canada_lit_le_format_publie(tmp_path):
    from fiskr.ingest import parse_canada_opensanctions_csv
    fichier = tmp_path / "ca.csv"
    fichier.write_text(
        '"id","schema","name","aliases","birth_date","countries","addresses","identifiers",'
        '"sanctions","phones","emails","program_ids","dataset","first_seen","last_seen","last_change"\n'
        '"NK-1","LegalEntity","SEVERTRUCKS LLC","","","ru","","","Russia / Russie - 1, Part 2",'
        '"","","CA-SEMA","Canadian Consolidated Autonomous Sanctions List","2024-08-06","2026-09-29","2025-05-19"\n',
        encoding="utf-8")
    fiches = list(parse_canada_opensanctions_csv(str(fichier)))
    assert len(fiches) == 1
    assert fiches[0]["primary_name"] == "SEVERTRUCKS LLC"
    assert fiches[0]["entity_id"].startswith("CAN-")


# --------------------------------------------------------------------------
# 2. Le délai de décision : une définition
# --------------------------------------------------------------------------

T = datetime(2026, 9, 1, 9, 0)


def test_le_delai_ecarte_les_paires_incoherentes():
    """
    Une décision antérieure à sa création ne mesure pas un délai ; un seul
    écart négatif de plusieurs jours suffirait à faire mentir la moyenne.
    """
    paires = [(T, T + timedelta(minutes=2)), (T, T + timedelta(minutes=4)),
              (T, T - timedelta(days=3)), (None, T), (T, None)]
    assert kpi.delai_moyen_secondes(paires) == 180


def test_sans_decision_datable_le_delai_est_inconnu_et_non_nul():
    """« Inconnu » et « zéro » ne disent pas la même chose."""
    assert kpi.delai_moyen_secondes([]) is None
    assert kpi.delai_moyen_secondes([(T, None)]) is None


def test_deux_minutes_ne_s_affichent_plus_zero_heure():
    """L'arrondi historique est conservé ; la mesure exacte voyage à côté."""
    secondes = kpi.delai_moyen_secondes([(T, T + timedelta(minutes=2))])
    assert secondes == 120
    assert kpi.en_heures(secondes) == 0.0


def test_une_decision_est_une_alerte_instruite_et_close_par_un_humain():
    """CLOSED_BY_RULE n'a été instruite par personne : hors de la population."""
    assert set(kpi.STATUTS_DECIDES) == {"CLOSED_CONFIRMED", "CLOSED_FALSE_POSITIVE"}


def test_les_trois_lecteurs_partagent_la_meme_definition():
    """
    Tableau de bord, ventilation par analyste et rapport d'activité : les trois
    calculaient à leur façon. Ils appellent désormais la même fonction.
    """
    import inspect
    from fiskr import api as api_mod
    source_kpi = inspect.getsource(kpi.indicateurs)
    assert source_kpi.count("delai_moyen_secondes(") == 2      # global + par analyste
    assert source_kpi.count("filtre_des_decisions()") >= 3     # compte, global, par analyste
    rapport = inspect.getsource(api_mod)
    debut = rapport.index("alerts_decided_q = db.query(Alert)")
    assert "delai_moyen_secondes(" in rapport[debut:debut + 1200]


def test_l_endpoint_n_est_plus_qu_un_appel():
    import inspect
    from fiskr import api as api_mod
    source = inspect.getsource(api_mod.get_compliance_kpis)
    assert "indicateurs(db)" in source
    assert len(source.splitlines()) < 20


def test_le_kpi_porte_la_fraicheur_des_listes(db):
    """Le volume disait « combien », jamais « depuis quand »."""
    rendu = kpi.indicateurs(db)
    fraicheur = rendu["lists"]["freshness"]
    for cle in ("lists_in_production", "lists_behind", "max_delay_hours",
                "pending_snapshots", "empty_lists", "by_list"):
        assert cle in fraicheur
    assert "avg_decision_seconds" in rendu["alerts"]


# --------------------------------------------------------------------------
# L'écran
# --------------------------------------------------------------------------

def test_l_ecran_choisit_l_unite_a_partir_de_la_mesure_exacte():
    source = Path("fiskr/static/app.js").read_text(encoding="utf-8")
    assert "function formatDuree(secondes)" in source
    assert 'a.avg_decision_hours + " h"' not in source
    assert "r.avg_decision_hours + \" h\"" not in source


def test_l_historique_d_une_regle_est_enfin_charge():
    """
    L'éditeur réservait une place au journal d'une règle anti-faux positifs —
    qui clôt des alertes sans qu'aucun analyste ne les voie — et rien ne venait
    jamais la remplir.
    """
    source = Path("fiskr/static/app.js").read_text(encoding="utf-8")
    assert "loadFpRuleChanges(ruleId);" in source
    assert "/api/fprules/${encodeURIComponent(ruleId)}/changes" in source


def test_chaque_action_journalisee_d_une_regle_a_son_libelle():
    """Un libellé manquant afficherait le code brut ; un libellé en trop, une action fantôme."""
    import re
    api = Path("fiskr/api.py").read_text(encoding="utf-8")
    journalisees = set(re.findall(r'_log_rule_change\(db, \w+, "([A-Z_]+)"', api))
    journalisees |= {"ENABLED", "DISABLED"}   # ternaire d'activation
    js = Path("fiskr/static/app.js").read_text(encoding="utf-8")
    bloc = js[js.index("const FP_RULE_CHANGE_LABELS"):]
    bloc = bloc[:bloc.index("};")]
    libelles = set(re.findall(r"\b([A-Z_]{4,}):", bloc))
    assert journalisees == libelles, (journalisees ^ libelles)


def test_le_remplacement_d_une_regle_est_journalise_comme_tel():
    # Le code seulement : le commentaire qui raconte l'ancien defaut le cite.
    code = "\n".join(l for l in Path("fiskr/api.py").read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))
    assert '_log_rule_change(db, old, "SUPERSEDED", ' in code
    assert "if False else" not in code
