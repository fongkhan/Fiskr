"""
Une campagne reprise après interruption ne compte pas deux fois.

La reprise (démon tué en pleine campagne, travail remis en file) repart de
zéro : les résultats partiels sont purgés et les compteurs remis à zéro —
tous sauf deux. `hits_count` (correspondances trouvées) et `opened_count`
(alertes réellement ouvertes), ajoutés après la reprise, n'y figuraient pas :
la campagne reprise les additionnait à ceux de la tentative interrompue, et le
compte rendu annonçait deux fois les correspondances. Relevé par le lot
« finitions » du 21 octobre 2026.
"""
import json

import pytest

from fiskr import jobs
from fiskr.database import BatchCampaign, BatchResult, get_db


@pytest.mark.usefixtures("listes_de_demonstration")
def test_la_reprise_remet_tous_les_compteurs_a_zero(tmp_path):
    db = next(get_db())
    try:
        # La tentative interrompue avait déjà compté
        campagne = BatchCampaign(name="reprise", status="RUNNING", total_clients=0,
                                 processed_clients=3, alert_count=1, no_match_count=2,
                                 rejected_count=0, hits_count=5, opened_count=2,
                                 created_by="testeur")
        db.add(campagne)
        db.commit()
        campagne_id = campagne.id
        fichier = tmp_path / "profils.json"
        fichier.write_text(json.dumps([]), encoding="utf-8")

        jobs.submit("batch_campaign", params={
            "campaign_id": campagne_id, "profiles_path": str(fichier),
            "username": "testeur", "requested_lists": None})

        db.expire_all()
        reprise = db.get(BatchCampaign, campagne_id)
        assert reprise.status == "DONE", reprise.error_message
        assert (reprise.processed_clients, reprise.alert_count) == (0, 0)
        assert reprise.hits_count == 0, "correspondances de la tentative interrompue recomptées"
        assert reprise.opened_count == 0, "alertes de la tentative interrompue recomptées"
    finally:
        db.query(BatchResult).filter(BatchResult.campaign_id == campagne_id).delete()
        db.query(BatchCampaign).filter(BatchCampaign.id == campagne_id).delete()
        db.commit()
        db.close()
