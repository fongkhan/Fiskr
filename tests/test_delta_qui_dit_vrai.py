"""
Un lot à homologuer ne montre que ce qui a vraiment changé.

Relevé en production (lecture seule, 19 octobre 2026) : environ 23 lots par
jour à homologuer, plus aucune homologation depuis le 2 septembre — et un delta
qui criait au loup :

* BETERROR : 875 fiches « modifiées », compte variant de 604 à 875 d'un jour à
  l'autre contre une base figée ; les cent détaillées ne changeaient que
  l'ORDRE de leurs références (« A - B » devenu « B - A ») ;
* GBFCDO : 85 fiches sur 100 ne différaient que par leurs guillemets ou leur
  échappement, et 15 n'avaient AUCUN champ différent — l'écran affichait
  « Modifications 24 » au-dessus d'une liste de 19, sans un mot sur les autres ;
* sur cinq jours, dans 52 cas sur 89, le lot du jour portait exactement le delta
  de la veille, encore en attente : un lot de plus à examiner pour rien ;
* `pep_role` portait des programmes de sanction sur des listes qui n'ont rien
  de PEP.

Mesuré ici sur deux publications réelles d'OpenSanctions à deux jours
d'intervalle, lues par le lecteur de production : 750 fiches belges, 598
japonaises et 1 576 britanniques « modifiées » avant ce lot ; 0, 0 et 1 après.
"""
import csv
import hashlib
import io
import json
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.database import Base, Snapshot, SyncReport, WatchlistEntity, compute_checksum
from fiskr.delta import calculate_delta_db, champs_d_identite
from fiskr.ingest import parse_opensanctions_simple_csv, parse_pep_targets_csv
from fiskr.quality import evaluate_and_clean
from fiskr.names import ensure_parsed_name
from fiskr.settings import SETTING_REQUIRE_APPROVAL, set_setting
from fiskr.sources import OPENSANCTIONS_BY_KEY
from fiskr.sync import OPENSANCTIONS_RUNNERS, build_watchlist_entity

RUN_KEY = "be_terror"
SOURCE = OPENSANCTIONS_BY_KEY[RUN_KEY]
FILE_TYPE = SOURCE.file_type
COLONNES = ["id", "schema", "name", "aliases", "birth_date", "countries", "addresses",
            "identifiers", "sanctions", "phones", "emails", "first_seen"]


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'delta_vrai.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _fiche(os_id, nom, sanctions, aliases="", dob="1969-05-12", infos=""):
    return {"id": os_id, "schema": "Person", "name": nom, "aliases": aliases,
            "birth_date": dob, "countries": "ru", "addresses": "", "identifiers": infos,
            "sanctions": sanctions, "phones": "", "emails": "", "first_seen": "2023-04-01"}


def _csv(fiches):
    tampon = io.StringIO()
    ecrivain = csv.DictWriter(tampon, fieldnames=COLONNES, quoting=csv.QUOTE_ALL)
    ecrivain.writeheader()
    for fiche in fiches:
        ecrivain.writerow(fiche)
    return tampon.getvalue()


def _fetcher(contenu):
    return lambda url, dest: Path(dest).write_text(contenu, encoding="utf-8")


def _synchroniser(db, fiches):
    return OPENSANCTIONS_RUNNERS[RUN_KEY](db, fetcher=_fetcher(_csv(fiches)))


def _pivots(fiches):
    """Les fiches telles que les lit le lecteur de production."""
    with tempfile.TemporaryDirectory() as dossier:
        chemin = Path(dossier) / "targets.simple.csv"
        chemin.write_text(_csv(fiches), encoding="utf-8")
        return list(parse_opensanctions_simple_csv(
            str(chemin), id_prefix=SOURCE.id_prefix, origin=SOURCE.origin,
            designation_reasons=SOURCE.designation_reasons))


def _lot_pivots(db, items, statut="READY", decalage_h=0, prefixe="lot"):
    """Snapshot construit par le chemin d'ecriture de la synchronisation."""
    sid = f"{prefixe}-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=sid, file_type=FILE_TYPE, file_name=f"{sid}.csv",
                    file_hash=uuid.uuid4().hex, record_count=len(items), status=statut,
                    uploaded_at=datetime(2026, 10, 1) + timedelta(hours=decalage_h)))
    for item in items:
        item = ensure_parsed_name(item)
        db.add(build_watchlist_entity(sid, item, evaluate_and_clean(item)))
    db.commit()
    return sid


