"""
Le plafond de composantes de blocking était vérifié en LECTURE seulement.

`MAX_BLOCKING_FIELDS = 3` existe pour une raison mesurable : l'interrogation de
l'index essaie **toutes les combinaisons de jokers** des composantes de champ —
sans quoi une fiche listée qui ne renseigne pas ce champ devient
structurellement inatteignable. C'est 2^N sondes par criblage.

Ce plafond n'était appliqué que par `_valid_layout`, au moment de **lire** le
réglage. En **écriture**, un layout à quatre composantes de champ était :

  * accepté — `200 « Blocking keys mises à jour. Cache de criblage rechargé. »`
  * écrit en base,
  * tracé au journal d'administration,

puis **silencieusement ignoré** par le moteur, qui retombait sur le layout par
défaut. L'exploitant croyait avoir changé la sélection des candidats du
criblage ; rien n'avait changé, et rien ne le disait.

C'est la même classe de défaut que le poids `token_set` inatteignable : un
réglage qu'on peut enregistrer mais qui n'agit jamais.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db
from fiskr.settings import (BLOCKING_FIELD_COMPONENTS, MAX_BLOCKING_FIELDS,
                            SETTING_BLOCKING_SCREENING, _valid_layout,
                            blocking_layout, set_setting)

DEFAUT = ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]


@pytest.fixture()
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": f"blk{uuid.uuid4().hex[:4]}", "full_name": "blk",
        "role": "admin", "roles": ["admin"]}
    with TestClient(app) as c:
        yield c
    db = next(get_db())
    set_setting(db, SETTING_BLOCKING_SCREENING, list(DEFAUT), updated_by="test")
    db.close()
    app.dependency_overrides.pop(get_current_user, None)


def _trop_de_champs():
    return list(BLOCKING_FIELD_COMPONENTS)[:MAX_BLOCKING_FIELDS + 1]


def test_le_jeu_de_composantes_permet_de_depasser_le_plafond():
    """Le défaut n'est possible que parce qu'il existe plus de composantes de
    champ que le plafond n'en autorise."""
    assert len(BLOCKING_FIELD_COMPONENTS) > MAX_BLOCKING_FIELDS


def test_un_layout_au_dela_du_plafond_est_refuse(client):
    reponse = client.put("/api/settings/blocking",
                         json={"screening_layout": _trop_de_champs()})
    assert reponse.status_code == 400, reponse.text
    detail = reponse.json()["detail"]
    # Le message dit COMBIEN, LESQUELLES, et POURQUOI
    assert str(MAX_BLOCKING_FIELDS) in detail
    assert BLOCKING_FIELD_COMPONENTS[0] in detail
    assert "sondes" in detail


def test_le_refus_n_ecrit_rien(client):
    """Le pire du défaut n'était pas le 200 : c'était le réglage écrit en base
    et jamais appliqué."""
    db = next(get_db())
    avant = blocking_layout(db, "SCREENING")
    client.put("/api/settings/blocking", json={"screening_layout": _trop_de_champs()})
    assert blocking_layout(db, "SCREENING") == avant
    db.close()


def test_le_canal_filtrage_est_borne_aussi(client):
    reponse = client.put("/api/settings/blocking",
                         json={"filtering_layout": _trop_de_champs()})
    assert reponse.status_code == 400, reponse.text


def test_un_layout_au_plafond_est_accepte_et_applique(client):
    """L'inverse du défaut : ce qui est accepté DOIT être appliqué."""
    layout = ["PHONETIC_FIRST"] + list(BLOCKING_FIELD_COMPONENTS)[:MAX_BLOCKING_FIELDS]
    reponse = client.put("/api/settings/blocking", json={"screening_layout": layout})
    assert reponse.status_code == 200, reponse.text
    db = next(get_db())
    assert blocking_layout(db, "SCREENING") == layout
    db.close()


def test_ecriture_et_lecture_appliquent_la_meme_regle():
    """La correction : les deux côtés comptent exactement le même ensemble de
    composantes. Une divergence recréerait le réglage fantôme."""
    accepte_en_lecture = [
        l for l in (
            DEFAUT,
            ["PHONETIC_FIRST"] + list(BLOCKING_FIELD_COMPONENTS)[:MAX_BLOCKING_FIELDS],
            _trop_de_champs(),
            list(BLOCKING_FIELD_COMPONENTS)[:MAX_BLOCKING_FIELDS],
        ) if _valid_layout(l)
    ]
    assert _trop_de_champs() not in accepte_en_lecture
    assert len(accepte_en_lecture) == 3
