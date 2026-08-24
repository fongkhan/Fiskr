"""
« Tous vos clients ont-ils été criblés ? »

C'est la première question d'un contrôle, et le produit ne savait pas y
répondre. Importer un référentiel clients déclenche un **contrôle de
complétude**, pas un criblage ; le re-criblage automatique se déclenche quand
une **liste** change (`rescreen_after_snapshot_change`), jamais quand des
**clients** arrivent. Un référentiel fraîchement importé restait donc entier
hors du criblage jusqu'à ce qu'une liste bouge ou qu'un lookback soit lancé —
et rien, nulle part, ne le signalait : ni écran, ni compteur, ni message
d'import.

Pire : l'action qui répare, le lookback, existait dans l'API
(`POST /api/rescreen/run`) **sans le moindre bouton**. La seule opération
capable de cribler un référentiel fraîchement importé n'était atteignable
qu'en appelant l'API à la main. Constater sans pouvoir agir n'aide personne :
la mesure et l'action vivent désormais sur la même carte.
"""
import re
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.couverture import (PLAFOND_DE_COMPTAGE, couverture_du_criblage,
                              phrase_de_couverture)
from fiskr.database import (AuditTrail, ClientEntity, Snapshot, get_db)


@pytest.fixture
def db_clients(tmp_path):
    """
    Base isolée : la couverture est un compte SUR TOUT le référentiel en
    production. Sur la base partagée des tests, ce que les autres y laissent
    ferait dériver le chiffre — et le test dirait alors quelque chose sur eux,
    pas sur la mesure.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from fiskr.database import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'couverture.sqlite3'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    identifiant = f"cb-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=identifiant, file_type="CLIENT_BASE",
                    file_name="clients.csv", file_hash=uuid.uuid4().hex,
                    record_count=0, status="READY", uploaded_at=datetime.utcnow()))
    db.commit()
    yield db, identifiant
    db.close()


def _client(db, snapshot_id, numero):
    reference = f"COUV-{uuid.uuid4().hex[:6]}-{numero}"
    db.add(ClientEntity(snapshot_id=snapshot_id, client_id=reference,
                        client_type="PP", client_last_name=f"NOM{numero}",
                        entity_checksum=f"ck-{reference}"))
    return reference


def _criblage(db, client_id):
    db.add(AuditTrail(client_id=client_id, client_name="—", client_type="PP",
                      watchlist_id="—", watchlist_name="—", base_score=0.0,
                      final_score=0.0, status="NO_MATCH", decision_tree={},
                      config_state={}, watchlist_version="v", watchlist_hash="h",
                      timestamp=datetime.utcnow()))


def test_sans_referentiel_la_mesure_se_tait(db_clients):
    """Zéro et « impossible à dire » ne sont pas la même chose : un écran ne
    doit pas avoir à les distinguer lui-même."""
    db, _ = db_clients
    mesure = couverture_du_criblage(db)
    # Le référentiel existe mais il est vide : rien n'est « jamais criblé ».
    assert mesure["jamais_cribles"] == 0
    assert phrase_de_couverture(mesure) is None


def test_un_client_importe_sans_criblage_est_compte(db_clients):
    """
    Le cœur du défaut : importer des clients ne les crible pas. La mesure doit
    le dire, pas le supposer.
    """
    db, snapshot = db_clients
    for i in range(3):
        _client(db, snapshot, i)
    db.commit()

    mesure = couverture_du_criblage(db)
    assert mesure["clients"] == 3
    assert mesure["jamais_cribles"] == 3
    assert mesure["plafonne"] is False
    assert "jamais été criblés" in phrase_de_couverture(mesure)


def test_un_client_crible_une_fois_ne_compte_plus(db_clients):
    db, snapshot = db_clients
    references = [_client(db, snapshot, i) for i in range(3)]
    db.commit()
    _criblage(db, references[0])
    db.commit()

    mesure = couverture_du_criblage(db)
    assert mesure["jamais_cribles"] == 2


def test_tout_crible_ne_produit_aucune_phrase(db_clients):
    """Le silence est la bonne réponse quand tout va bien : un écran qui parle
    tout le temps n'est plus lu."""
    db, snapshot = db_clients
    for i in range(2):
        _criblage(db, _client(db, snapshot, i))
    db.commit()

    mesure = couverture_du_criblage(db)
    assert mesure["jamais_cribles"] == 0
    assert phrase_de_couverture(mesure) is None