def _lot(db, fiches, **options):
    return _lot_pivots(db, _pivots(fiches), **options)


# Deux publications du même contenu : références d'une entrée et entrées
# elles-mêmes dans un autre ordre, alias permutés, lignes inversées.
JOUR_1 = [
    _fiche("Q1", "Viktor Testov", "2022/1529 (OJ L239) - 2022/353 (OJ L66);2020/716 (OJ L168)",
           aliases="V. Testov;Vik Testov"),
    _fiche("Q2", "Olga Essaieva", "2023/571 (OJ L75 I) - 2023/806 (OJ L101)"),
]
JOUR_2 = [
    _fiche("Q2", "Olga Essaieva", "2023/806 (OJ L101) - 2023/571 (OJ L75 I)"),
    _fiche("Q1", "Viktor Testov", "2020/716 (OJ L168);2022/353 (OJ L66) - 2022/1529 (OJ L239)",
           aliases="Vik Testov;V. Testov"),
]


# ------------------ 1. L'ORDRE N'EST PAS UNE MODIFICATION ------------------

def test_l_ordre_de_la_colonne_sanctions_ne_change_ni_l_empreinte_ni_le_delta(db):
    premier = _synchroniser(db, JOUR_1)
    assert premier.status == "SUCCESS", premier.message
    second = _synchroniser(db, JOUR_2)

    assert second.status == "NO_CHANGE", second.message
    assert second.delta_report["summary"]["added_count"] == 0
    assert second.delta_report["summary"]["removed_count"] == 0
    assert second.delta_report["summary"]["modified_count"] == 0
    empreintes = {}
    for ligne in db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(
            [premier.snapshot_id, second.snapshot_id])).all():
        empreintes.setdefault(ligne.entity_id, set()).add(ligne.entity_checksum)
    assert all(len(v) == 1 for v in empreintes.values()), empreintes


def test_des_guillemets_echappes_autrement_ne_sont_pas_une_modification(db):
    """GBFCDO : « 'involved person' », « ""involved person"" », « \\"involved
    person\\" » et « “involved person” » disent la même chose."""
    def fiche(motif, nom):
        return _fiche("Q9", nom, f"Regulations 2019 - Petrov is an {motif} - 2022-03-15")

    variantes = [fiche("'involved person'", "Ivan O'Petrov"),
                 fiche('""involved person""', "Ivan O’Petrov"),
                 fiche('\\"involved person\\"', "Ivan O'Petrov"),
                 fiche("“involved person”", "Ivan O‘Petrov")]
    lots = [_lot(db, [v], decalage_h=i) for i, v in enumerate(variantes)]
    for ancien, nouveau in zip(lots, lots[1:]):
        rapport = calculate_delta_db(db, ancien, nouveau)
        assert rapport["summary"]["modified_count"] == 0, rapport["details"]["modified"]
    # La même empreinte, d'emblée : rien n'a eu à être réaligné
    assert len({l.entity_checksum for l in db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id.in_(lots)).all()}) == 1


# ------------------ 2. LE COMPTEUR ET LA LISTE DISENT LA MÊME CHOSE ------------------

def test_une_empreinte_differente_sans_champ_different_n_est_pas_une_modification(db):
    """Les 15 fiches GBFCDO : l'empreinte était calculée sur l'enregistrement
    lu, un texte qui ne variait qu'au-delà des 500 caractères gardés par la base
    la faisait changer. Calculée sur ce qui est stocké, elle ne bouge plus ; et
    une empreinte périmée (écrite par une version antérieure) n'est pas comptée,
    elle est réalignée — et dite."""
    def lu(fin):
        return {"entity_id": "GB-Q5", "entity_type": "I", "primary_name": "Anna Longueva",
                "designation": "The Russia Regulations 2019 - " + "x" * 600 + fin}
    a, b = _lot_pivots(db, [lu(" fin A")]), _lot_pivots(db, [lu(" fin B")], decalage_h=1)
    assert calculate_delta_db(db, a, b)["summary"]["modified_count"] == 0

    # Empreinte périmée : même contenu, empreinte d'une autre version du code
    c = _lot(db, [_fiche("Q6", "Boris Perimov", "Programme Alpha")], decalage_h=2)
    d = _lot(db, [_fiche("Q6", "Boris Perimov", "Programme Alpha")], decalage_h=3)
    db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == c).update(
        {"entity_checksum": "empreinte-d-une-version-anterieure"})
    db.commit()
    rapport = calculate_delta_db(db, c, d)
    assert rapport["summary"]["modified_count"] == 0
    assert rapport["details"]["modified"] == []
    assert rapport["details"]["checksum_only_count"] == 1
    # Réalignée une fois pour toutes : la comparaison suivante n'a plus rien à dire
    assert "checksum_only_count" not in calculate_delta_db(db, c, d)["details"]


