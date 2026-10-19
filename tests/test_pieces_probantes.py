"""
Ce que le produit affirme sans l'avoir vérifié.

L'audit de processus avait relevé trois fois le même défaut, à trois endroits
sans rapport : le journal marquait « envoyé » sans regarder si l'envoi avait
abouti, un contrôle jugeait le démon sur son pouls plutôt que sur ses
conséquences, et un compteur allait mesurer la taille décodée d'un fichier là
où c'est le fil qu'on paie. **Ce lot chasse la classe, pas l'instance.**

Deux nouvelles prises, dans le même registre, et la plus grave d'abord.

1. **Une pièce justificative vit en deux endroits** : une ligne en base qui
   porte son nom, et un fichier sur le disque. Les écrans lisaient la ligne
   seule. Le téléchargement, lui, lit le fichier — et découvrait son absence
   au moment où quelqu'un clique. Dans un produit de conformité, ce moment-là
   porte un nom : le contrôle. Une preuve ne se reconstitue pas ; il faut donc
   poser la question **à froid**, pendant qu'une sauvegarde peut encore la
   rendre.
2. **`smtplib.sendmail` ne lève que si TOUS les destinataires sont refusés.**
   Un refus partiel revenait dans un dictionnaire que personne ne lisait, et
   l'appelant en concluait « envoyé ». Deux adresses sur cinq refusées ne
   peuvent pas s'écrire comme un envoi réussi dans la pièce qu'on produit pour
   prouver qu'on a prévenu.
"""
import smtplib
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from fiskr import notify as notify_mod
from fiskr import preuves
from fiskr.database import AlertAttachment, WatchlistEntity, WhitelistPair, get_db
from fiskr.mise_en_service import _pieces_probantes

UID = uuid.uuid4().hex[:8]


@pytest.fixture()
def db():
    session = next(get_db())
    yield session
    session.query(AlertAttachment).filter(
        AlertAttachment.file_name.like(f"p{UID}%")).delete(synchronize_session=False)
    session.query(WhitelistPair).filter(
        WhitelistPair.client_id.like(f"C{UID}%")).delete(synchronize_session=False)
    session.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id.like(f"S{UID}%")).delete(synchronize_session=False)
    session.commit()
    session.close()


def _piece(tmp_path, nom, ecrire=True):
    chemin = tmp_path / nom
    if ecrire:
        chemin.write_bytes(b"justificatif")
    return str(chemin)


# --------------------------------------------------------------------------
# 1. Présent, absent, et « pas de pièce » — trois états, pas deux
# --------------------------------------------------------------------------

def test_une_piece_presente_est_reconnue(tmp_path):
    assert preuves.piece_presente(_piece(tmp_path, "ok.pdf")) is True


def test_une_piece_disparue_est_reconnue_absente(tmp_path):
    assert preuves.piece_presente(_piece(tmp_path, "parti.pdf", ecrire=False)) is False


def test_une_ligne_sans_piece_n_est_pas_une_piece_manquante():
    """
    Une absence DÉCLARÉE et une promesse ROMPUE ne se confondent pas : la
    première est normale, la seconde est le défaut que ce lot traque.
    """
    assert preuves.piece_presente(None) is False
    assert preuves.piece_presente("") is False


def test_un_dossier_n_est_pas_une_piece(tmp_path):
    """`exists()` dirait oui : un dossier ne se télécharge pas."""
    (tmp_path / "dossier").mkdir()
    assert preuves.piece_presente(str(tmp_path / "dossier")) is False


def test_un_chemin_illisible_ne_remonte_jamais_en_exception():
    """Un écran ne doit pas tomber parce qu'un chemin en base est aberrant."""
    assert preuves.piece_presente("\0/chemin/impossible") is False


# --------------------------------------------------------------------------
# L'inventaire
# --------------------------------------------------------------------------

def test_l_inventaire_separe_ce_qui_est_la_de_ce_qui_manque(db, tmp_path):
    db.add(AlertAttachment(alert_id=1, file_name=f"p{UID}_ok.pdf",
                           file_path=_piece(tmp_path, "a.pdf"), uploaded_by="t"))
    db.add(AlertAttachment(alert_id=1, file_name=f"p{UID}_perdu.pdf",
                           file_path=_piece(tmp_path, "b.pdf", ecrire=False),
                           uploaded_by="t"))
    db.commit()

    famille = next(f for f in preuves.inventaire(db)["familles"] if f["cle"] == "alertes")
    assert famille["annoncees"] >= 2
    assert famille["manquantes"] >= 1
    noms = [a["nom"] for a in famille["absentes"]]
    assert f"p{UID}_perdu.pdf" in noms
    assert f"p{UID}_ok.pdf" not in noms


