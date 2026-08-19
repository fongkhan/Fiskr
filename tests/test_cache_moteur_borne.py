"""
Vue du cache moteur : une réponse bornée.

`GET /api/watchlist` rendait `watchlist_store` **en entier**. Mesuré sur le
cache réel : ~1,8 Ko par fiche. En production, 895 157 fiches en production —
soit **plus de 1,5 Go** sérialisés en mémoire dans le processus web, pour un
seul appel. Sur un hébergement mutualisé, cet appel emportait l'application.

Le chiffre est une estimation dérivée d'une mesure locale : appeler cet
endpoint en production aurait été déclencher exactement ce qu'on décrit.

Cet endpoint est un point de **contrôle** du cache — « la fiche que je viens de
modifier est-elle rechargée ? » — pas un moyen de parcourir le référentiel :
pour ça il y a `GET /api/watchlist/db`, paginé, filtrable et lu en base.

La réponse est donc coupée, mais elle le **dit** : `total` reste exact et
`truncated` signale la coupe. Le chiffre qui sert au contrôle n'est jamais
faux ; seul l'échantillon l'est.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api_module
from fiskr.api import app, WATCHLIST_CACHE_PREVIEW
from fiskr.auth import get_current_user

TAG = uuid.uuid4().hex[:6].upper()


def _fiche(n: int) -> dict:
    return {"id": 10_000 + n, "entity_id": f"CM-{TAG}-{n}", "entity_type": "I",
            "primary_name": f"Cache {TAG} {n}", "designation": f"poste {n}"}


@pytest.fixture()
def contexte(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "cm", "role": "admin", "roles": ["admin"]}
    # 250 fiches : au-dela de l'echantillon par defaut (100)
    monkeypatch.setattr(api_module, "watchlist_store", [_fiche(i) for i in range(250)])
    monkeypatch.setattr(api_module, "watchlist_hash", "h-test")
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


def test_la_reponse_est_coupee_par_defaut(contexte):
    corps = contexte.get("/api/watchlist").json()
    assert len(corps["items"]) == WATCHLIST_CACHE_PREVIEW
    assert corps["total"] == 250
    assert corps["truncated"] is True


def test_le_total_reste_exact_meme_coupe(contexte):
    """C'est le chiffre qui sert au contrôle : il ne doit jamais mentir sur la
    taille du cache sous prétexte que l'échantillon est court."""
    assert contexte.get("/api/watchlist?limit=1").json()["total"] == 250


def test_une_reponse_complete_se_declare_non_coupee(contexte):
    corps = contexte.get("/api/watchlist?limit=1000").json()
    assert len(corps["items"]) == 250
    assert corps["truncated"] is False


def test_la_borne_haute_est_appliquee(contexte):
    """Sans plafond, `limit` rouvrirait la porte au vidage complet."""
    assert contexte.get("/api/watchlist?limit=1001").status_code == 422
    assert contexte.get("/api/watchlist?limit=0").status_code == 422


def test_une_fiche_precise_se_lit_sans_dependre_de_sa_position(contexte):
    """Le cas d'usage réel du contrôle : la 249e fiche n'est dans aucun
    échantillon par défaut, et doit rester atteignable."""
    corps = contexte.get(f"/api/watchlist?entity_id=CM-{TAG}-249").json()
    assert len(corps["items"]) == 1
    assert corps["items"][0]["primary_name"] == f"Cache {TAG} 249"
    assert corps["total"] == 250


def test_une_entite_absente_rend_une_liste_vide(contexte):
    corps = contexte.get("/api/watchlist?entity_id=CM-INCONNU").json()
    assert corps["items"] == []
    assert corps["total"] == 250
    assert corps["truncated"] is True


def test_l_enveloppe_garde_ses_champs_d_origine(contexte):
    corps = contexte.get("/api/watchlist").json()
    assert "hash" in corps and "version" in corps and "items" in corps


def test_un_cache_vide_ne_se_declare_pas_coupe(contexte, monkeypatch):
    monkeypatch.setattr(api_module, "watchlist_store", [])
    corps = contexte.get("/api/watchlist").json()
    assert corps["items"] == [] and corps["total"] == 0
    assert corps["truncated"] is False