def test_quand_le_detail_est_borne_l_identite_passe_devant(db):
    """Trois modifications d'information, deux d'identité, deux lignes de
    détail : ce sont les deux d'identité qu'on voit, et le reste est compté."""
    def fiches(infos, dob):
        return ([_fiche(f"Q{i}", f"Nom Numero{i}", "P", infos=infos) for i in range(3)]
                + [_fiche(f"Q{i}", f"Nom Numero{i}", "P", dob=dob) for i in range(3, 5)])
    ancien = _lot(db, fiches("NOTE-1", "1960-01-01"))
    nouveau = _lot(db, fiches("NOTE-2", "1961-01-01"), decalage_h=1)
    rapport = calculate_delta_db(db, ancien, nouveau, limite=2)
    assert rapport["summary"]["modified_count"] == 5
    assert rapport["summary"]["modified_identity_count"] == 2
    assert [m["id"] for m in rapport["details"]["modified"]] == ["BET-Q3", "BET-Q4"]
    assert rapport["details"]["modified_truncated"] == 3


def test_l_ecran_d_homologation_dit_ce_qui_n_est_pas_detaille():
    """Constaté dans un vrai navigateur : « Modifications 24 », une liste de 19,
    rien sur les 5 autres — le rendu de l'étape 1 ignorait `modified_truncated`.
    Le titre de chaque section porte désormais le TOTAL et une ligne dit ce qui
    n'est pas détaillé."""
    source = (Path(__file__).resolve().parents[1] / "fiskr" / "static" / "app.js").read_text(
        encoding="utf-8")
    debut = source.index("function renderReviewDeltaDetails(")
    corps = source[debut:source.index("\n}\n", debut)]
    for categorie in ("added", "removed", "modified"):
        assert f"details.{categorie}_truncated" in corps, categorie
    assert "_ligneNonDetaillee(restant)" in corps
    assert "checksum_only_count" in corps
    assert "reader_form_count" in corps


# ------------------ 3. IDENTITÉ OU INFORMATION ------------------

def test_seule_additional_informations_change_c_est_une_information(db):
    ancien = _lot(db, [_fiche("Q1", "Viktor Testov", "P", infos="PASS-1"),
                       _fiche("Q2", "Olga Essaieva", "P", dob="1970-01-01")])
    nouveau = _lot(db, [_fiche("Q1", "Viktor Testov", "P", infos="PASS-1;NOTE-2"),
                        _fiche("Q2", "Olga Essaieva", "P", dob="1971-01-01")], decalage_h=1)
    rapport = calculate_delta_db(db, ancien, nouveau)
    assert rapport["summary"]["modified_count"] == 2
    assert rapport["summary"]["modified_identity_count"] == 1
    natures = {m["id"]: m["nature"] for m in rapport["details"]["modified"]}
    assert natures == {"BET-Q1": "INFORMATION", "BET-Q2": "IDENTITE"}
    # L'identité passe devant : quand le détail est borné, c'est elle qu'on voit
    assert [m["id"] for m in rapport["details"]["modified"]] == ["BET-Q2", "BET-Q1"]


def test_les_champs_d_identite_sont_ceux_que_le_moteur_lit():
    from fiskr.screenpool import ENTITY_PROJECTION
    identite = champs_d_identite()
    assert set(ENTITY_PROJECTION) - {"entity_id", "snapshot_id", "entity_checksum"} <= identite
    assert "sanction_programs" in identite
    assert not {"additional_informations", "designation", "pep_role",
                "designation_reasons", "address"} & identite


