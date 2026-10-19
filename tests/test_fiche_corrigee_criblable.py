"""
Une fiche listee corrigee a la main reste criblable.

Releve en passant pendant le chantier des webhooks : apres un PATCH sur une
fiche en production, TOUT criblage qui la rencontrait finissait en 500, sans
ligne d'audit — /api/screen compris. Le cache de criblage copiait les colonnes
de la fiche telles quelles, `modified_at` compris (un datetime) ; la fiche
voyageait jusque dans l'arbre de decision, que la colonne JSON du journal
refuse brut. Une correction de fiche — geste de gouvernance ordinaire — rendait
donc la personne listee INCRIBLABLE, et l'echec ne laissait aucune trace.

Le cache ne porte plus que des valeurs serialisables : une date y voyage en
ISO 8601, comme elle sortirait de l'API.
"""
import uuid
from datetime import date, datetime

from fiskr.database import AuditTrail, get_db
from tests.test_entity_patch import _patch, _upload_entity, client  # noqa: F401


def _cribler(client, nom, prenom):
    return client.post("/api/screen", json={
        "client_id": f"CORR-{uuid.uuid4().hex[:8]}", "client_type": "PP",
        "client_first_name": prenom, "client_last_name": nom,
        "client_countries": {"nationality": ["RU"], "residence": [],
                             "birth_country": [], "registration_country": []},
    })


def test_une_fiche_corrigee_reste_criblable_et_journalisee(client):
    nom = f"Corrigov{uuid.uuid4().hex[:5]}"
    pk, _ = _upload_entity(client, f"Ivan {nom}")
    _patch(client, pk, {"city": "Moscou"})

    reponse = _cribler(client, nom, "Ivan")
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["best_match"], "la fiche corrigee n'a pas ete rencontree"
    assert nom.upper() in str(corps["best_match"]).upper()

    db = next(get_db())
    try:
        ligne = db.query(AuditTrail).filter(
            AuditTrail.id == corps["audit_trail_id"]).one()
        assert ligne.decision_tree is not None
    finally:
        db.close()


def test_le_cache_ne_porte_aucune_date_brute(client):
    import fiskr.api as api
    pk, _ = _upload_entity(client, f"Ivan Datov{uuid.uuid4().hex[:5]}")
    _patch(client, pk, {"city": "Moscou"})
    fiche = next(e for e in api.watchlist_store if e.get("id") == pk)
    assert fiche["modified_at"], "le PATCH doit avoir date la fiche"
    brutes = {k: v for k, v in fiche.items() if isinstance(v, (date, datetime))}
    assert not brutes, f"valeurs non serialisables dans le cache : {sorted(brutes)}"