def test_la_mesure_plafonnee_le_dit(db_clients):
    """
    Un chiffre plafonné qui aurait l'air exact serait pire que pas de chiffre.
    Quand la mesure s'arrête, elle l'annonce.
    """
    db, snapshot = db_clients
    for i in range(5):
        _client(db, snapshot, i)
    db.commit()

    mesure = couverture_du_criblage(db, plafond=2)
    assert mesure["jamais_cribles"] == 2
    assert mesure["plafonne"] is True
    assert phrase_de_couverture(mesure).startswith("Plus de 2 clients")


def test_une_base_illisible_ne_fait_pas_tomber_l_ecran():
    """Une mesure qui lève emporterait l'écran qui l'affiche."""
    class _SessionCassee:
        def query(self, *a, **k):
            raise RuntimeError("base injoignable")

    mesure = couverture_du_criblage(_SessionCassee())
    assert mesure["sans_referentiel"] is True
    assert mesure["jamais_cribles"] == 0


def test_le_plafond_par_defaut_est_annonce():
    assert PLAFOND_DE_COMPTAGE >= 1000


# ----------------------------------------------------- l'API et l'interface

@pytest.fixture
def client_admin():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin_couverture", "full_name": "Admin",
        "role": "admin", "roles": ["admin"],
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_l_endpoint_rend_la_mesure_et_sa_phrase(client_admin):
    reponse = client_admin.get("/api/screening/couverture")
    assert reponse.status_code == 200
    corps = reponse.json()
    assert set(corps) >= {"clients", "jamais_cribles", "plafonne",
                          "sans_referentiel", "phrase"}


def test_le_lookback_a_enfin_un_bouton():
    """
    L'endpoint existait, l'interface non : la seule opération capable de
    cribler un référentiel fraîchement importé n'était atteignable qu'en
    appelant l'API à la main. Une capacité sans affordance n'existe pas pour
    qui utilise le produit.
    """
    import os
    static = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "fiskr", "static")
    with open(os.path.join(static, "index.html"), encoding="utf-8") as f:
        html = f.read()
    with open(os.path.join(static, "app.js"), encoding="utf-8") as f:
        app_js = f.read()

    assert 'onclick="lancerLookback()"' in html
    assert "function lancerLookback(" in app_js
    assert '"/api/rescreen/run"' in app_js, "le bouton doit appeler la vraie route"


def test_la_carte_de_couverture_se_charge_avec_son_onglet():
    """Une mesure qu'il faut penser à rafraîchir soi-même n'est pas lue."""
    import os
    chemin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "fiskr", "static", "app.js")
    with open(chemin, encoding="utf-8") as f:
        app_js = f.read()
    debut = app_js.index('} else if (subTabId === "screening-batch") {')
    assert "chargerCouvertureDuCriblage()" in app_js[debut:debut + 300]


# ------------------------------------- les renvois de l'écran de mise en service

def test_chaque_renvoi_de_la_mise_en_service_pointe_sur_un_ecran_reel():
    """
    Garde dérivée de la SORTIE, pas du texte source : un lien peut être passé
    par mot-clé (`lien=`) ou par position, et une garde qui lit le source rate
    la moitié des cas — c'est exactement comme ça que quatre renvois cassés
    sont passés (`#clients`, qui n'existe pas ; `settings-retention`, qui
    n'existe pas non plus).

    Un point de mise en service qui renvoie dans le vide est pire qu'un point
    sans lien : il promet un remède et ouvre une page blanche.
    """
    import os
    from fiskr.mise_en_service import etat_de_mise_en_service

    static = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "fiskr", "static")
    with open(os.path.join(static, "index.html"), encoding="utf-8") as f:
        html = f.read()
    sections = set(re.findall(r'id="sec-([\w-]+)"', html))
    panneaux = set(re.findall(r'id="sub-sec-([\w-]+)"', html))

    db = next(get_db())
    try:
        fautes = []
        for controle in etat_de_mise_en_service(db)["controles"]:
            lien = controle.get("lien") or ""
            if not lien:
                continue
            forme = re.fullmatch(r"#([\w-]+)(?:/([\w-]+))?", lien)
            if not forme:
                fautes.append(f'{controle["cle"]} : « {lien} » n\'est pas un renvoi valide')
                continue
            section, panneau = forme.group(1), forme.group(2)
            if section not in sections:
                fautes.append(f'{controle["cle"]} : section « sec-{section} » inconnue')
            elif panneau and panneau not in panneaux:
                fautes.append(f'{controle["cle"]} : panneau « sub-sec-{panneau} » inconnu')
        assert not fautes, "Renvois cassés :\n  " + "\n  ".join(fautes)
    finally:
        db.close()
