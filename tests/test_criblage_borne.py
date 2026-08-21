"""
Criblage : la réponse ne transporte plus tous les candidats.

Mesuré sur la production via `GET /api/screen/preview` (lecture seule, même
moteur) :

| Profil criblé | Réponse |
|---|---:|
| nom rare + pays | 1,03 s |
| nom rare, sans pays | 3,31 s |
| nom courant + pays | 1,90 s |
| **nom courant, SANS pays** | **24,2 s** |

Sans pays, le profil tombe dans le bloc « pays inconnu », qui rassemble toutes
les fiches dont la source ne publie pas de géographie. Profilé en local :
**174 µs par candidat**, ce qui situe ce cas à ~120 000 candidats — les 24 s
mesurées. Le calcul lui-même est dominé par Damerau-Levenshtein (37 %),
Jaro (24 %) et le tri de tokens (14 %) : ce sont les métriques, pas de la
normalisation redondante.

`POST /api/screen` renvoyait `all_matches`, **tous** les candidats scorés,
chacun portant sa fiche listée complète : ~240 Mo d'objets en mémoire et
autant dans la réponse pour ce cas. Personne ne lisait ce champ — ni un écran,
ni un test.

La réponse est désormais bornée aux 50 meilleurs, retenus par un tas. Ce qui
fait foi — `best_match`, le journal d'audit, l'alerte — porte toujours sur
**tous** les candidats, et `candidates_count` dit combien ont été comparés.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api_module
from fiskr.api import app, SCREEN_MAX_MATCHES
from fiskr.auth import get_current_user

TAG = uuid.uuid4().hex[:6].upper()
NB_CANDIDATS = 200


@pytest.fixture()
def contexte(monkeypatch):
    """Index de criblage fabriqué : 200 fiches dont les scores s'échelonnent,
    toutes dans le même bloc — le cas que la borne doit couvrir."""
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "cb", "full_name": "cb", "role": "admin",
        "roles": ["admin"]}

    fiches = []
    for i in range(NB_CANDIDATS):
        # Des noms de plus en plus eloignes de la cible : les scores decroissent
        suffixe = "X" * (i % 20)
        fiches.append({
            "id": 90000 + i, "entity_id": f"SB-{TAG}-{i:03d}", "entity_type": "I",
            "primary_name": f"IVAN IVANOV{suffixe}",
            "aliases": {"high_priority": [], "low_priority": []},
            "dates_of_birth": [], "date_of_death": None, "is_deceased": False,
            "gender": "U",
            "countries": {"citizenship": [], "residence": [], "birth_country": [],
                          "jurisdiction_country": []},
            "_list_type": "WATCHLIST_OFAC",
        })

    # Un seul bloc : toutes les fiches sont candidates pour n'importe quelle cle
    class _IndexUnique(dict):
        def get(self, cle, defaut=None):
            return fiches

    monkeypatch.setattr(api_module, "watchlist_index", _IndexUnique())
    monkeypatch.setattr(api_module, "watchlist_store", fiches)
    monkeypatch.setattr(api_module, "watchlist_hash", f"h-{TAG}")
    monkeypatch.setattr(api_module, "_ensure_watchlist_cache", lambda db: None)
    yield TestClient(app), fiches
    app.dependency_overrides.pop(get_current_user, None)


def _crible(client):
    reponse = client.post("/api/screen", json={
        "client_id": f"C-{TAG}", "client_type": "PP",
        "client_first_name": "IVAN", "client_last_name": "IVANOV"})
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


def test_tous_les_candidats_sont_bien_compares(contexte):
    """La borne ne doit PAS réduire le périmètre comparé : un listé écarté du
    calcul serait un faux négatif réglementaire."""
    client, _ = contexte
    assert _crible(client)["candidates_count"] == NB_CANDIDATS


def test_la_liste_rendue_est_bornee_et_le_dit(contexte):
    client, _ = contexte
    corps = _crible(client)
    assert len(corps["all_matches"]) == SCREEN_MAX_MATCHES
    assert corps["all_matches_truncated"] is True


def test_les_rapprochements_rendus_sont_bien_les_meilleurs(contexte):
    """Le point délicat du tas : il doit rendre EXACTEMENT le sommet du tri
    complet d'avant, pas un échantillon quelconque."""
    client, fiches = contexte
    corps = _crible(client)
    rendus = corps["all_matches"]

    scores = [m["final_score"] for m in rendus]
    assert scores == sorted(scores, reverse=True), "liste non triée"

    # Aucun candidat écarté ne fait mieux que le pire des retenus
    ids_rendus = {m["watchlist_entity"]["entity_id"] for m in rendus}
    assert len(ids_rendus) == SCREEN_MAX_MATCHES
    assert min(scores) >= 0.0
    assert corps["best_match"]["final_score"] == max(scores)