def test_les_exclusions_sont_cherchees_par_l_index_partiel(db):
    """
    La table des fiches listées compte des millions de lignes et aucune
    colonne de pièce n'y est indexée. La leçon est déjà payée ici : la même
    requête sans l'index partiel `ix_wl_entities_excluded` mettait 21 à 35 s
    en production pour rendre zéro ligne. Le critère `excluded IS TRUE` doit
    donc figurer dans la requête — il ne retranche rien, les deux colonnes
    étant écrites ensemble.
    """
    famille = next(f for modele, f in preuves.familles() if f.cle == "exclusions")
    assert famille.prefiltre is not None

    from fiskr.database import WatchlistEntity
    requete = db.query(WatchlistEntity).filter(famille.prefiltre(WatchlistEntity))
    assert "excluded" in str(requete.statement.compile()).lower()

    # Et les familles qui n'en ont pas besoin n'en portent pas : un préfiltre
    # inutile est un filtre de plus à relire le jour d'un doute.
    autres = [f for _, f in preuves.familles() if f.cle != "exclusions"]
    assert all(f.prefiltre is None for f in autres)


def test_l_inventaire_couvre_les_trois_familles(db):
    cles = {f["cle"] for f in preuves.inventaire(db)["familles"]}
    assert cles == {"alertes", "liste_blanche", "exclusions"}


def test_les_lignes_sans_piece_ne_sont_pas_comptees(db, tmp_path):
    """Compter les lignes sans pièce gonflerait le total d'une absence normale."""
    avant = preuves.inventaire(db)["annoncees"]
    db.add(AlertAttachment(alert_id=1, file_name=f"p{UID}_vide.pdf",
                           file_path="", uploaded_by="t"))
    db.commit()
    assert preuves.inventaire(db)["annoncees"] == avant


def test_un_inventaire_partiel_le_dit(db, tmp_path):
    """
    Annoncer « tout est là » après n'avoir regardé qu'une partie serait la
    faute même que ce module traque.
    """
    for i in range(3):
        db.add(AlertAttachment(alert_id=1, file_name=f"p{UID}_{i}.pdf",
                               file_path=_piece(tmp_path, f"c{i}.pdf"), uploaded_by="t"))
    db.commit()
    etat = preuves.inventaire(db, plafond=1)
    famille = next(f for f in etat["familles"] if f["cle"] == "alertes")
    assert famille["tronque"] is True
    assert famille["verifiees"] < famille["annoncees"]
    assert etat["tronque"] is True


# --------------------------------------------------------------------------
# Le contrôle de mise en service
# --------------------------------------------------------------------------

def _controle(monkeypatch, etat):
    monkeypatch.setattr("fiskr.preuves.inventaire", lambda db_, *a, **k: etat)
    return _pieces_probantes(None)


def test_rien_d_annonce_rien_qui_manque(monkeypatch):
    verdict = _controle(monkeypatch, {"familles": [], "annoncees": 0,
                                      "manquantes": 0, "tronque": False})
    assert verdict["etat"] == "OK"
    assert "rien ne manque" in verdict["constat"]


def test_toutes_les_pieces_presentes_donne_un_controle_vert(monkeypatch):
    verdict = _controle(monkeypatch, {
        "familles": [{"cle": "alertes", "libelle": "Pièces jointes d'alertes",
                      "annoncees": 12, "verifiees": 12, "manquantes": 0,
                      "tronque": False, "absentes": []}],
        "annoncees": 12, "manquantes": 0, "tronque": False})
    assert verdict["etat"] == "OK"
    assert "12" in verdict["constat"]


def test_une_piece_manquante_leve_une_attention_qui_nomme_la_famille(monkeypatch):
    verdict = _controle(monkeypatch, {
        "familles": [{"cle": "liste_blanche", "libelle": "Justificatifs de liste blanche",
                      "annoncees": 4, "verifiees": 4, "manquantes": 2,
                      "tronque": False, "absentes": []},
                     {"cle": "alertes", "libelle": "Pièces jointes d'alertes",
                      "annoncees": 9, "verifiees": 9, "manquantes": 0,
                      "tronque": False, "absentes": []}],
        "annoncees": 13, "manquantes": 2, "tronque": False})
    assert verdict["etat"] == "ATTENTION"
    assert "Justificatifs de liste blanche : 2" in verdict["constat"]
    assert "Pièces jointes d'alertes" not in verdict["constat"], (
        "une famille intacte n'a pas à figurer dans une alarme")


def test_le_remede_interdit_d_effacer_la_reference(monkeypatch):
    """
    Effacer la ligne d'une pièce disparue effacerait la trace qu'elle a
    existé — le contraire du service rendu.
    """
    verdict = _controle(monkeypatch, {
        "familles": [{"cle": "alertes", "libelle": "Pièces jointes d'alertes",
                      "annoncees": 3, "verifiees": 3, "manquantes": 1,
                      "tronque": False, "absentes": []}],
        "annoncees": 3, "manquantes": 1, "tronque": False})
    assert "trace" in verdict["remede"]
    assert "sauvegarde" in verdict["remede"]