def test_le_rapport_de_synchronisation_porte_le_compte_d_identite(db):
    assert _synchroniser(db, [_fiche("Q1", "Viktor Testov", "P", dob="1960-01-01")]).status == "SUCCESS"
    rapport = _synchroniser(db, [_fiche("Q1", "Viktor Testov", "P", dob="1961-01-01")])
    assert rapport.modified_count == 1
    assert rapport.modified_identity_count == 1
    assert rapport.delta_report["summary"]["modified_identity_count"] == 1


# ------------------ 4. PAS DE LOT EMPILÉ SUR UN LOT IDENTIQUE ------------------

def test_un_contenu_identique_au_dernier_lot_en_attente_n_est_pas_empile(db):
    assert _synchroniser(db, JOUR_1).status == "SUCCESS"
    set_setting(db, SETTING_REQUIRE_APPROVAL, True)
    db.commit()

    avec_ajout = JOUR_1 + [_fiche("Q3", "Pavel Nouveauv", "P")]
    en_attente = _synchroniser(db, avec_ajout)
    assert en_attente.status == "PENDING_REVIEW"

    # Même contenu, autres octets (lignes inversées) : rien de nouveau
    meme = _synchroniser(db, list(reversed(avec_ajout)))
    assert meme.status == "NO_CHANGE", meme.message
    assert en_attente.snapshot_id in meme.message
    assert meme.previous_snapshot_id == en_attente.snapshot_id
    attente = db.query(Snapshot).filter(Snapshot.file_type == FILE_TYPE,
                                        Snapshot.status == "PENDING_REVIEW").all()
    assert [s.snapshot_id for s in attente] == [en_attente.snapshot_id]
    archive = db.query(Snapshot).filter(Snapshot.snapshot_id == meme.snapshot_id).one()
    assert archive.status == "SUPERSEDED"

    # Le moindre champ différent : le lot est empilé comme avant
    different = _synchroniser(db, avec_ajout[:-1] + [_fiche("Q3", "Pavel Nouveauv", "P", dob="1980-02-02")])
    assert different.status == "PENDING_REVIEW"
    assert db.query(Snapshot).filter(Snapshot.file_type == FILE_TYPE,
                                     Snapshot.status == "PENDING_REVIEW").count() == 2


def test_un_lot_en_attente_perime_ne_sert_pas_de_reference(db):
    """Un lot plus ancien que la production ne s'approuve plus
    (fiskr/fraicheur.py) : s'aligner sur lui jetterait le bon."""
    from fiskr.sync import _dernier_lot_en_attente
    perime = _lot(db, JOUR_1, statut="PENDING_REVIEW", decalage_h=0, prefixe="perime")
    _lot(db, JOUR_1, statut="READY", decalage_h=5, prefixe="prod")
    assert _dernier_lot_en_attente(db, FILE_TYPE, sauf="aucun") is None
    recent = _lot(db, JOUR_1, statut="PENDING_REVIEW", decalage_h=9, prefixe="recent")
    assert _dernier_lot_en_attente(db, FILE_TYPE, sauf="aucun").snapshot_id == recent
    assert perime != recent


# ------------------ 5. PREMIER PASSAGE APRÈS LE DÉPLOIEMENT ------------------

def _production_de_la_version_precedente(db, fiches):
    """Ce que la version précédente a laissé en base : programmes de sanction
    dans `pep_role` dans l'ordre du jour, `designation` = la première entrée
    telle que lue, et une empreinte calculée sur l'enregistrement lu, sans
    normalisation."""
    sid = f"prec-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=sid, file_type=FILE_TYPE, file_name=f"{sid}.csv",
                    file_hash=uuid.uuid4().hex, record_count=len(fiches), status="READY",
                    uploaded_at=datetime(2026, 9, 1)))
    for item, brut in zip(_pivots(fiches), fiches):
        entrees = [e.strip() for e in brut["sanctions"].split(";") if e.strip()]
        item.update(designation=entrees[0] if entrees else None,
                    pep_role="; ".join(entrees) or None, sanction_programs=[])
        empreinte = hashlib.sha256(json.dumps(
            {k: v for k, v in item.items() if k not in ("id", "snapshot_id", "entity_checksum")},
            sort_keys=True, default=str).encode("utf-8")).hexdigest()
        item = ensure_parsed_name(item)
        ligne = build_watchlist_entity(sid, item, evaluate_and_clean(item))
        ligne.entity_checksum = empreinte
        db.add(ligne)
    db.commit()
    return sid