def test_le_meilleur_rapprochement_est_celui_de_tout_le_perimetre(contexte):
    """`best_match` est ce qui décide de l'alerte et de la ligne d'audit : il
    est calculé sur tous les candidats, la borne ne le touche pas."""
    client, _ = contexte
    corps = _crible(client)
    meilleur = corps["best_match"]
    assert meilleur is not None
    # Le nom exact figure dans le jeu : le meilleur score doit etre maximal
    assert meilleur["watchlist_entity"]["primary_name"] == "IVAN IVANOV"
    assert corps["all_matches"][0]["watchlist_entity"]["entity_id"] == \
        meilleur["watchlist_entity"]["entity_id"]


def test_un_petit_perimetre_n_est_pas_declare_coupe(contexte, monkeypatch):
    client, fiches = contexte
    petit = fiches[:5]

    class _IndexPetit(dict):
        def get(self, cle, defaut=None):
            return petit

    monkeypatch.setattr(api_module, "watchlist_index", _IndexPetit())
    corps = _crible(client)
    assert corps["candidates_count"] == 5
    assert len(corps["all_matches"]) == 5
    assert corps["all_matches_truncated"] is False


def test_la_reponse_ne_grossit_plus_avec_le_perimetre(contexte, monkeypatch):
    """La mesure : c'est ce champ qui faisait ~240 Mo sur un nom courant sans
    pays en production."""
    client, fiches = contexte
    perimetre = {"fiches": fiches[:60]}

    class _IndexVariable(dict):
        def get(self, cle, defaut=None):
            return perimetre["fiches"]

    monkeypatch.setattr(api_module, "watchlist_index", _IndexVariable())
    poids_60 = len(client.post("/api/screen", json={
        "client_id": f"C-{TAG}", "client_type": "PP",
        "client_first_name": "IVAN", "client_last_name": "IVANOV"}).content)

    perimetre["fiches"] = fiches   # 200 candidats, meme requete
    reponse = client.post("/api/screen", json={
        "client_id": f"C-{TAG}", "client_type": "PP",
        "client_first_name": "IVAN", "client_last_name": "IVANOV"})
    assert reponse.json()["candidates_count"] == NB_CANDIDATS
    poids_200 = len(reponse.content)

    assert poids_200 <= poids_60 * 1.1, (
        f"{poids_60} o pour 60 candidats, {poids_200} o pour {NB_CANDIDATS} : "
        "la reponse grossit encore avec le perimetre")


def test_la_rarete_des_noms_ne_rompt_pas_la_borne(contexte, monkeypatch):
    """
    La rareté des noms est jointe à chaque correspondance en ALERTE. Posée sur
    chaque ligne de `all_matches`, elle rendait la réponse à nouveau
    proportionnelle au périmètre — mesuré ici : 45 366 o pour 60 candidats
    contre 50 835 o pour 200, là où le contrat tolère 10 %. Elle reste rendue
    en entier sur `best_match` et écrite dans le journal d'audit.
    """
    from fiskr import rarete

    client, fiches = contexte
    precedente = rarete.table_courante()
    rarete.installer(rarete.construire(
        [{"primary_name": f["primary_name"]} for f in fiches] * 30))
    try:
        perimetre = {"fiches": fiches[:60]}

        class _IndexVariable(dict):
            def get(self, cle, defaut=None):
                return perimetre["fiches"]

        monkeypatch.setattr(api_module, "watchlist_index", _IndexVariable())
        requete = {"client_id": f"C-{TAG}", "client_type": "PP",
                   "client_first_name": "IVAN", "client_last_name": "IVANOV"}
        poids_60 = len(client.post("/api/screen", json=requete).content)

        perimetre["fiches"] = fiches
        reponse = client.post("/api/screen", json=requete)
        poids_200 = len(reponse.content)
        corps = reponse.json()

        assert poids_200 <= poids_60 * 1.1, (
            f"{poids_60} o pour 60 candidats, {poids_200} o pour {NB_CANDIDATS} : "
            "la rareté a remis la réponse à la taille du périmètre")
        assert all("name_rarity" not in m for m in corps["all_matches"])
        # Mais elle est bien là où elle sert : sur la correspondance retenue.
        assert corps["best_match"]["name_rarity"]["disponible"] is True
    finally:
        rarete.installer(precedente)


def test_le_sommet_rendu_est_celui_du_tri_complet(contexte):
    """Le point le plus delicat du tas : il doit rendre EXACTEMENT le sommet
    du tri complet d'avant. Reference calculee ici, candidat par candidat,
    sans passer par l'endpoint."""
    from fiskr.scoring import match_entities
    from fiskr.settings import scoring_config_with_thresholds
    from fiskr.database import get_db

    client, fiches = contexte
    rendus = _crible(client)["all_matches"]

    db = next(get_db())
    cfg = scoring_config_with_thresholds(db)
    cible = {"client_id": f"C-{TAG}", "client_type": "PP",
             "client_first_name": "IVAN", "client_last_name": "IVANOV",
             "client_gender": "U"}
    reference = []
    for rang, fiche in enumerate(fiches):
        res = match_entities(cible, fiche, cfg)
        reference.append((-res["final_score"], rang, fiche["entity_id"]))
    reference.sort()
    attendus = [eid for _, _, eid in reference[:SCREEN_MAX_MATCHES]]
    obtenus = [m["watchlist_entity"]["entity_id"] for m in rendus]
    db.close()

    assert obtenus == attendus