def test_un_controle_partiel_annonce_sa_portee(monkeypatch):
    verdict = _controle(monkeypatch, {
        "familles": [{"cle": "alertes", "libelle": "Pièces jointes d'alertes",
                      "annoncees": 9000, "verifiees": 5000, "manquantes": 0,
                      "tronque": True, "absentes": []}],
        "annoncees": 9000, "manquantes": 0, "tronque": True})
    assert verdict["etat"] == "OK"
    assert "5000 vérifiée(s) sur 9000" in verdict["constat"]


def test_le_controle_est_enregistre():
    from fiskr.mise_en_service import _CONTROLES_BASE
    assert _pieces_probantes in _CONTROLES_BASE


# --------------------------------------------------------------------------
# Les écrans cessent de promettre un fichier qu'ils n'ont pas vérifié
# --------------------------------------------------------------------------

def test_la_liste_blanche_dit_si_le_justificatif_est_encore_la(db, tmp_path):
    from fiskr.api import _whitelist_summary
    pair = WhitelistPair(client_id=f"C{UID}1", client_name="Essai",
                         watchlist_entity_id="W1", watchlist_name="X",
                         list_type="WATCHLIST_OFAC", justification="—",
                         evidence_file_name="justif.pdf",
                         evidence_file_path=_piece(tmp_path, "w.pdf", ecrire=False),
                         created_by="t", created_at=datetime.utcnow())
    db.add(pair)
    db.commit()
    vue = _whitelist_summary(pair)
    assert vue["evidence_file_name"] == "justif.pdf", "la référence reste affichée"
    assert vue["evidence_file_present"] is False


def test_un_justificatif_present_est_annonce_present(db, tmp_path):
    from fiskr.api import _whitelist_summary
    pair = WhitelistPair(client_id=f"C{UID}2", client_name="Essai",
                         watchlist_entity_id="W2", watchlist_name="X",
                         list_type="WATCHLIST_OFAC", justification="—",
                         evidence_file_name="justif.pdf",
                         evidence_file_path=_piece(tmp_path, "w2.pdf"),
                         created_by="t", created_at=datetime.utcnow())
    db.add(pair)
    db.commit()
    assert _whitelist_summary(pair)["evidence_file_present"] is True


def test_le_front_ne_propose_pas_au_telechargement_une_piece_absente():
    """
    Le lien tiendrait une promesse que le clic romprait — et il la romprait le
    jour du contrôle. La garde lit le rendu réel.
    """
    source = Path("fiskr/static/app.js").read_text(encoding="utf-8")
    assert source.count("file_present === false") >= 2
    assert "evidence_file_present === false" in source
    for bloc in ("api/alerts/attachments/${att.id}", "api/whitelist/evidence/${p.id}"):
        avant = source[:source.index(bloc)]
        assert "_present === false" in avant[-700:], (
            f"le lien {bloc} doit être gardé par la vérification")


def test_les_chaines_ajoutees_sont_traduites():
    i18n = Path("fiskr/static/i18n.js").read_text(encoding="utf-8")
    for cle in ("Fichier introuvable", "Fichier introuvable sur le serveur",
                "Pièces probantes"):
        assert f'"{cle}"' in i18n, f"chaîne non traduite : {cle}"


# --------------------------------------------------------------------------
# 2. Le refus partiel d'un serveur SMTP
# --------------------------------------------------------------------------

class _ServeurFactice:
    def __init__(self, refuses=None):
        self.refuses = refuses or {}
        self.envoye = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, sender, recipients, message):
        self.envoye = True
        return dict(self.refuses)


def _smtp_configure(monkeypatch, serveur):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM", "fiskr@test")
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: serveur)


def test_un_refus_partiel_ne_se_lit_plus_comme_un_envoi_reussi(monkeypatch):
    """
    La garde du lot. `sendmail` ne lève que si TOUS les destinataires sont
    refusés ; deux sur cinq revenaient dans un dictionnaire que personne ne
    lisait, et la ligne du journal disait « envoyé ».
    """
    serveur = _ServeurFactice({"absent@test": (550, b"No such user"),
                               "plein@test": (452, b"Mailbox full")})
    _smtp_configure(monkeypatch, serveur)

    with pytest.raises(RuntimeError) as echec:
        notify_mod.send_email(["ok@test", "absent@test", "plein@test"],
                              "Sujet", "Corps")
    message = str(echec.value)
    assert "absent@test" in message and "plein@test" in message
    assert "550" in message and "452" in message
    assert "ok@test" not in message, "l'adresse servie n'est pas un échec"


def test_un_envoi_sans_refus_reste_silencieux(monkeypatch):
    """Le correctif ne doit pas transformer un envoi normal en incident."""
    serveur = _ServeurFactice()
    _smtp_configure(monkeypatch, serveur)
    notify_mod.send_email(["ok@test"], "Sujet", "Corps")
    assert serveur.envoye is True