def test_le_premier_passage_apres_le_changement_de_normalisation_ne_fait_pas_de_vague(db):
    """La production a été écrite par la version précédente : empreintes non
    normalisées, programmes dans `pep_role`. Le premier lot relu par cette
    version-ci ne doit annoncer AUCUNE modification : ni la normalisation, ni la
    correction du lecteur ne sont des modifications de la source. Elles sont
    dites à part — et la correction du lecteur reste à homologuer une fois,
    puisqu'elle change ce que la production garde."""
    sans_sanction = _fiche("Q4", "Ilya Sanssanction", "", aliases="I. S.;Ilya S.")
    prod = _production_de_la_version_precedente(db, JOUR_1 + [sans_sanction])
    rapport = _synchroniser(db, JOUR_2 + [dict(sans_sanction, aliases="Ilya S.;I. S.")])

    resume = rapport.delta_report["summary"]
    assert (resume["added_count"], resume["removed_count"], resume["modified_count"]) == (0, 0, 0)
    assert resume["modified_identity_count"] == 0
    details = rapport.delta_report["details"]
    assert details["reader_form_count"] == 2          # Q1 et Q2 : forme seule
    assert "reader_form_unverified_count" not in details
    assert details["checksum_only_count"] == 1        # Q4 : empreinte périmée seule
    # Pas un non-événement : la production n'a pas encore la forme corrigée
    assert rapport.status == "SUCCESS", rapport.message
    assert rapport.previous_snapshot_id == prod


def test_une_vraie_modification_n_est_pas_prise_pour_un_changement_de_forme(db):
    _production_de_la_version_precedente(db, JOUR_1)
    modifie = [JOUR_2[0], _fiche("Q1", "Viktor Testov", "2020/716 (OJ L168);2022/353 (OJ L66) - 2022/1529 (OJ L239);2024/1 (OJ L1)",
                                 aliases="Vik Testov;V. Testov")]
    rapport = _synchroniser(db, modifie)
    resume = rapport.delta_report["summary"]
    assert resume["modified_count"] == 1
    assert resume["modified_identity_count"] == 1      # un programme de plus
    assert rapport.delta_report["details"]["modified"][0]["id"] == "BET-Q1"


def test_un_changement_de_forme_ne_fait_pas_d_une_information_une_identite(db):
    """Vu à la vérification dans le navigateur : une note ajoutée sur une fiche
    dont le lecteur range aussi les programmes ailleurs était classée IDENTITÉ
    (`sanction_programs` faisait partie des champs changés). La forme est
    retirée de la modification ; il reste ce que la source a changé."""
    _production_de_la_version_precedente(db, [dict(JOUR_1[1], identifiers="PASS-1")])
    rapport = _synchroniser(db, [dict(JOUR_2[0], identifiers="PASS-1;NOTE-2")])
    resume = rapport.delta_report["summary"]
    assert (resume["modified_count"], resume["modified_identity_count"]) == (1, 0)
    ligne = rapport.delta_report["details"]["modified"][0]
    assert ligne["nature"] == "INFORMATION"
    assert ligne["changes_detected"] == ["additional_informations"]
    assert set(ligne["after"]) == {"additional_informations"}


# ------------------ 6. pep_role SUR UNE LISTE QUI N'EST PAS PEP ------------------

def test_pep_role_ne_porte_plus_les_programmes_d_une_liste_de_sanctions(tmp_path):
    chemin = tmp_path / "targets.simple.csv"
    chemin.write_text(_csv([JOUR_1[0]]), encoding="utf-8")
    sanction = list(parse_opensanctions_simple_csv(str(chemin), id_prefix="BET"))[0]
    assert sanction["pep_role"] is None
    assert sanction["sanction_programs"] == ["2020/716 (OJ L168)",
                                             "2022/1529 (OJ L239) - 2022/353 (OJ L66)"]
    assert sanction["designation"] == "2020/716 (OJ L168)"
    # Sur la liste PEP, la même colonne reste la fonction
    pep = list(parse_pep_targets_csv(str(chemin)))[0]
    assert pep["pep_role"] == "2020/716 (OJ L168); 2022/1529 (OJ L239) - 2022/353 (OJ L66)"
    assert not pep.get("sanction_programs")
